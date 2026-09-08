import { describe, expect, it } from "vitest";

import type { ModuleRunAction } from "./moduleData";
import { buildDiscoveryParameters } from "./ModulePage";

// GAP-B1 / F1: the BACnet device-instance range is pair-or-neither. The sidecar
// sends a bounded Who-Is only when BOTH low and high arrive; a lone bound falls
// through to a global Who-Is. So buildDiscoveryParameters must emit low/high only
// as a validated pair (both present, integers, 0 <= low <= high <= 4194303) and
// otherwise omit BOTH keys, or a half-filled/invalid form silently scans every
// instance while the UI shows the operator's bound as accepted. These tests lock
// the boundary at the builder, the last place before the wire (the engine-side
// _scan_query test only proves the wire is honoured, not that the frontend omits).
describe("buildDiscoveryParameters — BACnet sidecar instance range (GAP-B1/F1)", () => {
  // The bacnet-scanner run action exactly as moduleData wires it.
  const bacnetSidecarAction: Extract<ModuleRunAction, { kind: "discovery" }> = {
    id: "bacnet-scanner.run",
    kind: "discovery",
    label: "Run BACnet Discovery",
    helper: "",
    runKind: "bacnet_sidecar",
    jobType: "bacnet_scanner",
  };
  const baseOptions = { authorized: true, dryRun: false, scanPorts: [] };

  it("omits low and high when both range fields are blank (global Who-Is)", () => {
    // The normal scan: the UI seeds both with useState("") and tells the operator
    // to leave both blank. Neither key may reach the wire.
    const params = buildDiscoveryParameters(bacnetSidecarAction, {
      ...baseOptions,
      bacnetInstanceLow: "",
      bacnetInstanceHigh: "  ", // whitespace trims to blank too
    });
    expect(params).not.toHaveProperty("low");
    expect(params).not.toHaveProperty("high");
    expect(params.authorized).toBe(true); // the object was actually built
  });

  it("passes filled low and high through as numbers", () => {
    const params = buildDiscoveryParameters(bacnetSidecarAction, {
      ...baseOptions,
      bacnetInstanceLow: "100",
      bacnetInstanceHigh: "200",
    });
    expect(params.low).toBe(100);
    expect(params.high).toBe(200);
  });

  it("omits BOTH keys when only low is filled (pair-or-neither)", () => {
    const params = buildDiscoveryParameters(bacnetSidecarAction, {
      ...baseOptions,
      bacnetInstanceLow: "100",
      bacnetInstanceHigh: "",
    });
    expect(params).not.toHaveProperty("low");
    expect(params).not.toHaveProperty("high");
  });

  it("omits BOTH keys when only high is filled (pair-or-neither)", () => {
    const params = buildDiscoveryParameters(bacnetSidecarAction, {
      ...baseOptions,
      bacnetInstanceLow: "",
      bacnetInstanceHigh: "200",
    });
    expect(params).not.toHaveProperty("low");
    expect(params).not.toHaveProperty("high");
  });

  it("omits BOTH keys for an inverted range (low > high)", () => {
    const params = buildDiscoveryParameters(bacnetSidecarAction, {
      ...baseOptions,
      bacnetInstanceLow: "200",
      bacnetInstanceHigh: "100",
    });
    expect(params).not.toHaveProperty("low");
    expect(params).not.toHaveProperty("high");
  });

  it("omits BOTH keys when a bound is out of range (0..4194303)", () => {
    const params = buildDiscoveryParameters(bacnetSidecarAction, {
      ...baseOptions,
      bacnetInstanceLow: "0",
      bacnetInstanceHigh: "4194304", // one past 2^22-1
    });
    expect(params).not.toHaveProperty("low");
    expect(params).not.toHaveProperty("high");
  });

  it("accepts the inclusive boundary pair 0..4194303", () => {
    const params = buildDiscoveryParameters(bacnetSidecarAction, {
      ...baseOptions,
      bacnetInstanceLow: "0",
      bacnetInstanceHigh: "4194303",
    });
    expect(params.low).toBe(0);
    expect(params.high).toBe(4194303);
  });

  it("omits BOTH keys for a non-integer bound", () => {
    const params = buildDiscoveryParameters(bacnetSidecarAction, {
      ...baseOptions,
      bacnetInstanceLow: "10.5",
      bacnetInstanceHigh: "200",
    });
    expect(params).not.toHaveProperty("low");
    expect(params).not.toHaveProperty("high");
  });
});
