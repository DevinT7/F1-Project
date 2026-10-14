import { describe, expect, it } from "vitest";

import comparison from "@/fixtures/api/comparison_VER_ALO.json";

import { buildReplay, distanceAt, gapAt, jumpToCorner, timeAt, trendAt } from "./replay";

// a 1000 m toy lap: the reference at a constant 50 m/s (20 s); the target 10% slower on the
// first half (11 s), then as fast (10 s): it loses 1 s, all of it in the first 500 m
const D = Array.from({ length: 101 }, (_, i) => i * 10);
const REF = D.map((d) => d / 50);
const TGT = D.map((d) => (d <= 500 ? (d / 50) * 1.1 : 11 + (d - 500) / 50));
const r = buildReplay(D, REF, TGT)!;

describe("lap replay maths", () => {
  it("starts both drivers on the line and ends when the slower lap ends", () => {
    expect(distanceAt(r, "reference", 0)).toBe(0);
    expect(distanceAt(r, "target", 0)).toBe(0);
    expect(r.lapTime).toEqual({ reference: 20, target: 21 });
    expect(r.duration).toBe(21);
  });

  it("inverts time(distance): position at time t, time at distance d", () => {
    expect(distanceAt(r, "reference", 10)).toBeCloseTo(500, 6);
    expect(distanceAt(r, "target", 11)).toBeCloseTo(500, 6);
    expect(timeAt(r, "target", 500)).toBeCloseTo(11, 6);
    expect(distanceAt(r, "reference", 25)).toBe(1000);       // finished: stays on the line
  });

  it("the gap grows where the target loses time and holds where it is as fast", () => {
    const at5 = gapAt(r, 5), at11 = gapAt(r, 11), at18 = gapAt(r, 18);
    expect(at5.leader).toBe("reference");
    expect(at5.seconds).toBeCloseTo(0.4545, 3);              // target at 227.3 m, reference was there at 4.545 s
    expect(at11.seconds).toBeCloseTo(1, 6);
    expect(at18.seconds).toBeCloseTo(1, 6);
    expect(at18.metres).toBeCloseTo(50, 6);                  // 1 s at 50 m/s
    expect(trendAt(r, 5)).toBe("pulling_away");
    expect(trendAt(r, 15)).toBe("steady");
    expect(gapAt(r, 30).seconds).toBeCloseTo(1, 6);          // after the line: the lap-time gap
  });

  it("a corner jump puts the faster driver at the corner and the slower where it was then", () => {
    const j = jumpToCorner(r, 800);
    expect(j.faster).toBe("reference");
    expect(j.t).toBeCloseTo(16, 6);
    expect(j.slowerDistance).toBeCloseTo(750, 6);            // target is 1 s (50 m) behind
    expect(j.gapSeconds).toBeCloseTo(1, 6);
    expect(j.gapMetres).toBeCloseTo(50, 6);
  });

  it("the faster driver can be either one (whoever reaches the corner first)", () => {
    const flipped = buildReplay(D, TGT, REF)!;
    expect(jumpToCorner(flipped, 800).faster).toBe("target");
  });

  it("on the real comparison the final gap equals the lap-time difference", () => {
    const real = buildReplay(comparison.distance_m, comparison.channels.reference.time_s, comparison.channels.target.time_s)!;
    const gap = gapAt(real, real.duration + 1);
    expect(gap.seconds).toBeCloseTo(Math.abs(comparison.delta_s.at(-1)!), 2);
    // ALO (target) is 0.488 s slower: VER leads at the line
    expect(gapAt(real, real.lapTime.reference).leader).toBe("reference");
  });
});
