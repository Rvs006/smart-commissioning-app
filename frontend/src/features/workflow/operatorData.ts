// Live-data helpers and per-route workspace chrome for the module pages.
//
// Everything rendered as results or findings is REAL: recent runs (GET /runs),
// discovery devices/points/topics (GET /discovery/runs/{id}/results),
// validation issues (GET /validation/runs/{id}/issues), and the report queue
// (GET /reports). Before a run exists the UI shows a neutral empty state.
//
// moduleWorkspaces carries static page chrome only (titles, headlines, table
// titles, result columns, evidence labels). Its former sample `rows` and
// fallback `issues` fixtures — like projectSummary, runRows and assetRows
// before them — were dead data shipped in the bundle and are deleted;
// operatorData.test.ts pins that the fields stay gone.

import type {
  UdmiAssetPayloadView,
  UdmiAssetResult,
  ValidationIssueRecord,
} from "../../api/client";

export type HealthState = "ready" | "warning" | "failed" | "running" | "queued";

export type IssueRow = {
  id: string;
  assetId: string;
  severity: "critical" | "major" | "minor";
  area: string;
  // The full joined description used by the row "View" modal and anything else
  // that wants one string. The issue CARDS render the structured fragments below
  // as separate readable lines instead (ITEM-9).
  message: string;
  description?: string;
  statusDetail?: string | null;
  // "Expected X, observed Y", already formatted (empty -> "empty") — ITEM-9.
  expectedObserved?: string;
  suggestedAction?: string | null;
  // The engine's point_name, when the issue is point-scoped. Combined with
  // matching provenance below to derive conservative legacy evidence paths.
  pointName?: string | null;
  // Matching provenance used to keep a unit mismatch pinned to the exact
  // `units` line instead of tinting the whole point object.
  matchBasis?: string | null;
  evidencePath?: string | null;
};

// Groups validation issues by asset, then by derived payload type (mq9m4bnv).
// Payload type is read from the issue's issue_type / topic / point_name
// (pointset, metadata, state) when present; anything else falls into "other".
// This is DERIVED, not authoritative — the issue schema has no dedicated
// payload-type field, so the grouping is best-effort over the available fields.
export type PayloadTypeGroup = {
  payloadType: string;
  issues: IssueRow[];
};

export type AssetIssueGroup = {
  assetId: string;
  issues: IssueRow[];
  byPayloadType: PayloadTypeGroup[];
};

const PAYLOAD_TYPES = ["pointset", "metadata", "state"] as const;

export function derivePayloadType(issue: ValidationIssueRecord): string {
  const haystack = `${issue.issue_type ?? ""} ${issue.topic ?? ""} ${issue.point_name ?? ""}`.toLowerCase();
  for (const type of PAYLOAD_TYPES) {
    if (haystack.includes(type)) {
      return type;
    }
  }
  return "other";
}

export function groupIssuesByAsset(
  issues: ValidationIssueRecord[],
  toRow: (issue: ValidationIssueRecord) => IssueRow,
): AssetIssueGroup[] {
  const byAsset = new Map<string, { rows: IssueRow[]; byType: Map<string, IssueRow[]> }>();
  for (const issue of issues) {
    const assetId = issue.asset_id ?? "Unknown asset";
    const payloadType = derivePayloadType(issue);
    const row = toRow(issue);
    if (!byAsset.has(assetId)) {
      byAsset.set(assetId, { byType: new Map(), rows: [] });
    }
    const entry = byAsset.get(assetId)!;
    entry.rows.push(row);
    if (!entry.byType.has(payloadType)) {
      entry.byType.set(payloadType, []);
    }
    entry.byType.get(payloadType)!.push(row);
  }
  return Array.from(byAsset.entries()).map(([assetId, entry]) => ({
    assetId,
    byPayloadType: Array.from(entry.byType.entries()).map(([payloadType, rows]) => ({
      issues: rows,
      payloadType,
    })),
    issues: entry.rows,
  }));
}

