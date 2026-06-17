import { describe, expect, it } from "vitest";
import type { UdmiAssetPayloadView } from "../../api/client";
import { mergeAssetGroups, type AssetIssueGroup, type IssueRow } from "./operatorData";

function issue(id: string, assetId: string): IssueRow {
  return { id, assetId, severity: "minor", area: "pointset validation", message: "msg", owner: "team" };
}

describe("mergeAssetGroups (mq9m4bnv)", () => {
  it("merges issues with payload views per asset and surfaces payload-only assets", () => {
    const issueGroups: AssetIssueGroup[] = [
      {
        assetId: "AHU-1",
        issues: [issue("i1", "AHU-1")],
        byPayloadType: [{ payloadType: "pointset", issues: [issue("i1", "AHU-1")] }],
      },
    ];
    const payloadViews: UdmiAssetPayloadView[] = [
      {
        asset_id: "AHU-1",
        payload_types: [
          { payload_type: "pointset", expected: { units: {} }, observed: { points: {} }, observed_present: true },
          { payload_type: "state", expected: { manufacturer: "X" }, observed: null, observed_present: false },
        ],
      },
      {
        asset_id: "AHU-2",
        payload_types: [
          { payload_type: "state", expected: null, observed: { system: {} }, observed_present: true },
        ],
      },
    ];

    const merged = mergeAssetGroups(issueGroups, payloadViews);
    const byAsset = Object.fromEntries(merged.map((g) => [g.assetId, g]));

    // AHU-1: pointset has the issue AND a payload view; state has a payload view, zero issues.
    const a1 = byAsset["AHU-1"];
    expect(a1.issues).toHaveLength(1);
    const a1Types = Object.fromEntries(a1.payloadTypes.map((t) => [t.payloadType, t]));
    expect(a1Types.pointset.issues).toHaveLength(1);
    expect(a1Types.pointset.hasPayloadView).toBe(true);
    expect(a1Types.pointset.observedPresent).toBe(true);
    expect(a1Types.state.issues).toHaveLength(0);
    expect(a1Types.state.hasPayloadView).toBe(true);
    expect(a1Types.state.observedPresent).toBe(false);

    // AHU-2 has payloads but zero issues and still appears.
    const a2 = byAsset["AHU-2"];
    expect(a2.issues).toHaveLength(0);
    expect(a2.payloadTypes.map((t) => t.payloadType)).toEqual(["state"]);
    expect(a2.payloadTypes[0].observedPresent).toBe(true);
  });

  it("returns issue-only groups unchanged when there are no payload views", () => {
    const issueGroups: AssetIssueGroup[] = [
      {
        assetId: "AHU-1",
        issues: [issue("i1", "AHU-1")],
        byPayloadType: [{ payloadType: "metadata", issues: [issue("i1", "AHU-1")] }],
      },
    ];
    const merged = mergeAssetGroups(issueGroups, []);
    expect(merged).toHaveLength(1);
    expect(merged[0].payloadTypes[0].hasPayloadView).toBe(false);
    expect(merged[0].payloadTypes[0].issues).toHaveLength(1);
  });
});
