"""Phase 4 — /api/v1 against the Parquet fixture repository (2024 R5 Qualifying), no network.

Uses httpx.AsyncClient over ASGITransport with raise_app_exceptions=False so the 500
path is exercised exactly as a client would see it.
"""
from __future__ import annotations

import gzip
import json
import shutil
import statistics
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from app.api import deps, errors, services
from app.api.routes import router as v1_router
from app.api.schemas.comparisons import ComparisonResponse
from app.api.services import ComparisonService
from app.ingestion.fastf1_client import FastF1Client
from app.main import app
from app.processing.errors import InsufficientDataError
from app.storage.repository import ParquetRepository
from scripts.export_openapi import OUT as OPENAPI_JSON
from scripts.export_openapi import openapi_document, render
from tests import ingestion_synth as synth

FIXTURE_STORE = Path(__file__).parent / "fixtures" / "ingestion" / "store"
KEY = "2024-05-Q"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def repo():
    return ParquetRepository(FIXTURE_STORE)


@pytest.fixture
def source():
    src = MagicMock(spec=FastF1Client)
    src.fastf1_version = "3.5.3"
    src.load_session.side_effect = lambda y, r, c: synth.raw_session(y, r, c)
    src.lap_telemetry.side_effect = lambda raw, d, n: synth.raw_telemetry_frame()
    return src


@pytest.fixture
def comparison_service(repo):
    return ComparisonService(repo)