// Merge issue groups with the authoritative per-payload-type payload views from
// result_summary.payload_views (mq9m4bnv). Each asset's payload types union its
// issues (when any) with expected/observed payload content (when pasted or
// captured). Assets that have payload content but ZERO issues still appear, so a
// clean multi-payload asset shows its payload types rather than nothing.
export type MergedPayloadType = {
  payloadType: string;
  issues: IssueRow[];
  expected: unknown;
  observed: unknown;
  observedPresent: boolean;
  hasPayloadView: boolean;
};

export type MergedAssetGroup = {
  assetId: string;
  system: string;
  issues: IssueRow[];
  payloadTypes: MergedPayloadType[];
};

const PAYLOAD_TYPE_ORDER = ["pointset", "metadata", "state"];

function payloadTypeRank(type: string): number {
  const index = PAYLOAD_TYPE_ORDER.indexOf(type);
  return index === -1 ? PAYLOAD_TYPE_ORDER.length : index;
}

export function mergeAssetGroups(
  issueGroups: AssetIssueGroup[],
  payloadViews: UdmiAssetPayloadView[],
): MergedAssetGroup[] {
  type Acc = { issues: IssueRow[]; system: string; types: Map<string, MergedPayloadType> };
  const order: string[] = [];
  const byAsset = new Map<string, Acc>();
  const ensureAsset = (assetId: string): Acc => {
    let acc = byAsset.get(assetId);
    if (!acc) {
      acc = { issues: [], system: "Unspecified", types: new Map() };
      byAsset.set(assetId, acc);
      order.push(assetId);
    }
    return acc;
  };
  const ensureType = (acc: Acc, payloadType: string): MergedPayloadType => {
    let entry = acc.types.get(payloadType);
    if (!entry) {
      entry = {
        payloadType,
        issues: [],
        expected: null,
        observed: null,
        observedPresent: false,
        hasPayloadView: false,
      };
      acc.types.set(payloadType, entry);
    }
    return entry;
  };

  for (const group of issueGroups) {
    const acc = ensureAsset(group.assetId);
    acc.issues.push(...group.issues);
    for (const entry of group.byPayloadType) {
      ensureType(acc, entry.payloadType).issues.push(...entry.issues);
    }
  }
  for (const view of payloadViews) {
    const acc = ensureAsset(view.asset_id);
    const system = view.system?.trim();
    if (system) {
      acc.system = system;
    }
    for (const pt of view.payload_types) {
      const entry = ensureType(acc, pt.payload_type);
      entry.expected = pt.expected;
      entry.observed = pt.observed;
      entry.observedPresent = pt.observed_present;
      entry.hasPayloadView = true;
    }
  }

  return order.map((assetId) => {
    const acc = byAsset.get(assetId)!;
    const payloadTypes = Array.from(acc.types.values()).sort(
      (a, b) => payloadTypeRank(a.payloadType) - payloadTypeRank(b.payloadType),
    );
    return { assetId, system: acc.system, issues: acc.issues, payloadTypes };
  });
}

// Single source of truth for the per-payload-type UDMI verdict, shared by the
// results-table rows, the row "View" detail, and the per-asset payload sections
// so the three surfaces can never disagree. RAG scheme per the 2026-07-15 field
// ask: red = expected asset not observed during the capture (only ever when a capture was
// actually attempted), amber = publishing but not UDMI compliant (any severity,
// no issue weighting), green = compliant and observed. "Not received" — no
// capture evidence either way — stays neutral: no shade, no claim.
export type UdmiVerdictKind = "pass" | "pass-notes" | "fail" | "offline" | "none";

export type UdmiVerdict = {
  verdict: UdmiVerdictKind;
  label: string;
};

