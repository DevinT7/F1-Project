# Insight schema (Phase 3 findings JSON)

`generate_findings(comparison, corners, thresholds, context=...)` in
`backend/app/processing/insights.py` returns a `FindingsReport`. Its JSON is the
**verified findings** the AI race engineer explains. The AI may rephrase these numbers,
but it must never add new ones. The Pydantic models in `insights.py` are the canonical
definition. The schema block at the end of this file is generated from them, and
`tests/test_processing_insights_real.py::test_schema_doc_matches_pydantic_model`
fails if the two drift apart. To regenerate:

```bash
cd backend && python -m app.processing.insights   # prints the JSON schema
```

## Conventions

- **Sign:** `time_loss_s > 0` means the **target** lost time to the **reference**.
  Every `*_diff*` metric is **target − reference**. So a negative
  `braking_start_diff_m` means the target braked earlier, and a negative
  `min_speed_diff_kph` means the target was slower at the apex.
- **Missing values are `null`, never `0`.** No braking in a flat-out corner gives
  `braking_start_m: null`, and the diff is `null` too. NaN never appears
  (serialization uses `allow_nan=False`).
- **Units:** metres, km/h, seconds, m/s². Distances are along the reference lap's grid.
- **IDs:** `"<session>:<TARGET>-vs-<REFERENCE>:<code>"`. Codes are `T7` / `T7A`
  (official corner), `A4148` (an unnamed speed minimum at 4148 m), `STR2`
  (straight), `SEC1` (sector) and `LAP`.
- **Causes are triggered rules, not explanations.** A cause appears only when its
  threshold is met. Treat causes as *contributing factors*, not proof. If a segment
  loses at least the noise floor but no rule fires, its cause is `"unclear"`; nothing
  is invented. A cause can point the opposite way to the segment's net loss (for
  example, `late_braking_gain` on a corner the target lost overall).

## Report structure

| Field | Meaning |
|---|---|
| `comparison_id` | `"<session>:<TARGET>-vs-<REFERENCE>"` |
| `session_context` | Per lap: driver, lap number, lap and sector times, compound, tyre age, fresh tyre, air and track temperature, rainfall, raw track status and decoded flags. Plus notes such as **"traffic data unavailable"**. This lets the AI tell driver, tyres and conditions apart using facts that are present. Traffic is never asserted, because gap-to-car-ahead is not reliably available. |
| `lap_summary` | `kind: "lap_summary"`. Contains `total_delta_s`, `official_gap_s`, `sector_{1,2,3}_delta_s` (official), `top_speed_diff_kph`, `corner_count`, `corners_lost`, `corners_gained` and `corners_unclear`. |
| `corners` | One finding per corner segment, in distance order. **They tile the lap:** their `time_loss_s` values sum to the total delta (the invariant is checked to 1e-9 s, or the report is refused). |
| `straights` | Stretches of at least 300 m where the reference is at 95 % throttle or more. They overlap corner segments, so never add them to the corner losses. |
| `sectors` | Grid-delta loss per official sector, with `official_sector_delta_s` and `boundary_discrepancy_s` (grid − official). |
| `largest_losses` / `largest_gains` | Corner ids, top 3, with \|loss\| ≥ the noise floor. Biggest first. |
| `quality` | Phase 2 quality block, plus `tiling_error_s` and the sector cross-check result. |
| `notes` | Report-wide caveats. Always included: causes are contributing factors, and braking uses the on/off channel, so there are no brake-pressure claims. |

## Corner segments

1. **Apexes.** The reference lap's speed is smoothed (Savitzky–Golay, 9 m). Minima with
   prominence ≥ 8 km/h become candidates. Each official corner (`CornerInfo`) is
   matched one-to-one, closest first, to a minimum within ±60 m (`apex_source:
   "matched"`). An unmatched official corner uses its official distance
   (`"official"`). An unmatched minimum is kept with an auto label such as "Unnamed
   corner at 4148 m" (`"detected"`, typically part of a chicane).
2. **X/Y check.** The official corner X/Y must lie within 50 m of the reference lap's
   X/Y at the official distance, or the finding gets a note. FastF1 X/Y are
   **decimetres**; we verified this because path length / Distance = 9.95. On 2024
   Shanghai all 16 corners are within 1.6–11 m.
