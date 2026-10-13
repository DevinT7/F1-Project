import { describe, expect, it } from "vitest";

import {
  emptyState, isComparable, normalizeSessionKey, parseLapRef, parseSessionKey, parseUrlState,
  serializeUrlState, sessionKey, type UrlState,
} from "./url-state";

describe("session keys", () => {
  it.each([
    ["2024-05-Q", "2024-05-Q"],
    ["2024_05_q", "2024-05-Q"],
    [" 2023-22-r ", "2023-22-R"],
    ["2024-05-FP1", "2024-05-FP1"],
  ])("normalizes %s", (raw, expected) => expect(normalizeSessionKey(raw)).toBe(expected));

  it.each(["", "2024-5-Q", "24-05-Q", "2024-05-XX", "2024-05", null])("rejects %s", (raw) =>
    expect(normalizeSessionKey(raw)).toBeNull());

  it("builds and parses parts", () => {
    expect(sessionKey({ year: 2024, round: 5, code: "q" })).toBe("2024-05-Q");
    expect(parseSessionKey("2024_05_Q")).toEqual({ year: 2024, round: 5, code: "Q" });
    expect(parseSessionKey("nope")).toBeNull();
  });
});

describe("lap refs", () => {
  it.each([
    ["NOR:fastest", { driver: "NOR", lap: "fastest" }],
    ["ver:15", { driver: "VER", lap: 15 }],
    ["VER", { driver: "VER", lap: "fastest" }],
    ["44:FASTEST", { driver: "44", lap: "fastest" }],
  ])("parses %s", (raw, expected) => expect(parseLapRef(raw)).toEqual(expected));

  it.each(["", ":15", "VER:0", "VER:abc", "VER:15:2", "TOOLONG:1", "VER:1234"])("rejects %s", (raw) =>
    expect(parseLapRef(raw)).toBeNull());
});

describe("URL state", () => {
  const full: UrlState = {
    session: "2024-05-Q", ref: { driver: "NOR", lap: "fastest" }, tgt: { driver: "VER", lap: 15 },
    resolution: 2, demo: false, tyre: null, asof: null,
  };

  it("carries the tyre panel's driver and as-of lap, and drops as-of without a driver", () => {
    const tyre = { ...full, session: "2024-99-R", tyre: "AAA", asof: 35 };
    expect(serializeUrlState(tyre)).toContain("tyre=AAA&asof=35");
    expect(parseUrlState(serializeUrlState(tyre))).toEqual(tyre);
    expect(serializeUrlState({ ...full, asof: 35 })).not.toContain("asof");
    expect(parseUrlState("?s=2024-99-R&tyre=aaa&asof=0")).toMatchObject({ tyre: "AAA", asof: null });
  });

  it("serializes to the documented shareable form", () => {
    expect(serializeUrlState(full)).toBe("s=2024-05-Q&ref=NOR:fastest&tgt=VER:15");
  });

  it("round-trips", () => {
    for (const state of [full, { ...full, resolution: 4, demo: true }, emptyState, { ...emptyState, session: "2024-05-R" }]) {
      expect(parseUrlState(serializeUrlState(state))).toEqual(state);
    }
  });

  it("parses the plan's example URL alone", () => {
    const state = parseUrlState("?s=2024-05-Q&ref=NOR:fastest&tgt=VER:fastest");
    expect(state).toEqual({ ...emptyState, session: "2024-05-Q", ref: { driver: "NOR", lap: "fastest" },
      tgt: { driver: "VER", lap: "fastest" } });
    expect(isComparable(state)).toBe(true);
  });

  it("drops invalid values instead of throwing", () => {
    const state = parseUrlState("s=garbage&ref=:&tgt=VER:0&res=99&demo=yes");
    expect(state).toEqual(emptyState);
    expect(isComparable(state)).toBe(false);
  });

  it("accepts encoded colons and underscores", () => {
    expect(parseUrlState("s=2024_05_Q&ref=NOR%3Afastest").ref).toEqual({ driver: "NOR", lap: "fastest" });
  });
});
