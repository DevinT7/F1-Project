"""Phase 7D: the LLM client abstraction. Provider-specific objects never leave this module.

    LLMClient.generate(system=, messages=, response_schema=, temperature=, max_tokens=, timeout_s=) -> LLMResult

Implementations
  FakeLLMClient    scripted responses per call (dict / str / LLMError / callable): every test, no network
  ProviderClient   real provider over HTTP (httpx), chosen by environment:
                     LLM_PROVIDER = anthropic | openai     (openai also covers OpenAI-compatible endpoints)
                     LLM_MODEL    = the model name          (required; never hard-coded)
                     LLM_API_KEY  or ANTHROPIC_API_KEY / OPENAI_API_KEY
                     LLM_BASE_URL (optional), LLM_TIMEOUT_S (default 60)
                   Structured output: Anthropic forces a tool whose input_schema is the report schema;
                   OpenAI uses response_format json_schema (strict). Either way `parsed` is the object.

Errors: LLMTimeout, LLMTransientError (429/5xx/connection; retried here, max 2, exponential backoff with
jitter), LLMProviderError (anything else), LLMConfigError. The validation retry lives in
report_generator, not here. API keys never appear in logs, reprs or exception messages.
"""
from __future__ import annotations

import json
import os
import random
import time

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx
import structlog

log = structlog.get_logger("raceiq.ai")


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMResult:
    text: str
    parsed: dict[str, Any] | None
    usage: Usage
    latency_ms: int
    model: str
    request_id: str | None


class LLMError(Exception):
    """Base class: a provider/transport failure (not a bad report)."""


class LLMTimeout(LLMError):
    pass


class LLMTransientError(LLMError):
    pass


class LLMProviderError(LLMError):
    pass


class LLMConfigError(LLMError):
    pass