3. **Partition.** Segment boundaries are the midpoints between consecutive apexes. The
   first segment starts at 0 and the last ends at L_ref. A corner segment therefore
   includes half of each adjacent straight, so its loss covers its approach and exit,
   not only the turn itself.
4. **Type** by reference minimum speed: `slow` < 120, `medium` 120–220, `fast` > 220 km/h.

## Corner metrics (`target` / `reference` objects, one per lap)

A = apex distance. Windows are clipped to the segment.

| Key | Definition |
|---|---|
| `braking_start_m` | First distance in [A − 400, A] where `brake` turns on **and stays on ≥ 0.3 s**. `null` if there's no braking. |
| `braking_duration_s`, `peak_decel_ms2` | Informational: duration of that brake run, and peak −dv/dt (smoothed speed). |
| `entry_speed_kph` | Speed at `braking_start_m`, or at A − 100 m when there's no braking. |
| `min_speed_kph`, `min_speed_m` | Minimum speed in [A − 80, A + 80] and where it occurs. |
| `exit_speed_kph` | Speed at A + 100 m. |
| `throttle_reapply_m` | First distance ≥ A with throttle ≥ 20 %. |
| `full_throttle_m` | First distance ≥ A where throttle is ≥ 98 % for ≥ 0.3 s, before the segment ends. Otherwise `null` (and a note). |
| `time_to_full_throttle_s` | t(`full_throttle_m`) − t(A). |
| `gear_min`, `gear_at_apex` | Lowest gear in the segment, and the gear at A. Gear is step-interpolated, never fractional. |

`metrics` (target − reference): `braking_start_diff_m`, `entry_speed_diff_kph`,
`min_speed_diff_kph`, `exit_speed_diff_kph`, `throttle_reapply_diff_m`,
`time_to_full_throttle_diff_s`, `braking_duration_diff_s`, `gear_min_diff`.
The loss split is `entry_loss_s` = delta(A) − delta(start) and `exit_loss_s` =
delta(end) − delta(A). Straights carry `top_speed_kph`, `top_speed_m`,
`entry_speed_kph`, `exit_speed_kph`, `drs_open_fraction` and `time_s`, with matching
`*_diff*` metrics.

## Attribution rules (`causes`)

All thresholds live in `app/processing/config.py::Thresholds`.

| Cause | Rule |
|---|---|
| `early_braking_loss` / `late_braking_gain` | `braking_start_diff_m` ≤ −5 / ≥ +5 |
| `low_min_speed` / `high_min_speed` | `min_speed_diff_kph` ≤ −3 / ≥ +3 |
| `late_throttle` / `early_throttle` | `time_to_full_throttle_diff_s` ≥ +0.15 / ≤ −0.15 |
| `lower_exit_speed` / `higher_exit_speed` | `exit_speed_diff_kph` ≤ −3 / ≥ +3 |
| `straight_line_deficit` / `straight_line_advantage` | straights: `top_speed_diff_kph` ≤ −3 / ≥ +3 |
| `unclear` | No rule fired, but \|time_loss_s\| ≥ 0.02 s |

**Braking resolution.** At FastF1's ~8 Hz, the boolean brake channel is sampled about
every 10 m at 300 km/h, which is coarser than the 5 m rule. When a triggered braking
difference is under twice the local sample spacing, the finding says so in `notes`.

## Confidence

- `low`: \|time_loss_s\| < 0.02 s (below the delta noise floor), or the telemetry is sparse (< 60 samples/km).
- `medium`: the apex came from a fallback (`official` or `detected`), the official corner failed the X/Y check, **or the Phase 2 sector cross-check failed for this pair**. That last check is common on real data (see `docs/progress.md`): integrated-distance drift leaves ~40–60 ms of mid-lap uncertainty, so corner losses can't be trusted to the noise floor.
- `high`: every check above passed.

## Ranking

Corners are ranked by \|`time_loss_s`\| descending, with ties broken by segment start
distance, which makes the ranking deterministic. `rank` is set on every corner.
`largest_losses` and `largest_gains` list at most `top_n` (3) corner ids beyond the
noise floor.

## JSON Schema (generated)

