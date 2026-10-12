import { describe, expect, it } from "vitest";

import comparison from "@/fixtures/api/comparison_VER_ALO.json";
import corners from "@/fixtures/api/corners.json";

import { buildTrack, nearestDistance, pointAt, rotate, segmentPath, type TrackGeometry } from "./track";

const square: TrackGeometry = { distance: [0, 10, 20, 30, 40], px: [0, 10, 10, 0, 0], py: [0, 0, 10, 10, 0], width: 10, height: 10 };

describe("rotate (FastF1 convention: counter-clockwise degrees)", () => {
  it("90° maps +x to +y", () => {
    const [x, y] = rotate(1, 0, 90);
    expect(x).toBeCloseTo(0, 9);
    expect(y).toBeCloseTo(1, 9);
  });
});

describe("segmentPath", () => {
  it("interpolates endpoints and includes interior grid points", () => {
    expect(segmentPath(square, 5, 25)).toBe("M5 0 L10 0 L10 10 L5 10");
  });

  it("is empty for empty or inverted spans", () => {
    expect(segmentPath(square, 10, 10)).toBe("");
    expect(segmentPath(square, 20, 10)).toBe("");
  });

  it("adjacent segments share their boundary point", () => {
    const a = segmentPath(square, 0, 15).split(" L");
    const b = segmentPath(square, 15, 40);
    expect(b.startsWith(`M${a[a.length - 1]}`)).toBe(true);
  });
});

describe("on the real Shanghai lap", () => {
  const ref = comparison.channels.reference;
  const track = buildTrack(comparison.distance_m, ref.x, ref.y, corners.circuit_rotation_deg)!;

  it("fits inside the viewBox with padding", () => {
    expect(track).not.toBeNull();
    expect(Math.min(...track.px)).toBeGreaterThanOrEqual(17.9);
    expect(Math.max(...track.px)).toBeLessThanOrEqual(382.1);
    expect(Math.min(...track.py)).toBeGreaterThanOrEqual(17.9);
    expect(Math.max(...track.py)).toBeLessThanOrEqual(282.1);
  });

  it("the Phase 3 corner segments tile the lap into one continuous line", () => {
    const segs = comparison.findings.corners.map((f) => segmentPath(track, f.segment!.start_m, f.segment!.end_m));
    expect(segs.every((p) => p.startsWith("M"))).toBe(true);
    for (let i = 1; i < segs.length; i++) {
      const prevEnd = segs[i - 1]!.split(" ").slice(-2).join(" ").replace(/^L/, "");
      expect(segs[i]!.slice(1).startsWith(prevEnd)).toBe(true);
    }
  });

  it("pointAt and nearestDistance invert each other", () => {
    for (const d of [0, 1000, 2504, 5000]) {
      const [x, y] = pointAt(track, d);
      expect(Math.abs(nearestDistance(track, x, y) - d)).toBeLessThanOrEqual(4); // 4 m demo resolution
    }
  });
});
