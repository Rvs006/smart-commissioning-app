import type {
  DiscoveryRunKind,
  ImportType,
  JobType,
  ReportFormat,
  ReportType,
  ValidationRunKind,
} from "../../api/client";

export type ModuleRunAction =
  | {
      kind: "discovery";
      label: string;
      helper: string;
      runKind: DiscoveryRunKind;
      jobType: JobType;
    }
  | {
      kind: "validation";
      label: string;
      helper: string;
      runKind: ValidationRunKind;
      jobType: JobType;
    }
  | {
      kind: "report";
      label: string;
      helper: string;
      format?: ReportFormat;
      reportType: ReportType;
    };

export type ModuleDefinition = {
  route: string;
  title: string;
  summary: string;
  purpose: string;
  backendService: string;
  workerJob: string;
  integrationStatus: string;
  primaryImports: string[];
  primaryOutputs: string[];
  readiness: string[];
  importTypes: ImportType[];
  runActions: ModuleRunAction[];
  nextImplementation: string;
};

const modules: ModuleDefinition[] = [
  {
    route: "configuration",
    title: "Configuration",
    summary:
      "Stores network, BACnet, MQTT, certificate, time, backup, and logging settings required by discovery and validation services.",
    purpose:
      "Replace browser-local mock state with backend-backed configuration, field validation, masked secrets, and health checks.",
    backendService: "Configuration API",
    workerJob: "No background job required",
    integrationStatus: "API contract ready",
    primaryImports: ["Configuration JSON", "Certificate references", "Broker settings"],
    primaryOutputs: ["Validated configuration snapshot", "Connectivity status", "Masked secret metadata"],
    readiness: [
      "Reject invalid IP, BACnet port, network number, and MQTT port values.",
      "Mask secret material in all frontend responses.",
      "Track configuration changes in audit logs."
    ],
    importTypes: [],
    runActions: [],
    nextImplementation:
      "Implement persistent configuration endpoints first, then replace the remaining prototype localStorage usage."
  },
  {
    route: "ip-scanner",
    title: "IP Scanner",
    summary:
      "Runs authorized network discovery jobs, compares results to the imported IP register, and identifies reachable, missing, and rogue hosts.",
    purpose:
      "Convert the current sample scan table into an async job-backed workflow with stored result history and exportable evidence.",
    backendService: "Discovery API",
    workerJob: "IP discovery job",
    integrationStatus: "Worker contract ready",
    primaryImports: ["IP register CSV/XLSX", "Scan target ranges", "Expected service definitions"],
    primaryOutputs: ["Reachability results", "Observed services", "Register comparison status"],
    readiness: [
      "Support long-running scans without blocking the UI.",
      "Preserve raw results and interpreted comparison outputs.",
      "Expose exportable issue and evidence views per run."
    ],
    importTypes: ["ip_register"],
    runActions: [
      {
        kind: "discovery",
        label: "Run IP Discovery",
        helper: "Queues an IP discovery run through the API and tracks it in the run monitor below.",
        runKind: "ip",
        jobType: "ip_discovery"
      }
    ],
    nextImplementation:
      "Implement the import model and async job lifecycle before connecting any real scan service."
  },
  {
    route: "bacnet-discovery",
    title: "BACnet Discovery",
    summary:
      "Discovers BACnet devices and objects, exposes live object properties, and compares discovered devices against the project register.",
    purpose:
      "Provide a scalable BACnet workflow that can handle the object volumes described in the specification.",
    backendService: "BACnet discovery API",
    workerJob: "BACnet discovery job",
    integrationStatus: "Service boundary ready",
    primaryImports: ["BACnet device register", "BACnet network settings", "Optional BBMD settings"],
    primaryOutputs: ["Device list", "Object list", "Property reads", "Register comparison status"],
    readiness: [
      "Preserve device-level and object-level result records.",
      "Allow operator drilldown into object status, reliability, and present value.",
      "Separate discovery results from validation results."
    ],
    importTypes: ["bacnet_register", "bacnet_points"],
    runActions: [
      {
        kind: "discovery",
        label: "Run BACnet Discovery",
        helper: "Queues a BACnet discovery run through the API and tracks it in the run monitor below.",
        runKind: "bacnet",
        jobType: "bacnet_discovery"
      }
    ],
    nextImplementation:
      "Introduce the BACnet result model after configuration and import handling are stable."
  },
  {
    route: "mqtt-discovery",
    title: "MQTT Discovery",
    summary:
      "Connects to the broker, subscribes to topics, inspects live payloads, extracts points, and compares discovered topics to the MQTT register.",
    purpose:
      "Act as the operator-facing gateway into MQTT and UDMI telemetry before deeper validation is applied.",
    backendService: "MQTT discovery API",
    workerJob: "MQTT discovery job",
    integrationStatus: "Worker contract ready",
    primaryImports: ["MQTT register", "Root topic filters", "TLS client settings"],
    primaryOutputs: ["Discovered topics", "Latest payloads", "Extracted points", "Register comparison status"],
    readiness: [
      "Handle TLS and certificate-backed connections from server-side services.",
      "Preserve raw payloads for later evidence and comparison.",
      "Treat MQTT discovery and UDMI validation as related but separate workflows."
    ],
    importTypes: ["mqtt_register", "mqtt_points"],
    runActions: [
      {
        kind: "discovery",
        label: "Run MQTT Discovery",
        helper: "Queues an MQTT discovery run through the API and tracks it in the run monitor below.",
        runKind: "mqtt",
        jobType: "mqtt_discovery"
      }
    ],
    nextImplementation:
      "Connect broker subscription services after the settings and import pipelines are live."
  },
  {
    route: "udmi-validation",
    title: "UDMI Payload Workbench",
    summary:
      "Detailed payload workbench for testing UDMI-style state, metadata, pointset, and controlled MQTT config publish flows.",
    purpose:
      "Port the useful MQTT validation logic from the standalone Python script into a shared app-level validation service.",
    backendService: "Validation API",
    workerJob: "UDMI payload validation job",
    integrationStatus: "Porting target identified",
    primaryImports: ["Asset metadata contracts", "MQTT topic mapping rules", "Broker and TLS settings"],
    primaryOutputs: ["Per-asset validation result", "Pointset and state evidence", "Issue records", "Summary JSON and XLSX"],
    readiness: [
      "Normalize state, payload, and pointset issues into one issue schema.",
      "Store per-device evidence artifacts instead of only displaying transient results.",
      "Support gateway-style state-only assets separately from pointset assets."
    ],
    importTypes: ["mqtt_register", "asset_validation", "mqtt_points"],
    runActions: [
      {
        kind: "validation",
        label: "Run UDMI Validation",
        helper: "Queues a UDMI validation run through the API and tracks it in the run monitor below.",
        runKind: "udmi",
        jobType: "udmi_validation"
      }
    ],
    nextImplementation:
      "Keep this as the technical payload workbench while the main Validation page owns normal operator validation runs."
  },
  {
    // Route stays /data-validation (and the runKind wiring is unchanged); only
    // the operator-facing title/nav label is renamed per review mqatkxi8. The
    // optional UDMI + Validation page merge is intentionally out of scope here.
    route: "data-validation",
    title: "BACnet to MQTT Validation",
    summary:
      "Run three checks in one place: MQTT/UDMI payload health, BACnet point health, and BACnet-to-MQTT live value comparison.",
    purpose:
      "Provide the cross-protocol evidence required for integration readiness and commissioning handover.",
    backendService: "Validation API",
    workerJob: "BACnet to MQTT comparison job",
    integrationStatus: "Model contract ready",
    primaryImports: ["Asset validation register", "BACnet point register", "MQTT point register", "Mapping file", "Tolerance file"],
    primaryOutputs: ["Asset validation results", "Point-level issues", "Mapping deltas", "Severity-tagged findings"],
    readiness: [
      "Keep every failure traceable to source data.",
      "Preserve not-tested, not-applicable, warning, and fail states distinctly.",
      "Treat tolerances and unit conversion as first-class comparison rules."
    ],
    importTypes: ["asset_validation", "bacnet_points", "mqtt_points", "mapping", "tolerances"],
    runActions: [
      {
        kind: "validation",
        label: "Run MQTT Payload Check",
        helper: "Checks UDMI/MQTT payload structure, timestamps, state, metadata, and live point values.",
        runKind: "udmi",
        jobType: "udmi_validation"
      },
      {
        kind: "validation",
        label: "Run BACnet Point Check",
        helper: "Checks BACnet point names, object details, reliability, units, and present values.",
        runKind: "bacnet",
        jobType: "bacnet_validation"
      },
      {
        kind: "validation",
        label: "Compare BACnet and MQTT",
        helper: "Compares matching live BACnet and MQTT values using mapping and tolerance templates.",
        runKind: "mapping",
        jobType: "mapping_validation"
      }
    ],
    nextImplementation:
      "Implement this after the shared import framework and UDMI result model are stable."
  },
  {
    route: "reports",
    title: "Reports",
    summary:
      "Generates evidence packs and issue reports for discovery and validation workflows, including JSON, CSV, XLSX, and formal downloadable outputs.",
    purpose:
      "Turn stored run records into reproducible evidence rather than one-off downloads from browser state.",
    backendService: "Reports API",
    workerJob: "Report generation job",
    integrationStatus: "Export contract ready",
    primaryImports: ["Completed discovery runs", "Completed validation runs", "User notes", "Project metadata"],
    primaryOutputs: ["Evidence pack", "Issue reports", "Validation exports", "Per-run report files"],
    readiness: [
      "Build reports from stored run data, not live screen state.",
      "Support filtered exports tied to run identifiers.",
      "Persist raw evidence references alongside rendered files."
    ],
    importTypes: [],
    runActions: [
      {
        kind: "report",
        label: "Generate Excel Report",
        helper: "Generates an XLSX report for review, filtering, and issue handover; it then appears in the Reports list below.",
        format: "xlsx",
        reportType: "issue_report"
      },
      {
        kind: "report",
        label: "Generate Word Report",
        helper: "Generates a DOCX report for formal commissioning handover; it then appears in the Reports list below.",
        format: "docx",
        reportType: "evidence_pack"
      }
    ],
    nextImplementation:
      "Implement report generation only after the run and issue models are persisted."
  }
];

export function getModuleByRoute(route: string): ModuleDefinition {
  const module = modules.find((entry) => entry.route === route);
  if (!module) {
    throw new Error(`Unknown module route: ${route}`);
  }
  return module;
}
