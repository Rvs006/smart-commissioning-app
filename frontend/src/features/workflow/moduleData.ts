import type {
  DiscoveryRunKind,
  ImportType,
  JobType,
  ReportFormat,
  ReportType,
  ValidationRunKind,
} from "../../api/client";

// `hiddenFromRunControls` hides an action from the Run Controls card list ONLY.
// ModulePage resolves every action through its explicit `id`, so reordering the
// configuration cannot send a click to another endpoint. Narrowing on `kind`
// still works through the intersection, as does
// Exclude<ModuleRunAction, { kind: "report" }>.
export type ModuleRunAction = (
  | {
      id: string;
      kind: "discovery";
      label: string;
      helper: string;
      runKind: DiscoveryRunKind;
      jobType: JobType;
    }
  | {
      id: string;
      kind: "validation";
      label: string;
      helper: string;
      runKind: ValidationRunKind;
      jobType: JobType;
    }
  | {
      id: string;
      kind: "report";
      label: string;
      helper: string;
      format?: ReportFormat;
      reportType: ReportType;
    }
) & { hiddenFromRunControls?: boolean };

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
    // The vendored network IP scanner sidecar is the operator-facing IP lane
    // (nav "IP Discovery" -> /ip-scanner). It runs through the plain discovery
    // path (runKind "ip_sidecar"), with no Nmap or sealed-preview flow.
    route: "ip-scanner",
    title: "IP Discovery",
    summary:
      "Runs the vendored network IP scanner sidecar, compares results to the imported IP register, and identifies reachable, missing, and rogue hosts.",
    backendService: "IP scanner sidecar",
    importTypes: ["ip_scanner_register"],
    runActions: [
      {
        id: "ip-scanner.run",
        kind: "discovery",
        label: "Run IP Discovery",
        helper: "Runs the IP scanner sidecar through the API and tracks it in the run monitor below.",
        runKind: "ip_sidecar",
        jobType: "ip_scanner"
      }
    ]
  },
  {
    // The built-in TCP/Nmap discovery engine. It moved off the primary nav when
    // the sidecar took over the /ip-scanner slug, but its engine, sealed-preview
    // authorization flow, and Nmap path are unchanged and reachable at
    // #/ip-scanner-sct. Route + jobType "ip_discovery" stay historical slugs the
    // backend and saved runs key off.
    route: "ip-scanner-sct",
    title: "IP Discovery",
    summary:
      "Runs authorized network discovery jobs, compares results to the imported IP register, and identifies reachable, missing, and rogue hosts.",
    backendService: "Discovery API",
    importTypes: ["ip_register"],
    runActions: [
      {
        id: "ip-discovery.run",
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
        id: "bacnet-discovery.run",
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
        id: "mqtt-discovery.run",
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
        id: "udmi-validation.run",
        // Hidden from the Run Controls card (field engineer 2026-07-15: the run control
        // belongs at the bottom, after the operator has been through the
        // options — the "Execute capture" button in Schedule and Payload
        // Evidence starts this same action).
        //
        // DO NOT DELETE this entry because the card is gone: ModulePage resolves
        // it by index (udmiRunActionIndex) and a missing entry fails the run as
        // "Unknown run action."
        hiddenFromRunControls: true,
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
        id: "data-validation.udmi",
        kind: "validation",
        label: "Run MQTT Payload Check",
        helper: "Checks UDMI/MQTT payload structure, timestamps, state, metadata, and live point values.",
        runKind: "udmi",
        jobType: "udmi_validation"
      },
      {
        id: "data-validation.bacnet",
        kind: "validation",
        label: "Run BACnet Point Check",
        helper: "Checks BACnet point names, object details, reliability, units, and present values.",
        runKind: "bacnet",
        jobType: "bacnet_validation"
      },
      {
        id: "data-validation.mapping",
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
      "Lists and exports evidence packs and issue reports generated from completed discovery and validation runs.",
    backendService: "Reports API",
    importTypes: [],
    runActions: []
  }
];

export function getModuleByRoute(route: string): ModuleDefinition {
  const module = modules.find((entry) => entry.route === route);
  if (!module) {
    throw new Error(`Unknown module route: ${route}`);
  }
  return module;
}
