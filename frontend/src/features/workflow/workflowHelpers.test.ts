import { describe, expect, it } from "vitest";
import type { ValidationIssueRecord } from "../../api/client";
import { matchesTopicFilter } from "./discoveryRows";
import { derivePayloadType, groupIssuesByAsset, type IssueRow } from "./operatorData";

describe("matchesTopicFilter (MQTT wildcards)", () => {
  it("matches everything for '#' or empty filter", () => {
    expect(matchesTopicFilter("a/b/c", "#")).toBe(true);
    expect(matchesTopicFilter("a/b/c", "")).toBe(true);
  });

  it("matches a single level with '+'", () => {
    expect(matchesTopicFilter("demo-site/b1/ahu/state", "demo-site/+/+/state")).toBe(true);
    expect(matchesTopicFilter("demo-site/b1/ahu/metadata", "demo-site/+/+/state")).toBe(false);
  });

  it("matches a multi-level tail with '#'", () => {
    expect(matchesTopicFilter("demo-site/b1/ahu/events/pointset", "demo-site/#")).toBe(true);
    expect(matchesTopicFilter("other/b1/ahu", "demo-site/#")).toBe(false);
  });

  it("requires an exact length match without trailing wildcard", () => {
    expect(matchesTopicFilter("a/b", "a/b")).toBe(true);
    expect(matchesTopicFilter("a/b/c", "a/b")).toBe(false);
  });
});

function makeIssue(overrides: Partial<ValidationIssueRecord>): ValidationIssueRecord {
  return {
    asset_id: "AHU-1",
    description: "desc",
    issue_id: Math.random().toString(36).slice(2),
    issue_type: "generic",
    severity: "low",
    ...overrides,
  };
}

const toRow = (issue: ValidationIssueRecord): IssueRow => ({
  area: issue.issue_type,
  assetId: issue.asset_id ?? "Unknown asset",
  id: issue.issue_id,
  message: issue.description,
  severity: "minor",
});

describe("derivePayloadType", () => {
  it("derives pointset/metadata/state from issue_type, topic, or point_name", () => {
    expect(derivePayloadType(makeIssue({ issue_type: "pointset_type_mismatch" }))).toBe("pointset");
    expect(derivePayloadType(makeIssue({ issue_type: "x", topic: "a/b/metadata" }))).toBe("metadata");
    expect(derivePayloadType(makeIssue({ issue_type: "x", point_name: "state_flag" }))).toBe("state");
  });

  it("falls back to 'other' when nothing matches", () => {
    expect(derivePayloadType(makeIssue({ issue_type: "register_mismatch", topic: null }))).toBe("other");
  });
});

describe("groupIssuesByAsset", () => {
  it("groups issues by asset and then by derived payload type", () => {
    const issues = [
      makeIssue({ asset_id: "AHU-1", issue_type: "pointset_a" }),
      makeIssue({ asset_id: "AHU-1", issue_type: "metadata_b" }),
      makeIssue({ asset_id: "AHU-1", topic: "x/pointset" }),
      makeIssue({ asset_id: "BLR-2", issue_type: "state_c" }),
      makeIssue({ asset_id: null, issue_type: "generic" }),
    ];

    const groups = groupIssuesByAsset(issues, toRow);
    const byAsset = Object.fromEntries(groups.map((group) => [group.assetId, group]));

    expect(byAsset["AHU-1"].issues).toHaveLength(3);
    const ahuTypes = Object.fromEntries(
      byAsset["AHU-1"].byPayloadType.map((entry) => [entry.payloadType, entry.issues.length]),
    );
    expect(ahuTypes).toEqual({ metadata: 1, pointset: 2 });

    expect(byAsset["BLR-2"].byPayloadType[0]).toEqual(
      expect.objectContaining({ payloadType: "state" }),
    );
    // A null asset_id is bucketed under a stable "Unknown asset" key.
    expect(byAsset["Unknown asset"].issues).toHaveLength(1);
  });
});
