import { describe, expect, it } from "vitest";

import type { ModuleRunAction } from "./moduleData";
import { buildDiscoveryParameters } from "./ModulePage";

// GAP-B1: the BACnet sidecar blank-field guard. Number("") === 0, so coercing a
// blank instance-range field before the emptiness check leaked low:0 / high:0
// onto the wire and pinned the sidecar's Who-Is to instance range [0,0] — the
// normal scan (both fields blank) then discovered almost nothing. These tests
// lock the boundary in buildDiscoveryParameters, where the 0/0 originated (the
// engine-side _scan_query test only proves the wire is honoured, not that the
// frontend omits the keys).
describe("buildDiscoveryParameters — BACnet sidecar instance range (GAP-B1)", () => {
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

  it("does not synthesize an inverted range when only low is filled", () => {
    const params = buildDiscoveryParameters(bacnetSidecarAction, {
      ...baseOptions,
      bacnetInstanceLow: "100",
      bacnetInstanceHigh: "",
    });
    expect(params.low).toBe(100);
    expect(params).not.toHaveProperty("high");
  });
});
