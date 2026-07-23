import { describe, expect, it } from "vitest";
import type { UdmiAssetPayloadView, UdmiAssetResult } from "../../api/client";
import {
  assetMatchesFacetFilter,
  buildAssetFacts,
  mergeAssetGroups,
  moduleWorkspaces,
  udmiPayloadVerdict,
  udmiVerdictForIssues,
  udmiVerdictTone,
  type AssetIssueGroup,
  type IssueRow,
  type MergedAssetGroup,
  type MergedPayloadType,
} from "./operatorData";

function issue(id: string, assetId: string): IssueRow {
  return { id, assetId, severity: "minor", area: "pointset validation", message: "msg" };
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
        system: "BMS",
        payload_types: [
          { payload_type: "pointset", expected: { units: {} }, observed: { points: {} }, observed_present: true },
          { payload_type: "state", expected: { manufacturer: "X" }, observed: null, observed_present: false },
        ],
      },
      {
        asset_id: "AHU-2",
        system: "Lighting",
        payload_types: [
          { payload_type: "state", expected: null, observed: { system: {} }, observed_present: true },
        ],
      },
    ];

    const merged = mergeAssetGroups(issueGroups, payloadViews);
    const byAsset = Object.fromEntries(merged.map((g) => [g.assetId, g]));

    // AHU-1: pointset has the issue AND a payload view; state has a payload view, zero issues.
    const a1 = byAsset["AHU-1"];
    expect(a1.system).toBe("BMS");
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
    expect(a2.system).toBe("Lighting");
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

describe("module workspace fixtures", () => {
  // moduleWorkspaces used to ship sample `rows` (register-comparison verdicts
  // no run had produced — the reports head's four invented rows were read by a
  // reviewer as reports the app had really made) and fallback `issues`
  // (ISS-#### copy rendered on validation routes before any run). Both are
  // deleted at the source, fields and all. This is a DATA-layer assertion on
  // purpose: nothing renders these fields any more, so no DOM test can pin
  // them — re-adding them changes nothing on screen, it just re-plants dead
  // fixture data for a future fallback to surface.
  it("carries no sample rows or fallback issues on any workspace", () => {
    for (const workspace of Object.values(moduleWorkspaces)) {
      expect(workspace).not.toHaveProperty("rows");
      expect(workspace).not.toHaveProperty("issues");
    }
  });
});

// Pure unit matrix for the UDMI RAG verdict mapping (mqf-udmi-rag). No DOM, so
// it is immune to the jsdom-cannot-see-theme-CSS constraint — every assertion
// is on the verdict kind, its label, and its tone string.
describe("udmiPayloadVerdict / udmiVerdictTone — RAG scheme (mqf-udmi-rag)", () => {
  function sev(severity: IssueRow["severity"]): IssueRow {
    return { id: "i", assetId: "A", severity, area: "pointset validation", message: "m" };
  }

  it("red offline wins only when a capture was attempted AND nothing was observed", () => {
    const verdict = udmiVerdictForIssues([sev("major")], false, true);
    expect(verdict.verdict).toBe("offline");
    expect(verdict.label).toBe("Not observed this run");
    expect(udmiVerdictTone("offline")).toBe("fail"); // offline shades RED
  });

  it("never paints an observed payload offline (honesty guard)", () => {
    // assetOffline=true but the payload WAS observed → offline must not win;
    // it falls through to the issue-based verdict. Pins the honesty rule so a
    // summary/issue mislabel can never override direct observation.
    const verdict = udmiVerdictForIssues([sev("critical")], true, true);
    expect(verdict.verdict).toBe("fail");
    expect(verdict.label).toBe("Non-compliant — 1 issue (1 critical)");
  });

  it("amber for a publishing device with a critical issue", () => {
    const verdict = udmiVerdictForIssues([sev("critical")], true);
    expect(verdict.verdict).toBe("fail");
    expect(verdict.label).toBe("Non-compliant — 1 issue (1 critical)");
    expect(udmiVerdictTone("fail")).toBe("warn"); // non-compliant shades AMBER
  });

  it("amber for a publishing device with a major issue (medium/high mapped)", () => {
    const verdict = udmiVerdictForIssues([sev("major"), sev("major")], true);
    expect(verdict.verdict).toBe("fail");
    expect(verdict.label).toBe("Non-compliant — 2 issues");
    expect(udmiVerdictTone("fail")).toBe("warn");
  });

  it("amber for minor-only issues (strict default; field engineer flip point is udmiVerdictTone)", () => {
    // OPEN field engineer question (2026-07-15): strict reading demotes minor-only
    // "Pass with notes" to amber. To restore green, change the single
    // `pass-notes` branch in udmiVerdictTone to return "pass" — this test and
    // that one line move together.
    const verdict = udmiVerdictForIssues([sev("minor")], true);
    expect(verdict.verdict).toBe("pass-notes");
    expect(verdict.label).toBe("Pass with notes");
    expect(udmiVerdictTone("pass-notes")).toBe("warn");
  });

  it("neutral 'Not received — N notes' for minor-only notes with no observed payload (ISSUE-10)", () => {
    // A payload type that was never received but still carries minor-only notes
    // must NOT read as a PASS ("Pass with notes"). It stays neutral, and the
    // note count remains visible in the label so nothing is hidden.
    const verdict = udmiVerdictForIssues([sev("minor"), sev("minor")], false);
    expect(verdict.verdict).toBe("none");
    expect(verdict.label).toBe("Not received — 2 notes");
    expect(udmiVerdictTone(verdict.verdict)).toBeNull();
  });

  it("singularises the note count in the unobserved-notes label (ISSUE-10)", () => {
    const verdict = udmiVerdictForIssues([sev("minor")], false);
    expect(verdict.verdict).toBe("none");
    expect(verdict.label).toBe("Not received — 1 note");
  });

  it("still reports hard fails as non-compliant when unobserved and not offline (ISSUE-10)", () => {
    // The observation gate only affects the minor-only branch; a critical/major
    // issue is a real finding regardless of whether a payload was observed.
    const verdict = udmiVerdictForIssues([sev("critical")], false, false);
    expect(verdict.verdict).toBe("fail");
    expect(verdict.label).toBe("Non-compliant — 1 issue (1 critical)");
  });

  it("green for a clean observed payload", () => {
    const verdict = udmiVerdictForIssues([], true);
    expect(verdict.verdict).toBe("pass");
    expect(verdict.label).toBe("Pass");
    expect(udmiVerdictTone("pass")).toBe("pass");
  });

  it("neutral (no shade) for a clean, unobserved, online payload — Not received", () => {
    const verdict = udmiVerdictForIssues([], false, false);
    expect(verdict.verdict).toBe("none");
    expect(verdict.label).toBe("Not received");
    expect(udmiVerdictTone("none")).toBeNull();
  });

  it("udmiPayloadVerdict offline guard mirrors the convenience wrapper", () => {
    expect(
      udmiPayloadVerdict({ criticalCount: 1, majorCount: 0, totalIssues: 1, observedPresent: false, assetOffline: true })
        .verdict,
    ).toBe("offline");
    expect(
      udmiPayloadVerdict({ criticalCount: 1, majorCount: 0, totalIssues: 1, observedPresent: true, assetOffline: true })
        .verdict,
    ).toBe("fail");
  });
});

