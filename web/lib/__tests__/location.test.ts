import { describe, expect, it } from "vitest";

import { buildLocationTimeline, locationsDaysOf } from "../location";
import type { LocationFixRow } from "../location";

describe("locationsDaysOf", () => {
  it("defaults to 7 and clamps to 1..30", () => {
    expect(locationsDaysOf(undefined)).toBe(7);
    expect(locationsDaysOf({ locationsDays: 14 })).toBe(14);
    expect(locationsDaysOf({ locationsDays: 90 })).toBe(30);
  });
  it("folds a legacy {n, unit} field in", () => {
    expect(locationsDaysOf({ locations: { n: 2, unit: "weeks" } })).toBe(14);
  });
});

describe("buildLocationTimeline window", () => {
  const nowMs = 100 * 86_400_000;
  const fix = (id: string, daysAgo: number): LocationFixRow =>
    ({ id, ts: (nowMs - daysAgo * 86_400_000) / 1000, lat: 1, lon: 2, accM: 10, src: "gnss", cached: false }) as unknown as LocationFixRow;
  it("uses the given window", () => {
    const fixes = [fix("a", 3), fix("b", 10)];
    expect(buildLocationTimeline(fixes, null, nowMs).map((e) => e.id)).toEqual(["a"]);
    expect(buildLocationTimeline(fixes, null, nowMs, 14).map((e) => e.id)).toEqual(["a", "b"]);
  });
});