class LLMClient(Protocol):
    def generate(self, *, system: str, messages: list[Message], response_schema: dict[str, Any],
                 temperature: float, max_tokens: int, timeout_s: float) -> LLMResult: ...


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Strict: the whole text must be one JSON object (a ```json fence is tolerated). None otherwise."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:] if t.lower().startswith("json") else t
    try:
        value = json.loads(t)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------- fake
Scripted = dict[str, Any] | str | LLMError | Callable[[str, list[Message]], "dict[str, Any] | str"]


@dataclass
class FakeLLMClient:
    """Plays back `script` one item per call: a dict (a parsed structured response), a str (raw text,
    parsed strictly), an LLMError to raise (timeouts, 500s), or a callable(system, messages) -> dict|str
    (e.g. an obedient model that follows instructions found in the FactSheet)."""
    script: Sequence[Scripted]
    model: str = "fake-model"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(self, *, system: str, messages: list[Message], response_schema: dict[str, Any],
                 temperature: float, max_tokens: int, timeout_s: float) -> LLMResult:
        i = len(self.calls)
        self.calls.append({"system": system, "messages": list(messages), "temperature": temperature,
                           "max_tokens": max_tokens, "timeout_s": timeout_s})
        if i >= len(self.script):
            raise LLMProviderError(f"fake script exhausted after {len(self.script)} calls")
        item = self.script[i]
        if isinstance(item, LLMError):
            raise item
        if callable(item):
            item = item(system, list(messages))
        if isinstance(item, dict):
            return LLMResult(json.dumps(item), item, Usage(1000, 300), 5, self.model, f"fake-{i}")
        return LLMResult(str(item), parse_json_object(str(item)), Usage(1000, 300), 5, self.model, f"fake-{i}")


# ---------------------------------------------------------------- real provider over HTTP
TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
MAX_TRANSIENT_RETRIES = 2
TOOL_NAME = "submit_report"


@dataclass(frozen=True)
class ProviderConfig:
    provider: Literal["anthropic", "openai"]
    model: str
    api_key: str = field(repr=False)
    base_url: str
    timeout_s: float = 60.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ProviderConfig:
        env = dict(os.environ if env is None else env)
        provider = env.get("LLM_PROVIDER", "").strip().lower()
        if provider not in ("anthropic", "openai"):
            raise LLMConfigError("set LLM_PROVIDER to 'anthropic' or 'openai'")
        model = env.get("LLM_MODEL", "").strip()
        if not model:
            raise LLMConfigError("set LLM_MODEL (no model name is hard-coded)")
        key = env.get("LLM_API_KEY") or env.get("ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY") or ""
        if not key.strip():
            raise LLMConfigError(f"no API key: set LLM_API_KEY or {'ANTHROPIC' if provider == 'anthropic' else 'OPENAI'}_API_KEY")
        default = "https://api.anthropic.com" if provider == "anthropic" else "https://api.openai.com"
        return cls("anthropic" if provider == "anthropic" else "openai", model, key.strip(),
                   env.get("LLM_BASE_URL", default).rstrip("/"),
                   float(env.get("LLM_TIMEOUT_S", "60")))


class ProviderClient:
    def __init__(self, config: ProviderConfig, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None) -> None:
        self._config = config
        self._http = httpx.Client(transport=transport, base_url=config.base_url)
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.model = config.model

    def __repr__(self) -> str:                      # never the key
        return f"ProviderClient(provider={self._config.provider!r}, model={self.model!r})"

    def _scrub(self, text: str) -> str:
        key = self._config.api_key
        return text.replace(key, "[redacted]") if key else text

    # -- request building / response reading, per provider
    def _request(self, system: str, messages: list[Message], schema: dict[str, Any], temperature: float,
                 max_tokens: int) -> tuple[str, dict[str, str], dict[str, Any]]:
        c = self._config
        msgs = [{"role": m.role, "content": m.content} for m in messages]
        if c.provider == "anthropic":
            body = {"model": c.model, "max_tokens": max_tokens, "temperature": temperature, "system": system,
                    "messages": msgs,
                    "tools": [{"name": TOOL_NAME, "description": "Submit the race engineer report.", "input_schema": schema}],
                    "tool_choice": {"type": "tool", "name": TOOL_NAME}}
            return "/v1/messages", {"x-api-key": c.api_key, "anthropic-version": "2023-06-01"}, body
        body = {"model": c.model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, *msgs],
                "response_format": {"type": "json_schema", "json_schema": {"name": "report", "strict": True, "schema": schema}}}
        return "/v1/chat/completions", {"authorization": f"Bearer {c.api_key}"}, body

    def _read(self, data: dict[str, Any]) -> tuple[str, dict[str, Any] | None, Usage]:
        if self._config.provider == "anthropic":
            blocks = data.get("content") or []
            tool = next((b for b in blocks if b.get("type") == "tool_use"), None)
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            parsed = tool.get("input") if tool and isinstance(tool.get("input"), dict) else parse_json_object(text)
            u = data.get("usage") or {}
            return json.dumps(parsed) if parsed is not None else text, parsed, Usage(int(u.get("input_tokens", 0)),
                                                                                    int(u.get("output_tokens", 0)))
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        u = data.get("usage") or {}
        return text, parse_json_object(text), Usage(int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)))

    def generate(self, *, system: str, messages: list[Message], response_schema: dict[str, Any],
                 temperature: float = 0.2, max_tokens: int = 2000, timeout_s: float | None = None) -> LLMResult:
        path, headers, body = self._request(system, messages, response_schema, temperature, max_tokens)
        timeout = timeout_s or self._config.timeout_s
        attempt = 0
        while True:
            start = time.perf_counter()
            try:
                response = self._http.post(path, json=body, headers=headers, timeout=timeout)
            except httpx.TimeoutException as exc:
                err: LLMError = LLMTimeout(f"{self._config.provider} request timed out after {timeout:.0f} s")
                cause: Exception | None = exc
            except httpx.TransportError as exc:
                err, cause = LLMTransientError(f"{self._config.provider} connection error: "
                                               f"{self._scrub(type(exc).__name__)}"), exc
            else:
                latency = int((time.perf_counter() - start) * 1000)
                status: int = response.status_code
                rid = response.headers.get("request-id") or response.headers.get("x-request-id")
                if 200 <= status < 300:
                    try:
                        text, parsed, usage = self._read(response.json())
                    except ValueError:
                        text, parsed, usage = self._scrub(response.text[:2000]), None, Usage()
                    log.info("ai.llm_call", provider=self._config.provider, model=self.model, status=status,
                             tokens_in=usage.input_tokens, tokens_out=usage.output_tokens, latency_ms=latency,
                             request_id=rid, transport_attempt=attempt + 1)
                    return LLMResult(text, parsed, usage, latency, self.model, rid)
                detail = self._scrub(response.text[:300])
                cls = LLMTransientError if status in TRANSIENT_STATUS else LLMProviderError
                err, cause = cls(f"{self._config.provider} HTTP {status} (request {rid}): {detail}"), None
                log.warning("ai.llm_call_failed", provider=self._config.provider, model=self.model, status=status,
                            latency_ms=latency, request_id=rid, transport_attempt=attempt + 1)
            if isinstance(err, (LLMTransientError, LLMTimeout)) and attempt < MAX_TRANSIENT_RETRIES:
                delay = 0.5 * 2 ** attempt + self._rng.uniform(0, 0.25)   # exponential backoff + jitter
                attempt += 1
                self._sleep(delay)
                continue
            raise err from (None if cause is None else LLMError(type(cause).__name__))




# ---------------------------------------------------------------- the no-key path
API_KEY_NEEDED = ("API KEY NEEDED: set LLM_PROVIDER (anthropic | openai), LLM_MODEL and LLM_API_KEY in backend/.env "
                  "(template: backend/.env.example; which phases need it: docs/api_keys.md)")
_warned_unconfigured = False


def client_from_env(env: dict[str, str] | None = None) -> ProviderClient | None:
    """The provider client, or None when no LLM is configured (the app then serves templates).
    Logs `ai.llm_unconfigured` once per process with what to set; never the key itself."""
    global _warned_unconfigured
    try:
        return ProviderClient(ProviderConfig.from_env(env))
    except LLMConfigError as exc:
        if not _warned_unconfigured:
            log.warning("ai.llm_unconfigured", reason=str(exc), action=API_KEY_NEEDED)
            _warned_unconfigured = True
        return None
