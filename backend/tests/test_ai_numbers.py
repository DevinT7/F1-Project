"""Phase 7A — display_forms and parse_numbers (the parser table the validator relies on)."""
from __future__ import annotations

import math

import pytest

from app.ai.numbers import display_forms, normalize_number_word, parse_numbers

# (text, expected tokens as (value, unit)) — value None marks a REJECTED number word
PARSER_TABLE: list[tuple[str, list[tuple[float | None, str | None]]]] = [
    # the plan's examples
    ("0.213 s", [(0.213, "s")]),
    ("−18 m", [(-18.0, "m")]),
    ("+7.1 km/h", [(7.1, "km/h")]),
    ("98%", [(98.0, "%")]),
    ("Turn 7", []),
    ("lap 24", [(24.0, "lap")]),
    ("T7", []),
    ("1,234.5 m", [(1234.5, "m")]),
    ("about 0.2 seconds", [(0.2, "s")]),
    # units, attached or spaced
    ("0.21s", [(0.21, "s")]),
    ("lost 0.213s in T7", [(0.213, "s")]),
    ("−7 km/h", [(-7.0, "km/h")]),
    ("7 kph slower", [(7.0, "km/h")]),
    ("9 m later", [(9.0, "m")]),
    ("12 metres", [(12.0, "m")]),
    ("12 meters", [(12.0, "m")]),
    ("0.034 s/lap", [(0.034, "s/lap")]),
    ("0.09 s per lap", [(0.09, "s/lap")]),
    ("32.1 °C", [(32.1, "°C")]),
    ("46.4 m/s²", [(46.4, "m/s²")]),
    ("17 laps", [(17.0, "laps")]),
    ("98 percent", [(98.0, "%")]),
    # signs: ASCII hyphen, Unicode minus, plus, and ranges with an en dash or hyphen
    ("-0.087 s", [(-0.087, "s")]),
    ("delta of +0.488 s", [(0.488, "s")]),
    ("95% CI 0.029–0.039", [(95.0, "%"), (0.029, None), (0.039, None)]),
    ("between 0.05-0.13 s", [(0.05, None), (0.13, "s")]),
    ("laps 41–48", [(41.0, "lap"), (48.0, None)]),
    # labels are not numbers
    ("Turn 16 and turn 6", []),
    ("Sector 2", []),
    ("S2 and S3", []),
    ("STR1", []),
    ("Straight 3 (0-451 m)", [(0.0, None), (451.0, "m")]),
    ("stint 2", []),
    ("in gear 3", []),
    ("car #44", []),
    # lap times, thousands, decimals without a leading zero
    ("best lap 1:33.660", [(93.66, "s")]),
    ("1,234 m", [(1234.0, "m")]),
    (".5 s", [(0.5, "s")]),
    # ordinals and number words
    ("3rd largest", [(3.0, None)]),
    ("the second sector", [(2.0, None)]),
    ("first, second and third", [(1.0, None), (2.0, None), (None, None)]),
    ("lost half a second", [(None, None), (None, "s")]),
    ("a second slower", [(None, "s")]),
    ("seven tenths", [(None, None), (None, None)]),
    ("two corners", [(2.0, None)]),
    ("one lap", [(1.0, None)]),
    # mixed sentences
    ("T7 cost 0.213 s: braking 18 m earlier and 7.1 km/h less at the apex.",
     [(0.213, "s"), (18.0, "m"), (7.1, "km/h")]),
    ("No numbers here.", []),
]


@pytest.mark.parametrize("text, expected", PARSER_TABLE, ids=[t for t, _ in PARSER_TABLE])
def test_parser_table(text, expected):
    got = parse_numbers(text)
    shape = [(None if (not t.allowed) else round(t.value, 6), t.unit) for t in got]
    assert shape == [(None if v is None else round(v, 6), u) for v, u in expected], [(t.raw, t.kind) for t in got]


def test_parser_table_is_large_enough():
    assert len(PARSER_TABLE) >= 40


def test_parser_reports_positions_signs_and_kinds():
    [t] = parse_numbers("gap −0.488 s")
    assert (t.sign, t.magnitude_text, t.kind, t.raw) == (-1, "0.488", "number", "−0.488 s")
    [lt] = parse_numbers("1:33.660")
    assert lt.kind == "laptime" and math.isclose(lt.value, 93.66)


def test_ignore_masks_identifiers_like_session_keys():
    assert [t.value for t in parse_numbers("2024-05-Q, T7 0.2 s")] != [0.2]
    assert [t.value for t in parse_numbers("2024-05-Q, T7 0.2 s", ignore=("2024-05-Q",))] == [0.2]


def test_number_words():
    assert normalize_number_word("Two") == 2 and normalize_number_word("second") == 2
    assert normalize_number_word("seven") is None and normalize_number_word("tenth") is None


# ---------------------------------------------------------------- display forms
@pytest.mark.parametrize("value, unit, expected", [
    (0.213, "s", ["0.213", "0.21", "0.2"]),
    (-0.087, "s", ["0.087", "0.09", "0.1"]),
    (0.004, "s", ["0.004"]),                       # 2 dp and 1 dp would read as zero
    (0.049, "s", ["0.049", "0.05"]),               # "0.0" dropped
    (1.0, "s", ["1.000", "1.00", "1.0"]),
    (0.0338, "s/lap", ["0.034", "0.03"]),
    (-17.1056, "km/h", ["17.1", "17"]),
    (0.3, "km/h", ["0.3"]),
    (5.0, "m", ["5.0", "5"]),
    (1567.0, "m", ["1567.0", "1567"]),
    (32.1, "°C", ["32.1", "32"]),
    (24, "lap", ["24"]),
    (3, "rank", ["3"]),
    (0.9972604, "ratio", ["0.9973", "0.997", "1.00"]),
    (1.0, "fraction", ["1.00", "1.0"]),
    (0.0, "s", ["0.000", "0.00", "0.0"]),
])
def test_display_forms(value, unit, expected):
    assert display_forms(value, unit) == expected


@pytest.mark.parametrize("value, unit", [(0.213, "s"), (-17.1056, "km/h"), (0.0338, "s/lap"), (1567.25, "m"),
                                         (93.66, "s"), (0.004, "s"), (0.9972604, "ratio")])
def test_display_forms_round_trip(value, unit):
    for form in display_forms(value, unit):
        decimals = len(form.split(".")[1]) if "." in form else 0
        assert abs(float(form) - abs(value)) <= 0.5 * 10 ** -decimals + 1e-9


def test_display_forms_never_render_null_or_nan():
    assert display_forms(None, "s") == [] and display_forms(float("nan"), "s") == []
    with pytest.raises(ValueError):
        display_forms(1.0, "furlongs")


@pytest.mark.parametrize("text, ordinal", [("the data second", True), ("a second", False), ("one second", False),
                                           ("per second", False), ("0.5 second", False), ("second place", True)])
def test_second_is_ordinal_only_when_not_a_time(text, ordinal):
    [t] = [t for t in parse_numbers(text) if t.raw.lower() == "second"]
    assert t.allowed is ordinal


def test_ignore_masks_whole_occurrences_only():
    """Masking the label 'Turn 2' must not consume the '2' of 'Turn 23' (found by the 7B corpus)."""
    assert [t.value for t in parse_numbers("Turn 23 cost 0.311 s", ignore=("Turn 2",))] == [0.311]
    assert parse_numbers("Straight 1 (431-2231 m) cost 0.1 s", ignore=("Straight 1 (431-2231 m)",))[0].value == 0.1
