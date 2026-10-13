from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query

from ...tires.service import MAX_HORIZON, Prediction, TireService
from ..deps import get_tire_service
from ..schemas.common import SESSION_KEY_DESCRIPTION, SESSION_KEY_PATTERN, canonical_session_key, error_responses, public_session_key
from ..schemas.tires import (
    INSUFFICIENT_EXAMPLE,
    PREDICTION_EXAMPLE,
    UNCERTAIN_EXAMPLE,
    TireModelInfo,
    TireModelsResponse,
    TirePrediction,
)

router = APIRouter(prefix="/tires", tags=["v1: tires"])


def to_schema(p: Prediction) -> TirePrediction:
    data = asdict(p)
    data["session_key"] = public_session_key(p.session_key)
    return TirePrediction.model_validate(data)


@router.get(
    "/predict", response_model=TirePrediction, summary="Tyre forecast and pit window at a lap",
    description="Uses only laps ≤ `lap` (all drivers, for the 6A exclusion rules) and the driver's current stint. "
                "Needs ≥ 5 clean laps in the stint, otherwise `status: insufficient_data` with a `reason` and no "
                "predictions or window. The pit window is a heuristic (interval_crossing_v2): it ignores "
                "undercut/overcut, traffic, track position, safety-car probability and tyre availability. "
                "Any field that cannot be computed is null and explained in `warnings`.",
    responses={**error_responses(404, 422, 503),
               200: {"content": {"application/json": {"examples": {
                   "ok": {"summary": "forecast with a pit window", "value": PREDICTION_EXAMPLE},
                   "uncertain": {"summary": "pit window null: too uncertain", "value": UNCERTAIN_EXAMPLE},
                   "insufficient_data": {"summary": "too few clean laps", "value": INSUFFICIENT_EXAMPLE}}}}}},
)
def predict(
    session: str = Query(pattern=SESSION_KEY_PATTERN, description=SESSION_KEY_DESCRIPTION, examples=["2024-14-R"]),
    driver: str = Query(min_length=1, max_length=8, description="driver code (LEC) or number (16)", examples=["LEC"]),
    lap: int = Query(ge=1, le=200, description="current lap: the forecast uses laps ≤ lap only", examples=[20]),
    horizon: int = Query(5, ge=1, le=MAX_HORIZON, description=f"laps to forecast (1–{MAX_HORIZON})"),
    threshold: float | None = Query(None, gt=0, le=5.0, description="pit-window X in seconds above best pace "
                                                                    "(default 1.0; a heuristic)"),
    service: TireService = Depends(get_tire_service),
) -> TirePrediction:
    return to_schema(service.predict(canonical_session_key(session), driver, lap, horizon, threshold))


@router.get("/models", response_model=TireModelsResponse, summary="Registered tyre models",
            description="Every registered model version with its status (production | candidate), provenance "
                        "(dataset hash, git SHA, trained-through date) and 6D evaluation summary.",
            responses=error_responses(503))
def models(service: TireService = Depends(get_tire_service)) -> TireModelsResponse:
    return TireModelsResponse(models=[
        TireModelInfo(name=e.name, version=e.version, kind=e.kind, status=e.status, dataset_hash=e.dataset_hash,
                      git_sha=e.git_sha, trained_through=e.trained_through,
                      trained_through_session=public_session_key(e.trained_through_session),
                      interval_alpha=e.alpha, metrics=e.metrics, notes=e.notes)
        for e in service.models()])
