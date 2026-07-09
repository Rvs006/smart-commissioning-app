import { describe, expect, it } from "vitest";
import type { DiscoveryResultsResponse } from "../../api/client";
import {
  bacnetBackendLabel,
  forbiddenOpenPorts,
  ipRowsFromResults,
  unexpectedOpenPorts,
  validationMetrics,
} from "./discoveryRows";

// Minimal IP discovery results shell carrying one discovered asset, so the row
// mapper can be exercised without the rest of the DiscoveryResultsResponse shape.
function ipResults(
  asset: Partial<DiscoveryResultsResponse["discovered_assets"][number]>,
): DiscoveryResultsResponse {
  return {
    run_id: "run-ip-1",
    job_type: "ip_discovery",
    status: "succeeded",
    result_summary: {},
    discovered_assets: [{ ip_address: "10.10.25.214", ...asset }],
    devices: [],
    points: [],
    topics: [],
  };
}

// Minimal terminal BACnet results shell; only result_summary.backend matters
// to bacnetBackendLabel, the rest satisfies the DiscoveryResultsResponse shape.
function bacnetResults(resultSummary: Record<string, unknown>): DiscoveryResultsResponse {
  return {
    run_id: "run-bacnet-1",
    job_type: "bacnet_discovery",
    status: "succeeded",
    result_summary: resultSummary,
    discovered_assets: [],
    devices: [],
    points: [],
    topics: [],
  };
}

describe("bacnetBackendLabel", () => {
  it("flags a simulated backend as demo data, not a real scan", () => {
    expect(bacnetBackendLabel(bacnetResults({ backend: "simulated" }))).toEqual({
      kind: "simulated",
      text: "SIMULATED — demo data, not a real BACnet scan.",
    });
  });

  it("confirms a real bacpypes3 scan", () => {
    expect(bacnetBackendLabel(bacnetResults({ backend: "bacpypes3" }))).toEqual({
      kind: "live",
      text: "Live bacpypes3 scan.",
    });
  });

  it("surfaces an unrecognised backend neutrally instead of swallowing it", () => {
    expect(bacnetBackendLabel(bacnetResults({ backend: "acme-sim" }))).toEqual({
      kind: "unknown",
      text: "Backend: acme-sim",
    });
  });

  it("returns null when no backend label is present (missing, empty, or non-string)", () => {
    expect(bacnetBackendLabel(bacnetResults({}))).toBeNull();
    expect(bacnetBackendLabel(bacnetResults({ backend: "" }))).toBeNull();
    expect(bacnetBackendLabel(bacnetResults({ backend: 3 }))).toBeNull();
  });
});

describe("ipRowsFromResults", () => {
  it("maps a real MAC address and hostname into the row cells", () => {
    const [row] = ipRowsFromResults(
      ipResults({ mac_address: "C0:A6:F3:F2:F3:2F", hostname: "plant-controller" }),
    );
    expect(row["MAC Address"]).toBe("C0:A6:F3:F2:F3:2F");
    expect(row.Hostname).toBe("plant-controller");
    expect(row["Observed IP"]).toBe("10.10.25.214");
  });

  it("degrades a missing MAC/hostname to a blank placeholder, never fabricated", () => {
    // Honesty: off-L2 hosts have no ARP entry and hosts without a PTR record have
    // no hostname; both legitimately arrive null and must render the em-dash blank.
    const [row] = ipRowsFromResults(ipResults({ mac_address: null, hostname: null }));
    expect(row["MAC Address"]).toBe("—");
    expect(row.Hostname).toBe("—");
  });
});

describe("forbiddenOpenPorts", () => {
  it("extracts the forbidden port list from a flagged IP status_detail", () => {
    // Mirrors the engine marker: "responsive: ... | FORBIDDEN PORTS OPEN: <ports>".
    expect(forbiddenOpenPorts("responsive: 80,23,443 | FORBIDDEN PORTS OPEN: 23")).toBe("23");
    expect(forbiddenOpenPorts("responsive: 23,2323 | FORBIDDEN PORTS OPEN: 23,2323")).toBe(
      "23,2323",
    );
  });

  it("returns empty string for clean hosts and missing status_detail", () => {
    expect(forbiddenOpenPorts("responsive: 80,443")).toBe("");
    expect(forbiddenOpenPorts(undefined)).toBe("");
    expect(forbiddenOpenPorts("—")).toBe("");
  });
});

describe("unexpectedOpenPorts", () => {
  it("extracts ports open that were not in the expected list", () => {
    expect(unexpectedOpenPorts("responsive: 80,8080 | UNEXPECTED PORTS OPEN: 8080")).toBe("8080");
    expect(unexpectedOpenPorts("responsive: 80,443")).toBe("");
    expect(unexpectedOpenPorts(undefined)).toBe("");
  });
});

describe("validationMetrics", () => {
  it("derives UDMI payload conformance and blocking issues from a real run", () => {
    const metrics = validationMetrics("udmi-validation", {
      expected_devices: 35,
      publishing_seen: 33,
      issue_count: 2,
    });
    expect(metrics).toEqual({
      primary: "94%",
      primaryLabel: "payload conformance",
      secondary: "2",
      secondaryLabel: "blocking issues",
    });
  });

  it("returns null for a UDMI-route run kind without expected_devices (e.g. config publish)", () => {
    // mqtt_config_publish runs under the udmi-validation route but carry no
    // expected_devices/publishing_seen/issue_count — must yield the empty state,
    // never NaN or the old hardcoded 94%.
    expect(
      validationMetrics("udmi-validation", {
        matched_point_count: 4,
        expected_point_count: 5,
        message_count: 12,
      }),
    ).toBeNull();
  });

  it("derives data-validation checks passed and issues found from total/ok", () => {
    const metrics = validationMetrics("data-validation", {
      total: 100,
      ok: 97,
      issue_count: 3,
    });
    expect(metrics).toEqual({
      primary: "97",
      primaryLabel: "checks passed",
      secondary: "3",
      secondaryLabel: "issues found",
    });
  });

  it("falls back to total-ok when data-validation issue_count is absent", () => {
    const metrics = validationMetrics("data-validation", { total: 10, ok: 8 });
    expect(metrics?.secondary).toBe("2");
  });

  it("returns null for a data-validation run kind without ok/total", () => {
    expect(validationMetrics("data-validation", { matched_point_count: 1 })).toBeNull();
  });

  it("returns null for an undefined or empty summary, and for non-validation routes", () => {
    expect(validationMetrics("udmi-validation", undefined)).toBeNull();
    expect(validationMetrics("data-validation", {})).toBeNull();
    expect(validationMetrics("ip-scanner", { ok: 1, total: 1 })).toBeNull();
  });

  it("guards against non-numeric summary values (no NaN)", () => {
    expect(
      validationMetrics("udmi-validation", {
        expected_devices: "35" as unknown as number,
      }),
    ).toBeNull();
  });
});
