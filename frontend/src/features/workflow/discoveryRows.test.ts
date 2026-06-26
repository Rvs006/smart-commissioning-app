import { describe, expect, it } from "vitest";
import { forbiddenOpenPorts, validationMetrics } from "./discoveryRows";

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
