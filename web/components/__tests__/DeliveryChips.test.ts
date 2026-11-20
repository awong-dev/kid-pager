import { describe, expect, it } from "vitest";

import { chipLabel } from "@/components/DeliveryChips";

describe("chipLabel fed", () => {
  it("names the server while waiting and after sending", () => {
    expect(chipLabel("fed", "queued", { server: "srv-b" })).toBe("waiting for srv-b");
    expect(chipLabel("fed", "sent", { server: "srv-b" })).toBe("sent to srv-b");
    expect(chipLabel("fed", "read", { server: "srv-b" })).toBe("sent to srv-b");
  });
  it("falls back to 'other family'", () => {
    expect(chipLabel("fed", "sent")).toBe("sent to other family");
  });
  it("maps failure reasons", () => {
    expect(chipLabel("fed", "failed", { error: "unpeered" })).toBe("failed: no longer linked");
    expect(chipLabel("fed", "failed", { error: "peer_offline" })).toBe("failed: family offline");
    expect(chipLabel("fed", "failed")).toBe("failed");
    expect(chipLabel("fed", "failed", { error: "weird" })).toBe("failed: weird");
    expect(chipLabel("bridge", "failed", { error: "bridge_offline" })).toBe("Google Chat failed: bridge phone offline");
  });
});
