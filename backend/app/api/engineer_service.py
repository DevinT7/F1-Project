"""Phase 7F: the engineer behind /api/v1/engineer (report, ask, facts).

    FactSheet  ← the comparison's verified findings (ComparisonService, cached) + the target's tyre
                 forecast "as of" a lap when requested (TireService) + session date / teams
    report     ← 7D generate_report (LLM) or 7C template; cached by
                   ("llm", factsheet_hash, prompt_version, model)   LLM reports that passed validation
                   ("template", factsheet_hash, template_version)   template reports
    answer     ← 7E answer_question (never cached; conversations are per id)

Kill switch: settings.engineer_llm_enabled False → templates only, the client is never built or
called. No LLM configured (no key) → templates too (outcome template_no_llm). Every request logs
one `ai.engineer_request` line (hash, prompt version, outcome, latency; never the FactSheet or the
question text unless RACEIQ_AI_DEBUG_LOG_FACTSHEET=1) and updates ai/metrics.py.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import structlog

from ..ai.factsheet import FactSheetContext, build_factsheet
from ..ai.fallback import TEMPLATE_VERSION, build_template_report
from ..ai.llm_client import LLMClient
from ..ai.metrics import METRICS, Metrics
from ..ai.prompts import PROMPT_VERSION
from ..ai.qa.conversation import ConversationStore
from ..ai.qa.engine import answer_question
from ..ai.report_generator import generate_report
from ..ai.schemas import FACTSHEET_VERSION, Bullet, FactSheet, Report
from ..caching import TTL_S, Cache, MemoryCache, cache_key, digest, single_flight
from ..ai.validator import validate_report
from ..processing.insights import FindingsReport
from ..tires.registry import ModelNotAvailableError
from ..tires.service import NotARaceSessionError, TireService
from .schemas.common import canonical_session_key
from .schemas.comparisons import ComparisonRequest
from .schemas.engineer import (
    BulletOut, EngineerAnswer, EngineerAskRequest, EngineerReport, EngineerReportRequest, EngineerReportSections,
    FactsResponse, FactView, FollowUpOut, TireContext,
)
from .services import ComparisonService

log = structlog.get_logger("raceiq.ai")

NON_RACE = "tyre forecasting requires a race session; this is not a race"
NOT_REQUESTED = "no tyre forecast was requested for this comparison"


class FactSheetNotFoundError(LookupError):
    pass


def _debug() -> bool:
    return os.environ.get("RACEIQ_AI_DEBUG_LOG_FACTSHEET") == "1"


def fact_views(fs: FactSheet, ids: set[str] | None = None) -> dict[str, FactView]:
    out: dict[str, FactView] = {}
    for f in fs.facts:
        if ids is None or f.id in ids:
            out[f.id] = FactView(id=f.id, value=f.value, unit=f.unit, display=f.display[0] if f.display else None,
                                 meaning=f.meaning)
    for c in fs.categorical_facts:
        if ids is None or c.id in ids:
            out[c.id] = FactView(id=c.id, value=c.value, unit=None, display=None, meaning=c.meaning)
    return out


def _segment_map(findings: FindingsReport, ids: set[str]) -> dict[str, str]:
    prefixes = {i.split(".", 1)[0] for i in ids}
    every = [findings.lap_summary, *findings.corners, *findings.straights, *findings.sectors]
    return {f.id.rsplit(":", 1)[1]: f.id for f in every if f.id.rsplit(":", 1)[1] in prefixes}


def _bullets(report: Report) -> list[Bullet]:
    return [report.headline, *report.biggest_time_loss, *report.strongest_areas, *report.tire_notes,
            *report.driving_suggestions, *report.strategy_options, *report.data_limitations]


class EngineerService:
    def __init__(self, comparisons: ComparisonService, tires: TireService, *,
                 client_factory: Callable[[], LLMClient | None], llm_enabled: Callable[[], bool],
                 store: ConversationStore | None = None, metrics: Metrics = METRICS,
                 cache: Cache | None = None) -> None:
        self.comparisons = comparisons
        self.tires = tires
        self._client_factory = client_factory
        self._llm_enabled = llm_enabled
        self.store = store or ConversationStore()
        self.metrics = metrics
        self.cache: Cache = cache if cache is not None else MemoryCache(max_entries=512)

    # ------------------------------------------------------------ FactSheet
    def _tire(self, key: str, driver: str, lap: int, ctx: TireContext | None, session_code: str | None
              ) -> tuple[dict[str, Any] | None, str | None]:
        from .routes.tires import to_schema       # here: routes import deps, which import this module
        if session_code != "R":                   # first: "not a race" is the real reason, requested or not
            return None, NON_RACE
        if ctx is None:
            return None, NOT_REQUESTED
        try:
            return to_schema(self.tires.predict(key, driver, ctx.lap or lap, ctx.horizon)).model_dump(mode="json"), None
        except ModelNotAvailableError:
            return None, "tyre model unavailable"
        except NotARaceSessionError:
            return None, NON_RACE
        except (LookupError, ValueError) as exc:     # a lap the forecast cannot use: the report still works
            return None, f"tyre forecast unavailable ({type(exc).__name__})"

    def factsheet(self, req: EngineerReportRequest | EngineerAskRequest) -> tuple[FactSheet, FindingsReport]:
        """The comparison's FactSheet, cached (raceiq:v1:fs:{session}:{digest}) and also stored by
        hash for GET /engineer/facts/{hash}. The digest includes the session's ingested_at, the
        FactSheet version, the laps and the tyre context."""
        key = canonical_session_key(req.session_key)
        cmp = self.comparisons.compare(ComparisonRequest(session_key=req.session_key, reference=req.reference,
                                                         target=req.target))
        findings = cmp.findings
        manifest = self.comparisons.repo.get_manifest(key)
        ctx = req.tire_context.model_dump() if req.tire_context else None
        fs_key = cache_key("fs", key, digest(FACTSHEET_VERSION, manifest.ingested_at.isoformat() if manifest else None,
                                             findings.comparison_id, cmp.meta.reference.lap_number,
                                             cmp.meta.target.lap_number, ctx))

        def build() -> bytes:
            info = self.comparisons.repo.get_session_info(key)
            tgt = cmp.meta.target
            tire, reason = self._tire(key, tgt.driver, tgt.lap_number, req.tire_context, info.session_code)
            teams = {m.driver: m.team for m in (cmp.meta.reference, cmp.meta.target) if m.team}
            context = FactSheetContext(session_date=info.date.date().isoformat() if info.date else None, teams=teams,
                                       tire_unavailable_reason=reason)
            return build_factsheet(findings, tire, context).model_dump_json().encode("utf-8")

        raw, _ = single_flight(self.cache, fs_key, build, TTL_S["fs"])
        fs = FactSheet.model_validate_json(raw)
        self.cache.set(cache_key("fsh", "all", fs.factsheet_hash), raw, TTL_S["fsh"])
        return fs, findings

    def facts(self, factsheet_hash: str) -> FactsResponse:
        raw = self.cache.get(cache_key("fsh", "all", factsheet_hash))
        if raw is None:
            raise FactSheetNotFoundError(factsheet_hash)
        fs = FactSheet.model_validate_json(raw)
        return FactsResponse(factsheet_hash=fs.factsheet_hash, factsheet_version=fs.factsheet_version,
                             session=fs.session.model_dump(), drivers=fs.drivers.model_dump(),
                             facts=list(fact_views(fs).values()))

    def _client(self) -> LLMClient | None:
        return self._client_factory() if self._llm_enabled() else None

    # ------------------------------------------------------------ report
    def _report_key(self, fs: FactSheet, client: LLMClient | None, model: str | None) -> str:
        """raceiq:v1:rpt:{session}:{digest}: FactSheet hash + prompt version + model (LLM) or the
        template version, so a prompt or model change can never serve an old report."""
        scope = canonical_session_key(fs.session.key or "0000-00-X")
        parts: tuple[Any, ...] = (("llm", fs.factsheet_hash, PROMPT_VERSION, model) if client is not None
                                  else ("template", fs.factsheet_hash, TEMPLATE_VERSION))
        return cache_key("rpt", scope, digest(*parts))

    def report(self, req: EngineerReportRequest, request_id: str | None = None) -> EngineerReport:
        start = time.perf_counter()
        fs, findings = self.factsheet(req)
        enabled = self._llm_enabled()
        client = self._client_factory() if enabled else None
        model = getattr(client, "model", None) if client is not None else None
        key = self._report_key(fs, client, model)
        if req.regenerate:
            self.cache.delete(key)
        run: dict[str, Any] = {}

        def compute() -> bytes:
            attempts: Any = ()
            first: Any = ()
            reason = None
            if client is None:
                report = build_template_report(fs)
                outcome = "template_no_llm" if enabled else "template_disabled"
            else:
                result = generate_report(fs, client)
                report, outcome, reason = result.report, result.outcome.value, result.fallback_reason
                attempts, first = result.attempts, result.first_attempt_violations
            assert validate_report(report, fs).ok
            ids = {i for b in _bullets(report) for i in b.fact_ids}
            sections = EngineerReportSections.model_validate(report.model_dump(include={
                "headline", "biggest_time_loss", "strongest_areas", "tire_notes", "driving_suggestions",
                "strategy_options", "data_limitations"}))
            out = EngineerReport(report=sections, generated_by=report.generated_by, outcome=outcome,
                                 fallback_reason=reason, prompt_version=report.prompt_version,
                                 factsheet_hash=fs.factsheet_hash, facts=fact_views(fs, ids),
                                 segments=_segment_map(findings, ids), retried=report.retried, cached=False,
                                 model=model if report.generated_by == "llm" else None)
            run.update(out=out, attempts=attempts, first=first, reason=reason)
            if client is not None and report.generated_by != "llm":   # a fallback: cache it as the template
                template = out.model_copy(update={"outcome": "template_no_llm", "fallback_reason": None})
                self.cache.set(self._report_key(fs, None, None), template.model_dump_json().encode("utf-8"),
                               TTL_S["rpt"])
            return out.model_dump_json().encode("utf-8")

        def ttl_for(value: bytes) -> int:
            # never cache a fallback under the LLM key: the LLM is tried again on the next request
            return TTL_S["rpt"] if client is None or b'"generated_by":"llm"' in value else 0

        raw, _ = single_flight(self.cache, key, compute, TTL_S["rpt"], ttl_for=ttl_for)
        if "out" in run:
            out: EngineerReport = run["out"]
            self._done("report", out.outcome, fs, start, cached=False, request_id=request_id,
                       attempts=run["attempts"], first=run["first"], reason=run["reason"],
                       prompt_version=out.prompt_version)
            return out
        cached = EngineerReport.model_validate_json(raw).model_copy(update={"cached": True})
        self._done("report", cached.outcome, fs, start, cached=True, request_id=request_id,
                   prompt_version=cached.prompt_version)
        return cached

    # ------------------------------------------------------------ ask
    def ask(self, req: EngineerAskRequest, request_id: str | None = None) -> EngineerAnswer:
        start = time.perf_counter()
        fs, findings = self.factsheet(req)
        enabled = self._llm_enabled()
        client = self._client_factory() if enabled else None
        res = answer_question(fs, req.question, client=client, store=self.store, conversation_id=req.conversation_id)
        a = res.answer
        outcome = a.outcome
        if not enabled and outcome == "template_no_llm":
            outcome = "template_disabled"
        ids = set(a.answer.fact_ids)
        out = EngineerAnswer(conversation_id=a.conversation_id, question=a.question, intent=a.intent,
                             entities=a.entities, answer=BulletOut(**a.answer.model_dump()),
                             follow_up_suggestions=[FollowUpOut(**f.model_dump()) for f in a.follow_up_suggestions],
                             generated_by=a.generated_by, outcome=outcome, fallback_reason=a.fallback_reason,
                             prompt_version=a.prompt_version, factsheet_hash=fs.factsheet_hash,
                             facts=fact_views(fs, ids), segments=_segment_map(findings, ids), retried=a.retried)
        latency = (time.perf_counter() - start) * 1000
        self.metrics.record_answer(outcome, a.intent, latency_ms=latency, attempts=res.attempts,
                                   first=res.first_attempt_violations, fallback_reason=a.fallback_reason)
        log.info("ai.engineer_request", kind="ask", factsheet_hash=fs.factsheet_hash, prompt_version=a.prompt_version,
                 outcome=outcome, intent=a.intent, latency_ms=round(latency, 1), request_id=request_id,
                 **({"question": req.question} if _debug() else {}))
        return out

    def _done(self, kind: str, outcome: str, fs: FactSheet, start: float, *, cached: bool, request_id: str | None,
              attempts: Any = (), first: Any = (), reason: str | None = None, prompt_version: str = "") -> None:
        latency = (time.perf_counter() - start) * 1000
        self.metrics.record_report(outcome, cached=cached, latency_ms=latency, attempts=attempts, first=first,
                                   fallback_reason=reason)
        log.info("ai.engineer_request", kind=kind, factsheet_hash=fs.factsheet_hash, prompt_version=prompt_version,
                 outcome=outcome, cached=cached,
                 latency_ms=round(latency, 1), request_id=request_id, fallback_reason=reason)