@pytest.fixture
async def client(repo, source, comparison_service):
    app.dependency_overrides[deps.get_repository] = lambda: repo
    app.dependency_overrides[deps.get_source_factory] = lambda: (lambda: source)
    app.dependency_overrides[deps.get_comparison_service] = lambda: comparison_service
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _assert_error(response: httpx.Response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    err = body["error"]
    assert {"code", "message", "status_code", "request_id"} <= set(err)
    assert err["code"] == code and err["status_code"] == status and err["message"]
    assert err["request_id"] and err["request_id"] == response.headers["x-request-id"]
    assert "Traceback" not in response.text and 'File "' not in response.text
    return err


def _compare_body(ref="VER", ref_lap="fastest", tgt="ALO", tgt_lap="fastest", **extra):
    return {"session_key": KEY, "reference": {"driver": ref, "lap": ref_lap},
            "target": {"driver": tgt, "lap": tgt_lap}, **extra}


# ---------------------------------------------------------------- meta
async def test_health_and_ready(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.headers["x-request-id"]
    r = await client.get("/api/v1/ready")
    assert r.status_code == 200 and r.json() == {"status": "ready", "checks": {"session_store": True}}


async def test_ready_reports_503_when_store_unreadable(client, repo):
    broken = MagicMock()
    broken.list_sessions.side_effect = OSError("disk gone")
    app.dependency_overrides[deps.get_repository] = lambda: broken
    r = await client.get("/api/v1/ready")
    assert r.status_code == 503 and r.json()["status"] == "not_ready"


async def test_request_id_is_echoed_when_safe(client):
    r = await client.get("/api/v1/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    r = await client.get("/api/v1/health", headers={"X-Request-ID": "bad id with spaces"})
    assert r.headers["x-request-id"] != "bad id with spaces"


# ---------------------------------------------------------------- seasons / events
async def test_seasons(client):
    body = (await client.get("/api/v1/seasons")).json()
    years = [s["year"] for s in body["seasons"]]
    assert years == sorted(years, reverse=True) and years[-1] == 2018 and body["earliest_supported"] == 2018
    assert next(s for s in body["seasons"] if s["year"] == 2024)["sessions_stored"] == 1


async def test_events_uses_cached_schedule(client, source, tmp_path):
    app.dependency_overrides[deps.get_repository] = lambda: ParquetRepository(tmp_path)
    source.event_schedule.return_value = __import__("pandas").DataFrame({
        "RoundNumber": [5], "Country": ["China"], "Location": ["Shanghai"], "EventName": ["Chinese Grand Prix"],
        "EventFormat": ["sprint_qualifying"], "EventDate": [__import__("pandas").Timestamp("2024-04-21")],
        "Session4": ["Qualifying"], "Session4DateUtc": [__import__("pandas").Timestamp("2024-04-20 07:00")],
    })
    for _ in range(2):
        body = (await client.get("/api/v1/seasons/2024/events")).json()
    assert body["year"] == 2024 and body["events"][0]["event_name"] == "Chinese Grand Prix"
    assert body["events"][0]["sessions"][0] == {"name": "Qualifying", "code": "Q", "date": "2024-04-20T07:00:00Z"}
    source.event_schedule.assert_called_once()


async def test_events_year_out_of_range(client):
    _assert_error(await client.get("/api/v1/seasons/1990/events"), 422, "validation_error")


# ---------------------------------------------------------------- sessions
@pytest.mark.parametrize("key", ["2024-05-Q", "2024_05_Q", "2024-05-q"])
async def test_session_status_ready(client, key):
    body = (await client.get(f"/api/v1/sessions/{key}")).json()
    assert body["status"] == "ready" and body["session_key"] == KEY
    assert body["info"]["event_name"] == "Chinese Grand Prix" and body["manifest"]["schema_version"] == 1


async def test_session_status_not_ingested_and_bad_key(client):
    body = (await client.get("/api/v1/sessions/2024-06-Q")).json()
    assert body == {"session_key": "2024-06-Q", "status": "not_ingested", "info": None, "manifest": None}
    _assert_error(await client.get("/api/v1/sessions/not-a-key"), 422, "validation_error")


async def test_ingest_then_skip(client, source, tmp_path):
    app.dependency_overrides[deps.get_repository] = lambda: ParquetRepository(tmp_path)
    first = (await client.post("/api/v1/sessions/2024-05-Q/ingest")).json()
    assert first["skipped"] is False and first["laps"] == 3 and first["session_key"] == KEY
    second = (await client.post("/api/v1/sessions/2024-05-Q/ingest")).json()
    assert second["skipped"] is True and source.load_session.call_count == 1
    forced = (await client.post("/api/v1/sessions/2024-05-Q/ingest?force=true")).json()
    assert forced["skipped"] is False and source.load_session.call_count == 2
    assert (await client.get("/api/v1/sessions/2024-05-Q")).json()["status"] == "ready"


async def test_ingest_unknown_session_is_404(client, source, tmp_path):
    from app.ingestion.errors import SessionNotFoundError

    app.dependency_overrides[deps.get_repository] = lambda: ParquetRepository(tmp_path)
    source.load_session.side_effect = SessionNotFoundError("FastF1 could not load 2024 round 99 Q")
    _assert_error(await client.post("/api/v1/sessions/2024-99-Q/ingest"), 404, "session_not_found")


async def test_drivers(client):
    body = (await client.get(f"/api/v1/sessions/{KEY}/drivers")).json()
    assert [d["code"] for d in body["drivers"]] == ["VER", "PER", "ALO"]
    assert all(d["team_color"].startswith("#") for d in body["drivers"])


async def test_laps_and_driver_filter(client):
    assert len((await client.get(f"/api/v1/sessions/{KEY}/laps")).json()["laps"]) == 9
    ver = (await client.get(f"/api/v1/sessions/{KEY}/laps", params={"driver": "VER"})).json()
    assert ver["driver"] == "VER" and {lap["driver"] for lap in ver["laps"]} == {"VER"} and len(ver["laps"]) == 3
    by_number = (await client.get(f"/api/v1/sessions/{KEY}/laps", params={"driver": "1"})).json()
    assert len(by_number["laps"]) == 3
    lap = ver["laps"][0]
    assert {"lap_time_s", "compound", "tyre_age", "is_valid_for_comparison", "invalid_reason"} <= set(lap)
    _assert_error(await client.get(f"/api/v1/sessions/{KEY}/laps", params={"driver": "HAM"}), 404, "driver_not_found")


async def test_fastest_laps(client):
    body = (await client.get(f"/api/v1/sessions/{KEY}/fastest-laps")).json()
    assert [(e["driver"], e["lap"]["lap_time_s"]) for e in body["laps"]] == [
        ("VER", 93.66), ("PER", 93.982), ("ALO", 94.148)]


async def test_corners(client):
    body = (await client.get(f"/api/v1/sessions/{KEY}/corners")).json()
    assert len(body["corners"]) == 16 and body["circuit_rotation_deg"] == 237.0


@pytest.mark.parametrize("path", ["drivers", "laps", "fastest-laps", "corners"])
async def test_not_ingested_session_is_404(client, path):
    _assert_error(await client.get(f"/api/v1/sessions/2024-06-Q/{path}"), 404, "session_not_found")


# ---------------------------------------------------------------- comparisons: contract
async def test_comparison_contract(client):
    r = await client.post("/api/v1/comparisons", json=_compare_body())
    assert r.status_code == 200, r.text
    body = r.json()
    ComparisonResponse.model_validate(body)
    n = body["meta"]["points"]
    arrays = [body["distance_m"], body["delta_s"]] + [
        arr for side in ("reference", "target") for arr in body["channels"][side].values()]
    assert all(len(a) == n for a in arrays)
    assert 2500 <= n <= 3500  # default 2 m on a 5.4 km lap
    assert body["delta_s"][0] == 0.0
    gap = body["meta"]["target"]["lap_time_s"] - body["meta"]["reference"]["lap_time_s"]
    assert abs(body["delta_s"][-1] - gap) <= 0.02
    assert body["distance_m"][0] == 0.0 and body["distance_m"] == sorted(body["distance_m"])
    assert all(isinstance(v, bool) for v in body["channels"]["target"]["brake"])
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in body["channels"]["target"]["gear"])
    meta = body["meta"]
    assert (meta["reference"]["driver"], meta["reference"]["lap_number"], meta["target"]["driver"]) == ("VER", 17, "ALO")
    assert meta["reference"]["team_color"] == "#3671C6" and meta["cached"] is False
    assert "Positive = the target is behind" in meta["sign_convention"]
    f = body["findings"]
    assert f["comparison_id"] == "2024-05-Q:ALO-vs-VER" and len(f["corners"]) == 16
    assert abs(sum(c["time_loss_s"] for c in f["corners"]) - f["lap_summary"]["time_loss_s"]) <= 1e-9


async def test_comparison_explicit_laps_and_resolution(client):
    fine = (await client.post("/api/v1/comparisons", json=_compare_body(ref_lap=17, tgt_lap=14, resolution_m=1))).json()
    coarse = (await client.post("/api/v1/comparisons", json=_compare_body(ref_lap=17, tgt_lap=14, resolution_m=10))).json()
    assert fine["meta"]["points"] > 5000 and 500 < coarse["meta"]["points"] < 600
    # thinning never changes the analysis
    assert fine["findings"]["lap_summary"] == coarse["findings"]["lap_summary"]
    assert fine["delta_s"][-1] == coarse["delta_s"][-1]


async def test_comparison_is_cached(client, comparison_service):
    first = (await client.post("/api/v1/comparisons", json=_compare_body())).json()
    second = (await client.post("/api/v1/comparisons", json=_compare_body(ref_lap=17))).json()  # same resolved lap
    assert first["meta"]["cached"] is False and second["meta"]["cached"] is True
    assert comparison_service.hits == 1 and comparison_service.misses == 1
    assert second["delta_s"] == first["delta_s"]


async def test_comparison_cache_is_bounded():
    svc = ComparisonService(ParquetRepository(FIXTURE_STORE), cache_size=1)
    from app.api.schemas.comparisons import ComparisonRequest

    svc.compare(ComparisonRequest.model_validate(_compare_body()))
    svc.compare(ComparisonRequest.model_validate(_compare_body(tgt="PER")))
    svc.compare(ComparisonRequest.model_validate(_compare_body()))
    assert (svc.hits, svc.misses, len(svc._cache)) == (0, 3, 1)


async def test_comparison_gzip(client):
    r = await client.post("/api/v1/comparisons", json=_compare_body(), headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"
    assert r.num_bytes_downloaded < len(r.content) / 3  # bytes on the wire vs decoded body


# ---------------------------------------------------------------- comparisons: every error path
async def test_error_session_not_ingested(client):
    body = _compare_body() | {"session_key": "2024-06-Q"}
    _assert_error(await client.post("/api/v1/comparisons", json=body), 404, "session_not_found")


async def test_error_driver_and_lap_not_found(client):
    _assert_error(await client.post("/api/v1/comparisons", json=_compare_body(tgt="HAM")), 404, "driver_not_found")
    _assert_error(await client.post("/api/v1/comparisons", json=_compare_body(tgt_lap=99)), 404, "lap_not_found")


async def test_error_lap_invalid(client):
    err = _assert_error(await client.post("/api/v1/comparisons", json=_compare_body(tgt_lap=1)), 422, "lap_invalid")
    assert "no_lap_time" in err["message"]


async def test_error_insufficient_data(client, monkeypatch):
    def boom(*args, **kwargs):
        raise InsufficientDataError("final delta +0.300s does not match the official gap +0.000s")

    monkeypatch.setattr(services, "compare_laps", boom)
    err = _assert_error(await client.post("/api/v1/comparisons", json=_compare_body()), 422, "insufficient_data")
    assert "official gap" in err["message"]


async def test_error_telemetry_unavailable(client, tmp_path):
    store = tmp_path / "store"
    shutil.copytree(FIXTURE_STORE, store)
    (store / "2024_05_Q" / "telemetry" / "ALO_014.parquet").unlink()  # ALO fastest valid lap
    repo = ParquetRepository(store)
    app.dependency_overrides[deps.get_comparison_service] = lambda: ComparisonService(repo)
    _assert_error(await client.post("/api/v1/comparisons", json=_compare_body()), 424, "telemetry_unavailable")


@pytest.mark.parametrize("body", [
    _compare_body(resolution_m=0.5),
    _compare_body(resolution_m=11),
    _compare_body() | {"session_key": "bogus"},
    _compare_body(tgt_lap="slowest"),
    {"session_key": KEY},
    _compare_body() | {"unexpected": 1},
])
async def test_error_validation(client, body):
    err = _assert_error(await client.post("/api/v1/comparisons", json=body), 422, "validation_error")
    assert err["details"]


async def test_error_payload_too_large_request(client):
    big = _compare_body() | {"padding": "x" * (errors.MAX_BODY_BYTES + 1)}
    _assert_error(await client.post("/api/v1/comparisons", json=big), 413, "payload_too_large")


async def test_error_payload_too_large_response(client, monkeypatch):
    monkeypatch.setattr(services, "MAX_POINTS", 100)
    _assert_error(await client.post("/api/v1/comparisons", json=_compare_body()), 413, "payload_too_large")


async def test_error_unexpected_is_500_without_leak(client):
    broken = MagicMock()
    broken.compare_json.side_effect = RuntimeError("secret internal detail at /srv/raceiq")
    app.dependency_overrides[deps.get_comparison_service] = lambda: broken
    r = await client.post("/api/v1/comparisons", json=_compare_body())
    err = _assert_error(r, 500, "internal_error")
    assert "secret" not in r.text and "/srv" not in r.text and "request_id" in err["message"]


async def test_error_rate_limited():
    mini = FastAPI()
    errors.install(mini, limiter=lambda request: False)
    mini.include_router(v1_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mini), base_url="http://test") as c:
        _assert_error(await c.get("/api/v1/health"), 429, "rate_limited")


async def test_unknown_route_uses_envelope(client):
    _assert_error(await client.get("/api/v1/nope"), 404, "not_found")


async def test_cors_only_configured_origin(client):
    ok = await client.options("/api/v1/health", headers={"Origin": "http://localhost:5173",
                                                         "Access-Control-Request-Method": "GET"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    bad = await client.options("/api/v1/health", headers={"Origin": "https://evil.example",
                                                          "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers


# ---------------------------------------------------------------- OpenAPI snapshot / docs
def test_openapi_snapshot_is_reviewed():
    assert OPENAPI_JSON.read_text(encoding="utf-8") == render(openapi_document()), (
        "OpenAPI changed: run `python -m scripts.export_openapi`, review the diff, regenerate frontend types"
    )


def test_openapi_documents_every_v1_route():
    doc = openapi_document()
    v1 = {(m.upper(), p) for p, item in doc["paths"].items() if p.startswith("/api/v1") for m in item}
    assert len(v1) == 12  # 11 from Phase 4 + GET /sessions/{key}/stints (Phase 6A)
    for path, item in doc["paths"].items():
        if path.startswith("/api/v1"):
            for op in item.values():
                assert op.get("summary") and op.get("description") and op["tags"][0].startswith("v1: ")
    delta = doc["components"]["schemas"]["ComparisonResponse"]["properties"]["delta_s"]
    assert "Positive = the target is behind" in delta["description"]
    assert doc["components"]["schemas"]["ComparisonRequest"]["examples"]
    errors_doc = doc["paths"]["/api/v1/comparisons"]["post"]["responses"]
    assert {"404", "413", "422", "424"} <= set(errors_doc)


# ---------------------------------------------------------------- performance
async def test_comparison_latency(client, repo, capsys):
    """p95 of a full (uncached) comparison at default resolution on a stored session."""
    timings = []
    for _ in range(20):
        app.dependency_overrides[deps.get_comparison_service] = lambda: ComparisonService(repo)  # cold LRU
        start = time.perf_counter()
        r = await client.post("/api/v1/comparisons", json=_compare_body(), headers={"Accept-Encoding": "gzip"})
        timings.append((time.perf_counter() - start) * 1000)
        assert r.status_code == 200
    timings.sort()
    p95 = timings[int(0.95 * len(timings)) - 1]
    with capsys.disabled():
        print(f"\n[perf] POST /comparisons uncached: median {statistics.median(timings):.0f} ms, p95 {p95:.0f} ms")
    assert p95 < 300
