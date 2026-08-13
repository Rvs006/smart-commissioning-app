import { describe, expect, it } from "vitest";
import {
  classifyScanAuthorization,
  formatIpHeadlineMetrics,
  serializeIpTargetRows,
  type IpTargetRow,
  type ScanAuthorizationRecord,
} from "./ipDiscoveryModel";

const authorization = (
  overrides: Partial<ScanAuthorizationRecord> = {},
): ScanAuthorizationRecord => ({
  authorization_id: "auth-1",
  preview_run_id: "preview-1",
  packet_plan_sha256: "a".repeat(64),
  not_before: "2026-08-12T09:00:00Z",
  not_after: "2026-08-12T11:00:00Z",
  max_uses: 1,
  use_count: 0,
  consumed_run_id: null,
  revoked_at: null,
  ...overrides,
});

describe("IP target drafts", () => {
  it("serializes repeatable CIDR, range, and address rows without merging exclusions", () => {
    const targets: IpTargetRow[] = [
      { id: "t-1", kind: "cidr", value: " 10.0.0.0/30 " },
      { id: "t-2", kind: "range", value: "10.0.0.8", end: "10.0.0.10" },
      { id: "t-3", kind: "address", value: "10.0.0.20" },
    ];
    const exclusions: IpTargetRow[] = [
      { id: "x-1", kind: "address", value: "10.0.0.1" },
      { id: "x-2", kind: "range", value: "10.0.0.9", end: "10.0.0.10" },
    ];

    expect(serializeIpTargetRows(targets, exclusions)).toEqual({
      target_expressions: [
        { kind: "cidr", cidr: "10.0.0.0/30" },
        { kind: "range", start: "10.0.0.8", end: "10.0.0.10" },
        { kind: "address", address: "10.0.0.20" },
      ],
      exclusions: [
        { kind: "address", address: "10.0.0.1" },
        { kind: "range", start: "10.0.0.9", end: "10.0.0.10" },
      ],
    });
  });

  it("rejects a blank or incomplete row instead of silently changing the plan", () => {
    expect(() =>
      serializeIpTargetRows([{ id: "t-1", kind: "range", value: "10.0.0.1", end: "" }], []),
    ).toThrow("Complete every target and exclusion row");
  });
});

describe("preview-bound scan authorization state", () => {
  const now = new Date("2026-08-12T10:00:00Z");

  it.each([
    ["no_access", false, undefined],
    ["none_available", true, undefined],
    ["not_started", true, authorization({ not_before: "2026-08-12T10:30:00Z" })],
    ["expired", true, authorization({ not_after: "2026-08-12T10:00:00Z" })],
    ["revoked", true, authorization({ revoked_at: "2026-08-12T09:30:00Z" })],
    ["exhausted", true, authorization({ use_count: 1, consumed_run_id: "run-live" })],
    ["drift_invalidated", true, authorization({ packet_plan_sha256: "b".repeat(64) })],
    ["valid", true, authorization()],
  ] as const)("returns %s", (expected, accessAllowed, record) => {
    expect(
      classifyScanAuthorization({
        accessAllowed,
        authorization: record,
        now,
        previewRunId: "preview-1",
        packetPlanSha256: "a".repeat(64),
      }),
    ).toBe(expected);
  });

  it("invalidates an approval from another preview even when its digest matches", () => {
    expect(
      classifyScanAuthorization({
        accessAllowed: true,
        authorization: authorization({ preview_run_id: "preview-2" }),
        now,
        previewRunId: "preview-1",
        packetPlanSha256: "a".repeat(64),
      }),
    ).toBe("drift_invalidated");
  });
});

describe("IP headline metric presentation", () => {
  it("keeps configured zero distinct from Not configured and shows progress counts", () => {
    expect(
      formatIpHeadlineMetrics({
        schema_version: "1.0",
        metrics: [
          {
            schema_version: "1.0",
            heading: "Expected Devices",
            configured: true,
            value: 0,
            denominator: 0,
            percentage: null,
            pending_count: 0,
            finalized_count: 0,
          },
          {
            schema_version: "1.0",
            heading: "Reachable Devices",
            configured: true,
            value: 1,
            denominator: 4,
            percentage: 25,
            pending_count: 2,
            finalized_count: 2,
          },
          {
            schema_version: "1.0",
            heading: "Register Matches",
            configured: false,
            value: null,
            denominator: null,
            percentage: null,
            pending_count: null,
            finalized_count: null,
          },
          {
            schema_version: "1.0",
            heading: "Unexpected / Unregistered Hosts",
            configured: false,
            value: null,
            denominator: null,
            percentage: null,
            pending_count: null,
            finalized_count: null,
          },
        ],
      }),
    ).toEqual([
      { heading: "Expected Devices", value: "0", progress: "0 finalized, 0 pending" },
      { heading: "Reachable Devices", value: "1 / 4 (25%)", progress: "2 finalized, 2 pending" },
      { heading: "Register Matches", value: "Not configured", progress: null },
      {
        heading: "Unexpected / Unregistered Hosts",
        value: "Not configured",
        progress: null,
      },
    ]);
  });

  it("rejects incomplete or reordered metric snapshots", () => {
    expect(() => formatIpHeadlineMetrics({ schema_version: "1.0", metrics: [] })).toThrow(
      "Invalid IP headline metric snapshot",
    );
  });
});