export function udmiPayloadVerdict(input: {
  criticalCount: number;
  majorCount: number;
  totalIssues: number;
  observedPresent: boolean;
  assetOffline?: boolean;
}): UdmiVerdict {
  const { criticalCount, majorCount, totalIssues, observedPresent, assetOffline } = input;
  // Offline wins FIRST — before the issue counts — so the asset's own
  // "not_publishing" issue cannot shadow it. The !observedPresent guard keeps
  // an actually-observed payload from ever being painted offline (honesty rule:
  // never render an observation the app did not make).
  if (assetOffline && !observedPresent) {
    return { label: "Not observed this run", verdict: "offline" };
  }
  if (criticalCount > 0) {
    return {
      label: `Non-compliant — ${totalIssues} issue${totalIssues === 1 ? "" : "s"} (${criticalCount} critical)`,
      verdict: "fail",
    };
  }
  if (majorCount > 0) {
    return {
      label: `Non-compliant — ${totalIssues} issue${totalIssues === 1 ? "" : "s"}`,
      verdict: "fail",
    };
  }
  if (totalIssues > 0) {
    // "Pass with notes" is an honest claim ONLY when a payload was actually
    // observed. A payload type that was never received but still carries
    // minor-only notes must not read as a PASS (ISSUE-10) — it stays neutral
    // "Not received", with the note count kept visible in the label. Hard fails
    // (critical/major) above are unaffected: those issues are real regardless of
    // whether a payload was observed.
    return observedPresent
      ? { label: "Pass with notes", verdict: "pass-notes" }
      : {
          label: `Not received — ${totalIssues} note${totalIssues === 1 ? "" : "s"}`,
          verdict: "none",
        };
  }
  return observedPresent
    ? { label: "Pass", verdict: "pass" }
    : { label: "Not received", verdict: "none" };
}

// Shading tone for a verdict under the RAG scheme: green (pass) for a compliant
// observed payload; amber (warn) for a publishing-but-non-compliant payload —
// both hard fails (critical/major) AND minor-only "Pass with notes"; red (fail)
// for an expected asset not observed during this run; null for "Not received".
//
// field engineer-pending (2026-07-15 field ask): the strict reading maps minor-only
// "Pass with notes" to amber. If he wants minor-only to stay green instead,
// flip the single `pass-notes` line below to `return "pass"` — this function is
// the one and only place that decision lives.
export function udmiVerdictTone(verdict: UdmiVerdictKind): "pass" | "warn" | "fail" | null {
  if (verdict === "offline") {
    return "fail";
  }
  if (verdict === "fail") {
    return "warn";
  }
  if (verdict === "pass-notes") {
    return "warn";
  }
  return verdict === "none" ? null : "pass";
}

// Convenience for callers holding an issue list: counts by severity, then
// delegates to udmiPayloadVerdict. assetOffline flags a device a capture
// attempt found silent (not_publishing) — never inferred from observed_present
// alone.
export function udmiVerdictForIssues(
  issues: IssueRow[],
  observedPresent: boolean,
  assetOffline = false,
): UdmiVerdict {
  return udmiPayloadVerdict({
    assetOffline,
    criticalCount: issues.filter((issue) => issue.severity === "critical").length,
    majorCount: issues.filter((issue) => issue.severity === "major").length,
    observedPresent,
    totalIssues: issues.length,
  });
}

// Inspector facets use the register's System value and the observation made in
// this run. They deliberately avoid inferring connection state from silence.
export type AssetFacts = {
  system: string;
  observed: boolean;
};

export function buildAssetFacts(
  groups: MergedAssetGroup[],
  summaryAssets: readonly UdmiAssetResult[] = [],
): Map<string, AssetFacts> {
  const facts = new Map<string, AssetFacts>();

  // The versioned summary is persisted for fixture and historical runs that do
  // not carry payload_views. Seed from it so their observation and register
  // system facets remain usable, including for clean assets with no issue row.
  for (const asset of summaryAssets) {
    facts.set(asset.asset_id, {
      system: asset.system?.trim() || "Unspecified",
      observed: asset.observed,
    });
  }

  for (const group of groups) {
    const summaryFact = facts.get(group.assetId);
    const hasPayloadEvidence = group.payloadTypes.some((entry) => entry.hasPayloadView);
    const observed = hasPayloadEvidence
      ? group.payloadTypes.some((entry) => entry.hasPayloadView && entry.observedPresent)
      : (summaryFact?.observed ?? false);
    const groupSystem = group.system?.trim();
    facts.set(group.assetId, {
      system:
        groupSystem && groupSystem !== "Unspecified"
          ? groupSystem
          : (summaryFact?.system ?? "Unspecified"),
      observed,
    });
  }
  return facts;
}

