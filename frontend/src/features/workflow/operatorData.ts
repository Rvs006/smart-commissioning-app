// Sample / preview data for UI areas that have no backing endpoint yet.
//
// What is REAL elsewhere (not here): recent runs (GET /runs), discovery
// devices/points/topics (GET /discovery/runs/{id}/results), validation issues
// (GET /validation/runs/{id}/issues), and the report queue (GET /reports).
//
// What stays sample (kept here, labelled in the UI as "Sample preview"):
//  - workflowStages: no workflow-status aggregate endpoint exists.
//  - moduleWorkspaces.*.rows + the "Result" interpretation columns for
//    validation modules: register-comparison verdicts are produced by a
//    validation run, not returned by discovery results.
//
// Headline metric numbers are NOT sample data: they derive from the latest live
// run (discoveryMetrics / validationMetrics in discoveryRows.ts, and the report
// queue for /reports). When no run exists the card shows a neutral empty state.
//  - issueRows: illustrative issue copy used only as a labelled fallback when no
//    live validation run has been executed yet.
//
// Removed as dead (no consumer, no endpoint): projectSummary, runRows,
// assetRows — replaced by the live queries listed above.

import type { UdmiAssetPayloadView, ValidationIssueRecord } from "../../api/client";

export type HealthState = "ready" | "warning" | "failed" | "running" | "queued";

export type IssueRow = {
  id: string;
  assetId: string;
  severity: "critical" | "major" | "minor";
  area: string;
  message: string;
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
  type Acc = { issues: IssueRow[]; types: Map<string, MergedPayloadType> };
  const order: string[] = [];
  const byAsset = new Map<string, Acc>();
  const ensureAsset = (assetId: string): Acc => {
    let acc = byAsset.get(assetId);
    if (!acc) {
      acc = { issues: [], types: new Map() };
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
    return { assetId, issues: acc.issues, payloadTypes };
  });
}

export type WorkflowStage = {
  name: string;
  state: HealthState;
  summary: string;
  action: string;
};

export type ModuleWorkspace = {
  route: string;
  title: string;
  headline: string;
  tableTitle: string;
  rows: Array<Record<string, string>>;
  columns: string[];
  issues: IssueRow[];
  evidence: string[];
};

export const workflowStages: WorkflowStage[] = [
  {
    name: "Configuration",
    state: "ready",
    summary: "Network, BACnet, MQTT, certificate, time, backup, and logging settings are loaded.",
    action: "Review settings",
  },
  {
    name: "Registers",
    state: "warning",
    summary: "IP and MQTT registers are accepted. BACnet point register still needs upload.",
    action: "Upload missing register",
  },
  {
    name: "Discovery",
    state: "running",
    summary: "IP and MQTT discovery are queued. BACnet discovery awaits approved network window.",
    action: "Monitor runs",
  },
  {
    name: "Validation",
    state: "failed",
    summary: "UDMI payload checks found pointset mismatches and silent devices.",
    action: "Resolve issues",
  },
];

// Labelled sample issues. Used only as a fallback in the module inspector when
// no live validation run has been executed; live runs replace these.
export const issueRows: IssueRow[] = [
  {
    id: "ISS-1042",
    assetId: "MDB5-00-043-BLR-1",
    severity: "critical",
    area: "UDMI pointset",
    message: "fault_status expected STRING but received NUMBER.",
  },
  {
    id: "ISS-1037",
    assetId: "AHU-L03-017",
    severity: "major",
    area: "BACnet discovery",
    message: "Four required points are absent from the live object list.",
  },
  {
    id: "ISS-1028",
    assetId: "MDB5-00-043-BLR-1",
    severity: "major",
    area: "MQTT discovery",
    message: "Telemetry interval exceeds configured tolerance by 70 seconds.",
  },
  {
    id: "ISS-1019",
    assetId: "MTR-ENERGY-009",
    severity: "minor",
    area: "Report metadata",
    message: "Asset location is missing floor reference in imported register.",
  },
];

