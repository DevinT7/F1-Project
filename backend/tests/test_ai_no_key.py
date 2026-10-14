"""No API key configured: the AI layer degrades to templates and says where the key is needed."""
from __future__ import annotations

import structlog

from app.ai import llm_client
from app.ai.llm_client import API_KEY_NEEDED, client_from_env
from scripts import eval_qa_quality, eval_report_quality


def test_client_from_env_returns_none_and_logs_once(monkeypatch):
    monkeypatch.setattr(llm_client, "_warned_unconfigured", False)
    with structlog.testing.capture_logs() as logs:
        assert client_from_env({}) is None
        assert client_from_env({"LLM_PROVIDER": "openai"}) is None
    events = [e for e in logs if e["event"] == "ai.llm_unconfigured"]
    assert len(events) == 1 and events[0]["action"] == API_KEY_NEEDED
    assert "backend/.env" in API_KEY_NEEDED and "docs/api_keys.md" in API_KEY_NEEDED


def test_client_from_env_builds_the_provider_when_configured():
    client = client_from_env({"LLM_PROVIDER": "openai", "LLM_MODEL": "m", "LLM_API_KEY": "sk-test-123"})
    assert client is not None and "sk-test-123" not in repr(client)


def test_live_evals_without_a_key_say_api_key_needed(monkeypatch, capsys):
    for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CI"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(eval_report_quality, "load_env_file", lambda: None)
    monkeypatch.setattr(eval_qa_quality, "load_env_file", lambda: None)
    assert eval_report_quality.main(["--live", "--no-progress"]) == 3
    assert "API KEY NEEDED" in capsys.readouterr().err
    assert eval_qa_quality.main(["--live", "--no-progress"]) == 3
    assert "API KEY NEEDED" in capsys.readouterr().err


def test_record_progress_replaces_the_placeholder(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    doc = tmp_path / "docs" / "progress.md"
    doc.write_text("# P\n\n### 7D live evaluation\n\nPLACEHOLDER — API KEY NEEDED.\n\n### 7E live Q&A check\n\n"
                   "PLACEHOLDER — API KEY NEEDED.\n", encoding="utf-8")
    monkeypatch.setattr(eval_report_quality, "REPO", tmp_path)
    eval_report_quality.record_progress("7D live evaluation", "| Outcome | Count |")
    text = doc.read_text(encoding="utf-8")
    assert text.count("PLACEHOLDER") == 1 and "| Outcome | Count |" in text
    assert text.index("7D live evaluation") < text.index("7E live Q&A check")
    eval_report_quality.record_progress("7E live Q&A check", "done")
    assert "PLACEHOLDER" not in doc.read_text(encoding="utf-8")
