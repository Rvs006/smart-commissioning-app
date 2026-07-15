import { describe, expect, it } from "vitest";
import type { DiscoveryResultsResponse } from "../../api/client";
import {
  bacnetBackendLabel,
  discoveryEmptyStateFor,
  expectedPortsOk,
  forbiddenOpenPorts,
  ipRowsFromResults,
  missingExpectedPorts,
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
    // No transport stamp (a run predating v0.1.12): the label says the scan was
    // live and stops there.
    expect(bacnetBackendLabel(bacnetResults({ backend: "bacpypes3" }))).toEqual({
      kind: "live",
      text: "Live bacpypes3 scan.",
    });
  });

  it("names the BBMD a foreign-device run registered with", () => {
    // The whole point of v0.1.12's pill: an engineer who set Foreign Device =
    // Enabled can confirm from the result that the run really went through the
    // BBMD, rather than discovering weeks later that the setting was ignored.
    expect(
      bacnetBackendLabel(
        bacnetResults({ backend: "bacpypes3", bacnet_mode: "foreign_device", bbmd_address: "10.10.30.4" }),
      ),
    ).toEqual({
      kind: "live",
      text: "Live bacpypes3 scan — foreign-device registration via BBMD 10.10.30.4.",
    });
  });

  it("says local broadcast only when the run registered with no BBMD", () => {
    // Consistent with the empty-state copy below: a local broadcast does not
    // reach devices behind a BBMD or on another subnet. Seeing this on a run
    // configured for Foreign Device is the visible symptom of the v0.1.12 bug.
    expect(bacnetBackendLabel(bacnetResults({ backend: "bacpypes3", bacnet_mode: "broadcast" }))).toEqual({
      kind: "live",
      text: "Live bacpypes3 scan — local broadcast only (no foreign-device registration configured).",
    });
  });

  it("does not claim broadcast for a live run that recorded no transport", () => {
    // Absent mode is unknown, not broadcast. Inventing "local broadcast only"
    // for a run that never stamped its transport would be a fabricated
    // observation — and would falsely accuse a correctly-configured old run.
    const label = bacnetBackendLabel(bacnetResults({ backend: "bacpypes3", bbmd_address: "10.10.30.4" }));
    expect(label?.text).toBe("Live bacpypes3 scan.");
    expect(label?.text).not.toMatch(/broadcast|foreign/i);
  });

  it("reports a foreign-device run with no BBMD recorded instead of inventing one", () => {
    // The engine fails such a run outright, so this is defensive: say what is
    // missing rather than silently downgrading the claim to broadcast.
    const label = bacnetBackendLabel(bacnetResults({ backend: "bacpypes3", bacnet_mode: "foreign_device" }));
    expect(label?.text).toBe("Live bacpypes3 scan — foreign-device registration (BBMD address not recorded).");
    expect(label?.text).not.toMatch(/local broadcast/i);
  });

  it("surfaces an unrecognised transport mode rather than guessing one", () => {
    const label = bacnetBackendLabel(bacnetResults({ backend: "bacpypes3", bacnet_mode: "carrier-pigeon" }));
    expect(label?.text).toBe("Live bacpypes3 scan — transport: carrier-pigeon.");
  });

  it("ignores blank or non-string transport stamps", () => {
    expect(bacnetBackendLabel(bacnetResults({ backend: "bacpypes3", bacnet_mode: "   " }))?.text).toBe(
      "Live bacpypes3 scan.",
    );
    expect(bacnetBackendLabel(bacnetResults({ backend: "bacpypes3", bacnet_mode: 7 }))?.text).toBe(
      "Live bacpypes3 scan.",
    );
    // A foreign-device run whose bbmd_address is blank must not render an empty gap.
    expect(
      bacnetBackendLabel(bacnetResults({ backend: "bacpypes3", bacnet_mode: "foreign_device", bbmd_address: "" }))
        ?.text,
    ).toBe("Live bacpypes3 scan — foreign-device registration (BBMD address not recorded).");
  });

  it("leaves a simulated backend's label free of transport claims", () => {
    // Simulated devices never touched the wire; a transport clause would imply
    // packets that were never sent.
    expect(
      bacnetBackendLabel(
        bacnetResults({ backend: "simulated", bacnet_mode: "foreign_device", bbmd_address: "10.10.30.4" }),
      )?.text,
    ).toBe("SIMULATED — demo data, not a real BACnet scan.");
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

describe("missingExpectedPorts", () => {
  it("extracts register-expected ports that did not answer", () => {
    // Mirrors the engine marker for expected ports the probe found closed.
    expect(
      missingExpectedPorts("responsive: 443 | MISSING EXPECTED PORTS: 135,139,445,5985,7070"),
    ).toBe("135,139,445,5985,7070");
    // Stops at the next token so trailing verdicts never bleed into the list.
    expect(
      missingExpectedPorts(
        "responsive: 443 | MISSING EXPECTED PORTS: 445 | HOSTNAME MISMATCH: expected a, got b",
      ),
    ).toBe("445");
  });

  it("returns empty string for hosts without the verdict", () => {
    expect(missingExpectedPorts("responsive: 80,443")).toBe("");
    expect(missingExpectedPorts("responsive: 443 | EXPECTED PORTS OK: 1/1 open")).toBe("");
    expect(missingExpectedPorts(undefined)).toBe("");
  });
});

describe("expectedPortsOk", () => {
  it("extracts the explicit all-expected-ports-open pass", () => {
    expect(expectedPortsOk("responsive: 135,443,445 | EXPECTED PORTS OK: 3/3 open")).toBe(
      "3/3 open",
    );
  });

  it("returns empty string when the pass verdict is absent", () => {
    expect(expectedPortsOk("responsive: 443 | MISSING EXPECTED PORTS: 445")).toBe("");
    expect(expectedPortsOk("responsive: 80,443")).toBe("");
    expect(expectedPortsOk(undefined)).toBe("");
  });
});

describe("validationMetrics", () => {
  it("prefers the engine-stamped conformance score and blocking issue count", () => {
    // publishing_seen/expected_devices would read 100%; the stamped 91 (already
    // floor'd and clamped server-side) must win, and blocking_issue_count (2)
    // must drive the secondary metric instead of the all-issues count (5).
    const metrics = validationMetrics("udmi-validation", {
      expected_devices: 35,
      publishing_seen: 35,
      issue_count: 5,
      payload_conformance_percent: 91,
      blocking_issue_count: 2,
    });
    expect(metrics).toEqual({
      primary: "91%",
      primaryLabel: "payload conformance",
      secondary: "2",
      secondaryLabel: "blocking issues",
    });
  });

  it("falls back to the publishing ratio and honest all-issues label for pre-upgrade runs", () => {
    // A summary without payload_conformance_percent / blocking_issue_count
    // (pre-upgrade run) keeps the old ratio, and the secondary metric is
    // labelled "issues found" — issue_count is ALL issues, not blocking ones.
    const metrics = validationMetrics("udmi-validation", {
      expected_devices: 35,
      publishing_seen: 33,
      issue_count: 2,
    });
    expect(metrics).toEqual({
      primary: "94%",
      primaryLabel: "payload conformance",
      secondary: "2",
      secondaryLabel: "issues found",
    });
  });

  it("returns null when the engine stamps payload_conformance_percent as null (unscoreable)", () => {
    // An explicit null is the engine saying "nothing to score" (no expected
    // devices). It must yield the neutral empty state — never fall through the
    // ?? to the liveness ratio, which would fabricate a 0% conformance.
    expect(
      validationMetrics("udmi-validation", {
        expected_devices: 0,
        publishing_seen: 0,
        payload_conformance_percent: null,
        blocking_issue_count: 0,
      }),
    ).toBeNull();
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

// A terminal discovery run that observed nothing: every collection is empty and
// only status + result_summary distinguish the outcomes.
function emptyResults(
  status: DiscoveryResultsResponse["status"],
  resultSummary: Record<string, unknown>,
): DiscoveryResultsResponse {
  return {
    run_id: "run-empty-1",
    job_type: "ip_discovery",
    status,
    result_summary: resultSummary,
    discovered_assets: [],
    devices: [],
    points: [],
    topics: [],
  };
}

describe("discoveryEmptyStateFor", () => {
  it("returns null when no results have arrived yet", () => {
    expect(discoveryEmptyStateFor("ip-scanner", undefined)).toBeNull();
  });

  it("reports hosts probed vs answered for a succeeded but empty IP scan", () => {
    const state = discoveryEmptyStateFor(
      "ip-scanner",
      emptyResults("succeeded", { hosts_scanned: 254, hosts_responsive: 0 }),
    );
    expect(state?.title).toMatch(/Scan complete — no responsive hosts found/);
    expect(state?.detail).toMatch(/254 hosts probed/);
    // Zero found is an observation, not a failure — and a silent negative must
    // not be oversold as proof the hosts are absent.
    expect(state?.title).not.toMatch(/fail/i);
    expect(state?.detail).toMatch(/not proof a host is absent/i);
  });

  it("singularises a one-host sweep", () => {
    const state = discoveryEmptyStateFor("ip-scanner", emptyResults("succeeded", { hosts_scanned: 1 }));
    expect(state?.detail).toMatch(/1 host probed/);
  });

  it("points at the target spec when nothing was probed at all", () => {
    const state = discoveryEmptyStateFor(
      "ip-scanner",
      emptyResults("succeeded", { hosts_scanned: 0, hosts_responsive: 0 }),
    );
    expect(state?.detail).toMatch(/0 hosts were probed/);
    expect(state?.detail).toMatch(/target override or the imported IP register/);
  });

  it("falls back to neutral copy when the summary carries no host count", () => {
    const state = discoveryEmptyStateFor("ip-scanner", emptyResults("succeeded", {}));
    expect(state?.title).toMatch(/Scan complete/);
    expect(state?.detail).toBe("The scan completed, but no host answered on the scanned ports.");
    expect(state?.detail).not.toMatch(/\d/);
  });

  it("labels a dry run as a preview instead of a real negative finding", () => {
    // base.py stamps hosts_scanned: 0 on dry runs; without the dry_run gate this
    // would claim "0 hosts were probed" as if the network had been checked.
    const state = discoveryEmptyStateFor(
      "ip-scanner",
      emptyResults("succeeded", { dry_run: true, hosts_scanned: 0, hosts_responsive: 0 }),
    );
    expect(state?.title).toMatch(/Dry run complete — preview only/);
    expect(state?.title).not.toMatch(/no responsive hosts/);
    expect(state?.detail).toMatch(/No packets were sent/);
  });

  it("resolves a failed or cancelled status before dry_run, never claiming completion", () => {
    // _apply_success writes result_summary (stamping dry_run) for every
    // non-exception engine return and resolves the terminal status afterwards,
    // so a cancelled dry run really does carry dry_run: true. It must not read
    // as "Dry run complete".
    const cancelled = discoveryEmptyStateFor(
      "ip-scanner",
      emptyResults("cancelled", { dry_run: true, hosts_scanned: 0 }),
    );
    expect(cancelled?.title).toBe("Run cancelled");
    expect(cancelled?.title).not.toMatch(/complete/i);

    const failed = discoveryEmptyStateFor(
      "ip-scanner",
      emptyResults("failed", { dry_run: true, hosts_scanned: 0 }),
      "Engine execution failed.",
    );
    expect(failed?.title).toMatch(/Run failed/);
    expect(failed?.title).not.toMatch(/complete/i);
  });

  it("names the Who-Is instance range and the BBMD caveat for empty BACnet discovery", () => {
    const state = discoveryEmptyStateFor(
      "bacnet-discovery",
      emptyResults("succeeded", {
        device_count: 0,
        device_instance_low: 0,
        device_instance_high: 4194303,
      }),
    );
    expect(state?.title).toMatch(/no BACnet devices responded/);
    expect(state?.detail).toMatch(/0–4194303/);
    expect(state?.detail).toMatch(/BBMD/);
  });

  it("omits the BACnet instance range when the summary does not carry one", () => {
    const state = discoveryEmptyStateFor("bacnet-discovery", emptyResults("succeeded", {}));
    expect(state?.detail).toMatch(/No devices answered the Who-Is\./);
    expect(state?.detail).not.toMatch(/instance range/);
  });

  it("prefers the engine's empty_scan_hint verbatim over the generic BACnet copy", () => {
    // The engine knows what the run actually did — that it registered with the
    // BBMD and the devices still stayed silent. That is a different diagnosis
    // from the fallback's "may not receive a local broadcast", and paraphrasing
    // or appending to it would put a second, less-informed voice on screen.
    const hint =
      "Registered with BBMD 10.10.30.4, but no devices answered the Who-Is (instances 0–4194303) " +
      "within 3s. Check the device-instance range and the BBMD's broadcast distribution.";
    const state = discoveryEmptyStateFor(
      "bacnet-discovery",
      emptyResults("succeeded", {
        device_count: 0,
        device_instance_low: 0,
        device_instance_high: 4194303,
        empty_scan_hint: hint,
      }),
    );
    expect(state?.title).toBe("Discovery complete — no BACnet devices responded");
    expect(state?.detail).toBe(hint);
    // Verbatim means verbatim: the fallback's wording must not be mixed in.
    expect(state?.detail).not.toMatch(/may not receive a local broadcast/);
  });

  it("keeps the pre-hint fallback copy for runs that stamped no hint", () => {
    // Runs recorded before v0.1.12 carry no hint; they must still explain
    // themselves rather than regress to bare "no results".
    const state = discoveryEmptyStateFor(
      "bacnet-discovery",
      emptyResults("succeeded", { device_count: 0, device_instance_low: 1, device_instance_high: 4194303 }),
    );
    expect(state?.detail).toMatch(/No devices answered the Who-Is \(instance range 1–4194303\)/);
    expect(state?.detail).toMatch(/may not receive a local broadcast/);
  });

  it("falls back when the hint is blank or not a string", () => {
    const blank = discoveryEmptyStateFor("bacnet-discovery", emptyResults("succeeded", { empty_scan_hint: "  " }));
    expect(blank?.detail).toMatch(/No devices answered the Who-Is/);
    const nonString = discoveryEmptyStateFor("bacnet-discovery", emptyResults("succeeded", { empty_scan_hint: 42 }));
    expect(nonString?.detail).toMatch(/No devices answered the Who-Is/);
  });

  it("does not let a hint override a failed or dry run", () => {
    // A hint describes an observation. A failed run recorded none, and a dry run
    // sent no packets — either reading as "we looked and found nothing" is the
    // exact dishonesty the status/dry_run ordering exists to prevent.
    const failed = discoveryEmptyStateFor(
      "bacnet-discovery",
      emptyResults("failed", { empty_scan_hint: "No devices answered the Who-Is." }),
      "The BBMD at 10.10.30.4:47808 refused foreign-device registration (result code 3).",
    );
    expect(failed?.title).toBe("Run failed — no results recorded");
    expect(failed?.detail).toBe("The BBMD at 10.10.30.4:47808 refused foreign-device registration (result code 3).");

    const dry = discoveryEmptyStateFor(
      "bacnet-discovery",
      emptyResults("succeeded", { dry_run: true, empty_scan_hint: "No devices answered the Who-Is." }),
    );
    expect(dry?.title).toMatch(/Dry run complete — preview only/);
    expect(dry?.detail).toMatch(/No packets were sent/);
  });

  it("names the capture window for an empty MQTT capture", () => {
    const state = discoveryEmptyStateFor(
      "mqtt-discovery",
      emptyResults("succeeded", { topics_discovered: 0, messages_captured: 0, capture_seconds: 30 }),
    );
    expect(state?.title).toMatch(/no MQTT messages received/);
    expect(state?.detail).toMatch(/30s capture window/);
  });

  it("echoes the engine's own failure message instead of implying nothing was found", () => {
    const state = discoveryEmptyStateFor(
      "mqtt-discovery",
      emptyResults("failed", {}),
      "MQTT discovery failed (capture_window_empty).",
    );
    expect(state?.title).toBe("Run failed — no results recorded");
    expect(state?.detail).toBe("MQTT discovery failed (capture_window_empty).");
    // Honesty, inverse direction: a failure must never read as "nothing found".
    expect(state?.title).not.toMatch(/complete|found/i);
  });

  it("points a message-less failure at the run monitor", () => {
    const state = discoveryEmptyStateFor("ip-scanner", emptyResults("failed", {}), null);
    expect(state?.detail).toMatch(/run monitor/i);
  });

  it("returns null for non-discovery routes and non-terminal statuses", () => {
    expect(discoveryEmptyStateFor("reports", emptyResults("succeeded", {}))).toBeNull();
    expect(discoveryEmptyStateFor("ip-scanner", emptyResults("running", { hosts_scanned: 254 }))).toBeNull();
  });
});