export type AssetFacetFilter = {
  system: string; // "all" or a register System value
  observation: string; // "all" | "observed" | "not-observed"
};

export function assetMatchesFacetFilter(
  facts: AssetFacts | undefined,
  filter: AssetFacetFilter,
): boolean {
  if (!facts) {
    return filter.system === "all" && filter.observation === "all";
  }
  if (filter.system !== "all" && facts.system !== filter.system) {
    return false;
  }
  if (filter.observation === "observed" && !facts.observed) {
    return false;
  }
  if (filter.observation === "not-observed" && facts.observed) {
    return false;
  }
  return true;
}

export type ModuleWorkspace = {
  route: string;
  title: string;
  headline: string;
  tableTitle: string;
  columns: string[];
  evidence: string[];
};

export const moduleWorkspaces: Record<string, ModuleWorkspace> = {
  "ip-scanner": {
    route: "ip-scanner",
    // Must stay in step with the "ip-scanner" title in moduleData.ts: this one
    // wins in the module hero (`workspace?.title ?? module.title`).
    // moduleTitles.test.ts guards the pair against drifting apart again.
    title: "IP Discovery",
    headline: "Find reachable, missing, and rogue network hosts against the expected register.",
    tableTitle: "Network Scan Results",
    columns: ["Asset", "Expected IP", "Observed", "MAC Address", "Ports", "Match Basis", "Last Seen", "Detailed Status", "Result"],
    evidence: [],
  },
  "bacnet-discovery": {
    route: "bacnet-discovery",
    title: "BACnet Discovery",
    headline: "Discover BACnet devices, object lists, and property health before validation.",
    tableTitle: "BACnet Devices",
    columns: ["Device", "Instance", "IP Address", "Network Number", "Objects", "Device Last Discovered", "Detailed Status", "Result"],
    evidence: ["Who-Is/I-Am capture", "Device object index", "Property read sample"],
  },
  "mqtt-discovery": {
    route: "mqtt-discovery",
    title: "MQTT Discovery",
    headline: "Subscribe to broker topics, capture payloads, and compare telemetry to the register.",
    tableTitle: "MQTT Topic Observations",
    columns: ["Topic", "Asset", "Payload Last Seen", "Message Count", "Detailed Connection Status", "Raw Payload", "Result"],
    evidence: ["Broker subscription log", "Payload samples", "Topic register comparison"],
  },
  "udmi-validation": {
    route: "udmi-validation",
    title: "UDMI Payload Workbench",
    headline: "Inspect state, metadata, pointset, and controlled publish payloads in detail.",
    tableTitle: "UDMI Payload Checks",
    columns: ["Asset", "State", "Pointset", "Payload Last Seen", "Message Count", "Raw Payload", "Result"],
    evidence: ["State payload evidence", "Pointset payload evidence", "Validation issue JSON"],
  },
  "data-validation": {
    route: "data-validation",
    title: "BACnet to MQTT Validation",
    headline: "Run MQTT payload checks, BACnet point checks, and BACnet-to-MQTT live value comparisons.",
    tableTitle: "Live Validation Results",
    columns: ["Asset", "Point", "BACnet", "MQTT", "Tolerance", "Result"],
    evidence: ["Comparison matrix", "Tolerance file", "Mapping delta register"],
  },
  reports: {
    route: "reports",
    title: "Reports",
    headline: "Create evidence packs, issue reports, and commissioning handover outputs.",
    tableTitle: "Report Queue",
    columns: ["Report", "Source", "Status", "File"],
    evidence: ["Evidence pack ZIP", "Issue report XLSX", "Executive summary PDF"],
  },
};