<!-- schema:start -->
```json
{
  "$defs": {
    "Finding": {
      "additionalProperties": false,
      "properties": {
        "id": {
          "description": "\"<session>:<TARGET>-vs-<REFERENCE>:<code>\", e.g. \"2024-05-Q:ALO-vs-VER:T7\"",
          "title": "Id",
          "type": "string"
        },
        "kind": {
          "enum": [
            "corner",
            "straight",
            "sector",
            "lap_summary"
          ],
          "title": "Kind",
          "type": "string"
        },
        "label": {
          "title": "Label",
          "type": "string"
        },
        "segment": {
          "anyOf": [
            {
              "$ref": "#/$defs/SegmentSpan"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "time_loss_s": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "target − reference over the segment; > 0 = target lost time",
          "title": "Time Loss S"
        },
        "entry_loss_s": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "corners: loss from segment start to apex",
          "title": "Entry Loss S"
        },
        "exit_loss_s": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "corners: loss from apex to segment end",
          "title": "Exit Loss S"
        },
        "metrics": {
          "additionalProperties": {
            "anyOf": [
              {
                "type": "number"
              },
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ]
          },
          "description": "differences, target − reference",
          "title": "Metrics",
          "type": "object"
        },
        "target": {
          "additionalProperties": {
            "anyOf": [
              {
                "type": "number"
              },
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ]
          },
          "title": "Target",
          "type": "object"
        },
        "reference": {
          "additionalProperties": {
            "anyOf": [
              {
                "type": "number"
              },
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ]
          },
          "title": "Reference",
          "type": "object"
        },
        "causes": {
          "description": "triggered rules: contributing factors, not proof",
          "items": {
            "enum": [
              "early_braking_loss",
              "late_braking_gain",
              "low_min_speed",
              "high_min_speed",
              "late_throttle",
              "early_throttle",
              "lower_exit_speed",
              "higher_exit_speed",
              "straight_line_deficit",
              "straight_line_advantage",
              "unclear"
            ],
            "type": "string"
          },
          "title": "Causes",
          "type": "array"
        },
        "rank": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "corners only: 1 = largest |time_loss_s|; ties by distance",
          "title": "Rank"
        },
        "confidence": {
          "enum": [
            "high",
            "medium",
            "low"
          ],
          "title": "Confidence",
          "type": "string"
        },
        "notes": {
          "items": {
            "type": "string"
          },
          "title": "Notes",
          "type": "array"
        },
        "corner_number": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Corner Number"
        },
        "corner_type": {
          "anyOf": [
            {
              "enum": [
                "slow",
                "medium",
                "fast"
              ],
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Corner Type"
        },
        "apex_source": {
          "anyOf": [
            {
              "enum": [
                "matched",
                "official",
                "detected"
              ],
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Apex Source"
        }
      },
      "required": [
        "id",
        "kind",
        "label",
        "confidence"
      ],
      "title": "Finding",
      "type": "object"
    },
    "LapContext": {
      "additionalProperties": false,
      "properties": {
        "driver": {
          "title": "Driver",
          "type": "string"
        },
        "lap_number": {
          "title": "Lap Number",
          "type": "integer"
        },
        "lap_time_s": {
          "title": "Lap Time S",
          "type": "number"
        },
        "sector_s": {
          "items": {
            "anyOf": [
              {
                "type": "number"
              },
              {
                "type": "null"
              }
            ]
          },
          "title": "Sector S",
          "type": "array"
        },
        "compound": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Compound"
        },
        "tyre_age_laps": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Tyre Age Laps"
        },
        "fresh_tyre": {
          "anyOf": [
            {
              "type": "boolean"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Fresh Tyre"
        },
        "air_temp_c": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Air Temp C"
        },
        "track_temp_c": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Track Temp C"
        },
        "rainfall": {
          "anyOf": [
            {
              "type": "boolean"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Rainfall"
        },
        "track_status": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Track Status"
        },
        "track_status_flags": {
          "items": {
            "type": "string"
          },
          "title": "Track Status Flags",
          "type": "array"
        }
      },
      "required": [
        "driver",
        "lap_number",
        "lap_time_s",
        "sector_s"
      ],
      "title": "LapContext",
      "type": "object"
    },
    "ReportQuality": {
      "additionalProperties": false,
      "properties": {
        "sample_count_ref": {
          "title": "Sample Count Ref",
          "type": "integer"
        },
        "sample_count_target": {
          "title": "Sample Count Target",
          "type": "integer"
        },
        "length_scale_factor": {
          "title": "Length Scale Factor",
          "type": "number"
        },
        "final_delta_error_s": {
          "title": "Final Delta Error S",
          "type": "number"
        },
        "repairs": {
          "title": "Repairs",
          "type": "integer"
        },
        "dropped_glitches": {
          "title": "Dropped Glitches",
          "type": "integer"
        },
        "tiling_error_s": {
          "title": "Tiling Error S",
          "type": "number"
        },
        "sector_check_ok": {
          "anyOf": [
            {
              "type": "boolean"
            },
            {
              "type": "null"
            }
          ],
          "title": "Sector Check Ok"
        },
        "sector_check_max_discrepancy_s": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "title": "Sector Check Max Discrepancy S"
        },
        "alignment": {
          "default": "distance",
          "description": "\"xy\": the target distance was corrected by projecting its X/Y position onto the reference path (blended 50/50 with the length-normalized distance); \"distance\": length normalization only (X/Y missing or implausible)",
          "enum": [
            "xy",
            "distance"
          ],
          "title": "Alignment",
          "type": "string"
        },
        "xy_correction_rms_m": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "RMS of the X/Y distance correction applied, metres",
          "title": "Xy Correction Rms M"
        }
      },
      "required": [
        "sample_count_ref",
        "sample_count_target",
        "length_scale_factor",
        "final_delta_error_s",
        "repairs",
        "dropped_glitches",
        "tiling_error_s",
        "sector_check_ok",
        "sector_check_max_discrepancy_s"
      ],
      "title": "ReportQuality",
      "type": "object"
    },
    "SegmentSpan": {
      "additionalProperties": false,
      "properties": {
        "start_m": {
          "title": "Start M",
          "type": "number"
        },
        "end_m": {
          "title": "End M",
          "type": "number"
        },
        "apex_m": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Apex M"
        }
      },
      "required": [
        "start_m",
        "end_m"
      ],
      "title": "SegmentSpan",
      "type": "object"
    },
    "SessionContext": {
      "additionalProperties": false,
      "properties": {
        "session_key": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Session Key"
        },
        "event_name": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Event Name"
        },
        "session_name": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Session Name"
        },
        "session_code": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Session Code"
        },
        "reference": {
          "$ref": "#/$defs/LapContext"
        },
        "target": {
          "$ref": "#/$defs/LapContext"
        },
        "notes": {
          "items": {
            "type": "string"
          },
          "title": "Notes",
          "type": "array"
        }
      },
      "required": [
        "reference",
        "target"
      ],
      "title": "SessionContext",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "schema_version": {
      "default": 1,
      "title": "Schema Version",
      "type": "integer"
    },
    "comparison_id": {
      "title": "Comparison Id",
      "type": "string"
    },
    "session_context": {
      "$ref": "#/$defs/SessionContext"
    },
    "lap_summary": {
      "$ref": "#/$defs/Finding"
    },
    "corners": {
      "description": "tile the lap in distance order; losses sum to the total",
      "items": {
        "$ref": "#/$defs/Finding"
      },
      "title": "Corners",
      "type": "array"
    },
    "straights": {
      "items": {
        "$ref": "#/$defs/Finding"
      },
      "title": "Straights",
      "type": "array"
    },
    "sectors": {
      "items": {
        "$ref": "#/$defs/Finding"
      },
      "title": "Sectors",
      "type": "array"
    },
    "largest_losses": {
      "description": "corner ids, |loss| ≥ noise floor, biggest first",
      "items": {
        "type": "string"
      },
      "title": "Largest Losses",
      "type": "array"
    },
    "largest_gains": {
      "items": {
        "type": "string"
      },
      "title": "Largest Gains",
      "type": "array"
    },
    "quality": {
      "$ref": "#/$defs/ReportQuality"
    },
    "notes": {
      "items": {
        "type": "string"
      },
      "title": "Notes",
      "type": "array"
    }
  },
  "required": [
    "comparison_id",
    "session_context",
    "lap_summary",
    "corners",
    "straights",
    "sectors",
    "largest_losses",
    "largest_gains",
    "quality"
  ],
  "title": "FindingsReport",
  "type": "object"
}
```
<!-- schema:end -->
