import { describe, expect, it } from "vitest";

import {
  describeGap, formatDelta, formatDiff, formatDistance, formatGear, formatLapTime, formatPercent,
  formatSeconds, formatSpeed, MISSING,
} from "./format";

describe("formatDelta locks the sign convention (positive = target slower)", () => {
  it.each([
    [0.213, "+0.213 s (slower)"],
    [-0.1, "−0.100 s (faster)"],
    [0.488, "+0.488 s (slower)"],
    [0, "±0.000 s (level)"],
    [0.0004, "±0.000 s (level)"],   // rounds to zero: never "+0.000 (slower)"
    [-0.0004, "±0.000 s (level)"],
    [1.23456, "+1.235 s (slower)"],
  ])("%s → %s", (input, expected) => expect(formatDelta(input)).toBe(expected));

  it("uses a typographic minus, not a hyphen", () => expect(formatDelta(-0.5)).toContain("−"));
  it("can omit the word", () => expect(formatDelta(0.2, { withWord: false })).toBe("+0.200 s"));
  it.each([null, undefined, Number.NaN, Number.POSITIVE_INFINITY])("missing %s → em dash", (v) =>
    expect(formatDelta(v)).toBe(MISSING));
});

describe("other formatters", () => {
  it("lap times", () => {
    expect(formatLapTime(93.66)).toBe("1:33.660");
    expect(formatLapTime(59.9996)).toBe("1:00.000");
    expect(formatLapTime(5.2)).toBe("0:05.200");
    expect(formatLapTime(null)).toBe(MISSING);
  });

  it("units", () => {
    expect(formatSpeed(245.01)).toBe("245.0 km/h");
    expect(formatDistance(5052.4)).toBe("5,052 m");
    expect(formatSeconds(0.375)).toBe("0.375 s");
    expect(formatPercent(98.2)).toBe("98%");
    expect(formatGear(4)).toBe("4");
    expect(formatGear(0)).toBe(MISSING);
  });

  it("signed diffs", () => {
    expect(formatDiff(-9.563, "km/h")).toBe("−9.6 km/h");
    expect(formatDiff(18, "m", 0)).toBe("+18 m");
    expect(formatDiff(0.02, "km/h")).toBe("±0.0 km/h");
    expect(formatDiff(null, "m")).toBe(MISSING);
  });

  it("explains the gap in words", () => {
    expect(describeGap(0.488, "ALO", "VER")).toMatch(/^ALO was 0\.488 s slower than VER/);
    expect(describeGap(-0.2, "ALO", "VER")).toMatch(/^ALO was 0\.200 s faster than VER/);
    expect(describeGap(0, "A", "B")).toMatch(/identical/);
  });
});
