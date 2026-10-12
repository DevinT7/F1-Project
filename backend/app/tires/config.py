"""Phase 6 tire-model configuration: the single SEED, thresholds and feature lists.

"Estimated degradation" anywhere in this package means a COMBINED ADJUSTED TREND
(tire wear + residual track evolution after the fuel adjustment), never pure tire wear.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

SEED = 42  # every random draw in app.tires is seeded from this

DATASET_SCHEMA_VERSION = 1

# FastF1 3.5.3 track-status codes (fastf1/_api.py::track_status_data docstring):
# 1 track clear, 2 yellow, 3 unused, 4 safety car, 5 red flag, 6 VSC deployed, 7 VSC ending.
# A lap's TrackStatus string concatenates every code seen during the lap ("12", "1267"),
# so it is treated as a SET of characters, never compared as a single value.
GREEN_ONLY: frozenset[str] = frozenset({"1"})
RED_FLAG_CODE = "5"
NEUTRALIZATION_CODES: frozenset[str] = frozenset({"4", "5", "6", "7"})  # SC, red flag, VSC, VSC ending
NO_NEUTRALIZATION_SENTINEL = 999  # laps_since_neutralization before any SC/VSC/red flag

WET_COMPOUNDS: frozenset[str] = frozenset({"INTERMEDIATE", "WET"})
UNKNOWN_COMPOUNDS: frozenset[str] = frozenset({"UNKNOWN", "TEST_UNKNOWN", "NONE", "NAN", ""})


@dataclass(frozen=True)
class ExclusionConfig:
    outlier_mad_k: float = 3.0           # drop laps slower than median + k × 1.4826 × MAD
    outlier_min_laps: int = 5            # only test stints with at least this many laps
    min_clean_stint_laps: int = 5        # short_stint threshold (also "model-eligible")
    keep_short_stints: bool = False      # diagnostics: keep short stints instead of dropping

    def as_dict(self) -> dict[str, float | int | bool]:
        return asdict(self)


RULE_ORDER_NOTE = (
    "Order matters: later rules only see laps the earlier ones kept, so stint_outlier and short_stint "
    "judge stints by their already-clean laps, and a lap failing several rules is counted once."
)

MAD_TO_SIGMA = 1.4826  # MAD → standard deviation for normally distributed data

# Columns of one dataset row (one race lap for one driver), in order.
DATASET_COLUMNS: tuple[str, ...] = (
    "session_key", "circuit", "year", "round", "driver", "team", "lap_number", "stint",
    "stint_lap_index", "compound", "tyre_age", "fresh_tyre", "lap_time_s", "track_temp_c",
    "air_temp_c", "rainfall", "laps_since_neutralization", "position", "prev_lap_time_s",
    "race_laps_total", "fuel_proxy", "traffic_known",
)
