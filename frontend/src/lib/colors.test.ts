import { describe, expect, it } from "vitest";

import { contrastRatio, ensureContrast, lossStyle, luminance, NOISE_FLOOR_S, SCALE, sameTeamColor } from "./colors";

const DARK_BG = "#141312";
const LIGHT_BG = "#ffffff";

describe("lossStyle: diverging scale with a ±0.02 s neutral band", () => {
  it.each([0, 0.019, -0.019, 0.01999, null, undefined, Number.NaN])("%s is neutral", (loss) => {
    const s = lossStyle(loss);
    expect(s.kind).toBe("neutral");
    expect(s.color).toBe(SCALE.neutral);
    expect(s.glyph).toBe("●");
  });

  it("the band edge (exactly 0.02 s) is no longer neutral", () => {
    expect(lossStyle(NOISE_FLOOR_S).kind).toBe("loss");
    expect(lossStyle(-NOISE_FLOOR_S).kind).toBe("gain");
  });

  it("positive = target lost time = red ▲, negative = gained = green ▼", () => {
    expect(lossStyle(0.171)).toMatchObject({ kind: "loss", glyph: "▲", label: "lost 0.171 s" });
    expect(lossStyle(-0.094)).toMatchObject({ kind: "gain", glyph: "▼", label: "gained 0.094 s" });
  });

  it("intensity grows with magnitude and saturates", () => {
    const small = lossStyle(0.03), mid = lossStyle(0.08), big = lossStyle(0.5);
    expect(small.intensity).toBeLessThan(mid.intensity);
    expect(big.intensity).toBe(1);
    expect(lossStyle(0.5).color).toBe(SCALE.loss);
    expect(lossStyle(-0.5).color).toBe(SCALE.gain);
  });
});

describe("ensureContrast (luminance adjustment for team colours)", () => {
  it("lightens a dark team colour on the dark theme until it reaches 3:1", () => {
    const williams = "#041e42"; // navy: nearly invisible on #141312
    expect(contrastRatio(williams, DARK_BG)).toBeLessThan(3);
    const adjusted = ensureContrast(williams, DARK_BG);
    expect(contrastRatio(adjusted, DARK_BG)).toBeGreaterThanOrEqual(3);
    expect(luminance(adjusted)).toBeGreaterThan(luminance(williams));
  });

  it("darkens a pale colour on the light theme", () => {
    const pale = "#ffd800";
    const adjusted = ensureContrast(pale, LIGHT_BG);
    expect(contrastRatio(adjusted, LIGHT_BG)).toBeGreaterThanOrEqual(3);
    expect(luminance(adjusted)).toBeLessThan(luminance(pale));
  });

  it("leaves colours that already pass unchanged (normalized)", () => {
    expect(ensureContrast("#FF8000", DARK_BG)).toBe("#ff8000");
    expect(ensureContrast("3671C6", DARK_BG)).toBe("#3671c6");
  });

  it("falls back for missing or malformed colours", () => {
    expect(contrastRatio(ensureContrast(null, DARK_BG), DARK_BG)).toBeGreaterThanOrEqual(3);
    expect(contrastRatio(ensureContrast("not-a-colour", LIGHT_BG), LIGHT_BG)).toBeGreaterThanOrEqual(3);
  });

  it("WCAG reference values", () => {
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrastRatio("#777777", "#ffffff")).toBeCloseTo(4.48, 2);
  });

  it("detects team-mates", () => {
    expect(sameTeamColor("#3671C6", "3671c6")).toBe(true);
    expect(sameTeamColor("#3671C6", "#ff8000")).toBe(false);
    expect(sameTeamColor(null, "#ff8000")).toBe(false);
  });
});
