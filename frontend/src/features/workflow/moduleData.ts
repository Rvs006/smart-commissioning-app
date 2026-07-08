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
  backendService: string;
  importTypes: ImportType[];
  runActions: ModuleRunAction[];
};

const modules: ModuleDefinition[] = [
  {
    route: "configuration",
    title: "Configuration",
    summary:
      "Stores network, BACnet, MQTT, certificate, time, backup, and logging settings required by discovery and validation services.",
    backendService: "Configuration API",
    importTypes: [],
    runActions: []
  },
  {
    route: "ip-scanner",
    title: "IP Scanner",
    summary:
      "Runs authorized network discovery jobs, compares results to the imported IP register, and identifies reachable, missing, and rogue hosts.",
    backendService: "Discovery API",
    importTypes: ["ip_register"],
    runActions: [
      {
        kind: "discovery",
        label: "Run IP Discovery",
        helper: "Runs an IP discovery through the API and tracks it in the run monitor below.",
        runKind: "ip",
        jobType: "ip_discovery"
      }
    ]
  },
  {
    route: "bacnet-discovery",
    title: "BACnet Discovery",
    summary:
      "Discovers BACnet devices and objects, exposes live object properties, and compares discovered devices against the project register.",
    backendService: "BACnet discovery API",
    importTypes: ["bacnet_register", "bacnet_points"],
    runActions: [
      {
        kind: "discovery",
        label: "Run BACnet Discovery",
        helper: "Runs a BACnet discovery through the API and tracks it in the run monitor below.",
        runKind: "bacnet",
        jobType: "bacnet_discovery"
      }
    ]
  },
  {
    route: "mqtt-discovery",
    title: "MQTT Discovery",
    summary:
      "Connects to the broker, subscribes to topics, inspects live payloads, extracts points, and compares discovered topics to the MQTT register.",
    backendService: "MQTT discovery API",
    importTypes: ["mqtt_register", "mqtt_points"],
    runActions: [
      {
        kind: "discovery",
        label: "Run MQTT Discovery",
        helper: "Runs an MQTT discovery through the API and tracks it in the run monitor below.",
        runKind: "mqtt",
        jobType: "mqtt_discovery"
      }
    ]
  },
  {
    route: "udmi-validation",
    title: "UDMI Payload Workbench",
    summary:
      "Detailed payload workbench for testing UDMI-style state, metadata, pointset, and controlled MQTT config publish flows.",
    backendService: "Validation API",
    importTypes: ["mqtt_register", "asset_validation", "mqtt_points"],
    runActions: [
      {
        kind: "validation",
        label: "Run UDMI Validation",
        helper: "Runs a UDMI validation through the API and tracks it in the run monitor below.",
        runKind: "udmi",
        jobType: "udmi_validation"
      }
    ]
  },
  {
    // Route stays /data-validation (and the runKind wiring is unchanged); only
    // the operator-facing title/nav label is renamed per review mqatkxi8. The
    // optional UDMI + Validation page merge is intentionally out of scope here.
    route: "data-validation",
    title: "BACnet to MQTT Validation",
    summary:
      "Run three checks in one place: MQTT/UDMI payload health, BACnet point health, and BACnet-to-MQTT live value comparison.",
    backendService: "Validation API",
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
    ]
  },
  {
    route: "reports",
    title: "Reports",
    summary:
      "Generates evidence packs and issue reports for discovery and validation workflows, including JSON, CSV, XLSX, and formal downloadable outputs.",
    backendService: "Reports API",
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
    ]
  }
];

export function getModuleByRoute(route: string): ModuleDefinition {
  const module = modules.find((entry) => entry.route === route);
  if (!module) {
    throw new Error(`Unknown module route: ${route}`);
  }
  return module;
}
