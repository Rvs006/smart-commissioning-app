export type HealthState = "ready" | "warning" | "failed" | "running" | "queued";

export type AssetRow = {
  assetId: string;
  name: string;
  protocol: "BACnet" | "MQTT" | "BACnet + MQTT";
  network: string;
  state: HealthState;
  lastSeen: string;
  points: string;
};

export type IssueRow = {
  id: string;
  assetId: string;
  severity: "critical" | "major" | "minor";
  area: string;
  message: string;
  owner: string;
};

export type RunRow = {
  id: string;
  type: string;
  status: HealthState;
  progress: number;
  stage: string;
  updated: string;
};

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
  primaryMetric: string;
  primaryMetricLabel: string;
  secondaryMetric: string;
  secondaryMetricLabel: string;
  tableTitle: string;
  rows: Array<Record<string, string>>;
  columns: string[];
  issues: IssueRow[];
  evidence: string[];
};

export const projectSummary = {
  project: "ElectraCom Smart Building",
  site: "Block B Plantroom",
  readiness: 68,
  assets: 126,
  onlineAssets: 111,
  openIssues: 14,
  evidencePacks: 3,
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
  {
    name: "Reports",
    state: "queued",
    summary: "Evidence pack can be generated after current validation issues are reviewed.",
    action: "Queue evidence",
  },
];

export const assetRows: AssetRow[] = [
  {
    assetId: "MDB5-00-043-BLR-1",
    name: "Boiler 1 Controller",
    protocol: "BACnet + MQTT",
    network: "10.10.25.101 / device 1532001",
    state: "failed",
    lastSeen: "2 min ago",
    points: "42 / 45",
  },
  {
    assetId: "MDB5-00-044-BLR-2",
    name: "Boiler 2 Controller",
    protocol: "BACnet + MQTT",
    network: "10.10.25.102 / device 1532002",
    state: "ready",
    lastSeen: "48 sec ago",
    points: "45 / 45",
  },
  {
    assetId: "AHU-L03-017",
    name: "Level 3 AHU",
    protocol: "BACnet",
    network: "10.10.25.117 / device 1532117",
    state: "warning",
    lastSeen: "7 min ago",
    points: "87 / 91",
  },
  {
    assetId: "MTR-ENERGY-009",
    name: "Energy Meter 9",
    protocol: "MQTT",
    network: "electracom/sct/1532/meter/009",
    state: "ready",
    lastSeen: "15 sec ago",
    points: "18 / 18",
  },
];

export const issueRows: IssueRow[] = [
  {
    id: "ISS-1042",
    assetId: "MDB5-00-043-BLR-1",
    severity: "critical",
    area: "UDMI pointset",
    message: "fault_status expected STRING but received NUMBER.",
    owner: "BMS contractor",
  },
  {
    id: "ISS-1037",
    assetId: "AHU-L03-017",
    severity: "major",
    area: "BACnet discovery",
    message: "Four required points are absent from the live object list.",
    owner: "Controls engineer",
  },
  {
    id: "ISS-1028",
    assetId: "MDB5-00-043-BLR-1",
    severity: "major",
    area: "MQTT discovery",
    message: "Telemetry interval exceeds configured tolerance by 70 seconds.",
    owner: "Integration engineer",
  },
  {
    id: "ISS-1019",
    assetId: "MTR-ENERGY-009",
    severity: "minor",
    area: "Report metadata",
    message: "Asset location is missing floor reference in imported register.",
    owner: "Commissioning lead",
  },
];

export const runRows: RunRow[] = [
  {
    id: "run_udmi_demo_001",
    type: "UDMI validation",
    status: "failed",
    progress: 100,
    stage: "Issue classification",
    updated: "3 min ago",
  },
  {
    id: "run_mqtt_demo_001",
    type: "MQTT discovery",
    status: "running",
    progress: 64,
    stage: "Subscribing to telemetry topics",
    updated: "Now",
  },
  {
    id: "run_ip_demo_001",
    type: "IP scanner",
    status: "ready",
    progress: 100,
    stage: "Register comparison complete",
    updated: "18 min ago",
  },
];