// Inspector facets use the register's System field and this run's observation.
describe("asset facet filters (ITEM-10)", () => {
  function payloadType(observedPresent: boolean): MergedPayloadType {
    return {
      payloadType: "state",
      issues: [],
      expected: null,
      observed: null,
      observedPresent,
      hasPayloadView: true,
    };
  }
  function group(assetId: string, system: string, observedPresent: boolean): MergedAssetGroup {
    return { assetId, system, issues: [], payloadTypes: [payloadType(observedPresent)] };
  }

  function summaryAsset(assetId: string, system: string, observed: boolean): UdmiAssetResult {
    return {
      asset_id: assetId,
      system,
      observed,
      expected_payloads: 1,
      received_payloads: observed ? 1 : 0,
      all_expected_payloads_received: observed,
      all_received_payloads_successfully_validated: observed,
      successfully_validated: observed,
      issue_count: 0,
      blocking_issue_count: 0,
      last_observed_at: observed ? "2026-07-23T01:00:00Z" : null,
      payload_results: [],
    };
  }

  it("builds register-backed system and observation facts", () => {
    const facts = buildAssetFacts([
      group("EM-1", "BMS", true),
      group("EM-2", "Lighting", false),
    ]);
    expect(facts.get("EM-1")).toEqual({ system: "BMS", observed: true });
    expect(facts.get("EM-2")).toEqual({ system: "Lighting", observed: false });
  });

  it("falls back to the stable summary when historical runs have no payload views", () => {
    const issueOnlyGroup: MergedAssetGroup = {
      assetId: "EM-1",
      system: "Unspecified",
      issues: [issue("issue-1", "EM-1")],
      payloadTypes: [{ ...payloadType(false), hasPayloadView: false }],
    };

    const facts = buildAssetFacts(
      [issueOnlyGroup],
      [summaryAsset("EM-1", "SEC", true), summaryAsset("CLEAN-1", "BMS", false)],
    );

    expect(facts.get("EM-1")).toEqual({ system: "SEC", observed: true });
    expect(facts.get("CLEAN-1")).toEqual({ system: "BMS", observed: false });
  });

  it("lets direct payload evidence override a conflicting summary fallback", () => {
    const facts = buildAssetFacts(
      [group("EM-1", "Unspecified", false)],
      [summaryAsset("EM-1", "BMS", true)],
    );

    expect(facts.get("EM-1")).toEqual({ system: "BMS", observed: false });
  });

  it("matches by system and observed/not-observed status", () => {
    const lighting = { system: "Lighting", observed: false };
    const bms = { system: "BMS", observed: true };
    expect(assetMatchesFacetFilter(lighting, { system: "all", observation: "not-observed" })).toBe(true);
    expect(assetMatchesFacetFilter(bms, { system: "all", observation: "not-observed" })).toBe(false);
    expect(assetMatchesFacetFilter(bms, { system: "all", observation: "observed" })).toBe(true);
    expect(assetMatchesFacetFilter(bms, { system: "Lighting", observation: "all" })).toBe(false);
  });

  it("an asset with no facts matches only the all-clear filter", () => {
    expect(assetMatchesFacetFilter(undefined, { system: "all", observation: "all" })).toBe(true);
    expect(assetMatchesFacetFilter(undefined, { system: "BMS", observation: "all" })).toBe(false);
    expect(assetMatchesFacetFilter(undefined, { system: "all", observation: "observed" })).toBe(false);
  });
});
