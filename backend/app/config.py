"""Central configuration. Reads from environment / .env (see .env.example)."""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RACEIQ_", extra="ignore")

    # --- Data sources ---
    openf1_base_url: str = "https://api.openf1.org/v1"
    openf1_api_key: str | None = None  # only needed for the live/sponsor tier
    request_timeout_s: float = 20.0

    # FastF1 on-disk cache (speeds up repeat telemetry/training pulls)
    fastf1_cache_dir: str = ".fastf1_cache"
    enable_fastf1: bool = True
    # Normalized session store written by `python -m app.ingestion` (Parquet + JSON)
    data_dir: str = str(_REPO_ROOT / "data")

    # --- App cache (in-memory dev default; point at Redis in prod) ---
    cache_ttl_seconds: int = 60 * 60  # historical data is immutable, cache hard
    redis_url: str | None = None

    # --- Tire model (Person 3) ---
    # Gitignored by design, so a fresh clone has no artifact and tires.py serves the
    # baseline. See ml/README.md for how to build one.
    tire_model_path: str = str(_REPO_ROOT / "ml" / "models" / "tire_model.pkl")
    # Serve the model even when its own artifact records beats_baseline=False.
    # Off by default: ml/models/MODEL_CARD.md currently says the trained model loses
    # to the trivial baselines, so shipping it would make the endpoint worse.
    tire_model_force: bool = False

    # --- Tyre prediction service (Phase 6E): registry of servable models, committed in models/ ---
    tire_registry_path: str = str(_REPO_ROOT / "models" / "registry.json")

    # --- AI race engineer (optional) ---
    openai_api_key: str | None = None
    ai_model: str = "gpt-4o-mini"

    # --- Phase 7F engineer endpoints (/api/v1/engineer) ---
    # Kill switch: off → always the template report / template answers, zero LLM calls.
    # Read from ENGINEER_LLM_ENABLED (the plan's name) or RACEIQ_ENGINEER_LLM_ENABLED.
    engineer_llm_enabled: bool = Field(
        default=True, validation_alias=AliasChoices("ENGINEER_LLM_ENABLED", "RACEIQ_ENGINEER_LLM_ENABLED"))
    # TEST ONLY: a scripted fake LLM for the end-to-end safety test ("inject": every answer
    # carries an ungrounded number and a fabricated cause). Never set in production.
    engineer_fake_llm: str | None = None
    # Admin token for GET /api/v1/engineer/metrics (unset → the endpoint is disabled).
    admin_token: str | None = None

    # --- Server ---
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