export const moduleWorkspaces: Record<string, ModuleWorkspace> = {
  "ip-scanner": {
    route: "ip-scanner",
    title: "IP Scanner",
    headline: "Find reachable, missing, and rogue network hosts against the expected register.",
    primaryMetric: "118",
    primaryMetricLabel: "reachable hosts",
    secondaryMetric: "6",
    secondaryMetricLabel: "exceptions",
    tableTitle: "Network Scan Results",
    columns: ["Asset", "Expected IP", "Observed", "MAC Address", "Ports", "Match Basis", "Last Seen", "Detailed Status", "Result"],
    rows: [
      { Asset: "Boiler 1 Controller", "Expected IP": "10.10.25.101", Observed: "Online", "MAC Address": "90:2C:D0:B0:03:36", Ports: "47808/udp, 443/tcp", "Match Basis": "MAC", "Last Seen": "48 sec ago", "Detailed Status": "Responded to UDP BACnet and HTTPS checks", Result: "Matched" },
      { Asset: "AHU Level 3", "Expected IP": "10.10.25.117", Observed: "Online", "MAC Address": "5C:A1:1D:8D:6F:FF", Ports: "47808/udp", "Match Basis": "IP", "Last Seen": "7 min ago", "Detailed Status": "BACnet reachable, HTTP missing", Result: "Service mismatch" },
      { Asset: "Unknown host", "Expected IP": "-", Observed: "10.10.25.214", "MAC Address": "C0:A6:F3:F2:F3:2F", Ports: "80/tcp, 443/tcp", "Match Basis": "None", "Last Seen": "Now", "Detailed Status": "Matched no imported MAC address", Result: "Rogue" },
    ],
    issues: [],
    evidence: [],
  },
  "bacnet-discovery": {
    route: "bacnet-discovery",
    title: "BACnet Discovery",
    headline: "Discover BACnet devices, object lists, and property health before validation.",
    primaryMetric: "37",
    primaryMetricLabel: "devices discovered",
    secondaryMetric: "1,284",
    secondaryMetricLabel: "objects indexed",
    tableTitle: "BACnet Devices",
    columns: ["Device", "Instance", "Objects", "Device Last Discovered", "Detailed Status", "Result"],
    rows: [
      { Device: "Boiler 1 Controller", Instance: "1532001", Objects: "118", "Device Last Discovered": "48 sec ago", "Detailed Status": "I-Am received and object list captured", Result: "Object list captured" },
      { Device: "Level 3 AHU", Instance: "1532117", Objects: "204", "Device Last Discovered": "7 min ago", "Detailed Status": "BACnet reliability flagged stale on four points", Result: "Four required points missing" },
      { Device: "CHW Pump Panel", Instance: "1532041", Objects: "96", "Device Last Discovered": "2 min ago", "Detailed Status": "Operational with no fault or stale flags", Result: "Ready" },
    ],
    issues: issueRows.filter((issue) => issue.area === "BACnet discovery"),
    evidence: ["Who-Is/I-Am capture", "Device object index", "Property read sample"],
  },
  "mqtt-discovery": {
    route: "mqtt-discovery",
    title: "MQTT Discovery",
    headline: "Subscribe to broker topics, capture payloads, and compare telemetry to the register.",
    primaryMetric: "412",
    primaryMetricLabel: "topics observed",
    secondaryMetric: "9",
    secondaryMetricLabel: "silent topics",
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
    primaryMetric: "94%",
    primaryMetricLabel: "payload conformance",
    secondaryMetric: "3",
    secondaryMetricLabel: "blocking issues",
    tableTitle: "UDMI Payload Checks",
    columns: ["Asset", "State", "Pointset", "Payload Last Seen", "Message Count", "Raw Payload", "Result"],
    rows: [
      { Asset: "MDB5-00-043-BLR-1", State: "Present", Pointset: "Present", "Payload Last Seen": "2 min ago", "Message Count": "47", "Raw Payload": "{\"pointset\":{\"points\":{\"fault_status\":{\"present_value\":1}}}}", Result: "Type mismatch" },
      { Asset: "MDB5-00-044-BLR-2", State: "Present", Pointset: "Present", "Payload Last Seen": "48 sec ago", "Message Count": "52", "Raw Payload": "{\"pointset\":{\"points\":{\"supply_air_temperature_setpoint\":{\"present_value\":22}}}}", Result: "Ready" },
      { Asset: "AHU-L03-017", State: "Present", Pointset: "Late", "Payload Last Seen": "7 min ago", "Message Count": "8", "Raw Payload": "{\"system\":{\"hardware\":{\"make\":\"Schneider\",\"model\":\"PM5111\"}}}", Result: "Warning" },
    ],
    issues: issueRows.filter((issue) => issue.area === "UDMI pointset"),
    evidence: ["State payload evidence", "Pointset payload evidence", "Validation issue JSON"],
  },
  "data-validation": {
    route: "data-validation",
    title: "Data Validation",
    headline: "Run MQTT payload checks, BACnet point checks, and BACnet-to-MQTT live value comparisons.",
    primaryMetric: "3",
    primaryMetricLabel: "validation modes",
    secondaryMetric: "1,041",
    secondaryMetricLabel: "points ready to compare",
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
    primaryMetric: "2",
    primaryMetricLabel: "export formats",
    secondaryMetric: "14",
    secondaryMetricLabel: "open findings",
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