export const moduleWorkspaces: Record<string, ModuleWorkspace> = {
  "ip-scanner": {
    route: "ip-scanner",
    title: "IP Scanner",
    headline: "Find reachable, missing, and rogue network hosts against the expected register.",
    tableTitle: "Network Scan Results",
    columns: ["Asset", "Expected IP", "Observed", "MAC Address", "Ports", "Match Basis", "Last Seen", "Detailed Status", "Result"],
    rows: [
      { Asset: "Boiler 1 Controller", "Expected IP": "10.10.25.101", Observed: "Online", "MAC Address": "90:2C:D0:B0:03:36", Ports: "80/tcp, 443/tcp", "Match Basis": "MAC", "Last Seen": "48 sec ago", "Detailed Status": "Responded to HTTP and HTTPS TCP checks", Result: "Matched" },
      { Asset: "AHU Level 3", "Expected IP": "10.10.25.117", Observed: "Online", "MAC Address": "5C:A1:1D:8D:6F:FF", Ports: "443/tcp", "Match Basis": "IP", "Last Seen": "7 min ago", "Detailed Status": "HTTPS reachable on TCP 443, HTTP missing", Result: "Service mismatch" },
      { Asset: "Unknown host", "Expected IP": "-", Observed: "10.10.25.214", "MAC Address": "C0:A6:F3:F2:F3:2F", Ports: "80/tcp, 443/tcp", "Match Basis": "None", "Last Seen": "Now", "Detailed Status": "Matched no imported MAC address", Result: "Rogue" },
    ],
    issues: [],
    evidence: [],
  },
  "bacnet-discovery": {
    route: "bacnet-discovery",
    title: "BACnet Discovery",
    headline: "Discover BACnet devices, object lists, and property health before validation.",
    tableTitle: "BACnet Devices",
    columns: ["Device", "Instance", "IP Address", "Network Number", "Objects", "Device Last Discovered", "Detailed Status", "Result"],
    rows: [
      { Device: "Boiler 1 Controller", Instance: "1532001", "IP Address": "10.10.25.101", "Network Number": "2001", Objects: "118", "Device Last Discovered": "48 sec ago", "Detailed Status": "I-Am received and object list captured", Result: "Object list captured" },
      { Device: "Level 3 AHU", Instance: "1532117", "IP Address": "10.10.25.117", "Network Number": "2001", Objects: "204", "Device Last Discovered": "7 min ago", "Detailed Status": "BACnet reliability flagged stale on four points", Result: "Four required points missing" },
      { Device: "CHW Pump Panel", Instance: "1532041", "IP Address": "—", "Network Number": "5", Objects: "96", "Device Last Discovered": "2 min ago", "Detailed Status": "MS/TP segment behind router; no IP", Result: "Ready" },
    ],
    issues: issueRows.filter((issue) => issue.area === "BACnet discovery"),
    evidence: ["Who-Is/I-Am capture", "Device object index", "Property read sample"],
  },
  "mqtt-discovery": {
    route: "mqtt-discovery",
    title: "MQTT Discovery",
    headline: "Subscribe to broker topics, capture payloads, and compare telemetry to the register.",
    tableTitle: "MQTT Topic Observations",
    columns: ["Topic", "Asset", "Payload Last Seen", "Message Count", "Detailed Connection Status", "Raw Payload", "Result"],
    rows: [
      { Topic: "electracom/sct/1532/boiler/1/pointset", Asset: "MDB5-00-043-BLR-1", "Payload Last Seen": "2 min ago", "Message Count": "47", "Detailed Connection Status": "Connected; payload type mismatch", "Raw Payload": "{\"points\":{\"fault_status\":{\"present_value\":1}}}", Result: "Type mismatch" },
      { Topic: "electracom/sct/1532/meter/009/pointset", Asset: "MTR-ENERGY-009", "Payload Last Seen": "15 sec ago", "Message Count": "126", "Detailed Connection Status": "Connected; TLS session healthy", "Raw Payload": "{\"points\":{\"energy_sensor\":{\"present_value\":1294.4}}}", Result: "Ready" },
      { Topic: "electracom/sct/1532/ahu/l03/state", Asset: "AHU-L03-017", "Payload Last Seen": "7 min ago", "Message Count": "8", "Detailed Connection Status": "Connected; reporting slower than expected", "Raw Payload": "{\"system\":{\"operation\":{\"operational\":true}}}", Result: "Slow interval" },
    ],
    issues: issueRows.filter((issue) => issue.area.includes("MQTT")),
    evidence: ["Broker subscription log", "Payload samples", "Topic register comparison"],
  },
  "udmi-validation": {
    route: "udmi-validation",
    title: "UDMI Payload Workbench",
    headline: "Inspect state, metadata, pointset, and controlled publish payloads in detail.",
    tableTitle: "UDMI Payload Checks",
    columns: ["Asset", "State", "Pointset", "Payload Last Seen", "Message Count", "Raw Payload", "Result"],
    rows: [
      { Asset: "MDB5-00-043-BLR-1", State: "Present", Pointset: "Present", "Payload Last Seen": "2 min ago", "Message Count": "47", "Raw Payload": "{\"pointset\":{\"points\":{\"fault_status\":{\"present_value\":1}}}}", Result: "Fail — fault_status type mismatch" },
      { Asset: "MDB5-00-044-BLR-2", State: "Present", Pointset: "Present", "Payload Last Seen": "48 sec ago", "Message Count": "52", "Raw Payload": "{\"pointset\":{\"points\":{\"supply_air_temperature_setpoint\":{\"present_value\":22}}}}", Result: "Pass" },
      { Asset: "AHU-L03-017", State: "Present", Pointset: "Late", "Payload Last Seen": "7 min ago", "Message Count": "8", "Raw Payload": "{\"system\":{\"hardware\":{\"make\":\"Schneider\",\"model\":\"PM5111\"}}}", Result: "Fail — pointset reporting interval exceeded" },
    ],
    issues: issueRows.filter((issue) => issue.area === "UDMI pointset"),
    evidence: ["State payload evidence", "Pointset payload evidence", "Validation issue JSON"],
  },
  "data-validation": {
    route: "data-validation",
    title: "BACnet to MQTT Validation",
    headline: "Run MQTT payload checks, BACnet point checks, and BACnet-to-MQTT live value comparisons.",
    tableTitle: "Live Validation Results",
    columns: ["Asset", "Point", "BACnet", "MQTT", "Tolerance", "Result"],
    rows: [
      { Asset: "Boiler 1", Point: "supply_temp", BACnet: "71.2 C", MQTT: "71.1 C", Tolerance: "0.5 C", Result: "Pass" },
      { Asset: "Boiler 1", Point: "fault_status", BACnet: "normal", MQTT: "0", Tolerance: "Exact", Result: "Needs mapping rule" },
      { Asset: "AHU L03", Point: "fan_enable", BACnet: "active", MQTT: "active", Tolerance: "Exact", Result: "Pass" },
    ],
    issues: issueRows.filter((issue) => issue.area !== "Report metadata"),
    evidence: ["Comparison matrix", "Tolerance file", "Mapping delta register"],
  },
  reports: {
    route: "reports",
    title: "Reports",
    headline: "Create evidence packs, issue reports, and commissioning handover outputs.",
    tableTitle: "Report Queue",
    columns: ["Report", "Source", "Status", "File"],
    rows: [
      { Report: "Excel issue report", Source: "Validation runs", Status: "Ready", File: "issue_report.xlsx" },
      { Report: "Word handover report", Source: "All completed runs", Status: "Ready", File: "commissioning_handover.docx" },
      { Report: "Evidence pack", Source: "All runs", Status: "Queued", File: "evidence_pack.zip" },
      { Report: "Blocked report", Source: "Incomplete validation", Status: "Blocked", File: "Awaiting validation" },
    ],
    issues: issueRows,
    evidence: ["Evidence pack ZIP", "Issue report XLSX", "Executive summary PDF"],
  },
};
