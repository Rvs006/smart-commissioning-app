import { describe, expect, it } from "vitest";

import { hostRangeFromCidr } from "./ipRange";

describe("hostRangeFromCidr", () => {
  it("returns the .1-.254 host range for a /24", () => {
    expect(hostRangeFromCidr("10.0.10.5/24")).toEqual({ start: "10.0.10.1", end: "10.0.10.254" });
  });

  it("computes the range for a non-/24 prefix", () => {
    expect(hostRangeFromCidr("172.28.0.1/20")).toEqual({ start: "172.28.0.1", end: "172.28.15.254" });
  });

  it("uses the single address for a /32", () => {
    expect(hostRangeFromCidr("192.168.1.10/32")).toEqual({
      start: "192.168.1.10",
      end: "192.168.1.10",
    });
  });

  it("returns null for Auto, blank, or a bare address", () => {
    expect(hostRangeFromCidr("Auto")).toBeNull();
    expect(hostRangeFromCidr("")).toBeNull();
    expect(hostRangeFromCidr("10.0.10.5")).toBeNull();
    expect(hostRangeFromCidr("10.0.10.5/99")).toBeNull();
  });
});
