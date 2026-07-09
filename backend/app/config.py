"""Central configuration. Reads from environment / .env (see .env.example)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RACEIQ_", extra="ignore")

    # --- Data sources ---
    openf1_base_url: str = "https://api.openf1.org/v1"
    openf1_api_key: str | None = None  # only needed for the live/sponsor tier
    request_timeout_s: float = 20.0

    # FastF1 on-disk cache (speeds up repeat telemetry/training pulls)
    fastf1_cache_dir: str = ".fastf1_cache"
    enable_fastf1: bool = True

    # --- App cache (in-memory dev default; point at Redis in prod) ---
    cache_ttl_seconds: int = 60 * 60  # historical data is immutable, cache hard
    redis_url: str | None = None

    # --- AI race engineer (optional) ---
    openai_api_key: str | None = None
    ai_model: str = "gpt-4o-mini"

    # --- Server ---
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
