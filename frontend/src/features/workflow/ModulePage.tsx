import {
  ChangeEvent,
  FormEvent,
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import { flushSync } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";
import {
  ApiError,
  approveDetectedNmap,
  cancelRun,
  createImport,
  createReport,
  createScanAuthorization,
  deleteReports,
  deleteUdmiSchemaSet,
  downloadFile,
  getDiscoveryResults,
  getDiscoveryComparison,
  getDiscoveryObservations,
  getDiscoveryPoints,
  getDiscoveryRun,
  getDiscoveryTopics,
  getDiscoveryTopicsXlsxPath,
  getImportErrors,
  getLatestImport,
  getNmapCapability,
  listScanAuthorizations,
  getValidationIssues,
  getValidationJsonExportPath,
  getValidationRun,
  getImportTemplatePath,
  getReportDownloadPath,
  REPORTS_EXPORT_PATH,
  getUdmiSchemaTemplatePath,
  ImportBatchSummary,
  ImportType,
  listImportProfiles,
  listReports,
  listRuns,
  listUdmiSchemaSets,
  rollbackMqttConfigPublish,
  startMqttConfigPublishRun,
  startAuthorizedDiscoveryRun,
  startBacnetPropertyRun,
  browseBacnetScannerObjects,
  startDiscoveryPreview,
  startDiscoveryRun,
  startValidationRun,
  uploadUdmiSchemaSet,
  DiscoveryAssetObservation,
  DiscoveryObservationRecord,
  DiscoveryResultsResponse,
  DiscoveryRowRecord,
  ReportListResponse,
  ReportSummary,
  ReportFormat,
  ReportType,
  UdmiReportVariant,
  UdmiAssetTopicDiscovery,
  UdmiAssetTopicDiscoveryAssetResult,
  UdmiAssetTopicDiscoveryStatus,
  UdmiAssetTopicObservation,
  UdmiAssetPayloadView,
  UdmiReportScopeV1,
  UdmiValidationSummaryV1,
  ValidationIssueRecord,
  type SessionBoundApiClient,
  type NmapProfileName,
  type BacnetPropertyName,
  type BacnetObjectBrowseResponse,
  type ScanAuthorizationV1,
} from "../../api/client";
import { getModuleByRoute, type ModuleRunAction } from "./moduleData";
import {
  assetMatchesFacetFilter,
  buildAssetFacts,
  groupIssuesByAsset,
  mergeAssetGroups,
  moduleWorkspaces,
  udmiVerdictForIssues,
  udmiVerdictTone,
  type AssetFacts,
  type IssueRow,
  type MergedAssetGroup,
  type UdmiVerdict,
} from "./operatorData";
import {
  bacnetBackendLabel,
  discoveryEmptyStateFor,
  discoveryMetrics,
  discoveryViewFor,
  expectedByRegisterSilent,
  expectedPortsOk,
  forbiddenOpenPorts,
  groupUdmiRowsByAsset,
  matchesTopicFilter,
  missingExpectedPorts,
  mqttRegisterCompareNote,
  resultRowMatchesFilter,
  unexpectedOpenPorts,
  validationMetrics,
} from "./discoveryRows";
import {
  formatAbsoluteTime,
  formatRelativeTime,
  formatRunProgress,
  humanizeStage,
  isTerminalStatus,
  runPollInterval,
  toHealthState,
} from "./runFormat";
import { alignPayloadDiff, tokenizeJsonLine, type AlignedRow } from "./payloadDiff";
import { useRunEvents } from "./useRunEvents";
import { LiveRunConsole } from "./LiveRunConsole";
import { resolvePermittedNmapProfile } from "./nmapProfileSelection";
import { ENGINEER_REQUIRED_TOOLTIP, useSession } from "../../app/sessionContext";
import type { RunRef, SessionScopeId, WorkspaceRef } from "../../app/sessionScope";
import { mutationKeys, queryKeys } from "../../api/queryKeys";
import { isPlainObject } from "../../utils/isPlainObject";

const BACNET_PROPERTY_OPTIONS: readonly BacnetPropertyName[] = [
  "object_name",
  "present_value",
  "units",
  "status_flags",
  "reliability",
  "out_of_service",
  "description",
];

const SCAN_AUTHORIZATION_WINDOW_HOURS = ["1", "4", "8", "24"] as const;
type ScanAuthorizationWindowHours = (typeof SCAN_AUTHORIZATION_WINDOW_HOURS)[number];

function isUsableScanAuthorization(authorization: ScanAuthorizationV1, now = Date.now()): boolean {
  const notBefore = Date.parse(authorization.not_before);
  const notAfter = Date.parse(authorization.not_after);
  return (
    !authorization.revoked_at &&
    !authorization.consumed_run_id &&
    authorization.use_count < authorization.max_uses &&
    Number.isFinite(notBefore) &&
    Number.isFinite(notAfter) &&
    now >= notBefore &&
    now < notAfter
  );
}

import {
  createReportIntent,
  createObservationFoldState,
  foldObservationPage,
  initialRunControllerState,
  isObservationTerminalSynchronized,
  latestAttachableRun,
  resultIdentity,
  runControllerReducer,
  toRunRef,
  type ReportIntent,
  type ObservationFoldState,
} from "./runIsolation";
import {
  formatIpHeadlineMetrics,
  formatBacnetHeadlineMetrics,
  formatBacnetRouters,
  serializeIpTargetRows,
  type IpTargetRow,
  type IpHeadlineMetricDisplay,
  type BacnetHeadlineMetricDisplay,
  type BacnetRouterDisplay,
} from "./ipDiscoveryModel";

function newIpSubmissionKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `ip-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

type ModulePageProps = {
  moduleRoute: string;
};

const ALL_REPORT_FORMATS = [
  "pdf",
  "docx",
  "xlsx",
  "zip",
] as const satisfies readonly ReportFormat[];
type ReportFormatSelection = ReportFormat | "all";

type CopyFeedback = {
  message: string;
  severity: "success" | "warning";
};

type DetailItem = {
  label: string;
  value: string;
};

type RunEpochOwner = {
  epoch: number;
  runId: string;
  sessionScopeId: SessionScopeId;
  workspaceRef: WorkspaceRef;
};

type RunAccessScope = {
  moduleRoute: string;
  sessionScopeId: SessionScopeId;
  workspaceRef: WorkspaceRef;
};

function sameRunEpochOwner(
  left: RunEpochOwner | null | undefined,
  right: RunEpochOwner | null | undefined,
): boolean {
  return Boolean(
    left &&
      right &&
      left.runId === right.runId &&
      left.epoch === right.epoch &&
      left.sessionScopeId === right.sessionScopeId &&
      left.workspaceRef.projectId === right.workspaceRef.projectId &&
      left.workspaceRef.siteId === right.workspaceRef.siteId,
  );
}

function sameRunAccessScope(
  left: RunAccessScope | null | undefined,
  right: RunAccessScope | null | undefined,
): boolean {
  return Boolean(
    left &&
      right &&
      left.moduleRoute === right.moduleRoute &&
      left.sessionScopeId === right.sessionScopeId &&
      left.workspaceRef.projectId === right.workspaceRef.projectId &&
      left.workspaceRef.siteId === right.workspaceRef.siteId,
  );
}

type ScanPort = {
  port: string;
  protocol: "tcp" | "udp";
};

type IPDiscoveryProvider = "builtin_tcp_connect" | "operator_managed_nmap";

const NMAP_PROFILE_LABELS: Record<NmapProfileName, string> = {
  tcp_connect_inventory: "TCP connect inventory",
  host_discovery: "Host discovery",
  tcp_syn_inventory: "TCP SYN inventory",
  selected_udp: "Selected UDP ports",
  service_version_inventory: "Service and version inventory",
  os_inventory: "OS inventory",
  traceroute_inventory: "Traceroute inventory",
  reviewed_script_inventory: "Reviewed script inventory",
};

// One extra point/value pair for a multi-write MQTT config publish (mq9n11wi).
type PointValuePair = {
  point: string;
  value: string;
};

// Which kind of run is being monitored, so we poll the right status endpoint.
// `restored` marks a run rehydrated from run history on page arrival rather than
// started by the operator here and now: it re-attaches the monitor and results
// without hijacking the step the operator is looking at (see the seed effect).
type ActiveRun = {
  epoch: number;
  runId: string;
  kind: "discovery" | "validation";
  restored?: boolean;
  ref: RunRef;
};

type RunSubmissionContext = {
  reservedRun?: ActiveRun;
  reservedOwner?: RunEpochOwner;
};

// The module page is split into three stages so the operator works one screen
// at a time instead of scrolling a single long page of every control at once.
type ModuleStep = "setup" | "run" | "results";

type FrozenUdmiReportScope = {
  scope: UdmiReportScopeV1 | null;
  filtered: boolean;
  expectedAssets: number;
  expectedPayloads: number;
  unexpectedDevices: number;
};

// Each protocol has two discovery lanes: the vendored sidecar (plain path) and
// the relocated built-in engine ("-sct"). "ip-scanner"/"bacnet-scanner"/
// "mqtt-scanner" are the operator-facing sidecars; "ip-scanner-sct"/
// "bacnet-discovery-sct"/"mqtt-discovery-sct" are the built-in engines.
const DISCOVERY_ROUTES = new Set([
  "ip-scanner",
  "ip-scanner-sct",
  "bacnet-scanner",
  "bacnet-discovery-sct",
  "mqtt-scanner",
  "mqtt-discovery-sct",
]);

// A large register can reject hundreds of rows. Render the first N and state the
// honest remainder count rather than building pagination for a pre-1.0 fix:
// fixing the listed rows and re-uploading surfaces the rest.
const IMPORT_ERROR_DISPLAY_CAP = 50;
const LONG_PAYLOAD_ISSUE_THRESHOLD = 8;
const REPORT_PAGE_SIZE = 100;
const TERMINAL_RUN_STATUS_RETRY_DELAYS_MS = [300, 600, 1_000] as const;

function isTransientRunStatusError(error: unknown): boolean {
  return (
    !(error instanceof ApiError) ||
    error.status === 408 ||
    error.status === 429 ||
    error.status >= 500
  );
}

function isDefinitiveLiveSubmissionRejection(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status >= 400 &&
    error.status < 500 &&
    error.status !== 408 &&
    error.status !== 429
  );
}

type ProjectedObservationRecord = Readonly<{
  entityKey: string;
  observation: DiscoveryObservationRecord;
  record: DiscoveryRowRecord;
}>;

function projectedDeviceRecords(fold: ObservationFoldState): ProjectedObservationRecord[] {
  if (fold.observationsPruned || fold.observationsQuarantined) {
    return [];
  }
  const records: ProjectedObservationRecord[] = [];
  for (const observation of fold.entities.values()) {
    const projection = observation.payload.projection_v1;
    if (!isPlainObject(projection) || projection.collection !== "devices") {
      continue;
    }
    if (!isPlainObject(projection.record)) {
      continue;
    }
    records.push({
      entityKey: observation.entity_key,
      observation,
      record: projection.record,
    });
  }
  return records.sort((left, right) => left.entityKey.localeCompare(right.entityKey));
}

const optionalText = (value: unknown): string | null =>
  typeof value === "string" && value.trim() !== "" ? value : null;

function projectedIpAsset(entry: ProjectedObservationRecord): DiscoveryAssetObservation {
  const attributes = isPlainObject(entry.record.attributes) ? entry.record.attributes : {};
  const observedPorts = Array.isArray(entry.record.observed_ports)
    ? entry.record.observed_ports
    : [];
  return {
    ...entry.record,
    asset_id: optionalText(entry.record.asset_id),
    hostname: optionalText(entry.record.hostname) ?? optionalText(entry.record.name),
    ip_address:
      optionalText(entry.record.ip_address) ??
      optionalText(attributes.ip_address) ??
      optionalText(entry.record.address),
    last_seen_at:
      optionalText(entry.record.last_seen_at) ??
      entry.observation.observed_at ??
      entry.observation.created_at,
    mac_address: optionalText(entry.record.mac_address) ?? optionalText(attributes.mac_address),
    match_basis: optionalText(entry.record.match_basis) ?? "none",
    observed_ports: observedPorts as DiscoveryAssetObservation["observed_ports"],
    status_detail: optionalText(entry.record.status_detail) ?? entry.observation.outcome,
  };
}

function bacnetPointCell(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return "[Unserializable value]";
    }
  }
  return String(value);
}

function bacnetPointRow(point: DiscoveryRowRecord) {
  const attributes = isPlainObject(point.attributes) ? point.attributes : {};
  const observedValue = isPlainObject(point.observed_value)
    ? point.observed_value.value
    : undefined;
  const readError = attributes.read_error ?? point.read_error;
  return {
    device:
      attributes.device_instance ?? point.device_ref ?? point.device_instance ?? point.instance,
    object: point.point_name ?? point.point_id ?? point.object_key ?? point.object_name,
    outcome: readError ? "Read failed" : (point.outcome ?? point.status ?? "Read"),
    position: point.position,
    units: point.units ?? point.property ?? point.property_name,
    value: observedValue ?? point.value ?? point.present_value,
  };
}

function provisionalDiscoveryViewFor(
  route: string,
  runId: string,
  jobType: DiscoveryResultsResponse["job_type"],
  fold: ObservationFoldState,
) {
  const projected = projectedDeviceRecords(fold);
  if (projected.length === 0) {
    return null;
  }
  const results: DiscoveryResultsResponse = {
    run_id: runId,
    job_type: jobType,
    status: "running",
    result_summary: {},
    discovered_assets:
      route === "ip-scanner-sct" ? projected.map((entry) => projectedIpAsset(entry)) : [],
    devices: projected.map((entry) => entry.record),
    points: [],
    topics: [],
  };
  const view = discoveryViewFor(route, results);
  if (!view) {
    return null;
  }
  return {
    ...view,
    rows: view.rows.map<Record<string, string>>((row, index) => ({
      ...row,
      __entityKey: projected[index]?.entityKey ?? "",
    })),
  };
}

function discoveryRowEntitySignature(
  route: string,
  row: Readonly<Record<string, string>>,
): string | null {
  // Progressive-observation reconciliation only runs for the built-in engine
  // (jobType "ip_discovery"); the sidecar module never reaches a provisional view.
  if (route === "ip-scanner-sct") {
    const identity = row["Observed IP"] || row["MAC Address"] || row.Asset;
    return identity ? `ip\u0000${identity}` : null;
  }
  if (route === "bacnet-discovery-sct") {
    const identity = [row.Instance, row.Address, row.Device].filter(Boolean).join("\u0000");
    return identity ? `bacnet\u0000${identity}` : null;
  }
  return null;
}

const validationModeCards = [
  {
    description:
      "Checks MQTT topics, UDMI state/metadata/pointset shape, timestamps, reporting cadence, and live point values.",
    step: "01",
    templates: "Default templates: MQTT register, MQTT points, asset validation.",
    title: "MQTT Payload Check",
  },
  {
    description:
      "Checks discovered BACnet devices and objects against expected point names, object metadata, reliability, units, and present values.",
    step: "02",
    templates: "Default templates: BACnet register, BACnet points.",
    title: "BACnet Point Check",
  },
  {
    description:
      "Matches BACnet and MQTT points, then compares live values using mapping rules and tolerances.",
    step: "03",
    templates: "Default templates: mapping, tolerances, BACnet points, MQTT points.",
    title: "BACnet vs MQTT Comparison",
  },
];

// The IP scanner performs a TCP connect test only, so the defaults are TCP
// service ports (not BACnet's UDP 47808 — that lives in BACnet Discovery).
const defaultScanPorts: ScanPort[] = [
  { port: "443", protocol: "tcp" },
  { port: "80", protocol: "tcp" },
  { port: "22", protocol: "tcp" },
];
const defaultExpectedSchedule = JSON.stringify(
  {
    asset_id: "AHU-1000001",
    guid: "ifc://expected-ahu-1000001",
    manufacturer: "Schneider",
    model: "PM5111",
    udmi_version: "1.5.2",
    units: {
      supply_air_temperature_setpoint: "degrees_celsius",
    },
  },
  null,
  2,
);
const defaultStatePayload = JSON.stringify(
  {
    system: {
      hardware: {
        make: "Schneider",
        model: "PM5111",
      },
      last_config: "2026-04-01T10:45:00.000+01:00",
      operation: {
        operational: true,
      },
      serial_no: "PM5111-1000001",
      software: {},
    },
    timestamp: "2026-04-01T10:47:38.697+01:00",
    version: "1.5.2",
  },
  null,
  2,
);
const defaultMetadataPayload = JSON.stringify(
  {
    pointset: {
      points: {
        supply_air_temperature_setpoint: {
          units: "degrees_celsius",
        },
      },
    },
    system: {
      physical_tag: {
        asset: {
          guid: "ifc://expected-ahu-1000001",
          name: "AHU-1000001",
        },
      },
    },
    timestamp: "2026-04-01T10:48:00.000+01:00",
    version: "1.5.2",
  },
  null,
  2,
);
const defaultPointsetPayload = JSON.stringify(
  {
    points: {
      supply_air_temperature_setpoint: {
        present_value: 22,
      },
    },
    timestamp: "2026-04-01T10:48:56.312+01:00",
    version: "1.5.2",
  },
  null,
  2,
);

export function ModulePage({ moduleRoute }: ModulePageProps) {
  // Discovery/validation/report runs, imports, cancel, publish, and rollback are
  // all engineer+ mutations server-side. A viewer/reviewer sees these controls
  // disabled with an explanatory tooltip rather than letting the click 403.
  const {
    apiClient,
    canAdmin,
    canEngineer,
    me,
    sessionScopeId,
    workspace: workspaceRef,
  } = useSession();
  const canEngineerRef = useRef(canEngineer);
  canEngineerRef.current = canEngineer;
  // Evidence downloads are read-only endpoints. Keep their access check
  // separate from the engineer-only mutation/report gate so a resolved viewer
  // session can retrieve the evidence it is allowed to inspect.
  const hasEvidenceReadAccess = me !== null;
  const hasEvidenceReadAccessRef = useRef(hasEvidenceReadAccess);
  hasEvidenceReadAccessRef.current = hasEvidenceReadAccess;
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const module = getModuleByRoute(moduleRoute);
  const workspace = moduleWorkspaces[moduleRoute];
  const isDiscoveryModule = DISCOVERY_ROUTES.has(module.route);
  const isSealedNetworkDiscoveryModule =
    module.route === "ip-scanner-sct" || module.route === "bacnet-discovery-sct";
  const requestedRunId = searchParams.get("run")?.trim() || null;
  const comparisonRunId = searchParams.get("compare")?.trim() || null;
  const setScopedRunUrl = useCallback(
    (runId: string | null) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (runId) {
            next.set("run", runId);
          } else {
            next.delete("run");
          }
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
  const [selectedImportType, setSelectedImportType] = useState<ImportType | "">("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [importOutcome, setImportOutcome] = useState<ImportBatchSummary | null>(null);
  const [runOutcome, setRunOutcome] = useState<string | null>(null);
  const [runAttachmentNotice, setRunAttachmentNotice] = useState<string | null>(null);
  const [lastReport, setLastReport] = useState<ReportSummary | null>(null);
  const [activeRun, setActiveRun] = useState<ActiveRun | null>(null);
  const [reservedLiveSubmissionOwner, setReservedLiveSubmissionOwner] =
    useState<RunEpochOwner | null>(null);
  const [definitiveLiveRejectionOwner, setDefinitiveLiveRejectionOwner] =
    useState<RunEpochOwner | null>(null);
  const reservedLiveSubmissionOwnerRef = useRef<RunEpochOwner | null>(null);
  const activeRunEpochRef = useRef(0);
  const nextActiveRunEpoch = useCallback(() => ++activeRunEpochRef.current, []);
  const [runController, dispatchRun] = useReducer(runControllerReducer, initialRunControllerState);
  const [observationFold, setObservationFold] = useState<ObservationFoldState | null>(null);
  const [copyFeedback, setCopyFeedback] = useState<CopyFeedback | null>(null);
  const [publishTopic, setPublishTopic] = useState("demo-site/b1/ahu-1000001/config");
  const [publishPayload, setPublishPayload] = useState(
    '{"pointset":{"points":{"supply_air_temperature_setpoint":{"set_value":22}}}}',
  );
  const [publishPoint, setPublishPoint] = useState("supply_air_temperature_setpoint");
  const [publishValue, setPublishValue] = useState("22");
  // Extra point/value pairs written into the SAME config payload alongside the
  // primary point above. The primary pair stays the one the backend confirm path
  // verifies; the extras are written but treated as on-site-untested (see note).
  const [publishExtraPoints, setPublishExtraPoints] = useState<PointValuePair[]>([]);
  const [publishConfirmed, setPublishConfirmed] = useState(false);
  const [publishUseLiveBroker, setPublishUseLiveBroker] = useState(false);
  const [publishPointsetTopic, setPublishPointsetTopic] = useState(
    "demo-site/b1/ahu-1000001/events/pointset",
  );
  const [publishWaitSeconds, setPublishWaitSeconds] = useState("5");
  const [scanPorts, setScanPorts] = useState<ScanPort[]>(defaultScanPorts);
  const [scanAuthorized, setScanAuthorized] = useState(false);
  const [scanDryRun, setScanDryRun] = useState(false);
  const [scanTarget, setScanTarget] = useState("");
  const [scanTargetRows, setScanTargetRows] = useState<IpTargetRow[]>([]);
  const [scanExclusionRows, setScanExclusionRows] = useState<IpTargetRow[]>([]);
  const [scanPreviewRunId, setScanPreviewRunId] = useState<string | null>(null);
  const [scanPreviewActive, setScanPreviewActive] = useState(false);
  const [scanAuthorizationId, setScanAuthorizationId] = useState<string | null>(null);
  const [scanAuthorizationTicket, setScanAuthorizationTicket] = useState("");
  const [scanAuthorizationPurpose, setScanAuthorizationPurpose] = useState("");
  const [scanAuthorizationWindowHours, setScanAuthorizationWindowHours] =
    useState<ScanAuthorizationWindowHours>("1");
  const [scanProvider, setScanProvider] = useState<IPDiscoveryProvider>("builtin_tcp_connect");
  const [nmapProfile, setNmapProfile] = useState<NmapProfileName>("tcp_connect_inventory");
  const applyNmapProfile = (profile: NmapProfileName) => {
    setNmapProfile(profile);
    setScanPorts((current) =>
      current.map((entry) => ({
        ...entry,
        protocol: profile === "selected_udp" ? ("udp" as const) : ("tcp" as const),
      })),
    );
  };
  // Register-driven mode: Run sends NO pasted schedule/payloads, so the backend
  // fans out one expected asset per imported mqtt_register row (topics + points
  // + units + schema version from the register). Auto-enabled when an
  // mqtt_register import is accepted on this page; the operator can untick it.
  const [udmiUseRegister, setUdmiUseRegister] = useState(false);
  const [udmiExpectedSchedule, setUdmiExpectedSchedule] = useState(defaultExpectedSchedule);
  const [udmiStatePayload, setUdmiStatePayload] = useState(defaultStatePayload);
  const [udmiMetadataPayload, setUdmiMetadataPayload] = useState(defaultMetadataPayload);
  const [udmiPointsetPayload, setUdmiPointsetPayload] = useState(defaultPointsetPayload);
  const [udmiUseLiveBroker, setUdmiUseLiveBroker] = useState(false);
  // This diagnostic is deliberately opt-in. The default bounded scope is the
  // register's common ancestor; the broad # scope requires a separate, explicit
  // acknowledgement before it can be selected.
  const [udmiTopicDiscoveryEnabled, setUdmiTopicDiscoveryEnabled] = useState(false);
  const [udmiTopicDiscoveryScope, setUdmiTopicDiscoveryScope] = useState<"bounded" | "all">(
    "bounded",
  );
  const [udmiTopicDiscoveryAllScopeConfirmed, setUdmiTopicDiscoveryAllScopeConfirmed] =
    useState(false);
  const [udmiStateTopic, setUdmiStateTopic] = useState("demo-site/b1/ahu-1000001/state");
  const [udmiMetadataTopic, setUdmiMetadataTopic] = useState("demo-site/b1/ahu-1000001/metadata");
  const [udmiPointsetTopic, setUdmiPointsetTopic] = useState(
    "demo-site/b1/ahu-1000001/events/pointset",
  );
  // Blank (the default) = run until every expected topic has reported a
  // payload or the run is cancelled; a positive number bounds the run time.
  const [udmiCaptureSeconds, setUdmiCaptureSeconds] = useState("");
  // Field ask 2026-07-14: real-world reporting intervals are hours-scale
  // (metadata commonly every 24h), so the run-time control carries a unit.
  // The wire value stays SECONDS — only the control converts.
  const [udmiCaptureUnit, setUdmiCaptureUnit] = useState<"seconds" | "minutes" | "hours">(
    "seconds",
  );
  // Non-published UDMI schema set upload (nonpub.N): version label + .json
  // files for the multipart POST; the uploaded-set list below it is GET-backed.
  const [schemaSetLabel, setSchemaSetLabel] = useState("");
  const [schemaSetFiles, setSchemaSetFiles] = useState<File[]>([]);
  const [selectedResultId, setSelectedResultId] = useState<string | null>(null);
  // Results-table client-side filter (ISSUE-4): free-text (substring across the
  // visible cells, or an MQTT wildcard against the Topic column) plus a verdict
  // tone filter. Row selection stays positional into the FULL resultRows, so the
  // filtered view preserves original indices (see visibleResultRows).
  const [resultsTextFilter, setResultsTextFilter] = useState("");
  const [resultsToneFilter, setResultsToneFilter] = useState("all");
  const [resultsTopicContainsFilter, setResultsTopicContainsFilter] = useState("");
  // UDMI facets use register System and this run's actual observation. Silence
  // is never presented as proof that a connected device is offline.
  const [resultsSystemFilter, setResultsSystemFilter] = useState("all");
  const [resultsObservationFilter, setResultsObservationFilter] = useState("all");
  const [resultsCategoryFilter, setResultsCategoryFilter] =
    useState<UdmiReportScopeV1["filters"]["category"]>("all");
  // Discovery routes retain their existing per-row View dialog. The UDMI
  // Workbench uses the persistent Inspector instead, so this state is never set
  // for UDMI rows.
  const [detailRow, setDetailRow] = useState<Record<string, string> | null>(null);
  // bacnet-scanner live object browse (ephemeral read, scoped to the open detail
  // dialog). Cleared whenever the viewed device changes so one device's objects
  // never render under another.
  const [objectBrowseResult, setObjectBrowseResult] = useState<BacnetObjectBrowseResponse | null>(
    null,
  );
  const [propertyExpansionNotice, setPropertyExpansionNotice] = useState<string | null>(null);
  const [propertyOwner, setPropertyOwner] = useState<RunEpochOwner | null>(null);
  const [propertyRunId, setPropertyRunId] = useState<string | null>(null);
  const [propertyPreviewRunId, setPropertyPreviewRunId] = useState<string | null>(null);
  const [propertyAuthorizationId, setPropertyAuthorizationId] = useState<string | null>(null);
  const [propertyRequestedReadSet, setPropertyRequestedReadSet] = useState<BacnetPropertyName[]>(
    [],
  );
  const [propertyRequest, setPropertyRequest] = useState<{
    parentRunId: string;
    deviceInstance: number;
    destination?: string;
    requestedReadSet: BacnetPropertyName[];
  } | null>(null);
  const [propertyCancelling, setPropertyCancelling] = useState(false);
  const [bacnetPointsCursor, setBacnetPointsCursor] = useState<string | null>(null);
  const [bacnetPointsSearch, setBacnetPointsSearch] = useState("");
  const detailDialogRef = useRef<HTMLDialogElement | null>(null);
  const detailDialogOpenerRef = useRef<HTMLButtonElement | null>(null);
  // Per-asset expansion in the UDMI per-payload-type results view (mq9m4bnv),
  // and the nested expected-vs-observed payload expand keyed `${asset}:${type}`.
  const [expandedAsset, setExpandedAsset] = useState<string | null>(null);
  const [expandedPayloadKey, setExpandedPayloadKey] = useState<string | null>(null);
  // Which asset summary rows are expanded in the grouped UDMI results table
  // (ITEM-7). Collapsed by default; the selected asset auto-expands (below) so
  // the inspector never shows a row the table hides (ISSUE-4).
  const [expandedResultAssets, setExpandedResultAssets] = useState<Set<string>>(new Set());
  // Reports page: which queued reports are ticked for "Export selected" and a
  // one-shot confirmation shown after a report is generated (mqatcqb3/mqautz9j).
  const [selectedReportIds, setSelectedReportIds] = useState<Set<string>>(new Set());
  const [reportToast, setReportToast] = useState<string | null>(null);
  const [reportToastWarning, setReportToastWarning] = useState(false);
  const [generatedAllReportIds, setGeneratedAllReportIds] = useState<readonly string[] | null>(
    null,
  );
  const [reportDeleteNotice, setReportDeleteNotice] = useState<string | null>(null);
  // PDF default: the field deliverable is a human-readable handover document
  // (ask 2026-07-14); Word/Excel/zip remain for editable/evidence workflows.
  const [reportExportFormat, setReportExportFormat] = useState<ReportFormatSelection>("pdf");
  const [udmiReportVariant, setUdmiReportVariant] = useState<UdmiReportVariant>("client");
  const [reportDialogOpen, setReportDialogOpen] = useState(false);
  const [reportTitle, setReportTitle] = useState("");
  const [reportScopeSnapshot, setReportScopeSnapshot] = useState<FrozenUdmiReportScope | null>(
    null,
  );
  const [reportIntents, setReportIntents] = useState<readonly ReportIntent[] | null>(null);
  const reportIntentOwnerRef = useRef<RunEpochOwner | null>(null);
  const reportDialogRef = useRef<HTMLDialogElement | null>(null);
  const reportTitleInputRef = useRef<HTMLInputElement | null>(null);
  const reportDialogOpenerRef = useRef<HTMLButtonElement | null>(null);
  // MQTT Explorer-like capture inputs (mq9nhbzu). The live broker capture itself
  // is on-site-untested; this drives the existing mqtt discovery run + topics.
  // Default BLANK: a blank filter is OMITTED from the run parameters, so the
  // engine falls back to its own "#" default and captures every topic. The Root
  // Topic field was removed from Configuration (2026-07-20 walkthrough ITEM-2),
  // so blank no longer inherits a saved value — it means capture-all. Keep the
  // omit-when-blank wire shape (do NOT send a literal "#"): an absent parameter
  // keeps override semantics clean and the engine default covers capture-all.
  const [captureTopicFilter, setCaptureTopicFilter] = useState("");
  const [captureSeconds, setCaptureSeconds] = useState("10");
  // Field ask 2026-07-14: day-scale windows are real (metadata often every 24h),
  // so the capture duration carries a unit. The wire value stays SECONDS — only
  // the control converts (mirrors the UDMI run-time unit).
  const [captureUnit, setCaptureUnit] = useState<"seconds" | "minutes" | "hours">("seconds");
  const [step, setStep] = useState<ModuleStep>("setup");
  // Snap target for the results-open scroll: the top-of-page hero section.
  const heroRef = useRef<HTMLElement | null>(null);
  const pageHeadingRef = useRef<HTMLHeadingElement | null>(null);
  // Per inspector payload-type-group DOM node, keyed `${assetId}:${payloadType}`,
  // so selecting a live-UDMI results row can expand its asset and scroll straight
  // to that payload's issues (ITEM-D). A ref map, not getElementById: asset ids
  // are arbitrary imported field data, unsafe to trust as DOM element ids.
  const payloadGroupRefs = useRef(new Map<string, HTMLDivElement>());
  // Long issue lists can put the comparison control well below the fold. Keep
  // an exact per-payload target so the jump action focuses the right control.
  const payloadComparisonControlRefs = useRef(new Map<string, HTMLButtonElement>());
  const reportDeleteButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const reportDeleteSelectedRef = useRef<HTMLButtonElement | null>(null);
  const reportsHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const reportDeleteFocusIntentRef = useRef<
    { kind: "bulk" } | { kind: "row"; reportId: string } | null
  >(null);
  const templateDownload = useFileDownload(apiClient);
  const reportDownload = useFileDownload(apiClient);
  const exportDownload = useFileDownload(apiClient);
  const captureExportDownload = useFileDownload(apiClient);
  const generatedAllBundleDownload = useFileDownload(apiClient);
  const validationJsonDownload = useFileDownload(apiClient);
  const schemaTemplateDownload = useFileDownload(apiClient);
  const activeRunMatchesReservedLiveSubmission = Boolean(
    activeRun &&
      sameRunEpochOwner(
        { epoch: activeRun.epoch, runId: activeRun.runId, sessionScopeId, workspaceRef },
        reservedLiveSubmissionOwner,
      ),
  );
  const activeRunMatchesDefinitiveLiveRejection = Boolean(
    activeRun &&
      sameRunEpochOwner(
        { epoch: activeRun.epoch, runId: activeRun.runId, sessionScopeId, workspaceRef },
        definitiveLiveRejectionOwner,
      ),
  );

  const profilesQuery = useQuery({
    queryFn: ({ signal }) => listImportProfiles({ client: apiClient, signal }),
    queryKey: queryKeys.importProfiles(sessionScopeId, workspaceRef),
  });

  // SSE-first run progress for the active run. status/stage/progress update
  // live from the stream; on stream error/unsupported, sseActive flips false
  // and the queries below resume the proven 1.5s polling (no regression).
  const runEvents = useRunEvents(
    activeRun?.ref,
    Boolean(activeRun) &&
      runController.phase !== "submitting" &&
      !activeRunMatchesReservedLiveSubmission &&
      !activeRunMatchesDefinitiveLiveRejection,
    apiClient,
    activeRun?.epoch ?? 0,
  );
  // A disabled stream returns to its neutral `idle` state. Preserve a closed
  // access boundary across submission fencing so a reserved epoch cannot make
  // an already-denied workspace readable again.
  const currentRunAccessScope: RunAccessScope = {
    moduleRoute: module.route,
    sessionScopeId,
    workspaceRef,
  };
  const currentRunAccessScopeRef = useRef(currentRunAccessScope);
  currentRunAccessScopeRef.current = currentRunAccessScope;
  const runAccessClosedScopeRef = useRef<RunAccessScope | null>(null);
  const runEventAccessScope: RunAccessScope | null = runEvents.runRef
    ? {
        moduleRoute: runEvents.runRef.module,
        sessionScopeId: runEvents.runRef.sessionScopeId,
        workspaceRef: runEvents.runRef.workspace,
      }
    : null;
  if (
    runEvents.connectionState === "closed" &&
    sameRunAccessScope(runEventAccessScope, currentRunAccessScope)
  ) {
    runAccessClosedScopeRef.current = runEventAccessScope;
  }
  const runAccessClosed = sameRunAccessScope(
    runAccessClosedScopeRef.current,
    currentRunAccessScope,
  );
  const activeRunOwner: RunEpochOwner | null = activeRun
    ? { epoch: activeRun.epoch, runId: activeRun.runId, sessionScopeId, workspaceRef }
    : null;
  const activeRunOwnerRef = useRef<RunEpochOwner | null>(activeRunOwner);
  activeRunOwnerRef.current = activeRunOwner;
  const ownsActiveRun = (owner: RunEpochOwner | null | undefined) =>
    !sameRunAccessScope(runAccessClosedScopeRef.current, currentRunAccessScopeRef.current) &&
    sameRunEpochOwner(owner, activeRunOwnerRef.current);
  const canApplyReservedLiveSubmission = (owner: RunEpochOwner | null | undefined) =>
    canEngineerRef.current && ownsActiveRun(owner);
  const sseEvent = runEvents.event;
  // A render can observe new active-run state before the event hook's effect
  // disposes the previous stream. Treat an SSE frame as authoritative only
  // when the hook owner, frame, and page state all name the same run.
  const sseDriving =
    runEvents.sseActive &&
    activeRun?.runId === runEvents.runId &&
    activeRun?.epoch === runEvents.epoch &&
    activeRun?.runId === sseEvent?.run_id;

  // Validation run monitor. SSE carries scalar status/stage/progress only, so
  // the run record must keep polling while active to refresh progressive UDMI
  // payload views, metrics, and issue counts.
  const validationRunQuery = useQuery({
    enabled:
      !runAccessClosed &&
      runController.phase !== "submitting" &&
      !activeRunMatchesReservedLiveSubmission &&
      !activeRunMatchesDefinitiveLiveRejection &&
      Boolean(activeRun) &&
      activeRun?.kind === "validation",
    queryFn: ({ signal }) =>
      getValidationRun(activeRun?.runId ?? "", { client: apiClient, signal }),
    queryKey: runAccessClosed
      ? [...queryKeys.workspace(sessionScopeId, workspaceRef), "run", "closed"]
      : activeRun?.ref
      ? [...queryKeys.run(sessionScopeId, workspaceRef, activeRun.ref), "epoch", activeRun.epoch]
      : [...queryKeys.workspace(sessionScopeId, workspaceRef), "run", module.route, "none"],
    refetchInterval: (query) =>
      runAccessClosed || isTerminalStatus(query.state.data?.status) ? false : 1500,
  });

  const nmapCapabilityQueryKey = [
    ...queryKeys.workspace(sessionScopeId, workspaceRef),
    "nmap-capability",
  ] as const;
  const nmapCapabilityQuery = useQuery({
    enabled: module.route === "ip-scanner-sct",
    queryFn: ({ signal }) =>
      getNmapCapability({
        projectId: workspaceRef.projectId,
        siteId: workspaceRef.siteId,
        context: { client: apiClient, signal },
      }),
    queryKey: nmapCapabilityQueryKey,
    staleTime: 15_000,
  });
  const approveNmapMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "nmap.approve_detected"),
    mutationFn: () =>
      approveDetectedNmap({
        projectId: workspaceRef.projectId,
        siteId: workspaceRef.siteId,
        context: { client: apiClient },
      }),
    onSuccess: (capability) => {
      queryClient.setQueryData(nmapCapabilityQueryKey, capability);
      const permittedProfile = resolvePermittedNmapProfile(
        nmapProfile,
        capability.permitted_profiles,
      );
      if (permittedProfile) {
        applyNmapProfile(permittedProfile);
      }
    },
  });

  const scanAuthorizationsQueryKey = queryKeys.scanAuthorizations(
    sessionScopeId,
    workspaceRef,
    scanPreviewRunId ?? undefined,
  );
  const scanAuthorizationsQuery = useQuery<ScanAuthorizationV1[]>({
    queryKey: scanAuthorizationsQueryKey,
    queryFn: () =>
      listScanAuthorizations({
        workspace: workspaceRef,
        previewRunId: scanPreviewRunId ?? undefined,
        context: { client: apiClient },
      }),
    enabled: isSealedNetworkDiscoveryModule && !scanDryRun && Boolean(scanPreviewRunId),
  });
  const authorizationNow = Date.now();
  const hasUsableScanAuthorization = (scanAuthorizationsQuery.data ?? []).some((authorization) =>
    isUsableScanAuthorization(authorization, authorizationNow),
  );
  const createScanAuthorizationMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, `${module.route}.scan-authorization.create`),
    mutationFn: () => {
      if (!scanPreviewRunId) {
        throw new Error("Run a dry preview before approving it.");
      }
      const notBefore = new Date();
      const windowHours = Number(scanAuthorizationWindowHours);
      return createScanAuthorization({
        context: { client: apiClient },
        notAfter: new Date(notBefore.getTime() + windowHours * 60 * 60 * 1000).toISOString(),
        notBefore: notBefore.toISOString(),
        previewRunId: scanPreviewRunId,
        purpose: scanAuthorizationPurpose.trim(),
        ticket: scanAuthorizationTicket.trim(),
      });
    },
    onSuccess: (authorization) => {
      setScanAuthorizationId(authorization.authorization_id);
      setScanAuthorizationTicket("");
      setScanAuthorizationPurpose("");
      queryClient.setQueryData<ScanAuthorizationV1[]>(scanAuthorizationsQueryKey, (current) => [
        authorization,
        ...(current ?? []).filter(
          (item) => item.authorization_id !== authorization.authorization_id,
        ),
      ]);
    },
  });

  // Discovery run monitor — same polling contract, against the discovery
  // status endpoint, so queued/running discovery runs update live.
  const discoveryRunQuery = useQuery({
    enabled:
      !runAccessClosed &&
      runController.phase !== "submitting" &&
      !activeRunMatchesDefinitiveLiveRejection &&
      Boolean(activeRun) &&
      activeRun?.kind === "discovery",
    queryFn: ({ signal }) => getDiscoveryRun(activeRun?.runId ?? "", { client: apiClient, signal }),
    queryKey: runAccessClosed
      ? [...queryKeys.workspace(sessionScopeId, workspaceRef), "run", "closed"]
      : activeRun?.ref
      ? [...queryKeys.run(sessionScopeId, workspaceRef, activeRun.ref), "epoch", activeRun.epoch]
      : [...queryKeys.workspace(sessionScopeId, workspaceRef), "run", module.route, "none"],
    refetchInterval: (query) => {
      if (runAccessClosed) {
        return false;
      }
      return runPollInterval({
        reachedTerminal: runEvents.reachedTerminal,
        recordTerminal: isTerminalStatus(query.state.data?.status),
        sseDriving,
      });
    },
  });

  const propertyRunQuery = useQuery({
    enabled:
      !runAccessClosed &&
      Boolean(propertyRunId) &&
      sameRunEpochOwner(propertyOwner, activeRunOwner),
    queryKey: runAccessClosed
      ? [...queryKeys.workspace(sessionScopeId, workspaceRef), "bacnet-property-run", "closed"]
      : propertyOwner && propertyRunId
        ? [
            ...queryKeys.workspace(propertyOwner.sessionScopeId, propertyOwner.workspaceRef),
            "bacnet-property-run",
            propertyOwner.runId,
            "epoch",
            propertyOwner.epoch,
            propertyRunId,
          ]
        : [...queryKeys.workspace(sessionScopeId, workspaceRef), "bacnet-property-run", "none"],
    queryFn: ({ signal }) => getDiscoveryRun(propertyRunId ?? "", { client: apiClient, signal }),
    refetchInterval: (query) =>
      runAccessClosed || isTerminalStatus(query.state.data?.status) ? false : 1500,
  });

  const propertyAuthorizationsQuery = useQuery<ScanAuthorizationV1[]>({
    enabled:
      !runAccessClosed &&
      Boolean(propertyPreviewRunId) &&
      sameRunEpochOwner(propertyOwner, activeRunOwner),
    queryKey: runAccessClosed
      ? [
          ...queryKeys.workspace(sessionScopeId, workspaceRef),
          "bacnet-property-authorizations",
          "closed",
        ]
      : propertyOwner && propertyPreviewRunId
        ? [
            ...queryKeys.workspace(propertyOwner.sessionScopeId, propertyOwner.workspaceRef),
            "bacnet-property-authorizations",
            propertyOwner.runId,
            "epoch",
            propertyOwner.epoch,
            propertyPreviewRunId,
          ]
        : [
            ...queryKeys.workspace(sessionScopeId, workspaceRef),
            "bacnet-property-authorizations",
            "none",
          ],
    queryFn: ({ signal }) =>
      listScanAuthorizations({
        workspace: propertyOwner?.workspaceRef ?? workspaceRef,
        previewRunId: propertyPreviewRunId ?? undefined,
        context: { client: apiClient, signal },
      }),
  });
  const propertyOwnerMatchesActiveRun = sameRunEpochOwner(propertyOwner, activeRunOwner);
  const propertyCancellingForActiveOwner = propertyCancelling && propertyOwnerMatchesActiveRun;

  const activeRunRecord = runAccessClosed
    ? undefined
    : activeRun?.kind === "discovery"
      ? discoveryRunQuery.data
      : validationRunQuery.data;
  // Prefer the live SSE frame for status/stage/progress; fall back to the
  // polled record for those fields and for everything else (result_summary).
  const activeRunStatus = (sseDriving ? sseEvent?.status : undefined) ?? activeRunRecord?.status;
  const scanPreviewSealed =
    (scanPreviewActive &&
      activeRun?.runId === scanPreviewRunId &&
      activeRunStatus === "succeeded") ||
    (scanAuthorizationsQuery.data ?? []).some(
      (authorization) => authorization.preview_run_id === scanPreviewRunId,
    );
  const activeRunStage = (sseDriving ? sseEvent?.stage : undefined) ?? activeRunRecord?.stage;
  const activeRunProgress =
    (sseDriving ? sseEvent?.progress_percent : undefined) ?? activeRunRecord?.progress_percent ?? 0;
  const activeRunError =
    (sseDriving ? sseEvent?.error_message : undefined) ?? activeRunRecord?.error_message;
  const activeRunTerminal = isTerminalStatus(activeRunStatus);
  const activeRunAuthoritativelyTerminal = Boolean(
    activeRun &&
      !activeRunMatchesDefinitiveLiveRejection &&
      activeRunRecord?.run_id === activeRun.runId &&
      runController.phase !== "submitting" &&
      runController.runRef?.runId === activeRun.runId &&
      runController.epoch === activeRun.epoch &&
      isTerminalStatus(activeRunRecord.status),
  );
  const activeRunAuthoritativelyTerminalRef = useRef(activeRunAuthoritativelyTerminal);
  activeRunAuthoritativelyTerminalRef.current = activeRunAuthoritativelyTerminal;
  const canApplyReportOwner = (owner: RunEpochOwner | null | undefined) =>
    canEngineerRef.current &&
    ownsActiveRun(owner) &&
    activeRunAuthoritativelyTerminalRef.current;
  const canReadActiveRunEvidence = (owner: RunEpochOwner | null | undefined) =>
    hasEvidenceReadAccessRef.current && ownsActiveRun(owner);
  const canReadTerminalActiveRunEvidence = (owner: RunEpochOwner | null | undefined) =>
    canReadActiveRunEvidence(owner) && activeRunAuthoritativelyTerminalRef.current;
  const propertyCeiling = useMemo<BacnetPropertyName[]>(() => {
    const contract = activeRunRecord?.parameters?.scan_contract_v1;
    if (!isPlainObject(contract) || !isPlainObject(contract.bacnet)) {
      return [];
    }
    const values = contract.bacnet.authorized_property_ceiling;
    if (!Array.isArray(values)) {
      return [];
    }
    return BACNET_PROPERTY_OPTIONS.filter((property) => values.includes(property));
  }, [activeRunRecord?.parameters]);
  const propertyRunState = useMemo(() => {
    const record = propertyRunQuery.data;
    if (!record) {
      return propertyRunId ? "queued" : null;
    }
    if (record.status === "succeeded") {
      return "sealed";
    }
    if (
      record.status === "failed" &&
      /authorization|expired|revoked/i.test(record.error_message ?? "")
    ) {
      return "authorization-expired";
    }
    if (propertyCancellingForActiveOwner) {
      return "cancelling";
    }
    return record.status;
  }, [propertyCancellingForActiveOwner, propertyRunId, propertyRunQuery.data]);

  // IP and BACnet rows are reconstructed from durable observation pages while
  // the run is active. MQTT retains its established topic-snapshot path. A
  // terminal legacy run without observation evidence also retains its existing
  // /results behavior, which keeps older installations and historical runs
  // readable after the additive U2 rollout.
  const progressiveObservationRun =
    activeRun?.kind === "discovery" &&
    (activeRun.ref.jobType === "ip_discovery" || activeRun.ref.jobType === "bacnet_discovery")
      ? activeRun
      : null;
  const progressiveObservationOwner = progressiveObservationRun
    ? { runId: progressiveObservationRun.runId, epoch: progressiveObservationRun.epoch }
    : null;
  const currentObservationFold =
    observationFold?.owner.runId === progressiveObservationRun?.runId &&
    observationFold?.owner.epoch === progressiveObservationRun?.epoch
      ? observationFold
      : null;
  const observationTerminalSynchronized = currentObservationFold
    ? isObservationTerminalSynchronized(currentObservationFold)
    : false;
  const terminalObservationEvidence = isPlainObject(
    activeRunRecord?.result_summary.observation_evidence_v1,
  );
  const progressiveObservationEnabled = Boolean(
    progressiveObservationRun &&
    activeRunRecord &&
    !runAccessClosed &&
    (!activeRunTerminal ||
      terminalObservationEvidence ||
      runEvents.observationAttempt !== null ||
      currentObservationFold),
  );
  const observationAfter = currentObservationFold?.acknowledgedCursor ?? 0;
  const discoveryObservationQuery = useQuery({
    enabled:
      progressiveObservationEnabled &&
      !observationTerminalSynchronized &&
      !currentObservationFold?.resnapshotRequired,
    queryFn: async ({ signal }) => {
      const owner = progressiveObservationOwner;
      if (!owner) {
        throw new Error("No active observation owner.");
      }
      const page = await getDiscoveryObservations(owner.runId, observationAfter, 100, {
        client: apiClient,
        signal,
      });
      // The API does not carry a client submission generation. Keep the owner
      // that requested this page with the response so a same-ID preview page
      // can never be folded into its accepted live successor.
      return { owner, page };
    },
    queryKey: progressiveObservationRun
      ? [
          ...queryKeys.run(sessionScopeId, workspaceRef, progressiveObservationRun.ref),
          "observations",
          "epoch",
          progressiveObservationRun.epoch,
          runEvents.observationAttempt ?? "unknown-attempt",
          observationAfter,
        ]
      : [
          ...queryKeys.workspace(sessionScopeId, workspaceRef),
          "observations",
          module.route,
          "none",
        ],
    refetchInterval: () =>
      observationTerminalSynchronized || runAccessClosed || sseDriving ? false : 1500,
  });

  // Every page is fenced to one run+attempt before its cursor can advance. The
  // server's latest_cursor and the SSE high-water are hints only; neither is
  // acknowledged until the corresponding complete page folds successfully.
  useEffect(() => {
    const observationResponse = discoveryObservationQuery.data;
    const page = observationResponse?.page;
    const run = progressiveObservationRun;
    if (
      runAccessClosed ||
      !page ||
      !run ||
      page.run_id !== run.runId ||
      observationResponse.owner.runId !== run.runId ||
      observationResponse.owner.epoch !== run.epoch
    ) {
      return;
    }
    setObservationFold((current) => {
      if (
        current &&
        current.owner.runId === run.runId &&
        current.owner.epoch === run.epoch &&
        current.attempt !== page.attempt
      ) {
        // A retry attempt starts a new namespace. Refetch from cursor zero before
        // accepting any row from it; never apply a page requested after the old
        // attempt's cursor.
        return createObservationFoldState({ runId: run.runId, epoch: run.epoch }, page.attempt);
      }
      const base =
        current && current.owner.runId === run.runId && current.owner.epoch === run.epoch
          ? current
          : createObservationFoldState(
              { runId: run.runId, epoch: run.epoch },
              runEvents.observationAttempt ?? page.attempt,
            );
      return foldObservationPage(base, page);
    });
  }, [
    discoveryObservationQuery.data,
    progressiveObservationRun,
    runAccessClosed,
    runEvents.observationAttempt,
  ]);

  useEffect(() => {
    setObservationFold(null);
  }, [activeRun?.epoch, activeRun?.runId, detailRow?.Instance]);

  useEffect(() => {
    if (
      currentObservationFold &&
      runEvents.observationAttempt !== null &&
      currentObservationFold.attempt !== runEvents.observationAttempt
    ) {
      setObservationFold(
        createObservationFoldState(currentObservationFold.owner, runEvents.observationAttempt),
      );
    }
  }, [currentObservationFold, runEvents.observationAttempt]);

  useEffect(() => {
    if (currentObservationFold?.resnapshotRequired) {
      setObservationFold(null);
    }
  }, [currentObservationFold?.resnapshotRequired]);

  useEffect(() => {
    const run = progressiveObservationRun;
    const folded = currentObservationFold;
    if (!run || !folded) {
      return;
    }
    dispatchRun({
      type: "observation-cursor-acknowledged",
      runId: run.runId,
      epoch: run.epoch,
      cursor: folded.acknowledgedCursor,
    });
    if (folded.terminal) {
      dispatchRun({
        type: "terminal-observed",
        runId: run.runId,
        epoch: run.epoch,
        terminalCursor: folded.terminal.terminal_cursor,
      });
    }
  }, [currentObservationFold, progressiveObservationRun]);

  const refetchDiscoveryObservations = discoveryObservationQuery.refetch;
  const terminalObservationCatchUpRef = useRef<string | null>(null);
  useEffect(() => {
    const terminalCatchUpKey =
      activeRunTerminal && progressiveObservationRun && !currentObservationFold?.terminal
        ? `${progressiveObservationRun.runId}:${progressiveObservationRun.epoch}:${observationAfter}`
        : null;
    const terminalCatchUpRequired =
      terminalCatchUpKey !== null &&
      !discoveryObservationQuery.isFetching &&
      !discoveryObservationQuery.data?.page.terminal &&
      terminalObservationCatchUpRef.current !== terminalCatchUpKey;
    if (
      progressiveObservationEnabled &&
      ((runEvents.latestObservationCursor !== null &&
        runEvents.latestObservationCursor > observationAfter) ||
        terminalCatchUpRequired)
    ) {
      if (terminalCatchUpKey !== null) {
        terminalObservationCatchUpRef.current = terminalCatchUpKey;
      }
      void refetchDiscoveryObservations();
    }
  }, [
    activeRunTerminal,
    currentObservationFold?.terminal,
    discoveryObservationQuery.data?.page.terminal,
    discoveryObservationQuery.isFetching,
    observationAfter,
    progressiveObservationEnabled,
    progressiveObservationRun,
    refetchDiscoveryObservations,
    runEvents.latestObservationCursor,
  ]);

  useEffect(() => {
    if (runAccessClosed) {
      const run = activeRun;
      if (run) {
        const runQueryKey = queryKeys.run(sessionScopeId, workspaceRef, run.ref);
        const resultsQueryKey = [
          ...queryKeys.results(sessionScopeId, workspaceRef, run.ref),
          "epoch",
          run.epoch,
        ] as const;
        const issuesQueryKey = [
          ...queryKeys.issues(sessionScopeId, workspaceRef, run.ref),
          "epoch",
          run.epoch,
        ] as const;
        const pointsQueryKey = [
          ...queryKeys.run(sessionScopeId, workspaceRef, run.ref),
          "bacnet-points",
          "epoch",
          run.epoch,
        ] as const;
        const comparisonQueryKey = [
          ...queryKeys.run(sessionScopeId, workspaceRef, run.ref),
          "discovery-comparison",
          "epoch",
          run.epoch,
        ] as const;
        const activeComparisonQueryKey = comparisonRunId
          ? [...comparisonQueryKey, comparisonRunId]
          : null;
        const topicsQueryKey = [...queryKeys.topics(sessionScopeId, workspaceRef, run.ref), "epoch", run.epoch] as const;
        const propertyRunQueryPrefix = [
          ...queryKeys.workspace(sessionScopeId, workspaceRef),
          "bacnet-property-run",
          run.runId,
          "epoch",
          run.epoch,
        ] as const;
        const propertyAuthorizationsQueryPrefix = [
          ...queryKeys.workspace(sessionScopeId, workspaceRef),
          "bacnet-property-authorizations",
          run.runId,
          "epoch",
          run.epoch,
        ] as const;
        void queryClient.cancelQueries({ queryKey: runQueryKey });
        void queryClient.cancelQueries({ exact: true, queryKey: resultsQueryKey });
        void queryClient.cancelQueries({ exact: true, queryKey: issuesQueryKey });
        void queryClient.cancelQueries({ queryKey: pointsQueryKey });
        void queryClient.cancelQueries({ queryKey: comparisonQueryKey });
        if (activeComparisonQueryKey) {
          void queryClient.cancelQueries({ exact: true, queryKey: activeComparisonQueryKey });
        }
        void queryClient.cancelQueries({ exact: true, queryKey: topicsQueryKey });
        void queryClient.cancelQueries({ queryKey: propertyRunQueryPrefix });
        void queryClient.cancelQueries({ queryKey: propertyAuthorizationsQueryPrefix });
        queryClient.removeQueries({ queryKey: runQueryKey });
        queryClient.removeQueries({ exact: true, queryKey: resultsQueryKey });
        queryClient.removeQueries({ exact: true, queryKey: issuesQueryKey });
        queryClient.removeQueries({ queryKey: pointsQueryKey });
        queryClient.removeQueries({ queryKey: comparisonQueryKey });
        if (activeComparisonQueryKey) {
          queryClient.removeQueries({ exact: true, queryKey: activeComparisonQueryKey });
        }
        queryClient.removeQueries({ exact: true, queryKey: topicsQueryKey });
        queryClient.removeQueries({ queryKey: propertyRunQueryPrefix });
        queryClient.removeQueries({ queryKey: propertyAuthorizationsQueryPrefix });
        queryClient.removeQueries({
          exact: true,
          queryKey: queryKeys.topics(sessionScopeId, workspaceRef, run.ref),
        });
      }
      setObservationFold(null);
      setSelectedResultId(null);
      setDetailRow(null);
      dispatchRun({ type: "reset" });
      pageHeadingRef.current?.focus();
    }
  }, [activeRun, comparisonRunId, queryClient, runAccessClosed, sessionScopeId, workspaceRef]);

  // Elapsed timer + progress presentation for the active run (ITEM-6). The run
  // monitor renders live now that a run is started in the background (ITEM-4), so
  // a stuck-at-15% bar would otherwise be the face of every run. While running,
  // the timer ticks from the run's created_at; once terminal it freezes to
  // updated_at - created_at. A bounded capture fills over its own window (never
  // claiming 100% before the terminal flip); an indefinite/unknown run shows an
  // active sweep. Clock source is the polled run record, not the SSE frame (the
  // frame carries no created_at).
  const runIsActive =
    Boolean(activeRun) &&
    !runAccessClosed &&
    !activeRunTerminal &&
    !activeRunMatchesDefinitiveLiveRejection;
  const activeRunElapsedSeconds = useElapsedSeconds(
    activeRunRecord?.created_at,
    runIsActive,
    activeRunRecord?.updated_at,
  );
  const captureSecondsParam =
    typeof activeRunRecord?.parameters?.capture_seconds === "number"
      ? (activeRunRecord.parameters.capture_seconds as number)
      : undefined;
  const boundedCapture =
    runIsActive && captureSecondsParam !== undefined && captureSecondsParam > 0;
  // Fill over the capture window while running, but never past 99% until the run
  // actually reports terminal — the real progress_percent still wins if higher.
  const progressWidth = boundedCapture
    ? Math.max(
        activeRunProgress,
        Math.min(99, (activeRunElapsedSeconds / captureSecondsParam) * 100),
      )
    : activeRunProgress;
  const progressIndeterminate = runIsActive && !boundedCapture;

  // Any confirmed nonterminal run owns this head's monitor and Stop control.
  // Restored runs block a second start too, so reloading cannot create parallel
  // work while the original run is still live.
  const startedRunActive = runIsActive && !scanPreviewActive;

  // Validation issues are replaced alongside each progressive result snapshot.
  // Poll during an active validation and stop when the polled run is terminal.
  const validationIssuesQuery = useQuery({
    enabled: !runAccessClosed && Boolean(activeRun) && activeRun?.kind === "validation",
    queryFn: ({ signal }) =>
      getValidationIssues(activeRun?.runId ?? "", { client: apiClient, signal }),
    queryKey: runAccessClosed
      ? [...queryKeys.workspace(sessionScopeId, workspaceRef), "issues", "closed"]
      : activeRun?.ref
        ? [
          ...queryKeys.issues(sessionScopeId, workspaceRef, activeRun?.ref ?? null),
          "epoch",
          activeRun.epoch,
        ]
        : [...queryKeys.workspace(sessionScopeId, workspaceRef), "issues", "none"],
    refetchInterval: () =>
      runAccessClosed || isTerminalStatus(validationRunQuery.data?.status) ? false : 1500,
  });

  // Discovery results — fetched only once the discovery run is terminal.
  const discoveryResultsQuery = useQuery({
    enabled:
      !runAccessClosed &&
      Boolean(activeRun) &&
      activeRun?.kind === "discovery" &&
      runController.phase === "settled" &&
      runController.runRef?.runId === activeRun?.runId &&
      runController.epoch === activeRun?.epoch,
    queryFn: ({ signal }) =>
      getDiscoveryResults(activeRun?.runId ?? "", { client: apiClient, signal }),
    queryKey: runAccessClosed
      ? [...queryKeys.workspace(sessionScopeId, workspaceRef), "results", "closed"]
      : activeRun?.ref
        ? [
          ...queryKeys.results(sessionScopeId, workspaceRef, activeRun?.ref ?? null),
          "epoch",
          activeRun.epoch,
        ]
        : [...queryKeys.workspace(sessionScopeId, workspaceRef), "results", "none"],
  });

  const bacnetPointsQuery = useQuery({
    enabled:
      !runAccessClosed &&
      (module.route === "bacnet-scanner" || module.route === "bacnet-discovery-sct") &&
      Boolean(activeRun) &&
      activeRun?.kind === "discovery" &&
      activeRunAuthoritativelyTerminal,
    queryKey: runAccessClosed
      ? [...queryKeys.workspace(sessionScopeId, workspaceRef), "bacnet-points", "closed"]
      : activeRun?.ref
      ? [
      ...queryKeys.run(sessionScopeId, workspaceRef, activeRun.ref),
      "bacnet-points",
      "epoch",
      activeRun.epoch,
      bacnetPointsCursor,
      bacnetPointsSearch,
        ]
      : [...queryKeys.workspace(sessionScopeId, workspaceRef), "bacnet-points", "none"],
    queryFn: ({ signal }) =>
      getDiscoveryPoints(activeRun?.runId ?? "", {
        after: bacnetPointsCursor,
        context: { client: apiClient, signal },
        limit: 100,
        search: bacnetPointsSearch || undefined,
      }),
  });

  useEffect(() => {
    setBacnetPointsCursor(null);
    setBacnetPointsSearch("");
  }, [activeRun?.epoch, activeRun?.runId]);

  useEffect(() => {
    setCaptureTopicFilter("");
  }, [activeRun?.epoch, activeRun?.runId]);

  useEffect(() => {
    setPropertyRunId(null);
    setPropertyPreviewRunId(null);
    setPropertyOwner(null);
    setPropertyAuthorizationId(null);
    setPropertyRequestedReadSet([]);
    setPropertyRequest(null);
    setPropertyExpansionNotice(null);
    setPropertyCancelling(false);
  }, [activeRun?.epoch, activeRun?.runId, detailRow?.Instance]);

  const resetGeneratedAllBundleDownloadForActiveRun = generatedAllBundleDownload.reset;
  useEffect(() => {
    setSelectedResultId(null);
    setDetailRow(null);
    setReportToast(null);
    setReportToastWarning(false);
    setGeneratedAllReportIds(null);
    resetGeneratedAllBundleDownloadForActiveRun();
    setReportDialogOpen(false);
    setReportScopeSnapshot(null);
    setReportIntents(null);
    reportIntentOwnerRef.current = null;
  }, [
    activeRun?.epoch,
    activeRun?.runId,
    canEngineer,
    resetGeneratedAllBundleDownloadForActiveRun,
    runAccessClosed,
    sessionScopeId,
    workspaceRef.projectId,
    workspaceRef.siteId,
  ]);

  const resetCaptureExportDownloadForEvidenceOwner = captureExportDownload.reset;
  const resetValidationJsonDownloadForEvidenceOwner = validationJsonDownload.reset;
  useEffect(() => {
    resetCaptureExportDownloadForEvidenceOwner();
    resetValidationJsonDownloadForEvidenceOwner();
  }, [
    activeRun?.epoch,
    activeRun?.runId,
    hasEvidenceReadAccess,
    resetCaptureExportDownloadForEvidenceOwner,
    resetValidationJsonDownloadForEvidenceOwner,
    runAccessClosed,
    sessionScopeId,
    workspaceRef.projectId,
    workspaceRef.siteId,
  ]);

  useEffect(() => {
    if (!propertyOwner) {
      return;
    }
    const owner = propertyOwner;
    const propertyRunQueryPrefix = [
      ...queryKeys.workspace(owner.sessionScopeId, owner.workspaceRef),
      "bacnet-property-run",
      owner.runId,
      "epoch",
      owner.epoch,
    ] as const;
    const propertyAuthorizationsQueryPrefix = [
      ...queryKeys.workspace(owner.sessionScopeId, owner.workspaceRef),
      "bacnet-property-authorizations",
      owner.runId,
      "epoch",
      owner.epoch,
    ] as const;
    return () => {
      if (ownsActiveRun(owner)) {
        return;
      }
      void queryClient.cancelQueries({ queryKey: propertyRunQueryPrefix });
      void queryClient.cancelQueries({ queryKey: propertyAuthorizationsQueryPrefix });
      queryClient.removeQueries({ queryKey: propertyRunQueryPrefix });
      queryClient.removeQueries({ queryKey: propertyAuthorizationsQueryPrefix });
    };
  }, [propertyOwner, queryClient]);

  const discoveryComparisonQuery = useQuery({
    enabled:
      Boolean(comparisonRunId) &&
      !runAccessClosed &&
      Boolean(activeRun) &&
      activeRun?.kind === "discovery" &&
      activeRunAuthoritativelyTerminal,
    queryKey: runAccessClosed
      ? [...queryKeys.workspace(sessionScopeId, workspaceRef), "discovery-comparison", "closed"]
      : activeRun?.ref
      ? [...queryKeys.run(sessionScopeId, workspaceRef, activeRun.ref), "discovery-comparison", "epoch", activeRun.epoch, comparisonRunId]
      : [...queryKeys.workspace(sessionScopeId, workspaceRef), "discovery-comparison", "none"],
    queryFn: ({ signal }) =>
      getDiscoveryComparison(activeRun?.runId ?? "", comparisonRunId ?? "", {
        client: apiClient,
        signal,
      }),
  });

  // Live MQTT topic snapshot for the Explorer-like capture panel (mq9nhbzu).
  // Reuses the existing per-run topics endpoint; only enabled for an MQTT
  // discovery run so it never fires on other modules.
  const captureTopicsQuery = useQuery({
    enabled:
      !runAccessClosed &&
      !activeRunMatchesReservedLiveSubmission &&
      !activeRunMatchesDefinitiveLiveRejection &&
      (module.route === "mqtt-scanner" || module.route === "mqtt-discovery-sct") &&
      Boolean(activeRun) &&
      activeRun?.kind === "discovery",
    queryFn: ({ signal }) =>
      getDiscoveryTopics(activeRun?.runId ?? "", { client: apiClient, signal }),
    queryKey: runAccessClosed
      ? [...queryKeys.workspace(sessionScopeId, workspaceRef), "topics", "closed"]
      : activeRun?.ref
      ? [...queryKeys.topics(sessionScopeId, workspaceRef, activeRun.ref), "epoch", activeRun.epoch]
      : [...queryKeys.workspace(sessionScopeId, workspaceRef), "topics", "none"],
    // Poll while the run is active so the table refreshes the instant the run
    // goes terminal (topics persist at run end), then stop polling (mq9nhbzu).
    refetchInterval: () =>
      runAccessClosed ||
      isTerminalStatus(discoveryRunQuery.data?.status) ||
      runEvents.reachedTerminal
        ? false
        : 2000,
  });

  // Reports list for the reports page (per-report selection + Export selected).
  const reportsQuery = useQuery({
    enabled: module.route === "reports",
    queryFn: ({ signal }) =>
      listReports({ limit: REPORT_PAGE_SIZE }, { client: apiClient, signal }),
    queryKey: queryKeys.reports(sessionScopeId, workspaceRef),
  });

  // Uploaded non-published UDMI schema sets, shown on the UDMI workbench only.
  const udmiSchemaSetsQuery = useQuery({
    enabled: module.route === "udmi-validation",
    queryFn: ({ signal }) => listUdmiSchemaSets({ client: apiClient, signal }),
    queryKey: queryKeys.schemaSets(sessionScopeId, workspaceRef),
  });

  // Per-row rejection reasons for the import just uploaded. The POST returns
  // counts only, so a rejected upload used to say "4 rejected" and nothing more.
  //
  // Gate on status !== "accepted", NOT rejected_rows > 0: _status()
  // (import_service.py:929-934) returns "rejected" with rejected_rows 0 for an
  // empty or missing-columns file, and that case needs the explanation most.
  // Keying on import_id makes each new upload refetch; the route-change reset
  // effect nulls importOutcome, which disables the query on navigation.
  const importErrorsQuery = useQuery({
    enabled: Boolean(importOutcome && importOutcome.status !== "accepted"),
    queryFn: ({ signal }) =>
      getImportErrors(importOutcome?.import_id ?? "", { client: apiClient, signal }),
    queryKey: queryKeys.importErrors(sessionScopeId, workspaceRef, importOutcome?.import_id),
  });

  // Server-truth "already imported" lookup for the Setup card (ISSUE-5): the
  // newest usable import of the selected type for this project/site. Drives a
  // note telling the operator a register is on file and stored server-side
  // (survives restart / DB is the source of truth), instead of the native file
  // input's permanent "No file chosen". Disabled on report-only routes (no
  // import types) and until a type is selected. A 404 resolves to null and any
  // error leaves data undefined, so the note only ever renders on a real hit.
  const latestImportQuery = useQuery({
    enabled: module.importTypes.length > 0 && selectedImportType !== "",
    queryFn: ({ signal }) =>
      getLatestImport(
        selectedImportType as ImportType,
        workspaceRef.projectId,
        workspaceRef.siteId,
        { client: apiClient, signal },
      ),
    queryKey: queryKeys.latestImport(sessionScopeId, workspaceRef, selectedImportType),
  });

  // Run retention: the page state is wiped on every navigation, so arriving at a
  // head used to look like nothing had ever run there. Ask the run store for
  // this head's own runs and re-attach one, so the monitor and results survive
  // navigating away and back.
  //
  // Now that runs execute in the background (ITEM-4), a run can still be
  // RUNNING/QUEUED when the operator refreshes or navigates away. Prefer the
  // newest non-terminal run so its LIVE monitor and Stop control re-attach; fall
  // back to the newest terminal run otherwise, including failed and cancelled
  // runs. The seed effect below marks the
  // re-attached run restored:true, so rehydration never hijacks the step — and
  // polling resumes automatically because the run monitor queries key off
  // activeRun.
  //
  // Report actions carry no run lifecycle, so they are excluded — which also
  // naturally exempts the reports route (report-only actions => no query).
  const rehydratableActions = module.runActions.filter(
    (action): action is Exclude<ModuleRunAction, { kind: "report" }> => action.kind !== "report",
  );
  const requestedRunQuery = useQuery({
    enabled: Boolean(requestedRunId && rehydratableActions.length > 0),
    queryFn: ({ signal }) =>
      isDiscoveryModule
        ? getDiscoveryRun(requestedRunId ?? "", { client: apiClient, signal })
        : getValidationRun(requestedRunId ?? "", { client: apiClient, signal }),
    queryKey: [
      ...queryKeys.workspace(sessionScopeId, workspaceRef),
      "requested-run",
      module.route,
      requestedRunId ?? "none",
    ],
    retry: (failureCount, error) =>
      !(error instanceof ApiError && error.status === 404) && failureCount < 2,
  });
  const requestedRunAction = requestedRunQuery.data
    ? (rehydratableActions.find((action) => action.jobType === requestedRunQuery.data.job_type) ??
      null)
    : null;
  const requestedRunUnavailable =
    requestedRunQuery.error instanceof ApiError && requestedRunQuery.error.status === 404;
  const requestedRunIncompatible = requestedRunQuery.isSuccess && requestedRunAction === null;

  useEffect(() => {
    if (!requestedRunId || (!requestedRunUnavailable && !requestedRunIncompatible)) {
      return;
    }
    setRunAttachmentNotice(
      "The requested run is not available in this workspace. Showing the latest accessible run.",
    );
    setScopedRunUrl(null);
  }, [requestedRunId, requestedRunIncompatible, requestedRunUnavailable, setScopedRunUrl]);

  const lastRunQuery = useQuery({
    enabled:
      rehydratableActions.length > 0 &&
      (!requestedRunId || requestedRunUnavailable || requestedRunIncompatible),
    // Keyed by route so one head's cached run can never be handed to another.
    queryKey: queryKeys.latestRun(sessionScopeId, workspaceRef, module.route),
    queryFn: async ({ signal }) => {
      const responses = await Promise.all(
        rehydratableActions.map((action) =>
          listRuns(
            {
              jobType: action.jobType,
              limit: 20,
              projectId: workspaceRef.projectId,
              siteId: workspaceRef.siteId,
            },
            { client: apiClient, signal },
          ),
        ),
      );
      return latestAttachableRun(responses.flatMap((response) => response.runs));
    },
  });

  // A terminal scalar event is only the start of completion. The controller
  // enters terminal-sync, then forces the authoritative run and evidence
  // endpoints through one barrier. Completed metrics stay hidden until every
  // response succeeds and confirms the same run identity.
  useEffect(() => {
    if (activeRun && activeRunTerminal) {
      dispatchRun({ type: "terminal-observed", runId: activeRun.runId, epoch: activeRun.epoch });
    }
  }, [activeRun, activeRunTerminal]);

  const evidenceSyncRef = useRef<number | null>(null);
  useEffect(() => {
    evidenceSyncRef.current = null;
  }, [activeRun?.epoch]);

  const refetchValidationRun = validationRunQuery.refetch;
  const refetchDiscoveryRun = discoveryRunQuery.refetch;
  const refetchValidationIssues = validationIssuesQuery.refetch;
  const refetchDiscoveryResults = discoveryResultsQuery.refetch;
  useEffect(() => {
    const run = activeRun;
    if (
      runAccessClosed ||
      !run ||
      runController.phase !== "terminal-sync" ||
      runController.runRef?.runId !== run.runId ||
      runController.epoch !== run.epoch ||
      evidenceSyncRef.current === run.epoch
    ) {
      return;
    }
    evidenceSyncRef.current = run.epoch;
    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let resolveRetry: (() => void) | null = null;

    const waitForRunStatusRetry = (delay: number) =>
      new Promise<void>((resolve) => {
        resolveRetry = resolve;
        retryTimer = setTimeout(() => {
          retryTimer = null;
          resolveRetry = null;
          resolve();
        }, delay);
      });

    void (async () => {
      try {
        let terminalRunConfirmed = false;
        for (let attempt = 0; attempt <= TERMINAL_RUN_STATUS_RETRY_DELAYS_MS.length; attempt += 1) {
          const runResult =
            run.kind === "discovery" ? await refetchDiscoveryRun() : await refetchValidationRun();
          if (disposed) {
            return;
          }
          if (runResult.data?.run_id && runResult.data.run_id !== run.runId) {
            throw new Error("Final run evidence did not match the active run.");
          }
          if (!runResult.isError && runResult.data?.run_id === run.runId) {
            if (isTerminalStatus(runResult.data.status)) {
              terminalRunConfirmed = true;
              break;
            }
          } else if (!isTransientRunStatusError(runResult.error)) {
            throw runResult.error ?? new Error("Final run status could not be refreshed.");
          }

          const delay = TERMINAL_RUN_STATUS_RETRY_DELAYS_MS[attempt];
          if (delay === undefined) {
            throw runResult.error ?? new Error("Final run status did not reach a terminal state.");
          }
          await waitForRunStatusRetry(delay);
          if (disposed) {
            return;
          }
        }

        if (!terminalRunConfirmed || disposed) {
          return;
        }

        if (run.kind === "validation") {
          const issues = await refetchValidationIssues();
          if (disposed) {
            return;
          }
          if (issues.isError || issues.data?.run_id !== run.runId) {
            throw issues.error ?? new Error("Final issues did not match the active run.");
          }
        } else {
          const results = await refetchDiscoveryResults();
          if (disposed) {
            return;
          }
          if (results.isError || results.data?.run_id !== run.runId) {
            throw new Error("Final discovery evidence did not match the active run.");
          }
        }

        if (!disposed) {
          dispatchRun({
            type: "evidence-succeeded",
            runId: run.runId,
            epoch: run.epoch,
            requirements: run.kind === "validation" ? ["run", "issues"] : ["run", "results"],
          });
        }
      } catch (cause) {
        if (!disposed) {
          dispatchRun({
            type: "evidence-failed",
            runId: run.runId,
            epoch: run.epoch,
            error: cause instanceof Error ? cause.message : "Final evidence refresh failed.",
          });
        }
      }
    })();

    return () => {
      disposed = true;
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      resolveRetry?.();
      resolveRetry = null;
    };
  }, [
    activeRun,
    refetchDiscoveryResults,
    refetchDiscoveryRun,
    refetchValidationIssues,
    refetchValidationRun,
    runAccessClosed,
    runController.phase,
    runController.epoch,
    runController.runRef?.runId,
  ]);

  const observationBarrierRequired = Boolean(
    progressiveObservationRun && (progressiveObservationEnabled || currentObservationFold),
  );
  const finalEvidenceReady =
    runController.phase === "settled" &&
    runController.runRef?.runId === activeRun?.runId &&
    runController.epoch === activeRun?.epoch &&
    (!observationBarrierRequired || observationTerminalSynchronized);
  const resetTemplateDownload = templateDownload.reset;
  const resetReportDownload = reportDownload.reset;
  const resetExportDownload = exportDownload.reset;
  const resetCaptureExportDownload = captureExportDownload.reset;
  const resetGeneratedAllBundleDownload = generatedAllBundleDownload.reset;
  const resetValidationJsonDownload = validationJsonDownload.reset;

  useEffect(() => {
    setSelectedImportType(module.importTypes[0] ?? "");
    setSelectedFile(null);
    setImportOutcome(null);
    setRunOutcome(null);
    setRunAttachmentNotice(null);
    setLastReport(null);
    setActiveRun(null);
    setReservedLiveSubmissionOwner(null);
    setDefinitiveLiveRejectionOwner(null);
    reservedLiveSubmissionOwnerRef.current = null;
    runAccessClosedScopeRef.current = null;
    setScanPreviewActive(false);
    setObservationFold(null);
    setCopyFeedback(null);
    setSelectedResultId(null);
    setResultsTextFilter("");
    setResultsToneFilter("all");
    setResultsTopicContainsFilter("");
    setResultsSystemFilter("all");
    setResultsObservationFilter("all");
    setExpandedAsset(null);
    setSelectedReportIds(new Set());
    setReportToast(null);
    setReportToastWarning(false);
    setGeneratedAllReportIds(null);
    setReportDialogOpen(false);
    setReportTitle("");
    setReportScopeSnapshot(null);
    setReportIntents(null);
    dispatchRun({ type: "reset" });
    setScanAuthorized(false);
    setScanDryRun(false);
    setScanTarget("");
    setSchemaSetLabel("");
    setSchemaSetFiles([]);
    // The capture-window control only renders on udmi-validation, but the
    // over-cap guard also blocks data-validation's UDMI run action — clear it
    // so a stale hours-scale window never disables a Run button on a page with
    // no visible input or error.
    setUdmiCaptureSeconds("");
    setUdmiCaptureUnit("seconds");
    setStep("setup");
    resetTemplateDownload();
    resetReportDownload();
    resetExportDownload();
    resetCaptureExportDownload();
    resetGeneratedAllBundleDownload();
    resetValidationJsonDownload();
  }, [
    module.route,
    module.importTypes,
    resetTemplateDownload,
    resetReportDownload,
    resetExportDownload,
    resetCaptureExportDownload,
    resetGeneratedAllBundleDownload,
    resetValidationJsonDownload,
    sessionScopeId,
    workspaceRef.projectId,
    workspaceRef.siteId,
  ]);

  // Re-attach an explicitly linked run first. Without a usable ?run= link,
  // fall back to this head's newest scoped run (see the queries above).
  //
  // THIS EFFECT MUST STAY DECLARED AFTER THE RESET EFFECT ABOVE. React runs
  // effects in declaration order, and the reset's unconditional setActiveRun(null)
  // is what stops one head's run bleeding into the next: on a route change the
  // reset nulls the old run first, and only then does this effect seed from the
  // new route's own data. Re-ordering the two would re-introduce the bleed.
  //
  // Seeding is idempotent (the activeRun guard), so StrictMode's double
  // invocation and the reset/seed two-pass flush both settle on the same run.
  useEffect(() => {
    const run = requestedRunId
      ? requestedRunAction
        ? requestedRunQuery.data
        : null
      : lastRunQuery.data;
    if (!run) {
      return;
    }
    // The query cache can hand this effect an older run first, then replace it
    // with the server's newest run after the background refetch. Replace any
    // restored seed whose identity changed, including terminal-to-terminal;
    // otherwise a just-completed run can display the previous run's metrics and
    // rows after navigation. A session-started run (restored not set) is never
    // replaced.
    const replaceRestoredRun = activeRun?.restored === true && activeRun.runId !== run.run_id;
    if (activeRun && !replaceRestoredRun) {
      return;
    }
    // Belt-and-braces on top of the route-keyed query: only ever seed a run
    // whose job type this head can actually start.
    const action =
      requestedRunAction ??
      module.runActions.find((entry) => entry.kind !== "report" && entry.jobType === run.job_type);
    if (!action || action.kind === "report") {
      return;
    }
    const ref = toRunRef(sessionScopeId, workspaceRef, module.route, run, "restored");
    const epoch = nextActiveRunEpoch();
    setActiveRun({
      epoch,
      kind: action.kind,
      ref,
      restored: true,
      runId: run.run_id,
    });
    dispatchRun({ type: "restored", runRef: ref, status: run.status, epoch });
  }, [
    activeRun,
    lastRunQuery.data,
    module.route,
    module.runActions,
    nextActiveRunEpoch,
    requestedRunAction,
    requestedRunId,
    requestedRunQuery.data,
    sessionScopeId,
    workspaceRef,
  ]);

  // Auto-clear ordinary report confirmations after a few seconds. Keep the
  // Generate All result available so its combined download is not easy to miss.
  useEffect(() => {
    if (!reportToast || generatedAllReportIds) {
      return;
    }
    const timer = setTimeout(() => {
      setReportToast(null);
      setReportToastWarning(false);
    }, 8000);
    return () => clearTimeout(timer);
  }, [generatedAllReportIds, reportToast]);

  // One native modal is shared by both report buttons. showModal supplies focus
  // containment and Escape handling in browsers; the open-attribute fallback
  // keeps the control testable in jsdom without changing production behaviour.
  useEffect(() => {
    if (!reportDialogOpen) {
      return;
    }
    const dialog = reportDialogRef.current;
    if (!dialog) {
      return;
    }
    try {
      if (!dialog.open && typeof dialog.showModal === "function") {
        dialog.showModal();
      } else if (!dialog.open) {
        dialog.setAttribute("open", "");
      }
    } catch {
      dialog.setAttribute("open", "");
    }
    const frame = window.requestAnimationFrame(() => reportTitleInputRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      if (dialog.open && typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    };
  }, [reportDialogOpen]);

  // Discovery result detail keeps the native-dialog focus and Escape contract.
  useEffect(() => {
    if (!detailRow) {
      return;
    }
    const dialog = detailDialogRef.current;
    if (!dialog) {
      return;
    }
    try {
      if (!dialog.open && typeof dialog.showModal === "function") {
        dialog.showModal();
      } else if (!dialog.open) {
        dialog.setAttribute("open", "");
      }
    } catch {
      dialog.setAttribute("open", "");
    }
    const frame = window.requestAnimationFrame(() => dialog.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      if (dialog.open && typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    };
  }, [detailRow]);

  // Step flow: advance to Run the moment a run is queued, and to Results when it
  // reaches a terminal state, so the operator follows the job rather than
  // hunting down a long page. Manual step clicks still override at any time.
  //
  // A *restored* run never moves the step: the operator arrived here to set
  // something up, and yanking them to Results for a run they did not just start
  // would be worse than the stale-looking page this retention fixes. The run
  // monitor is visible on Setup anyway (the run-controls section is in the
  // "setup run" step group) and StepNav's Results button is one click away.
  useEffect(() => {
    if (activeRun && !activeRun.restored) {
      setStep("run");
    }
  }, [activeRun]);

  // Only a *successful* run advances to Results. A failed/cancelled run is left
  // on the Run step, where the monitor shows the terminal status and
  // activeRunError — otherwise the operator would land on an empty Results view
  // with no clue why the job ended.
  useEffect(() => {
    if (
      activeRunTerminal &&
      finalEvidenceReady &&
      activeRunStatus === "succeeded" &&
      activeRun &&
      !activeRun.restored &&
      !scanPreviewActive
    ) {
      setStep("results");
    }
  }, [activeRunTerminal, activeRunStatus, activeRun, finalEvidenceReady, scanPreviewActive]);

  // field engineer's walkthrough ask (2026-07-15): when Results opens, snap to the top of
  // the page so the operator sees the headline results first, not whatever
  // mid-page scroll position the Run step left behind.
  //
  // This watches `step` rather than hooking the effect above, so one insertion
  // covers every route into Results — the auto-advance on a succeeded run, the
  // setStep("results") in runMutation's report branch, and a manual StepNav
  // click — on all five heads. A *restored* run never advances the step (see
  // above), so rehydration on arrival never snaps.
  //
  // Instant ("auto") on purpose: this is a step change, not an animation, so
  // prefers-reduced-motion needs no handling. jsdom has no scrollIntoView; the
  // test setup installs a no-op.
  useEffect(() => {
    if (step === "results") {
      heroRef.current?.scrollIntoView({ behavior: "auto", block: "start" });
    }
  }, [step]);

  const importMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, `${module.route}.import`),
    mutationFn: (input: { importType: ImportType; file: File }) =>
      createImport({
        context: { client: apiClient },
        file: input.file,
        importType: input.importType,
        projectId: workspaceRef.projectId,
        siteId: workspaceRef.siteId,
      }),
    onSuccess: (summary) => {
      setImportOutcome(summary);
      // Refresh the "already imported" note so it reflects this upload the next
      // time the file input is empty (ISSUE-5).
      void queryClient.invalidateQueries({
        queryKey: queryKeys.latestImport(sessionScopeId, workspaceRef),
      });
      // Default accepted MQTT registers to uploaded-row validation against live
      // broker payloads; both options remain editable.
      if (summary.import_type === "mqtt_register" && summary.status !== "rejected") {
        setUdmiUseRegister(true);
        setUdmiUseLiveBroker(true);
      }
    },
  });

  // Non-published UDMI schema set upload/delete. Engineer-gated in the UI; a
  // 400 (bad label / missing roots / invalid JSON) surfaces via isError below.
  const schemaUploadMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "udmi-schema.upload"),
    mutationFn: () =>
      uploadUdmiSchemaSet({
        context: { client: apiClient },
        files: schemaSetFiles,
        versionLabel: schemaSetLabel.trim(),
      }),
    onSuccess: () => {
      void udmiSchemaSetsQuery.refetch();
    },
  });

  const schemaDeleteMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "udmi-schema.delete"),
    mutationFn: (versionLabel: string) => deleteUdmiSchemaSet(versionLabel, { client: apiClient }),
    onSuccess: () => {
      void udmiSchemaSetsQuery.refetch();
    },
  });

  const fenceRunEvidence = useCallback(
    (run: ActiveRun) => {
      const pointsQueryKey = [
        ...queryKeys.run(sessionScopeId, workspaceRef, run.ref),
        "bacnet-points",
        "epoch",
        run.epoch,
      ] as const;
      const comparisonQueryKey = [
        ...queryKeys.run(sessionScopeId, workspaceRef, run.ref),
        "discovery-comparison",
        "epoch",
        run.epoch,
      ] as const;
      const observationsQueryKey = [
        ...queryKeys.run(sessionScopeId, workspaceRef, run.ref),
        "observations",
        "epoch",
        run.epoch,
      ] as const;
      const topicsQueryKey = [
        ...queryKeys.topics(sessionScopeId, workspaceRef, run.ref),
        "epoch",
        run.epoch,
      ] as const;
      const queryKeysToFence = [
        pointsQueryKey,
        comparisonQueryKey,
        observationsQueryKey,
        topicsQueryKey,
      ];

      // cancelQueries aborts its current fetch synchronously. Keep the old
      // epoch cache until observers move to the reserved epoch; removing an
      // active query here would let its still-terminal observer recreate it.
      queryKeysToFence.forEach((queryKey) => {
        void queryClient.cancelQueries({ queryKey });
      });
    },
    [queryClient, sessionScopeId, workspaceRef],
  );

  const runMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, `${module.route}.run`),
    mutationFn: async ({ actionId, dryRun }: { actionId: string; dryRun: boolean }) => {
      const action = module.runActions.find((candidate) => candidate.id === actionId);
      if (!action) {
        throw new Error("Unknown run action.");
      }

      if (action.kind === "discovery") {
        const requiresSealedPreview = action.runKind === "ip" || action.runKind === "bacnet";
        if (!requiresSealedPreview) {
          return startDiscoveryRun({
            context: { client: apiClient },
            jobType: action.jobType,
            parameters: buildDiscoveryParameters(action, {
              authorized: scanAuthorized,
              captureSeconds: captureSecondsEffective,
              captureTopicFilter,
              dryRun,
              scanPorts,
              provider: scanProvider,
              nmapProfile,
              target: scanTarget,
            }),
            runKind: action.runKind,
            workspace: workspaceRef,
          });
        }
        const parameters = buildDiscoveryParameters(action, {
          authorized: scanAuthorized,
          captureSeconds: captureSecondsEffective,
          captureTopicFilter,
          dryRun,
          scanPorts,
          targetRows: scanTargetRows,
          exclusionRows: scanExclusionRows,
          provider: scanProvider,
          nmapProfile,
          target: scanTarget,
        });
        // The same header survives a transport retry of this submission. A new
        // button press creates a deliberate new IP run.
        const idempotencyKey = action.runKind === "ip" ? newIpSubmissionKey() : undefined;
        if (dryRun) {
          return startDiscoveryPreview({
            context: { client: apiClient },
            jobType: action.jobType as "ip_discovery" | "bacnet_discovery",
            parameters: { ...parameters, dry_run: true },
            runKind: action.runKind as "ip" | "bacnet",
            workspace: workspaceRef,
            idempotencyKey,
          });
        }
        if (!scanPreviewRunId || !scanAuthorizationId) {
          throw new Error("Select a valid preview and scan authorization before starting.");
        }
        return startAuthorizedDiscoveryRun({
          context: { client: apiClient },
          jobType: action.jobType as "ip_discovery" | "bacnet_discovery",
          previewRunId: scanPreviewRunId,
          scanAuthorizationId,
          runKind: action.runKind as "ip" | "bacnet",
          workspace: workspaceRef,
          idempotencyKey,
        });
      }

      if (action.kind === "validation") {
        return startValidationRun({
          context: { client: apiClient },
          jobType: action.jobType,
          parameters:
            action.runKind === "udmi"
              ? buildUdmiValidationParameters({
                  captureSeconds: udmiCaptureSecondsEffective,
                  expectedSchedule: udmiExpectedSchedule,
                  metadataPayload: udmiMetadataPayload,
                  metadataTopic: udmiMetadataTopic,
                  pointsetPayload: udmiPointsetPayload,
                  pointsetTopic: udmiPointsetTopic,
                  statePayload: udmiStatePayload,
                  stateTopic: udmiStateTopic,
                  topicDiscoveryAllScopeConfirmed: udmiTopicDiscoveryAllScopeConfirmed,
                  topicDiscoveryEnabled: udmiTopicDiscoveryEnabled,
                  topicDiscoveryScope: udmiTopicDiscoveryScope,
                  useLiveBroker: udmiUseLiveBroker,
                  useRegister: udmiUseRegister,
                })
              : undefined,
          runKind: action.runKind,
          workspace: workspaceRef,
        });
      }

      return createReport({
        context: { client: apiClient },
        format: action.format ?? "zip",
        reportType: action.reportType,
        workspace: workspaceRef,
      });
    },
    onMutate: (variables): RunSubmissionContext => {
      // A preview and its authorized live run can share an id in local/test
      // adapters. Treat each submission as a fresh evidence barrier even when
      // the backend reuses that identifier.
      evidenceSyncRef.current = null;
      const action = module.runActions.find((candidate) => candidate.id === variables.actionId);
      const reservesAuthorizedLiveEpoch =
        action?.kind === "discovery" &&
        !variables.dryRun &&
        activeRun?.kind === "discovery" &&
        (((action.runKind === "ip" || action.runKind === "bacnet") &&
          activeRun.runId === scanPreviewRunId) ||
          (action.runKind === "mqtt" &&
            (scanPreviewActive || activeRunMatchesDefinitiveLiveRejection)));
      if (reservesAuthorizedLiveEpoch && activeRun) {
        const reservedRun = { ...activeRun, epoch: nextActiveRunEpoch(), restored: false };
        const reservedOwner: RunEpochOwner = {
          epoch: reservedRun.epoch,
          runId: reservedRun.runId,
          sessionScopeId,
          workspaceRef,
        };
        // `onMutate` completes before mutationFn starts. Canceling the prior
        // epoch here fences same-ID preview evidence even when the adapter has
        // applied the live start but has not returned its HTTP response yet.
        flushSync(() => {
          activeRunOwnerRef.current = reservedOwner;
          reservedLiveSubmissionOwnerRef.current = reservedOwner;
          setActiveRun(reservedRun);
          setReservedLiveSubmissionOwner(reservedOwner);
          setDefinitiveLiveRejectionOwner(null);
          dispatchRun({ type: "submitting" });
          setScanPreviewActive(false);
        });
        if (action.runKind === "mqtt") {
          captureExportDownload.reset();
        }
        fenceRunEvidence(activeRun);
        return { reservedOwner, reservedRun };
      }
      if (
        action?.kind === "discovery" &&
        (action.runKind === "ip" || action.runKind === "bacnet" || action.runKind === "mqtt")
      ) {
        setScanPreviewActive(variables.dryRun);
      }
      dispatchRun({ type: "submitting" });
      return {};
    },
    onError: (_error, _variables, context) => {
      const reservedOwner = context?.reservedOwner ?? reservedLiveSubmissionOwnerRef.current;
      if (reservedOwner) {
        if (!canApplyReservedLiveSubmission(reservedOwner)) {
          return;
        }
        if (isDefinitiveLiveSubmissionRejection(_error)) {
          // The server definitely rejected this live start. Leave the reserved
          // epoch unreadable so a terminal preview cannot reattach, but release
          // the controller for a fresh authorized attempt.
          reservedLiveSubmissionOwnerRef.current = null;
          setReservedLiveSubmissionOwner(null);
          setDefinitiveLiveRejectionOwner(reservedOwner);
          dispatchRun({ type: "reset" });
          return;
        }
        // A transport failure is ambiguous: the server may already have
        // accepted the live start. Keep its reserved owner in submitting so
        // the terminal preview cannot re-attach as a readable live run.
        return;
      }
      if (activeRun) {
        dispatchRun({ type: "accepted", runRef: activeRun.ref, epoch: activeRun.epoch });
      } else {
        dispatchRun({ type: "reset" });
      }
    },
    onSuccess: (result, variables, context) => {
      const reservedOwner = context?.reservedOwner;
      if (
        reservedOwner &&
        !canApplyReservedLiveSubmission(reservedOwner)
      ) {
        return;
      }
      const action = module.runActions.find((candidate) => candidate.id === variables.actionId);
      if ("run_id" in result) {
        const requiresSealedPreview =
          action?.kind === "discovery" && (action.runKind === "ip" || action.runKind === "bacnet");
        if (requiresSealedPreview && variables.dryRun) {
          setScanPreviewRunId(result.run_id);
          setScanAuthorizationId(null);
          setScanPreviewActive(true);
        } else if (requiresSealedPreview) {
          setScanPreviewActive(false);
        }
        setScopedRunUrl(result.run_id);
        setRunOutcome(`${result.message} Run ID: ${result.run_id}`);
        setLastReport(null);
        if (action?.kind === "discovery") {
          const ref =
            context?.reservedRun?.runId === result.run_id
              ? context.reservedRun.ref
              : toRunRef(sessionScopeId, workspaceRef, module.route, result, "submitted");
          const epoch = context?.reservedRun?.epoch ?? nextActiveRunEpoch();
          if (context?.reservedRun) {
            setReservedLiveSubmissionOwner(null);
            reservedLiveSubmissionOwnerRef.current = null;
            setDefinitiveLiveRejectionOwner(null);
            evidenceSyncRef.current = null;
            activeRunOwnerRef.current = { epoch, runId: result.run_id, sessionScopeId, workspaceRef };
          }
          setActiveRun({ epoch, kind: "discovery", ref, runId: result.run_id });
          dispatchRun({ type: "accepted", runRef: ref, epoch });
        } else if (action?.kind === "validation") {
          const ref = toRunRef(sessionScopeId, workspaceRef, module.route, result, "submitted");
          const epoch = nextActiveRunEpoch();
          setActiveRun({ epoch, kind: "validation", ref, runId: result.run_id });
          dispatchRun({ type: "accepted", runRef: ref, epoch });
        }
      } else {
        setLastReport(result);
        setGeneratedAllReportIds(null);
        setRunOutcome(
          `Report generated. Report ID: ${result.report_id}, file: ${result.file_name}`,
        );
        // Report linking (mqautz9j): confirm where the report lives and refresh
        // the reports list so the new report is selectable for export.
        setReportToast("Report generated — see the Reports list below to download or export it.");
        setStep("results");
        void reportsQuery.refetch();
      }
    },
  });

  const propertyExpansionMutation = useMutation({
    mutationFn: ({
      owner,
      row,
      parentRunId,
      requestedReadSet,
    }: {
      owner: RunEpochOwner;
      row: Record<string, string>;
      parentRunId: string;
      requestedReadSet: BacnetPropertyName[];
    }) => {
      const deviceInstance = Number(row.Instance);
      if (!Number.isInteger(deviceInstance) || deviceInstance < 0) {
        throw new Error("This BACnet row has no valid device instance.");
      }
      return startBacnetPropertyRun({
        context: { client: apiClient },
        parentRunId,
        deviceInstance,
        destination: row["IP Address"] || undefined,
        requestedReadSet,
        workspace: owner.workspaceRef,
      });
    },
    onMutate: ({ owner, row, parentRunId, requestedReadSet }) => {
      if (!ownsActiveRun(owner)) {
        return;
      }
      setPropertyOwner(owner);
      setPropertyRequest({
        parentRunId,
        deviceInstance: Number(row.Instance),
        destination: row["IP Address"] || undefined,
        requestedReadSet,
      });
      setPropertyExpansionNotice(null);
      setPropertyAuthorizationId(null);
    },
    onSuccess: (result, { owner }) => {
      if (!ownsActiveRun(owner)) {
        return;
      }
      setPropertyPreviewRunId(result.run_id);
      setPropertyRunId(result.run_id);
      setPropertyExpansionNotice(`Property preview ${result.run_id} was created.`);
    },
    onError: (error, { owner }) => {
      if (!ownsActiveRun(owner)) {
        return;
      }
      setPropertyExpansionNotice(
        error instanceof Error ? error.message : "Property preview failed.",
      );
    },
  });

  // bacnet-scanner live object browse: an ephemeral read of one device's live
  // object list. Unlike the built-in property expansion, it starts no child run
  // and persists nothing — the sealed scan results are unchanged.
  const objectBrowseMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "bacnet-scanner.object-browse"),
    mutationFn: ({ runId, deviceInstance }: { runId: string; deviceInstance: number }) =>
      browseBacnetScannerObjects({
        context: { client: apiClient },
        runId,
        deviceInstance,
        authorized: scanAuthorized,
      }),
    onSuccess: (result) => setObjectBrowseResult(result),
  });

  useEffect(() => {
    setObjectBrowseResult(null);
  }, [detailRow]);

  const propertyLiveMutation = useMutation({
    mutationFn: ({
      authorizationId,
      owner,
      previewRunId,
      request,
    }: {
      authorizationId: string;
      owner: RunEpochOwner;
      previewRunId: string;
      request: NonNullable<typeof propertyRequest>;
    }) => {
      return startBacnetPropertyRun({
        ...request,
        previewRunId,
        scanAuthorizationId: authorizationId,
        workspace: owner.workspaceRef,
      });
    },
    onMutate: ({ owner }) => {
      if (!ownsActiveRun(owner)) {
        return;
      }
      setPropertyExpansionNotice(null);
      setPropertyCancelling(false);
    },
    onSuccess: (result, { owner }) => {
      if (!ownsActiveRun(owner)) {
        return;
      }
      setPropertyRunId(result.run_id);
      setPropertyExpansionNotice(`Property child ${result.run_id} was accepted.`);
    },
    onError: (error, { owner }) => {
      if (!ownsActiveRun(owner)) {
        return;
      }
      setPropertyExpansionNotice(
        error instanceof Error ? error.message : "Property child start failed.",
      );
    },
  });

  const propertyCancelMutation = useMutation({
    mutationFn: ({ runId }: { owner: RunEpochOwner; runId: string }) => {
      return cancelRun(runId, { client: apiClient });
    },
    onMutate: ({ owner }) => {
      if (ownsActiveRun(owner)) {
        setPropertyCancelling(true);
      }
    },
    onSuccess: (_result, { owner }) => {
      if (!ownsActiveRun(owner)) {
        return;
      }
      void propertyRunQuery.refetch();
    },
    onError: (_error, { owner }) => {
      if (ownsActiveRun(owner)) {
        setPropertyCancelling(false);
      }
    },
    onSettled: (_result, _error, { owner }) => {
      if (ownsActiveRun(owner)) {
        setPropertyCancelling(false);
      }
    },
  });
  const propertyExpansionPending =
    propertyExpansionMutation.isPending &&
    sameRunEpochOwner(propertyExpansionMutation.variables?.owner, activeRunOwner);
  const propertyLivePending =
    propertyLiveMutation.isPending &&
    sameRunEpochOwner(propertyLiveMutation.variables?.owner, activeRunOwner);
  const propertyCancelPending =
    propertyCancelMutation.isPending &&
    sameRunEpochOwner(propertyCancelMutation.variables?.owner, activeRunOwner);

  const publishMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "mqtt-config.publish"),
    mutationFn: () =>
      startMqttConfigPublishRun({
        context: { client: apiClient },
        confirmed: publishConfirmed,
        expectedPoint: publishPoint,
        expectedValue: parsePublishValue(publishValue),
        // Confirm-back now covers every written point (mq9n11wi): pass the
        // primary plus all extra pairs as expected so the backend verifies each.
        expectedPoints: [
          { point: publishPoint, value: parsePublishValue(publishValue) },
          ...publishExtraPoints.map((pair) => ({
            point: pair.point,
            value: parsePublishValue(pair.value),
          })),
        ],
        // Compose every point/value pair (primary + extras) into one config
        // payload so a single publish writes them all.
        payload: buildMultiPointPayload(
          publishPayload,
          publishPoint,
          publishValue,
          publishExtraPoints,
        ),
        pointsetTopic: publishPointsetTopic,
        topic: publishTopic,
        useLiveBroker: publishUseLiveBroker,
        waitSeconds: Number(publishWaitSeconds) || 5,
        workspace: workspaceRef,
      }),
    onSuccess: (result) => {
      setRunOutcome(`${result.message} Run ID: ${result.run_id}`);
      const ref = toRunRef(sessionScopeId, workspaceRef, module.route, result, "submitted");
      const epoch = nextActiveRunEpoch();
      setActiveRun({ epoch, kind: "validation", ref, runId: result.run_id });
      dispatchRun({ type: "accepted", runRef: ref, epoch });
    },
  });

  const cancelMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, `${module.route}.cancel`),
    mutationFn: (runId: string) => cancelRun(runId, { client: apiClient }),
    onSuccess: () => {
      if (activeRun?.kind === "discovery") {
        void discoveryRunQuery.refetch();
      } else {
        void validationRunQuery.refetch();
      }
    },
  });

  const rollbackMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "mqtt-config.rollback"),
    mutationFn: (runId: string) => rollbackMqttConfigPublish(runId, { client: apiClient }),
    onSuccess: (result) => {
      setRunOutcome(`${result.message} Run ID: ${result.run_id}`);
    },
  });

  // Generate a report off the back of a completed run (mqautz9j), scoped to the
  // originating run via source_run_ids so the report actually traces to it.
  // Format is operator-chosen (field ask 2026-07-14: PDF and Word exports).
  const reportFromRunMutation = useMutation({
    mutationKey: mutationKeys.reports(sessionScopeId, workspaceRef),
    mutationFn: async ({
      intents,
      owner,
      reportTitle: title,
    }: {
      intents: readonly ReportIntent[];
      owner: RunEpochOwner;
      reportTitle: string;
    }) => {
      const reports: ReportSummary[] = [];
      const failedFormats: ReportFormat[] = [];
      let firstFailure: unknown;
      for (const intent of intents) {
        if (!canApplyReportOwner(owner)) {
          return { failedFormats, ownerLost: true, reports, requestedCount: intents.length };
        }
        try {
          reports.push(
            await createReport({
              context: { client: apiClient },
              format: intent.format,
              reportTitle: title,
              reportType: intent.reportType,
              sourceRunIds: [intent.runId],
              udmiReportVariant: intent.udmiReportVariant,
              udmiScope: intent.udmiScope,
              workspace: owner.workspaceRef,
            }),
          );
        } catch (error) {
          if (!canApplyReportOwner(owner)) {
            return { failedFormats, ownerLost: true, reports, requestedCount: intents.length };
          }
          firstFailure ??= error;
          failedFormats.push(intent.format);
        }
      }
      if (reports.length === 0) {
        throw firstFailure instanceof Error ? firstFailure : new Error("Report generation failed.");
      }
      return { failedFormats, ownerLost: false, reports, requestedCount: intents.length };
    },
    onSuccess: ({ failedFormats, ownerLost, reports, requestedCount }, { owner }) => {
      if (ownerLost || !canApplyReportOwner(owner)) {
        return;
      }
      const allFormatsSucceeded =
        requestedCount === ALL_REPORT_FORMATS.length &&
        failedFormats.length === 0 &&
        reports.length === ALL_REPORT_FORMATS.length;
      setGeneratedAllReportIds(
        allFormatsSucceeded ? reports.map((report) => report.report_id) : null,
      );
      setReportDialogOpen(false);
      setReportScopeSnapshot(null);
      setReportIntents(null);
      reportIntentOwnerRef.current = null;
      window.requestAnimationFrame(() => reportDialogOpenerRef.current?.focus());
      if (failedFormats.length > 0) {
        setReportToastWarning(true);
        setReportToast(
          `${reports.length} of ${requestedCount} reports were generated. Failed formats: ${failedFormats
            .map((format) => format.toUpperCase())
            .join(", ")}. The completed reports are in the Reports tab.`,
        );
      } else if (reports.length === 1) {
        setReportToastWarning(false);
        setReportToast(
          `Report generated from this run. See the Reports tab. Report ID: ${reports[0].report_id}.`,
        );
      } else {
        setReportToastWarning(false);
        setReportToast(
          `${reports.length} reports generated from this run: PDF, Word, Excel, and evidence pack. See the Reports tab.`,
        );
      }
      // The toast points at the Reports tab, so the list behind it must not be
      // stale. The reports query is disabled off the reports route, so this
      // marks it stale and it refetches when that route enables it.
      void queryClient.invalidateQueries({
        queryKey: queryKeys.reports(owner.sessionScopeId, owner.workspaceRef),
      });
    },
  });
  const reportMutationOwned = sameRunEpochOwner(
    reportFromRunMutation.variables?.owner,
    activeRunOwner,
  );
  const reportMutationPending = reportMutationOwned && reportFromRunMutation.isPending;
  const reportMutationError = reportMutationOwned ? reportFromRunMutation.error : null;

  const deleteReportsMutation = useMutation({
    mutationKey: mutationKeys.reports(sessionScopeId, workspaceRef),
    mutationFn: (reportIds: string[]) =>
      deleteReports({ reportIds, context: { client: apiClient } }),
    onMutate: () => {
      setReportDeleteNotice(null);
    },
    onSuccess: (result) => {
      const deletedIds = new Set(result.deleted_report_ids);
      const reportsQueryKey = queryKeys.reports(sessionScopeId, workspaceRef);
      const cachedReports =
        queryClient.getQueryData<ReportListResponse>(reportsQueryKey)?.reports ?? [];
      const focusIntent = reportDeleteFocusIntentRef.current;
      reportDeleteFocusIntentRef.current = null;
      let nextFocusReportId: string | null = null;
      if (focusIntent?.kind === "row") {
        const deletedIndex = cachedReports.findIndex(
          (report) => report.report_id === focusIntent.reportId,
        );
        if (deletedIndex >= 0) {
          const nextReport = cachedReports
            .slice(deletedIndex + 1)
            .find((report) => !deletedIds.has(report.report_id));
          const previousReport = cachedReports
            .slice(0, deletedIndex)
            .reverse()
            .find((report) => !deletedIds.has(report.report_id));
          nextFocusReportId = nextReport?.report_id ?? previousReport?.report_id ?? null;
        }
      }
      // The delete response is authoritative. Remove those rows immediately so
      // a failed reconciliation fetch cannot leave dead Download/Delete actions.
      queryClient.setQueryData<ReportListResponse>(reportsQueryKey, (current) =>
        current
          ? {
              ...current,
              reports: current.reports.filter((report) => !deletedIds.has(report.report_id)),
            }
          : current,
      );
      setSelectedReportIds((current) => {
        const retained = new Set(current);
        for (const reportId of deletedIds) {
          retained.delete(reportId);
        }
        return retained;
      });
      const warningCount = result.artifact_cleanup_warnings.length;
      setReportDeleteNotice(
        `Deleted ${result.deleted_count} report${result.deleted_count === 1 ? "" : "s"}.${
          warningCount > 0
            ? ` ${warningCount} artifact cleanup warning${warningCount === 1 ? "" : "s"} recorded.`
            : ""
        }`,
      );
      window.requestAnimationFrame(() => {
        const nextRowAction = nextFocusReportId
          ? reportDeleteButtonRefs.current.get(nextFocusReportId)
          : null;
        if (nextRowAction) {
          nextRowAction.focus();
          return;
        }
        const bulkAction = reportDeleteSelectedRef.current;
        if (bulkAction && !bulkAction.disabled) {
          bulkAction.focus();
          return;
        }
        reportsHeadingRef.current?.focus();
      });
      void reportsQuery.refetch();
    },
    onError: () => {
      reportDeleteFocusIntentRef.current = null;
    },
  });

  const availableProfiles =
    profilesQuery.data?.filter((profile) => module.importTypes.includes(profile.import_type)) ?? [];

  const selectedProfile = availableProfiles.find(
    (profile) => profile.import_type === selectedImportType,
  );

  const udmiCaptureUnitSeconds = { hours: 3600, minutes: 60, seconds: 1 }[udmiCaptureUnit];
  // Blank and non-numeric values pass through untouched — the existing
  // downstream parsing (blank = indefinite) keeps handling them.
  const udmiCaptureSecondsEffective =
    udmiCaptureSeconds.trim() === "" || !Number.isFinite(Number(udmiCaptureSeconds))
      ? udmiCaptureSeconds
      : String(Number(udmiCaptureSeconds) * udmiCaptureUnitSeconds);
  // 48h is the queued worker's hard time limit — a longer window would be
  // killed mid-run, so refuse it up front instead of failing after two days.
  const udmiCaptureOverCap = Number(udmiCaptureSecondsEffective) > 172_800;

  // MQTT discovery capture duration carries the same unit + 48h cap (the
  // discover_mqtt actor runs at cap + 1h). Blank/non-numeric pass through
  // unchanged so the 0-sentinel (run until stopped) convention is untouched.
  const captureUnitSeconds = { hours: 3600, minutes: 60, seconds: 1 }[captureUnit];
  const captureSecondsEffective =
    captureSeconds.trim() === "" || !Number.isFinite(Number(captureSeconds))
      ? captureSeconds
      : String(Number(captureSeconds) * captureUnitSeconds);
  const mqttCaptureOverCap = Number(captureSecondsEffective) > 172_800;

  // Run actions the Run Controls card list renders. Used ONLY to decide which
  // branch the list shows — the map below still walks the full module.runActions
  // and each dispatch uses a stable action id rather than an array position.
  const visibleRunActions = module.runActions.filter((action) => !action.hiddenFromRunControls);
  // Index of the UDMI validation run action, used by the Schedule & Payload
  // Evidence "Execute capture" button — the only visible trigger for this run
  // now that the Run Controls card is hidden (mq9n7pbe).
  //
  // Deliberately NOT clamped to 0: a -1 flows into runMutation's "Unknown run
  // action." guard, surfacing a visible error panel on this same Setup step,
  // instead of silently dispatching whatever happens to sit at index 0.
  const udmiRunActionId =
    module.runActions.find((action) => action.kind === "validation" && action.runKind === "udmi")
      ?.id ?? "udmi-validation.missing";

  // Verdicts derive from the run's issues list, so an empty list only means
  // "no issues" once the issues query has actually SUCCEEDED. Payload views
  // can land first (they ride the run record), so until then — and permanently
  // if the issues fetch fails — every verdict surface (results-table rows, the
  // row View detail, the per-asset payload sections) must stay neutral instead
  // of deriving a false green "Pass" from the empty array. Reuses the 'none'
  // verdict kind, which carries no tone class.
  const udmiIssuesSettled =
    validationIssuesQuery.isSuccess && activeRunTerminal && finalEvidenceReady;
  const gatedUdmiVerdict = useCallback(
    (issues: IssueRow[], observedPresent: boolean, assetOffline: boolean): UdmiVerdict =>
      // Keep the "Verdict pending" gate FIRST so a summary-derived offline
      // signal (which rides the run record, arriving before issues settle) can
      // never paint red ahead of the issues query — preserves the existing
      // no-false-green / no-false-red gating contract.
      udmiIssuesSettled
        ? udmiVerdictForIssues(issues, observedPresent, assetOffline)
        : { label: "Verdict pending", verdict: "none" },
    [udmiIssuesSettled],
  );

  // Live issues for ANY terminal validation run (UDMI, BACnet, mapping), not
  // only UDMI. There is deliberately NO sample fallback: validation routes used
  // to render fabricated operatorData issues (ISS-####) as findings before any
  // run existed — the last placeholder surface to survive the v0.1.13 purge.
  // Pre-run the list is empty and the inspector shows its "Run a validation"
  // empty state below.
  const retiredUnexpectedIssueIds = useMemo(
    () =>
      new Set(
        (validationIssuesQuery.data?.issues ?? [])
          .filter((issue) => issue.issue_type.toLocaleLowerCase() === "unexpected_device")
          .map((issue) => issue.issue_id)
          .filter(Boolean),
      ),
    [validationIssuesQuery.data],
  );
  const retainedValidationIssues = useMemo(
    () =>
      (validationIssuesQuery.data?.issues ?? []).filter(
        (issue) => !retiredUnexpectedIssueIds.has(issue.issue_id),
      ),
    [retiredUnexpectedIssueIds, validationIssuesQuery.data],
  );
  const liveIssues =
    activeRun?.kind === "validation" && validationIssuesQuery.data
      ? retainedValidationIssues.map(toIssueRow)
      : null;
  const visibleIssues = liveIssues ?? [];

  // Per-asset / per-payload-type grouping for UDMI live issues (mq9m4bnv).
  // Collapsed shows a cross-payload-type summary per asset; expanding an asset
  // reveals pointset/metadata/state detail. Derived only from real issue data.
  const assetIssueGroups = useMemo(() => {
    if (module.route !== "udmi-validation" || activeRun?.kind !== "validation") {
      return null;
    }
    const records = retainedValidationIssues;
    if (!records || records.length === 0) {
      return null;
    }
    return groupIssuesByAsset(records, toIssueRow);
  }, [module.route, activeRun, retainedValidationIssues]);

  // Authoritative per-payload-type expected-vs-observed payloads from the run's
  // result_summary (mq9m4bnv). Real content only (pasted/captured); never faked.
  const payloadViews = useMemo<UdmiAssetPayloadView[] | null>(() => {
    if (module.route !== "udmi-validation" || activeRun?.kind !== "validation") {
      return null;
    }
    const raw = validationRunQuery.data?.result_summary?.payload_views;
    return Array.isArray(raw) ? (raw as UdmiAssetPayloadView[]) : null;
  }, [module.route, activeRun, validationRunQuery.data]);

  // Asset ids a capture attempt did not observe. This is evidence about the run
  // window, never proof that the device was disconnected or offline. Derived
  // from real capture evidence only: issues
  // stamped issue_type "not_publishing" (the complete path — single-asset
  // capture timeouts report silence ONLY as an issue, never in the summary
  // list), unioned with result_summary.not_publishing_devices (the
  // DevicesNotPublishing path) as defensive insurance. Never inferred from
  // observed_present=false alone, so a pasted-payload run (no capture attempted)
  // never paints a device red (honesty rule).
  const notObservedAssets = useMemo<Set<string>>(() => {
    const ids = new Set<string>();
    if (module.route !== "udmi-validation" || activeRun?.kind !== "validation") {
      return ids;
    }
    for (const issue of validationIssuesQuery.data?.issues ?? []) {
      if (issue.issue_type === "not_publishing" && issue.asset_id) {
        ids.add(issue.asset_id);
      }
    }
    const summary = validationRunQuery.data?.result_summary?.not_publishing_devices;
    if (Array.isArray(summary)) {
      for (const id of summary) {
        if (typeof id === "string") {
          ids.add(id);
        }
      }
    }
    return ids;
  }, [module.route, activeRun, validationIssuesQuery.data, validationRunQuery.data]);

  const payloadViewSource =
    activeRun?.kind === "validation"
      ? (validationRunQuery.data?.result_summary?.payload_view_source as string | undefined)
      : undefined;

  // The capture window the run ACTUALLY used (capture_mode +
  // capture_window_seconds, stamped by the UDMI engine at run end). null until
  // the terminal summary lands and for runs that never attempt a capture
  // (discovery, config publish, pasted-payload-only runs).
  const captureWindow =
    activeRun?.kind === "validation" && validationRunQuery.data
      ? formatCaptureWindow(validationRunQuery.data.result_summary)
      : null;
  const captureOutcome =
    activeRun?.kind === "validation" && validationRunQuery.data
      ? formatCaptureOutcome(validationRunQuery.data.status, validationRunQuery.data.result_summary)
      : null;

  const validationSummary = useMemo(
    () => readValidationSummary(validationRunQuery.data?.result_summary),
    [validationRunQuery.data?.result_summary],
  );
  const assetTopicDiscovery = useMemo(
    () => readAssetTopicDiscovery(validationRunQuery.data?.result_summary),
    [validationRunQuery.data?.result_summary],
  );
  const validationSummaryDisplay = useMemo(
    () => buildValidationSummaryDisplay(validationSummary, retiredUnexpectedIssueIds),
    [retiredUnexpectedIssueIds, validationSummary],
  );
  const validationAssetResultsById = useMemo(
    () => new Map((validationSummary?.asset_results ?? []).map((asset) => [asset.asset_id, asset])),
    [validationSummary],
  );
  const wrongTopicAssetsById = useMemo(
    () =>
      new Map(
        (validationSummary?.wrong_topic_assets ?? []).map((asset) => [asset.asset_id, asset]),
      ),
    [validationSummary],
  );
  const hasPersistedValidationEvidence =
    validationSummary !== null ||
    (payloadViews?.length ?? 0) > 0 ||
    (validationIssuesQuery.data?.issues.length ?? 0) > 0 ||
    typeof validationRunQuery.data?.result_summary?.expected_devices === "number";

  // Merge issue groups with payload views so an asset with payloads but no
  // issues still shows, and each payload type can reveal expected vs observed.
  const mergedAssetGroups = useMemo(() => {
    if (module.route !== "udmi-validation" || activeRun?.kind !== "validation") {
      return null;
    }
    const groups = assetIssueGroups ?? [];
    const views = payloadViews ?? [];
    if (groups.length === 0 && views.length === 0) {
      return null;
    }
    return mergeAssetGroups(groups, views);
  }, [module.route, activeRun, assetIssueGroups, payloadViews]);
  const resultAssetGroups = useMemo(() => {
    if (!validationSummary) {
      // Before the versioned summary lands, payload_views and live issues are
      // the only persisted evidence available. Keep those provisional rows.
      return mergedAssetGroups;
    }
    const mergedByAsset = new Map((mergedAssetGroups ?? []).map((group) => [group.assetId, group]));
    return validationSummary.asset_results.flatMap((asset) => {
      const group = mergedByAsset.get(asset.asset_id);
      const payloadTypes = asset.payload_results
        .filter(
          (payload) =>
            payload.payload_type === "state" ||
            payload.payload_type === "metadata" ||
            payload.payload_type === "pointset",
        )
        .map((payload) => {
          const evidence = group?.payloadTypes.find(
            (entry) => entry.payloadType === payload.payload_type,
          );
          return (
            evidence ?? {
              payloadType: payload.payload_type,
              issues: [],
              expected: null,
              observed: null,
              observedPresent: payload.received,
              hasPayloadView: false,
            }
          );
        });
      return payloadTypes.length > 0
        ? [
            {
              assetId: asset.asset_id,
              system: group?.system ?? asset.system,
              issues: payloadTypes.flatMap((entry) => entry.issues),
              payloadTypes,
            },
          ]
        : [];
    });
  }, [mergedAssetGroups, validationSummary]);

  // Live UDMI results table: before a versioned summary exists, rows come from
  // persisted payload views and issues. Once it exists, its exact canonical
  // payload keys own the row set; missing rich evidence stays neutral.
  const udmiLiveResults = useMemo<{
    columns: string[];
    rows: Array<Record<string, string>>;
  } | null>(() => {
    // job_type guard: mqtt_config_publish runs share this route's run monitor;
    // only a udmi_validation run may populate the per-asset payload table.
    if (
      module.route !== "udmi-validation" ||
      validationRunQuery.data?.job_type !== "udmi_validation"
    ) {
      return null;
    }
    const expectedRows = (resultAssetGroups ?? []).flatMap((group) =>
      group.payloadTypes.map((entry) => {
        const observed = entry.hasPayloadView ? (entry.observedPresent ? "Yes" : "No") : "—";
        const payloadSummary = validationAssetResultsById
          .get(group.assetId)
          ?.payload_results.find((payload) => payload.payload_type === entry.payloadType);
        const wrongTopicPayload = wrongTopicAssetsById
          .get(group.assetId)
          ?.payloads.find((payload) => payload.payload_type === entry.payloadType);
        const topic = wrongTopicPayload?.actual_topic ?? payloadSummary?.topic;
        const topicStatus = wrongTopicPayload
          ? "Wrong topic"
          : entry.observedPresent || payloadSummary?.received
            ? "Expected topic"
            : "No topic observed";
        // Shared (issues-gated) verdict helper so the row, its View detail,
        // and the per-asset payload sections can never disagree on the verdict.
        const { label, verdict } = gatedUdmiVerdict(
          entry.issues,
          entry.observedPresent,
          notObservedAssets.has(group.assetId),
        );
        return {
          System: group.system,
          Asset: group.assetId,
          Payload: `UDMI ${entry.payloadType}`,
          Topic: topic || "—",
          "Topic status": topicStatus,
          Observed: observed,
          Issues: String(entry.issues.length),
          "Raw Payload": entry.observed ? JSON.stringify(entry.observed) : "",
          Result: label,
          // Hidden row-shading tone (not in `columns`, so it never renders as a
          // cell): "pass" | "fail" | "" — sample/discovery rows never carry it.
          __tone: udmiVerdictTone(verdict) ?? "",
          // Hidden verdict kind for the Verdict filter. On udmi the tone diverges
          // from the verdict (Non-compliant is amber, Offline is red), so the
          // filter must key off the real verdict, not the shading tone. "none"
          // collapses to "" to match the filter's "no verdict" convention.
          __verdict: verdict === "none" ? "" : verdict,
          // Raw payload type (no "UDMI " prefix, unlike the visible Payload cell)
          // so the row's evidence controls key into the inspector payload refs
          // (ITEM-D). Hidden: not in `columns`, so it never renders as a cell.
          __payloadType: entry.payloadType,
          // Non-expected payloads stay visible as evidence, but the report API
          // accepts expected schedule rows only. Keep that boundary explicit.
          __expected:
            payloadSummary !== undefined
              ? payloadSummary.expected
                ? "true"
                : "false"
              : entry.expected !== null
                ? "true"
                : "false",
          __category: "validation",
        };
      }),
    );
    const unexpectedRows = (validationSummary?.unexpected_devices ?? []).map((device) => ({
      System: "Outside register",
      Asset: device.topic_root || device.topics[0] || "Unexpected publisher",
      Payload: "Unexpected device",
      Topic: device.topics.join(", ") || device.topic_root || "Not recorded",
      "Topic status": "Outside register",
      Observed: "Yes",
      Issues: "0",
      "Raw Payload": "",
      Result: "Unexpected device",
      __tone: "warn",
      __verdict: "",
      __payloadType: "",
      __category: "unexpected-devices",
      __unexpectedId: device.id,
      __lastSeen: device.last_seen ?? "",
      __topics: device.topics.join(", "),
    }));
    const rows = [...expectedRows, ...unexpectedRows];
    if (rows.length === 0) {
      return null;
    }
    return {
      columns: [
        "System",
        "Asset",
        "Payload",
        "Topic",
        "Topic status",
        "Observed",
        "Issues",
        "Raw Payload",
        "Result",
      ],
      rows,
    };
  }, [
    module.route,
    resultAssetGroups,
    validationSummary,
    validationRunQuery.data,
    gatedUdmiVerdict,
    notObservedAssets,
    validationAssetResultsById,
    wrongTopicAssetsById,
  ]);

  // Reset the row selection when the live UDMI view replaces the sample rows so
  // the inspector never shows a stale sample-row selection against live results.
  const hasUdmiLiveResults = udmiLiveResults !== null;
  useEffect(() => {
    if (hasUdmiLiveResults) {
      setSelectedResultId(null);
      setDetailRow(null);
      // A fresh result set starts unfiltered so a stale filter never hides new
      // rows behind a "no rows match" note (ISSUE-4). The facet filters (ITEM-10)
      // join the same reset choreography for the same reason.
      setResultsTextFilter("");
      setResultsToneFilter("all");
      setResultsTopicContainsFilter("");
      setResultsSystemFilter("all");
      setResultsObservationFilter("all");
      setResultsCategoryFilter("all");
      setExpandedResultAssets(new Set());
    }
  }, [hasUdmiLiveResults]);

  // A sealed result replaces the provisional fold only after the full terminal
  // cursor has been acknowledged. Until then IP/BACnet keep rendering the
  // durable partial projection, including while the event stream reconnects.
  const provisionalDiscoveryView = useMemo(() => {
    if (runAccessClosed || !progressiveObservationRun || !currentObservationFold) {
      return null;
    }
    return provisionalDiscoveryViewFor(
      module.route,
      progressiveObservationRun.runId,
      module.route === "ip-scanner-sct" ? "ip_discovery" : "bacnet_discovery",
      currentObservationFold,
    );
  }, [currentObservationFold, module.route, progressiveObservationRun, runAccessClosed]);
  const sealedDiscoveryView = useMemo(() => {
    if (
      runAccessClosed ||
      !isDiscoveryModule ||
      !discoveryResultsQuery.data ||
      !finalEvidenceReady
    ) {
      return null;
    }
    const sealed = discoveryViewFor(module.route, discoveryResultsQuery.data);
    if (!sealed || !provisionalDiscoveryView) {
      return sealed;
    }
    const entityKeyBySignature = new Map<string, string>();
    for (const row of provisionalDiscoveryView.rows) {
      const signature = discoveryRowEntitySignature(module.route, row);
      if (signature && row.__entityKey) {
        entityKeyBySignature.set(signature, row.__entityKey);
      }
    }
    return {
      ...sealed,
      rows: sealed.rows.map<Record<string, string>>((row) => {
        const signature = discoveryRowEntitySignature(module.route, row);
        const entityKey = signature ? entityKeyBySignature.get(signature) : undefined;
        return entityKey ? { ...row, __entityKey: entityKey } : row;
      }),
    };
  }, [
    finalEvidenceReady,
    isDiscoveryModule,
    discoveryResultsQuery.data,
    module.route,
    provisionalDiscoveryView,
    runAccessClosed,
  ]);
  const discoveryView = sealedDiscoveryView ?? provisionalDiscoveryView;
  const viewingProvisionalDiscovery =
    provisionalDiscoveryView !== null && sealedDiscoveryView === null;

  // Reset only when the run identity changes. Cursor pages replace entity
  // versions in place, so clearing on every view object would throw away the
  // operator's row selection during normal progressive updates.
  useEffect(() => {
    setSelectedResultId(null);
    setResultsTextFilter("");
    setResultsToneFilter("all");
  }, [activeRun?.runId]);

  const liveMetrics = useMemo(() => {
    if (!isDiscoveryModule || !discoveryResultsQuery.data || !finalEvidenceReady) {
      return null;
    }
    return discoveryMetrics(module.route, discoveryResultsQuery.data);
  }, [finalEvidenceReady, isDiscoveryModule, discoveryResultsQuery.data, module.route]);

  const ipHeadlineMetrics = useMemo<IpHeadlineMetricDisplay[] | null>(() => {
    if (
      (module.route !== "ip-scanner" && module.route !== "ip-scanner-sct") ||
      !discoveryResultsQuery.data ||
      !finalEvidenceReady
    ) {
      return null;
    }
    try {
      return formatIpHeadlineMetrics(
        discoveryResultsQuery.data.result_summary.ip_headline_metrics_v1,
      );
    } catch {
      // Older sealed runs do not carry the additive metric snapshot. Keep the
      // legacy two-number headline for those runs instead of inventing values.
      return null;
    }
  }, [discoveryResultsQuery.data, finalEvidenceReady, module.route]);

  const bacnetHeadlineMetrics = useMemo<BacnetHeadlineMetricDisplay[] | null>(() => {
    if ((module.route !== "bacnet-scanner" && module.route !== "bacnet-discovery-sct") || !discoveryResultsQuery.data || !finalEvidenceReady) {
      return null;
    }
    try {
      return formatBacnetHeadlineMetrics(
        discoveryResultsQuery.data.result_summary.bacnet_headline_metrics_v1,
      );
    } catch {
      return null;
    }
  }, [discoveryResultsQuery.data, finalEvidenceReady, module.route]);

  // Sidecar-only router/BBMD visibility: bacnet_scanner stamps result_summary.routers
  // (the built-in engine never does). null = no router section at all (absent key);
  // [] = the scan heard no router (render the "none responded" note).
  const bacnetRouters = useMemo<BacnetRouterDisplay[] | null>(() => {
    if (module.route !== "bacnet-scanner" || !discoveryResultsQuery.data || !finalEvidenceReady) {
      return null;
    }
    return formatBacnetRouters(discoveryResultsQuery.data.result_summary.routers);
  }, [discoveryResultsQuery.data, finalEvidenceReady, module.route]);

  // BACnet-only provenance: read result_summary.backend so simulated sample
  // devices are never mistaken for a real on-wire scan. Null for other routes
  // and until a terminal run's results arrive.
  const bacnetBackend = useMemo(() => {
    if ((module.route !== "bacnet-scanner" && module.route !== "bacnet-discovery-sct") || !discoveryResultsQuery.data) {
      return null;
    }
    return bacnetBackendLabel(discoveryResultsQuery.data);
  }, [module.route, discoveryResultsQuery.data]);

  const usingLiveResults = Boolean(discoveryView) || Boolean(udmiLiveResults);
  const tableColumns =
    discoveryView?.columns ?? udmiLiveResults?.columns ?? workspace?.columns ?? [];
  // Rows come from live run results only. There is deliberately no sample-row
  // fallback here: labelling fabricated rows as a "Sample preview" was not
  // enough to stop them being read as real findings, and a head with history
  // now re-attaches its last real run (see lastRunQuery) instead. When there
  // are no rows the table renders the "No results yet" empty state below.
  // Memoised so the filtered-view useMemo below has a stable input identity (the
  // `?? []` fallback would otherwise be a fresh array every render).
  const resultRows = useMemo(
    () => discoveryView?.rows ?? udmiLiveResults?.rows ?? [],
    [discoveryView, udmiLiveResults],
  );
  // Results-table filtering (ISSUE-4). MQTT-route rows carry a Topic column, so a
  // +/# query is matched with broker wildcard semantics; every other route (and
  // any plain query) uses substring matching. Rows keep their ORIGINAL index so
  // selection and Inspector evidence never point at the wrong row.
  const resultsTopicColumn = tableColumns.includes("Topic") ? "Topic" : undefined;
  // Per-asset facts for the System and observation filters. Both come from the
  // run snapshot; no asset-id heuristic or connection-state inference is used.
  const isUdmiValidation = module.route === "udmi-validation";
  const assetFacts = useMemo(
    () => buildAssetFacts(mergedAssetGroups ?? [], validationSummary?.asset_results ?? []),
    [mergedAssetGroups, validationSummary],
  );
  const systemOptions = useMemo(() => {
    const systems = new Set<string>();
    for (const facts of assetFacts.values()) {
      systems.add(facts.system);
    }
    return Array.from(systems).sort();
  }, [assetFacts]);
  const facetFilterActive =
    isUdmiValidation &&
    (resultsSystemFilter !== "all" ||
      resultsObservationFilter !== "all" ||
      resultsCategoryFilter !== "all");
  const isResultsFilterActive =
    resultsTextFilter.trim() !== "" ||
    resultsTopicContainsFilter.trim() !== "" ||
    resultsToneFilter !== "all" ||
    facetFilterActive;
  const visibleResultRows = useMemo(
    () =>
      resultRows
        .map((row, index) => ({ index, row }))
        .filter(({ row }) => {
          if (
            !resultRowMatchesFilter(
              row,
              { text: resultsTextFilter, tone: resultsToneFilter },
              resultsTopicColumn,
            )
          ) {
            return false;
          }
          const topicNeedle = resultsTopicContainsFilter.trim().toLocaleLowerCase();
          if (
            topicNeedle &&
            !String(row.Topic ?? "")
              .toLocaleLowerCase()
              .includes(topicNeedle)
          ) {
            return false;
          }
          if (
            isUdmiValidation &&
            resultsCategoryFilter !== "all" &&
            row.__category !== resultsCategoryFilter
          ) {
            return false;
          }
          // Facet filters are a claim about the ASSET, so they apply on the
          // udmi-validation route only (other routes have no asset facts).
          // Unexpected devices have no register-backed System facet. "Observed
          // this run" remains exclusive to registered expected-topic traffic.
          if (isUdmiValidation && row.__category === "unexpected-devices") {
            return resultsSystemFilter === "all" && resultsObservationFilter === "all";
          }
          return isUdmiValidation
            ? assetMatchesFacetFilter(assetFacts.get(row.Asset), {
                system: resultsSystemFilter,
                observation: resultsObservationFilter,
              })
            : true;
        }),
    [
      resultRows,
      resultsTextFilter,
      resultsTopicContainsFilter,
      resultsToneFilter,
      resultsTopicColumn,
      isUdmiValidation,
      assetFacts,
      resultsSystemFilter,
      resultsObservationFilter,
      resultsCategoryFilter,
    ],
  );
  const currentUdmiScope = useMemo<UdmiReportScopeV1 | null>(() => {
    if (!isUdmiValidation || !isResultsFilterActive || !activeRun?.runId) {
      return null;
    }
    const seenPayloads = new Set<string>();
    const selectedPayloads: UdmiReportScopeV1["selected_payloads"] = [];
    const unexpectedDeviceIds = new Set<string>();
    for (const { row } of visibleResultRows) {
      if (row.__category === "unexpected-devices") {
        if (row.__unexpectedId) {
          unexpectedDeviceIds.add(row.__unexpectedId);
        }
        continue;
      }
      const payloadType = row.__payloadType;
      if (payloadType !== "state" && payloadType !== "metadata" && payloadType !== "pointset") {
        continue;
      }
      if (row.__expected !== "true") {
        continue;
      }
      const key = `${activeRun.runId}\u0000${row.Asset}\u0000${payloadType}`;
      if (!seenPayloads.has(key)) {
        seenPayloads.add(key);
        selectedPayloads.push({
          source_run_id: activeRun.runId,
          asset_id: row.Asset,
          payload_type: payloadType,
        });
      }
    }
    selectedPayloads.sort((left, right) =>
      `${left.asset_id}\u0000${left.payload_type}`.localeCompare(
        `${right.asset_id}\u0000${right.payload_type}`,
      ),
    );
    const verdictValues: UdmiReportScopeV1["filters"]["verdict"][] = [
      "all",
      "pass",
      "pass-notes",
      "fail",
      "offline",
      "none",
    ];
    const verdict = verdictValues.includes(
      resultsToneFilter as UdmiReportScopeV1["filters"]["verdict"],
    )
      ? (resultsToneFilter as UdmiReportScopeV1["filters"]["verdict"])
      : "all";
    return {
      schema_version: "1.0",
      selected_payloads: selectedPayloads,
      unexpected_device_ids: Array.from(unexpectedDeviceIds).sort(),
      filters: {
        text: resultsTextFilter.trim(),
        verdict,
        topic_contains: resultsTopicContainsFilter.trim(),
        system: resultsSystemFilter,
        observation:
          resultsObservationFilter === "observed" || resultsObservationFilter === "not-observed"
            ? resultsObservationFilter
            : "all",
        category: resultsCategoryFilter,
      },
    };
  }, [
    activeRun?.runId,
    isResultsFilterActive,
    isUdmiValidation,
    resultsCategoryFilter,
    resultsObservationFilter,
    resultsSystemFilter,
    resultsTextFilter,
    resultsToneFilter,
    resultsTopicContainsFilter,
    visibleResultRows,
  ]);
  // The drill-down mirrors the exact visible expected payload rows. Text,
  // verdict, category, topic, system, and observation therefore cannot leave a
  // hidden payload open in the Inspector.
  const visibleAssetGroups = useMemo(() => {
    if (!resultAssetGroups) {
      return null;
    }
    if (!isUdmiValidation) {
      return resultAssetGroups;
    }
    if (!isResultsFilterActive) {
      return resultAssetGroups;
    }
    const visiblePayloadKeys = new Set(
      visibleResultRows
        .filter(({ row }) => row.__category === "validation")
        .map(({ row }) => `${row.Asset}\u0000${row.__payloadType}`),
    );
    return resultAssetGroups.flatMap((group) => {
      const payloadTypes = group.payloadTypes.filter((payload) =>
        visiblePayloadKeys.has(`${group.assetId}\u0000${payload.payloadType}`),
      );
      return payloadTypes.length > 0
        ? [{ ...group, issues: payloadTypes.flatMap((entry) => entry.issues), payloadTypes }]
        : [];
    });
  }, [resultAssetGroups, isUdmiValidation, isResultsFilterActive, visibleResultRows]);
  const summaryFiltersActive = isUdmiValidation && isResultsFilterActive;
  const displayedValidationSummary = useMemo(
    () => filterValidationSummary(validationSummaryDisplay, currentUdmiScope, assetFacts),
    [validationSummaryDisplay, currentUdmiScope, assetFacts],
  );
  const displayedAssetTopicDiscovery = useMemo(
    () =>
      filterAssetTopicDiscovery(
        assetTopicDiscovery,
        displayedValidationSummary?.asset_results ?? [],
        summaryFiltersActive,
      ),
    [assetTopicDiscovery, displayedValidationSummary, summaryFiltersActive],
  );
  // The selected row, resolved WITHIN the filtered view so the Inspector can
  // never show a row the table is hiding (ISSUE-4): when the active selection is
  // filtered out we fall back to the first visible row, and when NOTHING matches
  // the filter the selection is null so the Inspector renders its own empty state
  // instead of a hidden row's detail (which the table simultaneously denies).
  const selectedResult =
    visibleResultRows.length === 0
      ? null
      : (
          visibleResultRows.find(
            ({ row }) => resultIdentity(module.route, row) === selectedResultId,
          ) ?? visibleResultRows[0]
        ).row;
  // In the grouped UDMI table a collapsed asset unmounts its child rows, so the
  // inspector must not keep showing a hidden row's detail (ISSUE-4). Fall back to
  // the empty state until the asset is re-expanded — selectedResult (and its
  // index) is preserved, so re-expanding restores the detail, and the
  // auto-expand-on-select effect still runs off selectedResult, not this.
  const inspectorResult =
    selectedResult && hasUdmiLiveResults && !expandedResultAssets.has(selectedResult.Asset)
      ? null
      : selectedResult;
  const selectedInspectorAssetGroup =
    inspectorResult && visibleAssetGroups
      ? (visibleAssetGroups.find((group) => group.assetId === inspectorResult.Asset) ?? null)
      : null;
  const resultDetails = inspectorResult
    ? buildResultDetailItems(module.route, inspectorResult, usingLiveResults, resultAssetGroups)
    : [];
  // Group the visible UDMI rows by asset for the collapsible summary rows
  // (ITEM-7). Render-only over visibleResultRows, so child rows keep their
  // original index and the ISSUE-4 selection/detail joins are untouched.
  const udmiRowGroups = useMemo(
    () => (hasUdmiLiveResults ? groupUdmiRowsByAsset(visibleResultRows) : []),
    [hasUdmiLiveResults, visibleResultRows],
  );
  const visibleUdmiCounts = useMemo(() => {
    const expectedAssets = new Set<string>();
    const unexpectedDevices = new Set<string>();
    for (const { row } of visibleResultRows) {
      if (row.__category === "unexpected-devices") {
        if (row.__unexpectedId) {
          unexpectedDevices.add(row.__unexpectedId);
        }
      } else if (row.__category === "validation") {
        expectedAssets.add(row.Asset);
      }
    }
    return { expectedAssets: expectedAssets.size, unexpectedDevices: unexpectedDevices.size };
  }, [visibleResultRows]);
  const toggleResultAsset = useCallback((asset: string) => {
    setExpandedResultAssets((current) => {
      const next = new Set(current);
      if (next.has(asset)) {
        next.delete(asset);
      } else {
        next.add(asset);
      }
      return next;
    });
  }, []);
  // Keep the selected row's asset expanded so the inspector never shows a row the
  // grouped table has collapsed (preserves the ISSUE-4 selection contract). Since
  // a row is always selected when rows exist, this expands the first asset by
  // default, which is the intended "the asset you're looking at is open" state.
  const selectedResultAsset = selectedResult?.Asset;
  useEffect(() => {
    if (!hasUdmiLiveResults || !selectedResultAsset) {
      return;
    }
    setExpandedResultAssets((current) =>
      current.has(selectedResultAsset) ? current : new Set(current).add(selectedResultAsset),
    );
  }, [hasUdmiLiveResults, selectedResultAsset]);
  // Verdict filter options, worded per route so the label matches what the filter
  // actually does. MQTT discovery's pass/fail is register membership. udmi keys
  // off the real verdict kind (__verdict), so each option maps to exactly one
  // verdict the Result column shows — NOT the shading tone, which conflates
  // Non-compliant (amber) with Pass-with-notes and paints Offline red. Discovery
  // (ip/bacnet) keeps the RAG pass/fail/warn tones, where tone == verdict.
  const resultsToneOptions =
    module.route === "mqtt-scanner" || module.route === "mqtt-discovery-sct"
      ? [
          { label: "All verdicts", value: "all" },
          { label: "In register", value: "pass" },
          { label: "Not in register", value: "fail" },
          { label: "No verdict", value: "none" },
        ]
      : module.route === "udmi-validation"
        ? [
            { label: "All verdicts", value: "all" },
            { label: "Pass", value: "pass" },
            { label: "Pass with notes", value: "pass-notes" },
            { label: "Non-compliant", value: "fail" },
            { label: "Not observed this run", value: "offline" },
            { label: "No verdict", value: "none" },
          ]
        : [
            { label: "All verdicts", value: "all" },
            { label: "Pass", value: "pass" },
            { label: "Fail", value: "fail" },
            { label: "Warn", value: "warn" },
            { label: "No verdict", value: "none" },
          ];

  // Keep the selected row inside the FILTERED view: if the active selection is
  // filtered out, move it to the first visible row's ORIGINAL index so the
  // Inspector never shows a row hidden from the table (ISSUE-4). Settles because
  // once the selection is visible the guard stops firing setState.
  useEffect(() => {
    if (visibleResultRows.length === 0) {
      return;
    }
    const visibleResultIds = new Set(
      visibleResultRows.map(({ row }) => resultIdentity(module.route, row)),
    );
    const firstVisibleResultId = resultIdentity(module.route, visibleResultRows[0].row);
    setSelectedResultId((current) =>
      current !== null && visibleResultIds.has(current) ? current : firstVisibleResultId,
    );
  }, [module.route, visibleResultRows]);

  // The structured DiscoveredTopic record for the selected MQTT row, matched by
  // topic (topics are distinct per run by aggregation construction). Yields the
  // real last_payload OBJECT for the inspector's JsonTree — we NEVER re-parse
  // the row's stringified "Raw Payload" cell. Null off the mqtt-discovery route,
  // before results land, or when nothing is selected.
  const selectedMqttTopic = useMemo<DiscoveryRowRecord | null>(() => {
    if ((module.route !== "mqtt-scanner" && module.route !== "mqtt-discovery-sct") || !discoveryResultsQuery.data || !selectedResult) {
      return null;
    }
    const topic = selectedResult.Topic;
    return (
      discoveryResultsQuery.data.topics.find((record) => String(record.topic) === topic) ?? null
    );
  }, [module.route, discoveryResultsQuery.data, selectedResult]);

  // Terminal empty-state: a discovery run that completed with zero rows must say
  // so explicitly — distinct from "no run yet" / "in flight" / "failed"
  // (field engineer 2026-07-15: "it can't find anything, but it doesn't really tell us").
  // Gating on activeRun keeps this composable with run rehydration: a restored
  // terminal run sets activeRun and lights this state up unchanged.
  const discoveryEmptyState =
    isDiscoveryModule &&
    activeRun &&
    activeRunTerminal &&
    finalEvidenceReady &&
    resultRows.length === 0
      ? discoveryEmptyStateFor(module.route, discoveryResultsQuery.data, activeRunError)
      : null;
  const validationEmptyState =
    module.route === "udmi-validation" && resultRows.length === 0
      ? udmiResultsEmptyState({
          error: validationRunQuery.error,
          hasRun: Boolean(activeRun),
          loading: validationRunQuery.isLoading,
          status: activeRunStatus,
        })
      : null;

  // Honest headline metrics: real numbers derived from the latest terminal run
  // (discovery, validation, or reports). When nothing has run there is no number
  // to show, so the card renders a neutral empty state — never a hardcoded
  // sample value presented as if it were a real result.
  const metricsView = useMemo<{
    primary: string;
    primaryLabel: string;
    secondary: string;
    secondaryLabel: string;
  } | null>(() => {
    if (liveMetrics) {
      if (module.route === "udmi-validation") {
        return {
          ...liveMetrics,
          secondaryLabel: "Issues",
        };
      }
      return liveMetrics;
    }
    if (
      (module.route === "udmi-validation" || module.route === "data-validation") &&
      finalEvidenceReady &&
      isTerminalStatus(validationRunQuery.data?.status)
    ) {
      if (module.route === "udmi-validation" && displayedValidationSummary) {
        return {
          primary: formatMetricPercent(
            displayedValidationSummary.asset_metrics.successfully_validated,
            displayedValidationSummary.asset_metrics.expected,
          ),
          primaryLabel: "overall compliance",
          secondary: formatMetricCount(
            displayedValidationSummary.issue_metrics.blocking +
              displayedValidationSummary.issue_metrics.warning,
          ),
          secondaryLabel: "Issues",
        };
      }
      const derived = validationMetrics(module.route, validationRunQuery.data?.result_summary);
      if (derived) {
        return derived;
      }
    }
    if (module.route === "reports" && !reportsQuery.isLoading && reportsQuery.data) {
      const reports = reportsQuery.data.reports;
      const ready = reports.filter((report) => report.status === "succeeded").length;
      return {
        primary: String(ready),
        primaryLabel: "reports ready",
        secondary: String(reports.length),
        secondaryLabel: "reports generated",
      };
    }
    return null;
  }, [
    liveMetrics,
    module.route,
    finalEvidenceReady,
    validationRunQuery.data,
    displayedValidationSummary,
    reportsQuery.isLoading,
    reportsQuery.data,
  ]);

  const activeStatusClass = activeRunStatus ? toHealthState(activeRunStatus) : "queued";
  // Mid-run device progress for the monitor (BACnet enrichment writes it into
  // result_summary.progress). Read from the polled run record — the SSE frame
  // carries only status/stage/progress_percent — so it updates on the poll
  // cadence; null when absent, so the row simply does not render.
  const runProgressText = formatRunProgress(activeRunRecord?.result_summary);
  const discoveryConnectionMessage = useMemo(() => {
    if (runAccessClosed && activeRun?.kind === "discovery") {
      return "Access changed. Live run evidence is no longer available in this workspace.";
    }
    if (!progressiveObservationRun) {
      return null;
    }
    if (currentObservationFold?.observationsQuarantined) {
      return finalEvidenceReady
        ? "Provisional observations were quarantined after an integrity check; sealed results loaded."
        : "Provisional observations were quarantined after an integrity check; sealed results are loading.";
    }
    if (currentObservationFold?.observationsPruned) {
      return finalEvidenceReady
        ? "Provisional history expired; sealed results loaded."
        : "Provisional history expired; sealed results are loading.";
    }
    if (finalEvidenceReady) {
      return "Sealed results loaded.";
    }
    const acknowledgedCursor = currentObservationFold?.acknowledgedCursor ?? 0;
    const loadedCount = currentObservationFold?.events.size ?? 0;
    const latestCursor = Math.max(
      currentObservationFold?.latestCursor ?? 0,
      runEvents.latestObservationCursor ?? 0,
    );
    if (currentObservationFold?.terminal && !observationTerminalSynchronized) {
      return `${loadedCount} terminal observations loaded. Catching up before sealed results are shown.`;
    }
    if (latestCursor > acknowledgedCursor) {
      return `${loadedCount} observations loaded. Catching up.`;
    }
    if (
      runEvents.connectionState === "reconnecting" ||
      runEvents.connectionState === "unavailable" ||
      discoveryObservationQuery.isError
    ) {
      return `Connection interrupted. Showing ${loadedCount} provisional observations; retrying.`;
    }
    if (viewingProvisionalDiscovery) {
      return `${loadedCount} provisional observations loaded. Live updates are connected.`;
    }
    return "Connecting to live observations.";
  }, [
    activeRun?.kind,
    currentObservationFold,
    discoveryObservationQuery.isError,
    finalEvidenceReady,
    observationTerminalSynchronized,
    progressiveObservationRun,
    runAccessClosed,
    runEvents.connectionState,
    runEvents.latestObservationCursor,
    viewingProvisionalDiscovery,
  ]);
  // Cancel is an engineer+ mutation; hide it entirely for lower roles so a
  // viewer/reviewer monitoring a run never sees a button that would 403.
  const canCancel =
    canEngineer && Boolean(activeRun) && Boolean(activeRunStatus) && !activeRunTerminal;
  const canDownloadValidationJson =
    activeRun?.kind === "validation" &&
    validationRunQuery.data?.job_type === "udmi_validation" &&
    isTerminalStatus(validationRunQuery.data.status) &&
    finalEvidenceReady;

  // Export wiring: report downloads use the generated artifact endpoint. A
  // terminal UDMI run can also download its persisted, versioned JSON evidence.
  const exportReport = module.route === "reports" ? lastReport : null;
  const exportEnabled = Boolean(exportReport) || canDownloadValidationJson;
  const exportTooltip = canDownloadValidationJson
    ? "Download the stored validation evidence as versioned JSON."
    : exportReport
      ? `Download ${exportReport.file_name ?? "report"}`
      : "Generate a report first to enable a real download.";

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    setSelectedFile(event.target.files?.[0] ?? null);
    setImportOutcome(null);
    // Chromium fires no change event when the same path is re-picked while the
    // input still holds it, so a corrected CSV saved over the original was
    // silently never re-read (field engineer had to rename the file to get it uploaded).
    // Clearing the value makes every pick deliver a fresh File snapshot. The
    // File captured into state above stays valid for the upload, and the staged
    // name is rendered from state since the native input now always reads
    // "No file chosen".
    event.target.value = "";
  };

  const handleImport = () => {
    if (selectedFile && selectedImportType) {
      importMutation.mutate({
        file: selectedFile,
        importType: selectedImportType,
      });
    }
  };

  const changeScanPort = (index: number, field: keyof ScanPort, value: string) => {
    setScanPorts((current) =>
      current.map((entry, entryIndex) => {
        if (entryIndex !== index) {
          return entry;
        }
        return field === "protocol"
          ? { ...entry, protocol: value as ScanPort["protocol"] }
          : { ...entry, port: value };
      }),
    );
  };

  const addScanPort = () => {
    setScanPorts((current) => [...current, { port: "", protocol: "tcp" }]);
  };

  const removeScanPort = (index: number) => {
    setScanPorts((current) => current.filter((_entry, entryIndex) => entryIndex !== index));
  };

  const addTargetRow = (exclusion: boolean) => {
    const row: IpTargetRow = {
      id: `${exclusion ? "exclude" : "target"}-${Date.now()}-${Math.random()}`,
      kind: "address",
      value: "",
    };
    (exclusion ? setScanExclusionRows : setScanTargetRows)((current) => [...current, row]);
  };

  const updateTargetRow = (
    exclusion: boolean,
    id: string,
    field: "kind" | "value" | "end",
    value: string,
  ) => {
    const setter = exclusion ? setScanExclusionRows : setScanTargetRows;
    setter((current) => current.map((row) => (row.id === id ? { ...row, [field]: value } : row)));
  };

  const removeTargetRow = (exclusion: boolean, id: string) => {
    const setter = exclusion ? setScanExclusionRows : setScanTargetRows;
    setter((current) => current.filter((row) => row.id !== id));
  };

  const changeNmapProfile = (profile: NmapProfileName) => {
    applyNmapProfile(profile);
  };

  const changeScanProvider = (provider: IPDiscoveryProvider) => {
    setScanProvider(provider);
    if (provider === "builtin_tcp_connect") {
      setScanPorts((current) => current.map((entry) => ({ ...entry, protocol: "tcp" as const })));
      return;
    }
    const permittedProfiles = nmapCapabilityQuery.data?.permitted_profiles ?? [];
    const permittedProfile = resolvePermittedNmapProfile(nmapProfile, permittedProfiles);
    if (permittedProfile) {
      changeNmapProfile(permittedProfile);
    }
  };

  const addExtraPublishPoint = () => {
    setPublishExtraPoints((current) => [...current, { point: "", value: "" }]);
  };

  const changeExtraPublishPoint = (index: number, field: keyof PointValuePair, value: string) => {
    setPublishExtraPoints((current) =>
      current.map((entry, entryIndex) =>
        entryIndex === index ? { ...entry, [field]: value } : entry,
      ),
    );
  };

  const removeExtraPublishPoint = (index: number) => {
    setPublishExtraPoints((current) =>
      current.filter((_entry, entryIndex) => entryIndex !== index),
    );
  };

  const handleExport = () => {
    if (canDownloadValidationJson) {
      handleValidationJsonDownload();
      return;
    }
    if (!exportReport) {
      return;
    }
    void exportDownload.download({
      fallbackFilename:
        exportReport.file_name || `${exportReport.report_id}.${exportReport.output_format}`,
      key: "export",
      path: getReportDownloadPath(exportReport.report_id),
    });
  };

  // All reports remain selectable for deletion. Export derives its own subset
  // because only succeeded reports have bytes behind the download endpoint.
  const liveReports = reportsQuery.data?.reports ?? [];
  const hasUdmiReports = liveReports.some((report) => report.report_type === "udmi_validation");
  const downloadableReports = liveReports.filter((report) => report.status === "succeeded");
  const selectedReports = liveReports.filter((report) => selectedReportIds.has(report.report_id));
  const selectedDownloadableReports = downloadableReports.filter((report) =>
    selectedReportIds.has(report.report_id),
  );

  const toggleReportSelection = (reportId: string) => {
    setSelectedReportIds((current) => {
      const next = new Set(current);
      if (next.has(reportId)) {
        next.delete(reportId);
      } else {
        next.add(reportId);
      }
      return next;
    });
  };

  // Export selected reports (mqatcqb3). One ticked report downloads directly;
  // multiple bundle into a single zip via one fetch — a per-file download loop
  // tripped the browser's per-gesture throttle and kept only one file.
  const handleExportSelected = async () => {
    const chosen = selectedDownloadableReports;
    if (chosen.length === 0) {
      return;
    }
    if (chosen.length === 1) {
      const [report] = chosen;
      await exportDownload.download({
        fallbackFilename: report.file_name || `${report.report_id}.${report.output_format}`,
        key: `selected-${report.report_id}`,
        path: getReportDownloadPath(report.report_id),
      });
      return;
    }
    await exportDownload.download({
      fallbackFilename: "reports_export.zip",
      init: {
        body: JSON.stringify({ report_ids: chosen.map((report) => report.report_id) }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      },
      key: "selected-zip",
      path: REPORTS_EXPORT_PATH,
    });
  };

  const handleDownloadGeneratedAllReports = () => {
    const owner = activeRunOwner;
    if (
      generatedAllReportIds?.length !== ALL_REPORT_FORMATS.length ||
      !owner ||
      !canApplyReportOwner(owner)
    ) {
      return;
    }
    void generatedAllBundleDownload.download({
      fallbackFilename: "reports_export.zip",
      init: {
        body: JSON.stringify({ report_ids: generatedAllReportIds }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      },
      isCurrent: () => canApplyReportOwner(owner),
      key: "generated-all-zip",
      path: REPORTS_EXPORT_PATH,
    });
  };

  const handleDeleteReports = (
    reports: ReportSummary[],
    focusIntent: { kind: "bulk" } | { kind: "row"; reportId: string },
  ) => {
    if (!canEngineer || reports.length === 0 || deleteReportsMutation.isPending) {
      return;
    }
    const reportIds = Array.from(new Set(reports.map((report) => report.report_id)));
    const description =
      reportIds.length === 1
        ? reports[0].report_title?.trim() || reports[0].file_name || reportIds[0]
        : `${reportIds.length} selected reports`;
    const confirmed = window.confirm(
      `Delete ${description}? Generated report records and their local artifacts will be removed. Source runs remain intact.`,
    );
    if (!confirmed) {
      return;
    }
    reportDeleteFocusIntentRef.current = focusIntent;
    deleteReportsMutation.mutate(reportIds);
  };

  // "Generate report from this run" affordance shown on a terminal validation/
  // discovery run (mqautz9j). Scopes the report to the originating run id via
  // source_run_ids so the report traces back to it.
  const handleGenerateReportFromRun = (opener: HTMLButtonElement) => {
    const runId = activeRun?.runId;
    if (!canEngineer || runAccessClosed || !runId || !activeRunAuthoritativelyTerminal) {
      return;
    }
    setReportToast(null);
    setReportToastWarning(false);
    setGeneratedAllReportIds(null);
    generatedAllBundleDownload.reset();
    const reportType: ReportType =
      activeRun.kind === "discovery"
        ? ((module.route === "ip-scanner" || module.route === "ip-scanner-sct"
            ? "ip_discovery"
            : module.route === "bacnet-scanner" || module.route === "bacnet-discovery-sct"
              ? "bacnet_discovery"
              : "mqtt_discovery") as ReportType)
        : validationRunQuery.data?.job_type === "udmi_validation"
          ? "udmi_validation"
          : "issue_report";
    let frozenScope: UdmiReportScopeV1 | undefined;
    if (module.route === "udmi-validation") {
      // Freeze a deep copy when the operator opens the naming dialog. Filter
      // controls can update while a report request is being prepared; reading
      // currentUdmiScope at submit time could then generate a different report
      // from the one the operator reviewed before clicking Generate.
      const scope = currentUdmiScope
        ? {
            ...currentUdmiScope,
            selected_payloads: currentUdmiScope.selected_payloads.map((payload) => ({
              ...payload,
            })),
            unexpected_device_ids: [...currentUdmiScope.unexpected_device_ids],
            filters: { ...currentUdmiScope.filters },
          }
        : null;
      frozenScope = scope ?? undefined;
      setReportScopeSnapshot({
        scope,
        filtered: scope !== null,
        expectedAssets: scope
          ? new Set(scope.selected_payloads.map((payload) => payload.asset_id)).size
          : (displayedValidationSummary?.asset_metrics.expected ?? 0),
        expectedPayloads:
          scope?.selected_payloads.length ??
          displayedValidationSummary?.payload_metrics.expected ??
          0,
        unexpectedDevices:
          scope?.unexpected_device_ids.length ??
          displayedValidationSummary?.asset_metrics.unexpected ??
          0,
      });
    } else {
      setReportScopeSnapshot(null);
    }
    const formats = reportExportFormat === "all" ? ALL_REPORT_FORMATS : [reportExportFormat];
    setReportIntents(
      formats.map((format) =>
        createReportIntent({
          format,
          reportType,
          runId,
          ...(reportType === "udmi_validation" ? { udmiReportVariant } : {}),
          ...(frozenScope ? { udmiScope: frozenScope } : {}),
        }),
      ),
    );
    reportIntentOwnerRef.current = activeRun
      ? { epoch: activeRun.epoch, runId: activeRun.runId, sessionScopeId, workspaceRef }
      : null;
    reportDialogOpenerRef.current = opener;
    setReportTitle(defaultReportTitle(activeRunRecord));
    reportFromRunMutation.reset();
    setReportDialogOpen(true);
  };

  const closeReportDialog = () => {
    setReportDialogOpen(false);
    setReportScopeSnapshot(null);
    setReportIntents(null);
    reportIntentOwnerRef.current = null;
    window.requestAnimationFrame(() => reportDialogOpenerRef.current?.focus());
  };

  const handleReportDialogSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const title = reportTitle.trim();
    const owner = reportIntentOwnerRef.current;
    if (
      !reportIntents ||
      !title ||
      title.length > 160 ||
      !canEngineer ||
      runAccessClosed ||
      !activeRunAuthoritativelyTerminal ||
      !activeRun ||
      !owner ||
      !sameRunEpochOwner(owner, activeRunOwner)
    ) {
      return;
    }
    reportFromRunMutation.mutate({
      intents: reportIntents,
      owner,
      reportTitle: title,
    });
  };

  const handleValidationJsonDownload = () => {
    const run = validationRunQuery.data;
    const owner = activeRunOwner;
    if (
      !owner ||
      !canDownloadValidationJson ||
      !canReadTerminalActiveRunEvidence(owner) ||
      !run ||
      run.run_id !== owner.runId ||
      run.job_type !== "udmi_validation" ||
      !isTerminalStatus(run.status)
    ) {
      return;
    }
    void validationJsonDownload.download({
      fallbackFilename: `udmi-validation-${run.run_id}.json`,
      isCurrent: () => canReadTerminalActiveRunEvidence(owner),
      key: "validation-json",
      path: getValidationJsonExportPath(run.run_id),
    });
  };

  const renderGeneratedAllReportDownload = () => {
    if (generatedAllReportIds?.length !== ALL_REPORT_FORMATS.length) {
      return null;
    }
    return (
      <div className="inline-actions">
        <button
          className="secondary-button compact"
          disabled={generatedAllBundleDownload.pendingKey === "generated-all-zip"}
          onClick={handleDownloadGeneratedAllReports}
          title="Download the PDF, Word, Excel, and evidence pack together in one ZIP file."
          type="button"
        >
          {generatedAllBundleDownload.pendingKey === "generated-all-zip"
            ? "Preparing download..."
            : "Download all reports (.zip)"}
        </button>
        {generatedAllBundleDownload.error && (
          <span className="error-text">
            Combined report download failed: {generatedAllBundleDownload.error}
          </span>
        )}
      </div>
    );
  };

  // Latest payload per topic for the MQTT Explorer-like capture (mq9nhbzu),
  // filtered by the wildcard topic filter and built from the live topics
  // snapshot. No payloads are fabricated — empty until a real run reports them.
  const captureRows = useMemo(() => {
    if (runAccessClosed) {
      return [];
    }
    const topics = captureTopicsQuery.data?.topics ?? [];
    return topics
      .map((topic) => mqttCaptureRow(topic))
      .filter((row) => matchesTopicFilter(row.topic, captureTopicFilter));
  }, [captureTopicsQuery.data, captureTopicFilter, runAccessClosed]);

  const handleCaptureExport = () => {
    if (captureRows.length === 0) {
      return;
    }
    const csv = captureRowsToCsv(captureRows);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    triggerBlobDownload(blob, `mqtt-capture-${Date.now()}.csv`);
  };

  // Excel export (mq9nhbzu): the XLSX is generated server-side (openpyxl, same
  // as reports/templates) and pulled through the authenticated download helper,
  // scoped to the active run and the current topic filter.
  const handleCaptureExportXlsx = () => {
    const owner = activeRunOwner;
    if (
      captureRows.length === 0 ||
      !owner ||
      !canReadActiveRunEvidence(owner) ||
      activeRunMatchesReservedLiveSubmission ||
      activeRunMatchesDefinitiveLiveRejection ||
      activeRun?.kind !== "discovery" ||
      (module.route !== "mqtt-scanner" && module.route !== "mqtt-discovery-sct")
    ) {
      return;
    }
    void captureExportDownload.download({
      fallbackFilename: `mqtt-capture-${Date.now()}.xlsx`,
      isCurrent: () =>
        !activeRunMatchesReservedLiveSubmission &&
        !activeRunMatchesDefinitiveLiveRejection &&
        canReadActiveRunEvidence(owner),
      key: "capture-xlsx",
      path: getDiscoveryTopicsXlsxPath(owner.runId, captureTopicFilter),
    });
  };

  const handleCopyPayload = async (payload: string, label: string) => {
    try {
      await navigator.clipboard.writeText(payload);
      setCopyFeedback({ message: `${label} payload copied.`, severity: "success" });
    } catch {
      setCopyFeedback({
        message: "Could not copy payload in this browser context.",
        severity: "warning",
      });
    }
  };

  // For real (non-dry-run) discovery the operator must confirm authorization.
  const nmapAvailable =
    nmapCapabilityQuery.data?.state === "available" &&
    nmapCapabilityQuery.data.process_selection_allowed;
  const nmapSelectionBlocked =
    module.route === "ip-scanner-sct" && scanProvider === "operator_managed_nmap" && !nmapAvailable;
  const discoveryBlocked =
    (isSealedNetworkDiscoveryModule &&
      !scanDryRun &&
      (!scanAuthorized || !scanPreviewRunId || !scanAuthorizationId)) ||
    (module.route === "mqtt-discovery-sct" && !scanDryRun && !scanAuthorized) ||
    nmapSelectionBlocked;

  // Import warnings are informational (their rows stay accepted), so they get
  // their own amber panel below the outcome — never the red error styling.
  const importWarnings = importOutcome?.warnings ?? [];

  // Rejection reasons for the red panel. When the summary already names the
  // missing columns on its own line, the per-column missing_required_column
  // records (import_service.py:698-706) would repeat it verbatim as bullets —
  // drop them there only, so the reasons stay complete but nothing is said twice.
  const importErrors = (importErrorsQuery.data?.errors ?? []).filter(
    (error) =>
      error.code !== "missing_required_column" ||
      (importOutcome?.missing_columns.length ?? 0) === 0,
  );
  const visibleImportErrors = importErrors.slice(0, IMPORT_ERROR_DISPLAY_CAP);
  const hiddenImportErrorCount = Math.max(importErrors.length - IMPORT_ERROR_DISPLAY_CAP, 0);

  const jumpToPayloadComparison = useCallback((payloadKey: string) => {
    const target = payloadComparisonControlRefs.current.get(payloadKey);
    if (!target) {
      return;
    }
    const reducedMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target.focus({ preventScroll: true });
    target.scrollIntoView({
      behavior: reducedMotion ? "auto" : "smooth",
      block: "center",
    });
  }, []);

  // Selecting a live-UDMI result through its Select/View controls opens the
  // matching asset in the inspector and scrolls to that payload type's issues
  // (ITEM-D), so the inspector — now beside the table — surfaces exactly which
  // issues flagged the row. Guarded to live-UDMI rows: they carry an Issues
  // count and a hidden __payloadType; discovery rows have neither and no
  // inspector payload groups, so this no-ops for them.
  const focusInspectorPayload = (row: Record<string, string>) => {
    if (row.Issues === undefined || row.__category === "unexpected-devices") {
      return;
    }
    setExpandedAsset(row.Asset);
    const key = `${row.Asset}:${row.__payloadType ?? ""}`;
    // The payload-type-group mounts only once its asset expands, so wait a frame
    // for that re-render before scrolling to the freshly-stamped node.
    requestAnimationFrame(() => {
      const reducedMotion =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const target = payloadGroupRefs.current.get(key);
      if (!target) {
        return;
      }
      target.focus({ preventScroll: true });
      target.scrollIntoView({
        behavior: reducedMotion ? "auto" : "smooth",
        block: "nearest",
      });
    });
  };

  const selectResultEvidence = (row: Record<string, string>) => {
    setSelectedResultId(resultIdentity(module.route, row));
    focusInspectorPayload(row);
  };

  const closeResultDetailDialog = () => {
    setDetailRow(null);
    window.requestAnimationFrame(() => detailDialogOpenerRef.current?.focus());
  };

  // One results-table data row. Shared by the flat (discovery) render and the
  // grouped-by-asset render (ITEM-7) so the two can never drift. Selection is
  // keyed to the evidence fields, so a response reorder cannot move it.
  const renderResultRow = ({ row, index }: { row: Record<string, string>; index: number }) => {
    const evidenceId = resultIdentity(module.route, row);
    const rowId = `${evidenceId}:${index}`;
    const selected = selectedResultId === evidenceId;
    // Live-UDMI rows carry a real issue count; name it on the View affordance so
    // the button reads as "holds N issues", not a bare "View" (ITEM-D). Honest:
    // the count is only claimed when the row actually has issues.
    const issueCount = row.Issues === undefined ? 0 : Number(row.Issues);
    const viewLabel =
      issueCount > 0 ? `View ${issueCount} issue${issueCount === 1 ? "" : "s"}` : "View";
    const evidenceLabel = [row.Asset, row.__payloadType || row.Payload || row.Topic]
      .filter(Boolean)
      .join(" ");
    return (
      <tr
        aria-selected={isUdmiValidation ? selected : undefined}
        className={
          `${row.__tone ? `row-${row.__tone}` : ""}${selected ? " row-selected" : ""}${
            isUdmiValidation ? " result-row-selectable" : ""
          }`.trim() || undefined
        }
        key={rowId}
        onClick={isUdmiValidation ? () => selectResultEvidence(row) : undefined}
      >
        {tableColumns.map((column) => (
          <td key={column}>
            {isUdmiValidation && column === "Asset" ? (
              <button
                aria-pressed={selected}
                className="result-asset-button"
                onClick={(event) => {
                  event.stopPropagation();
                  selectResultEvidence(row);
                }}
                type="button"
              >
                {renderCell(row, column, handleCopyPayload)}
              </button>
            ) : (
              renderCell(row, column, handleCopyPayload)
            )}
          </td>
        ))}
        <td>
          <div className="result-row-actions">
            <button
              aria-description={evidenceLabel ? `Evidence for ${evidenceLabel}` : "Evidence row"}
              aria-pressed={selected}
              className={`secondary-button compact${selected ? " selected" : ""}`}
              onClick={(event) => {
                event.stopPropagation();
                selectResultEvidence(row);
              }}
              type="button"
            >
              {isUdmiValidation ? viewLabel : "Select evidence"}
            </button>
            {!isUdmiValidation && (
              <button
                aria-description={evidenceLabel ? `Evidence for ${evidenceLabel}` : "Evidence row"}
                className="secondary-button compact"
                onClick={(event) => {
                  selectResultEvidence(row);
                  detailDialogOpenerRef.current = event.currentTarget;
                  setDetailRow(row);
                }}
                type="button"
              >
                {viewLabel}
              </button>
            )}
          </div>
        </td>
      </tr>
    );
  };

  return (
    <div className="app-page">
      <section
        aria-label="Current run summary"
        className={`module-hero${isUdmiValidation ? " module-hero-workbench" : ""}`}
        ref={heroRef}
      >
        {isUdmiValidation ? (
          <div className="module-hero-copy">
            <h1>{workspace?.title ?? module.title}</h1>
            <p>{workspace?.headline ?? module.summary}</p>
          </div>
        ) : (
          <h2 className="visually-hidden" ref={pageHeadingRef} tabIndex={-1}>
            {workspace?.title ?? module.title}
          </h2>
        )}
        <div className="module-metrics">
          {metricsView ? (
            <>
              <article>
                <strong>{metricsView.primary}</strong>
                <span>{metricsView.primaryLabel}</span>
              </article>
              <article>
                <strong>{metricsView.secondary}</strong>
                <span>{isUdmiValidation ? "Issues" : metricsView.secondaryLabel}</span>
              </article>
            </>
          ) : (
            <article className="module-metrics-empty">
              <strong>—</strong>
              <span>
                {module.route === "reports"
                  ? reportsQuery.isLoading
                    ? "Loading reports..."
                    : "No reports yet"
                  : "No run yet"}
              </span>
            </article>
          )}
        </div>
        {ipHeadlineMetrics && (
          <section className="ip-headline-metrics" aria-label="IP scan headline metrics">
            {ipHeadlineMetrics.map((metric) => (
              <article key={metric.heading}>
                <strong>{metric.value}</strong>
                <span>{metric.heading}</span>
                {metric.progress && <small>{metric.progress}</small>}
              </article>
            ))}
          </section>
        )}
        {bacnetHeadlineMetrics && (
          <section className="ip-headline-metrics" aria-label="BACnet discovery headline metrics">
            {bacnetHeadlineMetrics.map((metric) => (
              <article key={metric.heading}>
                <strong>{metric.value}</strong>
                <span>{metric.heading}</span>
                {metric.progress && <small>{metric.progress}</small>}
              </article>
            ))}
          </section>
        )}
      </section>

      {!runAccessClosed && comparisonRunId && activeRun?.kind === "discovery" && (
        <section className="surface run-comparison-panel" aria-label="Sealed run comparison">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Comparison</span>
              <h3>Sealed run against {comparisonRunId}</h3>
            </div>
            <button
              className="secondary-button compact"
              onClick={() =>
                setSearchParams(
                  (current) => {
                    const next = new URLSearchParams(current);
                    next.delete("compare");
                    return next;
                  },
                  { replace: true },
                )
              }
              type="button"
            >
              Return to current run
            </button>
          </div>
          {discoveryComparisonQuery.isLoading ? (
            <p>Loading sealed comparison.</p>
          ) : discoveryComparisonQuery.isError ? (
            <div className="state-panel error" role="alert">
              <strong>Comparison unavailable</strong>
              <span>
                {discoveryComparisonQuery.error instanceof Error
                  ? discoveryComparisonQuery.error.message
                  : "The sealed comparison could not be loaded."}
              </span>
            </div>
          ) : discoveryComparisonQuery.data?.compatible ? (
            <div className="comparison-summary" aria-live="polite">
              <span>{discoveryComparisonQuery.data.additions.length} additions</span>
              <span>{discoveryComparisonQuery.data.removals.length} removals</span>
              <span>{discoveryComparisonQuery.data.changes.length} changes</span>
            </div>
          ) : (
            <div className="state-panel" role="status">
              <strong>Runs cannot be compared</strong>
              <span>
                {discoveryComparisonQuery.data?.reason ?? "The sealed runs are incompatible."}
              </span>
            </div>
          )}
        </section>
      )}

      {module.route !== "reports" && (
        <StepNav
          step={step}
          onStep={setStep}
          hasRun={Boolean(activeRun)}
          terminal={activeRunTerminal}
        />
      )}

      <div
        className={`module-steps${module.route === "reports" ? " reports-module-steps" : ""}`}
        data-step={step}
      >
        {module.route !== "reports" && (
          <section className="app-grid two-col" data-stepgroup="setup run">
            <article className="surface">
              <div className="surface-heading">
                <div>
                  <h3>Register Import</h3>
                </div>
              </div>

              {module.importTypes.length > 0 ? (
                <div className="form-stack">
                  <label>
                    Import profile
                    <select
                      disabled={profilesQuery.isLoading}
                      onChange={(event) => setSelectedImportType(event.target.value as ImportType)}
                      value={selectedImportType}
                    >
                      {availableProfiles.map((profile) => (
                        <option key={profile.import_type} value={profile.import_type}>
                          {profile.import_type.replace(/_/g, " ")}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label>
                    CSV or XLSX file
                    <input accept=".csv,.xlsx" onChange={handleFileChange} type="file" />
                  </label>
                  {/* handleFileChange clears the input's value, so the native
                  control always reads "No file chosen" — the staged file is
                  named here from state instead. */}
                  {selectedFile && <p className="field-note">Selected: {selectedFile.name}</p>}
                  {/* When nothing is staged in this session, surface the server's
                  own record of the last import so the empty file input does not
                  imply nothing was ever uploaded (ISSUE-5). Only ever shown on a
                  real hit — a 404/error leaves data undefined. */}
                  {!selectedFile && latestImportQuery.data && (
                    <div className="state-panel success import-on-file">
                      <strong>Register already imported</strong>
                      <span>
                        {latestImportQuery.data.file_name} — {latestImportQuery.data.accepted_rows}{" "}
                        of {latestImportQuery.data.total_rows} rows accepted,{" "}
                        {formatRelativeTime(latestImportQuery.data.created_at)}. This register is
                        stored and used by runs on this page; upload again only if the file changed.
                      </span>
                    </div>
                  )}

                  <button
                    className="primary-button"
                    disabled={
                      !selectedFile ||
                      !selectedImportType ||
                      importMutation.isPending ||
                      !canEngineer
                    }
                    onClick={handleImport}
                    title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
                    type="button"
                  >
                    {importMutation.isPending ? "Validating..." : "Upload and validate"}
                  </button>

                  {selectedImportType && (
                    <div className="schema-card template-card">
                      <div>
                        <strong>Default import template</strong>
                        <p>
                          Use this format as the normal project template. It includes the required
                          columns and one realistic example row.
                        </p>
                      </div>
                      <div className="inline-actions">
                        <button
                          className="secondary-button compact"
                          disabled={templateDownload.pendingKey !== null}
                          onClick={() =>
                            void templateDownload.download({
                              fallbackFilename: `${selectedImportType}_template.xlsx`,
                              key: "template-xlsx",
                              path: getImportTemplatePath(selectedImportType, "xlsx"),
                            })
                          }
                          type="button"
                        >
                          {templateDownload.pendingKey === "template-xlsx"
                            ? "Downloading..."
                            : "Download XLSX"}
                        </button>
                        <button
                          className="secondary-button compact"
                          disabled={templateDownload.pendingKey !== null}
                          onClick={() =>
                            void templateDownload.download({
                              fallbackFilename: `${selectedImportType}_template.csv`,
                              key: "template-csv",
                              path: getImportTemplatePath(selectedImportType, "csv"),
                            })
                          }
                          type="button"
                        >
                          {templateDownload.pendingKey === "template-csv"
                            ? "Downloading..."
                            : "Download CSV"}
                        </button>
                      </div>
                    </div>
                  )}

                  {templateDownload.error && (
                    <div className="state-panel error">
                      <strong>Template download failed</strong>
                      <span>{templateDownload.error}</span>
                    </div>
                  )}

                  {selectedProfile && (
                    <div className="schema-card">
                      <strong>Required columns</strong>
                      <div className="tag-cloud">
                        {selectedProfile.required_columns.slice(0, 8).map((column) => (
                          <span key={column}>{column}</span>
                        ))}
                      </div>
                      {(selectedProfile.optional_columns ?? []).length > 0 && (
                        <>
                          <strong>Optional columns</strong>
                          <div className="tag-cloud">
                            {(selectedProfile.optional_columns ?? []).slice(0, 8).map((column) => (
                              <span key={column} className="optional">
                                {column}
                              </span>
                            ))}
                          </div>
                        </>
                      )}
                    </div>
                  )}

                  {importMutation.isError && (
                    <div className="state-panel error">
                      <strong>Import failed</strong>
                      <span>{importMutation.error.message}</span>
                    </div>
                  )}

                  {importOutcome && (
                    <div className={`state-panel ${importOutcome.status}`}>
                      <strong>{importOutcome.status.toUpperCase()}</strong>
                      <span>
                        {importOutcome.accepted_rows} accepted · {importOutcome.rejected_rows}{" "}
                        rejected
                      </span>
                    </div>
                  )}

                  {importOutcome && importOutcome.status !== "accepted" && (
                    <div className="state-panel error import-errors">
                      <strong>
                        {importOutcome.status === "rejected"
                          ? "Import rejected — reasons below"
                          : `${importOutcome.rejected_rows} of ${importOutcome.total_rows} rows rejected — reasons below`}
                      </strong>
                      {importOutcome.missing_columns.length > 0 && (
                        <span>
                          Missing required columns: {importOutcome.missing_columns.join(", ")}
                        </span>
                      )}
                      {importErrorsQuery.isLoading && <span>Loading rejection reasons...</span>}
                      {/* Never let a failed fetch look like "no reasons": say so. */}
                      {importErrorsQuery.isError && (
                        <span>
                          Could not load rejection reasons: {importErrorsQuery.error.message}
                        </span>
                      )}
                      {visibleImportErrors.length > 0 && (
                        <ul>
                          {visibleImportErrors.map((error, index) => (
                            <li key={`${error.row_number ?? "file"}-${error.field ?? ""}-${index}`}>
                              {error.row_number != null ? `Row ${error.row_number} — ` : ""}
                              {error.field ? `${error.field}: ` : ""}
                              {error.message} ({error.code})
                            </li>
                          ))}
                        </ul>
                      )}
                      {hiddenImportErrorCount > 0 && (
                        <span>
                          ...and {hiddenImportErrorCount} more rejected rows not shown — fix the
                          rows listed above and re-upload to see the rest.
                        </span>
                      )}
                    </div>
                  )}

                  {importWarnings.length > 0 && (
                    <div className="state-panel warning">
                      <strong>
                        {importWarnings.length} warning(s) — affected rows are still accepted
                      </strong>
                      <ul>
                        {importWarnings.map((warning, index) => (
                          <li
                            key={`${warning.row_number ?? "file"}-${warning.field ?? ""}-${index}`}
                          >
                            {warning.row_number != null ? `Row ${warning.row_number}: ` : ""}
                            {warning.message}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              ) : (
                <div className="empty-workspace">
                  <strong>No direct import for this module</strong>
                  <span>Reports are built from completed discovery and validation runs.</span>
                </div>
              )}
            </article>

            <article className="surface">
              <div className="surface-heading">
                <div>
                  <h3>Run Controls</h3>
                </div>
              </div>

              {isDiscoveryModule && (
                <div className="form-stack scan-authorization">
                  <label className="confirm-row">
                    <input
                      checked={scanDryRun}
                      onChange={(event) => setScanDryRun(event.target.checked)}
                      type="checkbox"
                    />
                    Dry run — preview the scan plan with no network I/O (no authorization needed).
                  </label>
                  {!scanDryRun && (
                    <>
                      <label className="confirm-row">
                        <input
                          checked={scanAuthorized}
                          onChange={(event) => setScanAuthorized(event.target.checked)}
                          type="checkbox"
                        />
                        {isSealedNetworkDiscoveryModule
                          ? "I am authorized to scan this network. A sealed preview and approved authorization are also required."
                          : "I am authorized to scan this network and capture MQTT messages from this broker."}
                      </label>
                      {isSealedNetworkDiscoveryModule && (
                        <div className="form-stack">
                          <label htmlFor="scan-authorization">Sealed preview authorization</label>
                          <select
                            aria-describedby="scan-authorization-help"
                            disabled={!scanPreviewRunId || scanAuthorizationsQuery.isLoading}
                            id="scan-authorization"
                            onChange={(event) => setScanAuthorizationId(event.target.value || null)}
                            value={scanAuthorizationId ?? ""}
                          >
                            <option value="">
                              {scanPreviewRunId
                                ? "Select an approved authorization"
                                : "Run a dry preview first"}
                            </option>
                            {(scanAuthorizationsQuery.data ?? []).map((authorization) => (
                              <option
                                disabled={
                                  !isUsableScanAuthorization(authorization, authorizationNow)
                                }
                                key={authorization.authorization_id}
                                value={authorization.authorization_id}
                              >
                                {authorization.authorization_id} · {authorization.use_count}/
                                {authorization.max_uses}
                              </option>
                            ))}
                          </select>
                          <span id="scan-authorization-help" className="field-note">
                            {scanPreviewRunId
                              ? scanPreviewSealed
                                ? `Preview ${scanPreviewRunId}; start sends only this preview and authorization reference.`
                                : `Preview ${scanPreviewRunId} is sealing. Approval becomes available after it succeeds.`
                              : "Run a dry preview before selecting or creating an authorization."}
                          </span>
                          {scanPreviewSealed &&
                            !scanAuthorizationsQuery.isLoading &&
                            !hasUsableScanAuthorization &&
                            (canAdmin ? (
                              <div className="form-stack">
                                <strong>Approve this preview</strong>
                                <label>
                                  Change ticket
                                  <input
                                    disabled={createScanAuthorizationMutation.isPending}
                                    onChange={(event) =>
                                      setScanAuthorizationTicket(event.target.value)
                                    }
                                    value={scanAuthorizationTicket}
                                  />
                                </label>
                                <label>
                                  Approval purpose
                                  <input
                                    disabled={createScanAuthorizationMutation.isPending}
                                    onChange={(event) =>
                                      setScanAuthorizationPurpose(event.target.value)
                                    }
                                    value={scanAuthorizationPurpose}
                                  />
                                </label>
                                <label>
                                  Approval window
                                  <select
                                    disabled={createScanAuthorizationMutation.isPending}
                                    onChange={(event) => {
                                      const selectedWindow = SCAN_AUTHORIZATION_WINDOW_HOURS.find(
                                        (windowHours) => windowHours === event.target.value,
                                      );
                                      if (selectedWindow)
                                        setScanAuthorizationWindowHours(selectedWindow);
                                    }}
                                    value={scanAuthorizationWindowHours}
                                  >
                                    {SCAN_AUTHORIZATION_WINDOW_HOURS.map((windowHours) => (
                                      <option key={windowHours} value={windowHours}>
                                        {windowHours} {windowHours === "1" ? "hour" : "hours"}
                                      </option>
                                    ))}
                                  </select>
                                </label>
                                <button
                                  className="secondary-button compact"
                                  disabled={
                                    createScanAuthorizationMutation.isPending ||
                                    !scanAuthorizationTicket.trim() ||
                                    !scanAuthorizationPurpose.trim()
                                  }
                                  onClick={() => createScanAuthorizationMutation.mutate()}
                                  type="button"
                                >
                                  {createScanAuthorizationMutation.isPending
                                    ? "Approving..."
                                    : "Approve preview"}
                                </button>
                                {createScanAuthorizationMutation.isError && (
                                  <p className="field-note" role="alert">
                                    {createScanAuthorizationMutation.error instanceof Error
                                      ? createScanAuthorizationMutation.error.message
                                      : "Could not approve this preview. Try again or check your admin access."}
                                  </p>
                                )}
                              </div>
                            ) : (
                              <span className="field-note">
                                An admin must approve this preview before an engineer can run it.
                              </span>
                            ))}
                        </div>
                      )}
                    </>
                  )}
                </div>
              )}

              <div className="run-list">
                {visibleRunActions.length > 0 ? (
                  // Mapped over the FULL list and skipped in place, never
                  // filter-then-map: `index` must stay the action's real index in
                  // module.runActions or mutate(index) dispatches the wrong action.
                  module.runActions.map((action) => {
                    if (action.hiddenFromRunControls) {
                      return null;
                    }
                    const scanBlocked = action.kind === "discovery" && discoveryBlocked;
                    const mqttOverCapBlocked =
                      mqttCaptureOverCap &&
                      action.kind === "discovery" &&
                      action.runKind === "mqtt";
                    const overCapBlocked =
                      (udmiCaptureOverCap &&
                        action.kind === "validation" &&
                        action.runKind === "udmi") ||
                      mqttOverCapBlocked;
                    // One confirmed live run owns the monitor and Stop action. A
                    // restored run blocks another start exactly like one submitted
                    // in this session.
                    const blocked =
                      scanBlocked ||
                      !canEngineer ||
                      overCapBlocked ||
                      startedRunActive ||
                      runAccessClosed;
                    // Role gate takes priority in the tooltip; otherwise the existing
                    // scan-authorization hint is shown for a blocked real scan.
                    const blockedTooltip = !canEngineer
                      ? ENGINEER_REQUIRED_TOOLTIP
                      : runAccessClosed
                        ? "Run access for this workspace is closed. Reopen the module before starting another run."
                      : nmapSelectionBlocked
                        ? "Operator-managed Nmap is not confirmed and available for this site."
                        : startedRunActive
                          ? "A run is already in progress. Stop it before starting another."
                          : scanBlocked
                            ? isSealedNetworkDiscoveryModule
                              ? "Confirm scan authorization and select a sealed preview (or enable dry run) before starting a real scan."
                              : "Confirm broker-capture authorization (or enable dry run) before starting a real capture."
                            : mqttOverCapBlocked
                              ? "Run time exceeds the 48-hour capture limit."
                              : overCapBlocked
                                ? "Run time exceeds the 48-hour capture limit."
                                : undefined;
                    return (
                      <div className="run-card" key={action.id}>
                        <div>
                          <strong>{action.label}</strong>
                          <span>{action.helper}</span>
                        </div>
                        <button
                          className="secondary-button compact"
                          disabled={runMutation.isPending || blocked}
                          onClick={() =>
                            runMutation.mutate({
                              actionId: action.id,
                              dryRun: action.kind === "discovery" && scanDryRun,
                            })
                          }
                          title={blockedTooltip}
                          type="button"
                        >
                          {runMutation.isPending
                            ? "Working..."
                            : scanDryRun && action.kind === "discovery"
                              ? "Preview"
                              : module.route === "reports"
                                ? "Generate"
                                : "Run"}
                        </button>
                      </div>
                    );
                  })
                ) : module.runActions.length > 0 ? (
                  // This head HAS a run action, it is just started from elsewhere.
                  // Without this pointer the Run step is a dead end: StepNav never
                  // disables steps, so an operator can land here before any run and
                  // find no start control at all.
                  <div className="empty-workspace">
                    <strong>Run controls are at the bottom of Setup</strong>
                    <span>
                      Work through the options below, then start the run with Execute capture under
                      Schedule and Payload Evidence.
                    </span>
                  </div>
                ) : (
                  <div className="empty-workspace">
                    <strong>Saved synchronously</strong>
                    <span>This workflow does not need a background worker.</span>
                  </div>
                )}
              </div>

              {runMutation.isError && (
                <div className="state-panel error">
                  <strong>Run request failed</strong>
                  <span>{runMutation.error.message}</span>
                </div>
              )}

              {runOutcome && (
                <div className="state-panel success">
                  <strong>Accepted by API</strong>
                  <span>{runOutcome}</span>
                  {lastReport && (
                    <button
                      className="secondary-button compact inline-link-button"
                      disabled={reportDownload.pendingKey !== null}
                      onClick={() =>
                        void reportDownload.download({
                          fallbackFilename:
                            lastReport.file_name ||
                            `${lastReport.report_id}.${lastReport.output_format}`,
                          key: "report",
                          path: getReportDownloadPath(lastReport.report_id),
                        })
                      }
                      type="button"
                    >
                      {reportDownload.pendingKey === "report"
                        ? "Downloading..."
                        : `Download ${lastReport.output_format.toUpperCase()}`}
                    </button>
                  )}
                </div>
              )}

              {runAttachmentNotice && (
                <div className="state-panel" role="note">
                  <strong>Run link unavailable</strong>
                  <span>{runAttachmentNotice}</span>
                </div>
              )}

              {reportDownload.error && (
                <div className="state-panel error">
                  <strong>Report download failed</strong>
                  <span>{reportDownload.error}</span>
                </div>
              )}

              {copyFeedback && (
                <div className={`state-panel ${copyFeedback.severity}`}>
                  <strong>Payload copy</strong>
                  <span>{copyFeedback.message}</span>
                </div>
              )}

              {activeRun && (
                <div className="state-panel run-monitor">
                  <div className="run-monitor-heading">
                    <div>
                      <strong>
                        {activeRun.kind === "discovery" ? "Discovery" : "Validation"} run monitor
                      </strong>
                      <span>{activeRun.runId}</span>
                      <Link className="link-button" to="/run-history">
                        Run history
                      </Link>
                    </div>
                    <span className={`status-token ${activeStatusClass}`}>
                      {activeRunStatus ?? "queued"}
                    </span>
                  </div>

                  {discoveryConnectionMessage && (
                    <p
                      aria-label="Discovery connection"
                      aria-live="polite"
                      className="run-monitor-note"
                      role="status"
                    >
                      {discoveryConnectionMessage}
                    </p>
                  )}

                  <div className={`progress-track${progressIndeterminate ? " indeterminate" : ""}`}>
                    <div
                      style={progressIndeterminate ? undefined : { width: `${progressWidth}%` }}
                    />
                  </div>

                  {activeRunRecord && (
                    <LiveRunConsole
                      key={activeRunRecord.run_id}
                      assetTopicDiscovery={assetTopicDiscovery}
                      elapsed={formatElapsed(activeRunElapsedSeconds)}
                      issueCount={
                        typeof activeRunRecord.result_summary.issue_count === "number"
                          ? activeRunRecord.result_summary.issue_count
                          : (validationIssuesQuery.data?.issues.length ?? 0)
                      }
                      progress={activeRunProgress}
                      run={activeRunRecord}
                      stage={activeRunStage ?? ""}
                      status={activeRunStatus ?? "queued"}
                      validationSummary={validationSummary}
                    />
                  )}

                  {runController.phase === "terminal-sync" && (
                    <div className={`state-panel ${runController.evidenceError ? "error" : ""}`}>
                      <strong>
                        {runController.evidenceError
                          ? "Final evidence unavailable"
                          : "Final evidence is synchronising"}
                      </strong>
                      <span>
                        {runController.evidenceError ??
                          "Completed metrics and exports will appear after the final run data is confirmed."}
                      </span>
                    </div>
                  )}

                  <dl className="summary-grid">
                    <div>
                      <dt>Stage</dt>
                      <dd>{activeRunStage?.replace(/_/g, " ") ?? "Waiting for first update"}</dd>
                    </div>
                    <div>
                      <dt>Elapsed</dt>
                      <dd>{formatElapsed(activeRunElapsedSeconds)}</dd>
                    </div>
                    {runProgressText !== null && (
                      <div>
                        <dt>Progress</dt>
                        <dd>{runProgressText}</dd>
                      </div>
                    )}
                    <div>
                      <dt>Expected</dt>
                      <dd>
                        {formatSummaryValue(
                          validationRunQuery.data?.result_summary.expected_devices,
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Publishing</dt>
                      <dd>
                        {formatSummaryValue(
                          validationRunQuery.data?.result_summary.publishing_seen,
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Issues</dt>
                      <dd>
                        {formatSummaryValue(validationRunQuery.data?.result_summary.issue_count)}
                      </dd>
                    </div>
                    {captureWindow !== null && (
                      <div>
                        <dt>Capture window</dt>
                        <dd>{captureWindow}</dd>
                      </div>
                    )}
                  </dl>

                  <div className="inline-actions">
                    {canCancel && (
                      <button
                        className="secondary-button compact"
                        disabled={cancelMutation.isPending}
                        onClick={() => cancelMutation.mutate(activeRun.runId)}
                        type="button"
                      >
                        {cancelMutation.isPending ? "Stopping..." : "Stop run"}
                      </button>
                    )}
                    {canDownloadValidationJson && (
                      <button
                        className="secondary-button compact"
                        disabled={validationJsonDownload.pendingKey !== null}
                        onClick={handleValidationJsonDownload}
                        type="button"
                      >
                        {validationJsonDownload.pendingKey === "validation-json"
                          ? "Downloading JSON..."
                          : "Download raw JSON"}
                      </button>
                    )}
                    {canEngineer &&
                      activeRun.kind === "validation" &&
                      validationRunQuery.data?.job_type === "mqtt_config_publish" &&
                      activeRunTerminal &&
                      finalEvidenceReady && (
                        <button
                          className="secondary-button compact"
                          disabled={rollbackMutation.isPending}
                          onClick={() => rollbackMutation.mutate(activeRun.runId)}
                          type="button"
                        >
                          {rollbackMutation.isPending ? "Rolling back..." : "Roll back publish"}
                        </button>
                      )}
                    {canEngineer &&
                      activeRunAuthoritativelyTerminal &&
                      runController.phase !== "submitting" && (
                      <ReportFromRunControls
                        format={reportExportFormat}
                        isUdmiRun={
                          activeRun.kind === "validation" &&
                          validationRunQuery.data?.job_type === "udmi_validation"
                        }
                        udmiVariant={udmiReportVariant}
                        onUdmiVariantChange={setUdmiReportVariant}
                        onFormatChange={setReportExportFormat}
                        onGenerate={handleGenerateReportFromRun}
                        pending={reportMutationPending}
                      />
                    )}
                  </div>

                  {canCancel && (
                    <span className="run-monitor-note">
                      Stop run keeps the data collected so far — the stopped run can still generate
                      a report.
                    </span>
                  )}

                  {reportToast && (
                    <>
                      <span className="run-monitor-note">{reportToast}</span>
                      {renderGeneratedAllReportDownload()}
                    </>
                  )}
                  {reportMutationError && (
                    <span className="error-text">{reportMutationError.message}</span>
                  )}
                  {validationJsonDownload.error && (
                    <span className="error-text">
                      Raw JSON download failed: {validationJsonDownload.error}
                    </span>
                  )}

                  {cancelMutation.isError && (
                    <span className="error-text">{cancelMutation.error.message}</span>
                  )}
                  {rollbackMutation.isError && (
                    <span className="error-text">{rollbackMutation.error.message}</span>
                  )}

                  {activeRunError && <span className="error-text">{activeRunError}</span>}

                  {activeRun.kind === "discovery" && discoveryResultsQuery.isError && (
                    <span className="error-text">
                      Could not load discovery results:{" "}
                      {discoveryResultsQuery.error instanceof Error
                        ? discoveryResultsQuery.error.message
                        : "request failed"}
                    </span>
                  )}

                  {activeRun.kind === "validation" && validationIssuesQuery.isError && (
                    <span className="error-text">
                      Could not load validation issues — verdicts stay pending:{" "}
                      {validationIssuesQuery.error instanceof Error
                        ? validationIssuesQuery.error.message
                        : "request failed"}
                    </span>
                  )}
                </div>
              )}
            </article>
          </section>
        )}

        {module.route === "data-validation" && (
          <section className="surface" data-stepgroup="setup">
            <div className="surface-heading">
              <div>
                <h3>Three Checks Operators Can Understand</h3>
              </div>
            </div>
            <div className="mode-grid">
              {validationModeCards.map((mode) => (
                <article className="mode-card" key={mode.title}>
                  <span>{mode.step}</span>
                  <strong>{mode.title}</strong>
                  <p>{mode.description}</p>
                  <small>{mode.templates}</small>
                </article>
              ))}
            </div>
          </section>
        )}

        {module.route === "ip-scanner-sct" && (
          <section className="surface" data-stepgroup="setup">
            <div className="surface-heading">
              <div>
                <h3>Port and Protocol Selection</h3>
              </div>
              {(scanProvider === "builtin_tcp_connect" || nmapProfile !== "host_discovery") && (
                <button className="secondary-button compact" onClick={addScanPort} type="button">
                  Add port
                </button>
              )}
            </div>
            <div className="publish-grid capture-controls">
              <label>
                Discovery provider
                <select
                  onChange={(event) =>
                    changeScanProvider(event.target.value as IPDiscoveryProvider)
                  }
                  value={scanProvider}
                >
                  <option value="builtin_tcp_connect">Built-in TCP connect</option>
                  <option disabled={!nmapAvailable} value="operator_managed_nmap">
                    Operator-managed Nmap
                  </option>
                </select>
              </label>
              {scanProvider === "operator_managed_nmap" && (
                <label>
                  Fixed Nmap profile
                  <select
                    onChange={(event) => changeNmapProfile(event.target.value as NmapProfileName)}
                    value={nmapProfile}
                  >
                    {(nmapCapabilityQuery.data?.permitted_profiles ?? []).map((profile) => (
                      <option key={profile} value={profile}>
                        {NMAP_PROFILE_LABELS[profile]}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            {nmapCapabilityQuery.isError && (
              <p className="error-text">
                Nmap capability could not be checked. Built-in TCP connect remains available.
              </p>
            )}
            {nmapCapabilityQuery.data && (
              <p className="field-note">
                {nmapAvailable
                  ? `Nmap ${nmapCapabilityQuery.data.version ?? "version unavailable"} is confirmed for this site${
                      nmapCapabilityQuery.data.publisher
                        ? ` (${nmapCapabilityQuery.data.publisher})`
                        : ""
                    }. Only administrator-approved profiles are shown.`
                  : `Nmap process execution is unavailable: ${nmapCapabilityQuery.data.reason.replace(/_/g, " ")}.`}
              </p>
            )}
            {canAdmin &&
              me?.global_scope &&
              nmapCapabilityQuery.data &&
              !nmapAvailable &&
              nmapCapabilityQuery.data.reason !== "deployment_feature_disabled" && (
                <div className="inline-actions">
                  <button
                    className="secondary-button compact"
                    disabled={approveNmapMutation.isPending}
                    onClick={() => approveNmapMutation.mutate()}
                    type="button"
                  >
                    {approveNmapMutation.isPending ? "Approving Nmap..." : "Approve detected Nmap"}
                  </button>
                  <p className="field-note">
                    Records this signed local installation once. Approval is requested again only if
                    its installed files change.
                  </p>
                </div>
              )}
            {approveNmapMutation.isError && (
              <div className="state-panel error" role="alert">
                <strong>Nmap approval could not be completed</strong>
                <span>{approveNmapMutation.error.message}</span>
              </div>
            )}
            <p className="field-note">
              {scanProvider === "builtin_tcp_connect"
                ? "The built-in scanner runs TCP connect tests only. Use BACnet Discovery for BACnet/IP UDP 47808."
                : "Nmap runs only the selected fixed profile. Raw flags, scripts, paths, and command text cannot be entered here."}
            </p>
            {nmapProfile !== "host_discovery" || scanProvider === "builtin_tcp_connect" ? (
              <div className="port-editor">
                {scanPorts.map((entry, index) => (
                  <div className="port-row" key={`${entry.protocol}-${index}`}>
                    <label>
                      Port
                      <input
                        inputMode="numeric"
                        onChange={(event) => changeScanPort(index, "port", event.target.value)}
                        placeholder="443"
                        value={entry.port}
                      />
                    </label>
                    <label>
                      Protocol
                      <select
                        onChange={(event) =>
                          changeScanPort(
                            index,
                            "protocol",
                            event.target.value as ScanPort["protocol"],
                          )
                        }
                        value={entry.protocol}
                      >
                        {nmapProfile === "selected_udp" &&
                        scanProvider === "operator_managed_nmap" ? (
                          <option value="udp">UDP</option>
                        ) : (
                          <option value="tcp">TCP</option>
                        )}
                      </select>
                    </label>
                    <button
                      className="secondary-button compact"
                      disabled={scanPorts.length === 1}
                      onClick={() => removeScanPort(index)}
                      type="button"
                    >
                      Remove
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <p className="section-copy">
                Host discovery uses its fixed probe set. It does not accept a port list.
              </p>
            )}
            <p className="section-copy">
              Sent to the API as{" "}
              <strong>
                {scanProvider === "operator_managed_nmap" && nmapProfile === "host_discovery"
                  ? "the fixed host-discovery profile"
                  : scanPortSpecification(scanPorts) || "common ports"}
              </strong>
              . Built-in discovery uses the common TCP fallback when the list is empty.
            </p>
            <div className="publish-grid capture-controls">
              <label>
                Legacy single target override (optional)
                <input
                  onChange={(event) => setScanTarget(event.target.value)}
                  placeholder="Use the repeatable editor below"
                  value={scanTarget}
                />
              </label>
            </div>
            <div className="form-stack" aria-label="Repeatable IP target editor">
              <div className="surface-heading">
                <div>
                  <strong>Targets and exclusions</strong>
                  <span className="field-note">
                    Add numeric IPv4 addresses, CIDRs, or inclusive ranges. The server deduplicates
                    overlaps after exclusions.
                  </span>
                </div>
                <div className="inline-actions">
                  <button
                    className="secondary-button compact"
                    onClick={() => addTargetRow(false)}
                    type="button"
                  >
                    Add target
                  </button>
                  <button
                    className="secondary-button compact"
                    onClick={() => addTargetRow(true)}
                    type="button"
                  >
                    Add exclusion
                  </button>
                </div>
              </div>
              {[
                { exclusion: false, rows: scanTargetRows, label: "Target" },
                { exclusion: true, rows: scanExclusionRows, label: "Exclusion" },
              ].map(({ exclusion, rows, label }) =>
                rows.map((row) => (
                  <div className="port-row" key={row.id}>
                    <label>
                      {label} type
                      <select
                        value={row.kind}
                        onChange={(event) =>
                          updateTargetRow(exclusion, row.id, "kind", event.target.value)
                        }
                      >
                        <option value="address">Address</option>
                        <option value="cidr">CIDR</option>
                        <option value="range">Range</option>
                      </select>
                    </label>
                    <label>
                      {row.kind === "range" ? "Start" : "Value"}
                      <input
                        inputMode="numeric"
                        value={row.value}
                        onChange={(event) =>
                          updateTargetRow(exclusion, row.id, "value", event.target.value)
                        }
                      />
                    </label>
                    {row.kind === "range" && (
                      <label>
                        End
                        <input
                          inputMode="numeric"
                          value={row.end ?? ""}
                          onChange={(event) =>
                            updateTargetRow(exclusion, row.id, "end", event.target.value)
                          }
                        />
                      </label>
                    )}
                    <button
                      className="secondary-button compact"
                      onClick={() => removeTargetRow(exclusion, row.id)}
                      type="button"
                    >
                      Remove
                    </button>
                  </div>
                )),
              )}
            </div>
          </section>
        )}

        {(module.route === "mqtt-scanner" || module.route === "mqtt-discovery-sct") && (
          <section className="surface" data-stepgroup="run">
            <div className="surface-heading">
              <div>
                <h3>Incoming MQTT Payloads</h3>
              </div>
              <div className="inline-actions">
                <button
                  className="secondary-button compact"
                  disabled={captureRows.length === 0}
                  onClick={handleCaptureExport}
                  title={
                    captureRows.length === 0
                      ? "No captured topics yet — run an MQTT discovery with this topic filter."
                      : "Download the latest payload per topic as CSV."
                  }
                  type="button"
                >
                  Export to CSV
                </button>
                <button
                  className="secondary-button compact"
                  disabled={captureRows.length === 0 || captureExportDownload.pendingKey !== null}
                  onClick={handleCaptureExportXlsx}
                  title={
                    captureRows.length === 0
                      ? "No captured topics yet — run an MQTT discovery with this topic filter."
                      : "Download the latest payload per topic as an Excel (XLSX) file."
                  }
                  type="button"
                >
                  {captureExportDownload.pendingKey === "capture-xlsx"
                    ? "Exporting..."
                    : "Export to XLSX"}
                </button>
              </div>
            </div>
            <div className="publish-grid capture-controls">
              <label>
                Topic filter (MQTT wildcards: + and #)
                <input
                  onChange={(event) => setCaptureTopicFilter(event.target.value)}
                  placeholder="Blank = capture every topic (#)"
                  value={captureTopicFilter}
                />
                <small>
                  Leave blank to capture every topic (#). Enter a filter with MQTT wildcards (+ and
                  #) to narrow the capture, e.g. site/asset-1/#.
                </small>
              </label>
              <label>
                Run time (blank = run until all assets/topics seen or until the user stops the run)
                <input
                  inputMode="numeric"
                  onChange={(event) => setCaptureSeconds(event.target.value)}
                  placeholder="blank = run until you stop the run"
                  value={captureSeconds}
                />
              </label>
              <label>
                Run time unit
                <select
                  onChange={(event) =>
                    setCaptureUnit(event.target.value as "seconds" | "minutes" | "hours")
                  }
                  value={captureUnit}
                >
                  <option value="seconds">seconds</option>
                  <option value="minutes">minutes</option>
                  <option value="hours">hours</option>
                </select>
              </label>
            </div>
            {mqttCaptureOverCap && (
              <span className="error-text">
                Run time exceeds the 48-hour capture limit — shorten the window.
              </span>
            )}
            <p className="section-copy">
              Subscribes through an MQTT discovery run and shows the latest payload seen per topic.
              The live broker capture is on-site-untested here; with no broker reachable the run
              records broker_unreachable and this panel stays empty rather than showing fabricated
              payloads. The filter and run time are sent to the run; the run time is{" "}
              <strong>
                {Number(captureSecondsEffective) > 0
                  ? `${captureSecondsEffective}s`
                  : "blank (run until you press Stop run)"}
              </strong>
              . Blank runs until you press Stop run, the 500-distinct-topic cap, or the 48-hour
              safety limit. Closing the app ends the run, which is then marked interrupted at next
              start. Captured topics appear here when the run completes.
            </p>
            {activeRunTerminal &&
              discoveryRunQuery.data?.result_summary?.indefinite_bounded_inline === true && (
                <span className="error-text">
                  This run requested an indefinite capture but was bounded to{" "}
                  {String(discoveryRunQuery.data?.result_summary?.capture_seconds)}s because no stop
                  control was available for it.
                </span>
              )}
            <div className="data-table-wrap results-scroll">
              {captureRows.length > 0 ? (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Topic</th>
                      <th>Asset</th>
                      <th>Messages</th>
                      <th>Latest payload</th>
                      <th>Copy</th>
                    </tr>
                  </thead>
                  <tbody>
                    {captureRows.map((row) => (
                      <tr key={row.topic}>
                        <td>{row.topic}</td>
                        <td>{row.asset}</td>
                        <td>{row.messageCount}</td>
                        <td className="payload-cell">{row.payload || "—"}</td>
                        <td>
                          {row.payload ? (
                            <button
                              className="secondary-button compact"
                              onClick={() => handleCopyPayload(row.payload, row.topic)}
                              type="button"
                            >
                              Copy payload
                            </button>
                          ) : (
                            "—"
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="empty-workspace">
                  <strong>No captured payloads yet</strong>
                  <span>
                    Run an MQTT discovery; the latest payload per topic matching the filter appears
                    here once the run completes. Empty live results stay empty — no sample payloads
                    are shown.
                  </span>
                </div>
              )}
            </div>
            {captureTopicsQuery.isError && (
              <span className="error-text">
                Could not load captured topics:{" "}
                {captureTopicsQuery.error instanceof Error
                  ? captureTopicsQuery.error.message
                  : "request failed"}
              </span>
            )}
          </section>
        )}

        {module.route === "udmi-validation" && (
          <section className="surface" data-stepgroup="setup">
            <div className="surface-heading">
              <div>
                <h3>Non-Published UDMI Schema Sets</h3>
              </div>
            </div>
            <p className="section-copy">
              Payloads declaring a non-published UDMI version (e.g. nonpub.1) are validated against
              the uploaded schema set with that label. Download the published 1.5.2 schema set as a
              starting point, modify it, and upload it under a nonpub label.
            </p>
            <div className="form-stack">
              <button
                className="secondary-button"
                disabled={schemaTemplateDownload.pendingKey !== null}
                onClick={() =>
                  void schemaTemplateDownload.download({
                    fallbackFilename: "udmi-schema-template-1.5.2.zip",
                    key: "udmi-schema-template",
                    path: getUdmiSchemaTemplatePath(),
                  })
                }
                type="button"
              >
                {schemaTemplateDownload.pendingKey === "udmi-schema-template"
                  ? "Downloading..."
                  : "Download schema template (1.5.2)"}
              </button>
              {schemaTemplateDownload.error && (
                <div className="state-panel error">
                  <strong>Template download failed</strong>
                  <span>{schemaTemplateDownload.error}</span>
                </div>
              )}
              <label>
                Version label
                <input
                  onChange={(event) => setSchemaSetLabel(event.target.value)}
                  placeholder="nonpub.1"
                  type="text"
                  value={schemaSetLabel}
                />
              </label>
              <label>
                Schema JSON files
                <input
                  accept=".json"
                  multiple
                  onChange={(event) => {
                    setSchemaSetFiles(Array.from(event.target.files ?? []));
                    // Same Chromium re-pick trap as the register file input above:
                    // clear the value so re-picking the same schema files after
                    // editing them on disk always delivers fresh File snapshots.
                    event.target.value = "";
                  }}
                  type="file"
                />
              </label>
              {schemaSetFiles.length > 0 && (
                <p className="field-note">
                  Selected: {schemaSetFiles.map((file) => file.name).join(", ")}
                </p>
              )}
              <button
                className="primary-button"
                disabled={
                  schemaSetLabel.trim() === "" ||
                  schemaSetFiles.length === 0 ||
                  schemaUploadMutation.isPending ||
                  !canEngineer
                }
                onClick={() => schemaUploadMutation.mutate()}
                title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
                type="button"
              >
                {schemaUploadMutation.isPending ? "Uploading..." : "Upload schema set"}
              </button>

              {schemaUploadMutation.isError && (
                <div className="state-panel error">
                  <strong>Schema set upload failed</strong>
                  <span>{schemaUploadMutation.error.message}</span>
                </div>
              )}

              {schemaUploadMutation.isSuccess && (
                <div className="state-panel success">
                  <strong>ACCEPTED</strong>
                  <span>
                    {schemaUploadMutation.data.version_label} ·{" "}
                    {schemaUploadMutation.data.filenames.length} file
                    {schemaUploadMutation.data.filenames.length === 1 ? "" : "s"} stored
                  </span>
                </div>
              )}

              {(udmiSchemaSetsQuery.data ?? []).length > 0 ? (
                <div className="data-table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Version label</th>
                        <th>Files</th>
                        <th>Uploaded</th>
                        <th>Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(udmiSchemaSetsQuery.data ?? []).map((set) => (
                        <tr key={set.version_label}>
                          <td>{set.version_label}</td>
                          <td>{set.filenames.join(", ")}</td>
                          <td>{set.uploaded_at}</td>
                          <td>
                            <button
                              className="secondary-button compact"
                              disabled={schemaDeleteMutation.isPending || !canEngineer}
                              onClick={() => schemaDeleteMutation.mutate(set.version_label)}
                              title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
                              type="button"
                            >
                              Delete
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="section-copy">
                  No non-published schema sets uploaded yet. Canonical published UDMI versions need
                  no upload.
                </p>
              )}

              {schemaDeleteMutation.isError && (
                <span className="error-text">{schemaDeleteMutation.error.message}</span>
              )}
              {udmiSchemaSetsQuery.isError && (
                <span className="error-text">
                  Could not load uploaded schema sets:{" "}
                  {udmiSchemaSetsQuery.error instanceof Error
                    ? udmiSchemaSetsQuery.error.message
                    : "request failed"}
                </span>
              )}
            </div>
          </section>
        )}

        {module.route === "udmi-validation" && (
          <section className="surface" data-stepgroup="setup">
            <div className="surface-heading">
              <div>
                <h3>Schedule and Payload Evidence</h3>
              </div>
            </div>
            <label className="confirm-row">
              <input
                checked={udmiUseRegister}
                onChange={(event) => {
                  const useRegister = event.target.checked;
                  setUdmiUseRegister(useRegister);
                  if (!useRegister) {
                    setUdmiTopicDiscoveryEnabled(false);
                    setUdmiTopicDiscoveryScope("bounded");
                    setUdmiTopicDiscoveryAllScopeConfirmed(false);
                  }
                }}
                type="checkbox"
              />
              Validate against the imported MQTT register — one expected asset per row (topic,
              points, units, and Expected schema version come from the register). Auto-enabled after
              an accepted register import.
            </label>

            {udmiUseRegister ? (
              <p className="section-copy">
                Register-driven run: the pasted schedule and payload JSON below are ignored. Untick
                the option above to validate the pasted values instead.
              </p>
            ) : null}
            <div className="json-workbench">
              <label>
                Expected schedule JSON
                <textarea
                  disabled={udmiUseRegister}
                  onChange={(event) => setUdmiExpectedSchedule(event.target.value)}
                  rows={9}
                  value={udmiExpectedSchedule}
                />
              </label>
              <label>
                State payload JSON
                <textarea
                  disabled={udmiUseRegister}
                  onChange={(event) => setUdmiStatePayload(event.target.value)}
                  rows={9}
                  value={udmiStatePayload}
                />
              </label>
              <label>
                Metadata payload JSON
                <textarea
                  disabled={udmiUseRegister}
                  onChange={(event) => setUdmiMetadataPayload(event.target.value)}
                  rows={9}
                  value={udmiMetadataPayload}
                />
              </label>
              <label>
                Pointset payload JSON
                <textarea
                  disabled={udmiUseRegister}
                  onChange={(event) => setUdmiPointsetPayload(event.target.value)}
                  rows={9}
                  value={udmiPointsetPayload}
                />
              </label>
            </div>

            <label className="confirm-row">
              <input
                checked={udmiUseLiveBroker}
                onChange={(event) => {
                  const useLiveBroker = event.target.checked;
                  setUdmiUseLiveBroker(useLiveBroker);
                  if (!useLiveBroker) {
                    setUdmiTopicDiscoveryEnabled(false);
                    setUdmiTopicDiscoveryScope("bounded");
                    setUdmiTopicDiscoveryAllScopeConfirmed(false);
                  }
                }}
                type="checkbox"
              />
              Capture latest state, metadata, and pointset payloads from the configured MQTT broker.
            </label>

            {udmiUseLiveBroker && (
              <>
                <div className="publish-grid">
                  {!udmiUseRegister && (
                    <>
                      <label>
                        State topic
                        <input
                          onChange={(event) => setUdmiStateTopic(event.target.value)}
                          value={udmiStateTopic}
                        />
                      </label>
                      <label>
                        Metadata topic
                        <input
                          onChange={(event) => setUdmiMetadataTopic(event.target.value)}
                          value={udmiMetadataTopic}
                        />
                      </label>
                      <label>
                        Pointset topic
                        <input
                          onChange={(event) => setUdmiPointsetTopic(event.target.value)}
                          value={udmiPointsetTopic}
                        />
                      </label>
                    </>
                  )}
                  <label>
                    Run time (blank = run until all assets/topics seen or until the user stops the
                    run)
                    <input
                      inputMode="numeric"
                      onChange={(event) => setUdmiCaptureSeconds(event.target.value)}
                      placeholder="blank = run until all assets/topics seen or you stop the run"
                      value={udmiCaptureSeconds}
                    />
                  </label>
                  <label>
                    Run time unit
                    <select
                      onChange={(event) =>
                        setUdmiCaptureUnit(event.target.value as "seconds" | "minutes" | "hours")
                      }
                      value={udmiCaptureUnit}
                    >
                      <option value="seconds">seconds</option>
                      <option value="minutes">minutes</option>
                      <option value="hours">hours</option>
                    </select>
                  </label>
                </div>
                {udmiCaptureOverCap && (
                  <span className="error-text">
                    Run time exceeds the 48-hour capture limit — shorten the window.
                  </span>
                )}
                <p className="section-copy">
                  Blank runs until every expected asset/topic has reported or you press Stop run —
                  on the portable exe as well as the hosted worker. Every capture still ends at the
                  48-hour safety limit (real-world reporting intervals: metadata is often daily),
                  and the completion-driven safety limit is 500 distinct concrete topics. Closing
                  the app ends the run, which is then marked interrupted at next start.
                </p>
                {udmiUseRegister ? (
                  <fieldset className="topic-discovery-controls">
                    <legend>Topic discovery</legend>
                    <label className="confirm-row">
                      <input
                        checked={udmiTopicDiscoveryEnabled}
                        onChange={(event) => {
                          const enabled = event.target.checked;
                          setUdmiTopicDiscoveryEnabled(enabled);
                          if (!enabled) {
                            setUdmiTopicDiscoveryScope("bounded");
                            setUdmiTopicDiscoveryAllScopeConfirmed(false);
                          }
                        }}
                        type="checkbox"
                      />
                      Diagnose where registered asset IDs appear in MQTT topics.
                    </label>
                    {udmiTopicDiscoveryEnabled ? (
                      <div className="topic-discovery-options">
                        <label className="confirm-row">
                          <input
                            checked={udmiTopicDiscoveryAllScopeConfirmed}
                            onChange={(event) => {
                              const confirmed = event.target.checked;
                              setUdmiTopicDiscoveryAllScopeConfirmed(confirmed);
                              if (!confirmed) {
                                setUdmiTopicDiscoveryScope("bounded");
                              }
                            }}
                            type="checkbox"
                          />
                          I understand that an all-topic search can receive every broker topic this
                          account is authorised to receive.
                        </label>
                        <label className="confirm-row">
                          <input
                            checked={udmiTopicDiscoveryScope === "bounded"}
                            name="udmi-topic-discovery-scope"
                            onChange={() => {
                              setUdmiTopicDiscoveryScope("bounded");
                              setUdmiTopicDiscoveryAllScopeConfirmed(false);
                            }}
                            type="radio"
                            value="bounded"
                          />
                          Use the register&apos;s bounded topic scope.
                        </label>
                        <label className="confirm-row">
                          <input
                            checked={udmiTopicDiscoveryScope === "all"}
                            disabled={!udmiTopicDiscoveryAllScopeConfirmed}
                            name="udmi-topic-discovery-scope"
                            onChange={() => setUdmiTopicDiscoveryScope("all")}
                            type="radio"
                            value="all"
                          />
                          Search all authorised broker topics (#).
                        </label>
                        {!udmiTopicDiscoveryAllScopeConfirmed ? (
                          <p className="section-copy">
                            Acknowledge the broader scope above to enable the all-topic search.
                          </p>
                        ) : null}
                      </div>
                    ) : null}
                  </fieldset>
                ) : (
                  <p className="section-copy">
                    Topic discovery is available for live captures that validate against the
                    imported MQTT register.
                  </p>
                )}
              </>
            )}

            <div className="inline-actions execute-row">
              <button
                className="primary-button compact"
                disabled={
                  runMutation.isPending || !canEngineer || udmiCaptureOverCap || startedRunActive
                }
                onClick={() => runMutation.mutate({ actionId: udmiRunActionId, dryRun: false })}
                title={
                  !canEngineer
                    ? ENGINEER_REQUIRED_TOOLTIP
                    : udmiCaptureOverCap
                      ? "Run time exceeds the 48-hour capture limit."
                      : startedRunActive
                        ? "A run is already in progress. Stop it before starting another."
                        : undefined
                }
                type="button"
              >
                {runMutation.isPending ? "Executing..." : "Execute capture"}
              </button>
              <span className="section-copy execute-note">
                {udmiUseRegister
                  ? udmiUseLiveBroker
                    ? "Runs the UDMI validation for every imported register row, capturing each asset's state, metadata, and pointset payloads from its register topic. With no broker reachable the engine records broker_unreachable rather than fabricating payloads."
                    : "Runs the UDMI validation for every imported register row. Without broker capture there are no observed payloads, so expected points are reported as not received — tick the broker option to capture live payloads."
                  : udmiUseLiveBroker
                    ? "Runs the UDMI validation, capturing the state, metadata, and pointset payloads for the topics above. Live broker capture is on-site-untested; with no broker reachable the engine records broker_unreachable rather than fabricating payloads."
                    : "Runs the UDMI validation against the pasted state, metadata, and pointset payloads above. Tick the broker option to capture live payloads instead (on-site-untested)."}
              </span>
            </div>
          </section>
        )}

        {module.route === "udmi-validation" && (
          <section className="surface" data-stepgroup="run">
            <div className="surface-heading">
              <div>
                <h3>MQTT Config Payload</h3>
              </div>
            </div>
            <div className="publish-grid">
              <label>
                Config topic
                <input
                  onChange={(event) => setPublishTopic(event.target.value)}
                  value={publishTopic}
                />
              </label>
              <label>
                Primary point (confirmed)
                <input
                  onChange={(event) => setPublishPoint(event.target.value)}
                  value={publishPoint}
                />
              </label>
              <label>
                Primary set_value
                <input
                  onChange={(event) => setPublishValue(event.target.value)}
                  value={publishValue}
                />
              </label>
              <label className="publish-payload">
                Payload JSON
                <textarea
                  onChange={(event) => setPublishPayload(event.target.value)}
                  rows={6}
                  value={publishPayload}
                />
              </label>
            </div>

            <div className="multi-point-editor">
              <div className="surface-heading compact-heading">
                <div>
                  <h4>Write Multiple Points in One Config</h4>
                </div>
                <button
                  className="secondary-button compact"
                  onClick={addExtraPublishPoint}
                  type="button"
                >
                  Add point
                </button>
              </div>
              {publishExtraPoints.length === 0 ? (
                <p className="section-copy">
                  Optional. Add extra point/value pairs to write them all in a single config payload
                  alongside the primary point above.
                </p>
              ) : (
                <div className="port-editor">
                  {publishExtraPoints.map((pair, index) => (
                    <div className="port-row" key={`extra-${index}`}>
                      <label>
                        Point name
                        <input
                          onChange={(event) =>
                            changeExtraPublishPoint(index, "point", event.target.value)
                          }
                          placeholder="fan_enable"
                          value={pair.point}
                        />
                      </label>
                      <label>
                        set_value
                        <input
                          onChange={(event) =>
                            changeExtraPublishPoint(index, "value", event.target.value)
                          }
                          placeholder="true"
                          value={pair.value}
                        />
                      </label>
                      <button
                        className="secondary-button compact"
                        onClick={() => removeExtraPublishPoint(index)}
                        type="button"
                      >
                        Remove
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <p className="section-copy">
                All pairs are written into one config payload under <code>pointset.points</code>,
                and the backend confirm/verify step now checks every point/value here — one issue is
                raised per point whose value is not confirmed back. Live-broker confirmation (vs.
                the local verify) remains on-site-untested, the same as for the primary point.
              </p>
            </div>

            <label className="confirm-row">
              <input
                checked={publishUseLiveBroker}
                onChange={(event) => setPublishUseLiveBroker(event.target.checked)}
                type="checkbox"
              />
              Publish through the configured MQTT broker and wait for the next pointset message.
            </label>
            {publishUseLiveBroker && (
              <div className="publish-grid">
                <label className="publish-payload">
                  Pointset topic to verify
                  <input
                    onChange={(event) => setPublishPointsetTopic(event.target.value)}
                    value={publishPointsetTopic}
                  />
                </label>
                <label>
                  Wait seconds
                  <input
                    inputMode="numeric"
                    onChange={(event) => setPublishWaitSeconds(event.target.value)}
                    value={publishWaitSeconds}
                  />
                </label>
              </div>
            )}
            <label className="confirm-row">
              <input
                checked={publishConfirmed}
                onChange={(event) => setPublishConfirmed(event.target.checked)}
                type="checkbox"
              />
              I confirm this config payload should be published to the selected topic.
            </label>
            <button
              className="primary-button"
              disabled={publishMutation.isPending || !publishConfirmed || !canEngineer}
              onClick={() => publishMutation.mutate()}
              title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
              type="button"
            >
              {publishMutation.isPending ? "Publishing..." : "Publish and verify next pointset"}
            </button>
            {publishMutation.isError && (
              <div className="state-panel error">
                <strong>Publish request failed</strong>
                <span>{publishMutation.error.message}</span>
              </div>
            )}
          </section>
        )}

        {module.route === "reports" && (
          // Shown on every step, not just Results: the Reports page always lands on
          // Setup, which used to hide this table behind a step click nobody knew to
          // make. The Generate buttons live in the "setup run" Run Controls section,
          // so defaulting this route to Results instead would just hide those.
          <section className="surface" data-stepgroup="setup run results">
            <div className="surface-heading">
              <div>
                <h3 className="report-list-heading" ref={reportsHeadingRef} tabIndex={-1}>
                  Generated Reports
                </h3>
              </div>
              <div className="report-list-actions">
                <button
                  className="secondary-button compact"
                  disabled={
                    selectedDownloadableReports.length === 0 || exportDownload.pendingKey !== null
                  }
                  onClick={() => void handleExportSelected()}
                  title={
                    selectedDownloadableReports.length === 0
                      ? "Select one or more completed reports to export them."
                      : `Download ${selectedDownloadableReports.length} selected completed report(s).`
                  }
                  type="button"
                >
                  {exportDownload.pendingKey?.startsWith("selected-")
                    ? "Exporting..."
                    : "Export selected"}
                </button>
                {canEngineer && (
                  <button
                    className="secondary-button compact destructive"
                    disabled={selectedReports.length === 0 || deleteReportsMutation.isPending}
                    onClick={() => handleDeleteReports(selectedReports, { kind: "bulk" })}
                    ref={reportDeleteSelectedRef}
                    title={
                      selectedReports.length === 0
                        ? "Select one or more reports to delete them."
                        : `Delete ${selectedReports.length} selected report(s).`
                    }
                    type="button"
                  >
                    {deleteReportsMutation.isPending ? "Deleting..." : "Delete selected"}
                  </button>
                )}
              </div>
            </div>
            <p className="section-copy">
              Every report generated here is stored against its source run and listed below.
              Generate a scoped report from a completed discovery or validation run. Each entry
              remains traceable to the run it came from.
            </p>
            {reportToast && (
              <div
                className={`state-panel ${reportToastWarning ? "warning" : "success"}`}
                role="status"
              >
                <strong>
                  {reportToastWarning
                    ? "Report generation incomplete"
                    : generatedAllReportIds
                      ? "Reports ready"
                      : "Report generated"}
                </strong>
                <span>{reportToast}</span>
                {renderGeneratedAllReportDownload()}
              </div>
            )}
            {reportDeleteNotice && (
              <div className="state-panel success" role="status">
                <strong>Reports deleted</strong>
                <span>{reportDeleteNotice}</span>
              </div>
            )}
            <div className="data-table-wrap results-scroll" aria-label="Generated report list">
              {liveReports.length > 0 ? (
                <table className="data-table report-list-table">
                  <thead>
                    <tr>
                      <th>Select</th>
                      <th>Report</th>
                      <th>Type</th>
                      {hasUdmiReports && <th>Product</th>}
                      <th>Format</th>
                      <th>Status</th>
                      <th>Generated</th>
                      <th>Source runs</th>
                      <th>File</th>
                      <th>Download</th>
                      {canEngineer && <th>Delete</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {liveReports.map((report) => {
                      const downloadable = report.status === "succeeded";
                      return (
                        <tr key={report.report_id}>
                          <td>
                            <input
                              aria-label={`Select report ${report.file_name || report.report_id}`}
                              checked={selectedReportIds.has(report.report_id)}
                              onChange={() => toggleReportSelection(report.report_id)}
                              title="Select this report for export or deletion."
                              type="checkbox"
                            />
                          </td>
                          <td>
                            <div className="report-name-cell">
                              <strong>{report.report_title?.trim() || report.report_id}</strong>
                              {report.report_title?.trim() ? (
                                <small>{report.report_id}</small>
                              ) : null}
                              {report.evidence_set_id ? (
                                <small>Evidence: {report.evidence_set_id}</small>
                              ) : null}
                            </div>
                          </td>
                          <td>{report.report_type}</td>
                          {hasUdmiReports && (
                            <td>
                              {report.report_type === "udmi_validation"
                                ? report.udmi_report_variant === "client"
                                  ? "Client summary"
                                  : "Technical evidence"
                                : "—"}
                            </td>
                          )}
                          <td>{report.output_format.toUpperCase()}</td>
                          <td>
                            <span className={`status-token ${toHealthState(report.status)}`}>
                              {report.status}
                            </span>
                          </td>
                          <td>{formatAbsoluteTime(report.created_at)}</td>
                          {/* The source run ids in full, not a count: tracing a report back
                            to the runs it was built from is the whole point of an ITP
                            evidence pack, and run ids are already shown raw in the Report
                            column and the run monitor. `?? []` because a response from an
                            older backend (or a cached query payload) carries neither new
                            field — formatAbsoluteTime already tolerates undefined. */}
                          <td>{(report.source_run_ids ?? []).join(", ") || "—"}</td>
                          <td>{report.file_name || "—"}</td>
                          <td>
                            <button
                              className="secondary-button compact"
                              disabled={!downloadable || exportDownload.pendingKey !== null}
                              onClick={() =>
                                void exportDownload.download({
                                  fallbackFilename:
                                    report.file_name ||
                                    `${report.report_id}.${report.output_format}`,
                                  key: `row-${report.report_id}`,
                                  path: getReportDownloadPath(report.report_id),
                                })
                              }
                              title={
                                downloadable
                                  ? `Download ${report.file_name || report.report_id}`
                                  : "Only completed reports can be downloaded."
                              }
                              type="button"
                            >
                              {exportDownload.pendingKey === `row-${report.report_id}`
                                ? "Downloading..."
                                : "Download"}
                            </button>
                          </td>
                          {canEngineer && (
                            <td>
                              <button
                                aria-label={`Delete report ${report.file_name || report.report_id}`}
                                className="secondary-button compact destructive"
                                disabled={deleteReportsMutation.isPending}
                                onClick={() =>
                                  handleDeleteReports([report], {
                                    kind: "row",
                                    reportId: report.report_id,
                                  })
                                }
                                ref={(el) => {
                                  if (el) {
                                    reportDeleteButtonRefs.current.set(report.report_id, el);
                                  } else {
                                    reportDeleteButtonRefs.current.delete(report.report_id);
                                  }
                                }}
                                type="button"
                              >
                                Delete
                              </button>
                            </td>
                          )}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : (
                <div className="empty-workspace">
                  <strong>
                    {reportsQuery.isLoading
                      ? "Loading reports..."
                      : reportsQuery.isError
                        ? "Could not load reports"
                        : "No reports yet"}
                  </strong>
                  <span>
                    {reportsQuery.isError
                      ? "The report list request failed. Retry to load the stored report metadata."
                      : "Generate a scoped report from a completed discovery or validation run; it will appear here for selection and export."}
                  </span>
                  {reportsQuery.isError && (
                    <button
                      className="secondary-button compact"
                      onClick={() => void reportsQuery.refetch()}
                      type="button"
                    >
                      Retry
                    </button>
                  )}
                </div>
              )}
            </div>
            {reportsQuery.isError && (
              <span className="error-text">
                Could not load reports:{" "}
                {reportsQuery.error instanceof Error
                  ? reportsQuery.error.message
                  : "request failed"}
              </span>
            )}
            {exportDownload.error && (
              <div className="state-panel error">
                <strong>Export failed</strong>
                <span>{exportDownload.error}</span>
              </div>
            )}
            {deleteReportsMutation.isError && (
              <div className="state-panel error" role="alert">
                <strong>Delete failed</strong>
                <span>
                  {deleteReportsMutation.error instanceof Error
                    ? deleteReportsMutation.error.message
                    : "The report deletion request failed."}
                </span>
              </div>
            )}
          </section>
        )}

        {isUdmiValidation && activeRun?.kind === "validation" && (
          <section className="udmi-summary-shell" data-stepgroup="results">
            {validationRunQuery.isError ? (
              <div className="state-panel error" role="alert">
                <strong>Could not load validation results</strong>
                <span>
                  {validationRunQuery.error instanceof Error
                    ? validationRunQuery.error.message
                    : "The validation snapshot request failed."}
                </span>
              </div>
            ) : !activeRunTerminal ? (
              <div className="state-panel" role="status">
                <strong>Validation in progress</strong>
                <span>
                  Provisional results below update as payloads arrive. Counts can change until the
                  capture finishes; raw JSON and reports remain unavailable until then.
                </span>
              </div>
            ) : activeRunStatus === "cancelled" ? (
              <div className="state-panel warning" role="status">
                <strong>
                  {hasPersistedValidationEvidence
                    ? "Partial results from a stopped run"
                    : "Run stopped"}
                </strong>
                <span>
                  {hasPersistedValidationEvidence
                    ? `Only evidence collected before stopping is included.${captureOutcome ? ` ${captureOutcome}.` : ""}`
                    : "No validation evidence was stored for this run."}
                </span>
              </div>
            ) : activeRunStatus === "failed" ? (
              <div className="state-panel error" role="alert">
                <strong>
                  {hasPersistedValidationEvidence
                    ? "Partial results from a failed run"
                    : "Validation failed"}
                </strong>
                <span>
                  {hasPersistedValidationEvidence
                    ? `Stored evidence remains available, but this is not a complete validation.${captureOutcome ? ` ${captureOutcome}.` : ""}`
                    : (activeRunError ?? "No validation evidence was stored for this run.")}
                </span>
              </div>
            ) : null}

            {displayedValidationSummary ? (
              <UdmiSummaryPanel
                filtered={isResultsFilterActive}
                lastRunAt={validationRunQuery.data?.updated_at}
                provisional={!activeRunTerminal}
                summary={displayedValidationSummary}
              />
            ) : activeRunTerminal && !validationRunQuery.isLoading ? (
              <div className="empty-workspace">
                <strong>No summary metrics available</strong>
                <span>This legacy or empty run has no metric snapshot to display.</span>
              </div>
            ) : null}
          </section>
        )}

        {/* Keep the results table full-width. The inspector follows below it so a
          long topic or payload value never has to compete with a narrow side
          column, and the selected evidence remains readable at normal zoom. */}
        {module.route !== "reports" && (
          <section className="app-grid" data-stepgroup="results">
            <article className="surface">
              <div className="surface-heading">
                <div>
                  <h3>{workspace?.tableTitle ?? "Workflow Results"}</h3>
                </div>
                <button
                  className="secondary-button compact"
                  disabled={
                    !exportEnabled ||
                    exportDownload.pendingKey !== null ||
                    validationJsonDownload.pendingKey !== null
                  }
                  onClick={handleExport}
                  title={exportTooltip}
                  type="button"
                >
                  {validationJsonDownload.pendingKey === "validation-json"
                    ? "Downloading JSON..."
                    : exportDownload.pendingKey === "export"
                      ? "Exporting..."
                      : canDownloadValidationJson
                        ? "Download raw JSON"
                        : "Export"}
                </button>
              </div>

              {usingLiveResults && (
                <div className="sample-banner" role="note">
                  {isDiscoveryModule ? (
                    module.route === "ip-scanner" || module.route === "ip-scanner-sct" ? (
                      'Live discovery observations. The Result column reports this scan’s response and register-port verdicts; "no response on scanned ports" is inconclusive — a TCP-connect miss is not proof a host is absent.'
                    ) : (module.route === "mqtt-scanner" || module.route === "mqtt-discovery-sct") &&
                      discoveryResultsQuery.data?.register_comparison ? (
                      discoveryResultsQuery.data.register_comparison.register_available ? (
                        <>
                          Green rows match a topic in the uploaded MQTT register; red rows were
                          observed on the broker but are not in the register.
                          {mqttRegisterCompareNote(discoveryResultsQuery.data) ? (
                            <>
                              <br />
                              {mqttRegisterCompareNote(discoveryResultsQuery.data)}
                            </>
                          ) : null}
                        </>
                      ) : (
                        "No accepted MQTT register import for this project/site — upload one to compare observed topics against the template."
                      )
                    ) : (
                      // No register comparison available (non-MQTT discovery, or an
                      // MQTT run that observed nothing / has no register): the
                      // discovery table shows observations, and register verdicts are
                      // otherwise produced by validation.
                      'Live discovery observations. Register-comparison verdicts (matched / rogue / missing) are produced by validation, not discovery, so no "Result" column is shown here.'
                    )
                  ) : (
                    `${activeRunStatus === "cancelled" || activeRunStatus === "failed" ? "Partial" : activeRunTerminal ? "Live" : "Provisional live"} validation results — per-asset payload checks from the latest stored snapshot. Observed payloads were ${
                      payloadViewSource === "live_capture"
                        ? "captured from the MQTT broker"
                        : "supplied directly (pasted), not captured from a broker"
                    }.${captureWindow !== null ? ` Capture window: ${captureWindow}.` : ""}${
                      captureOutcome
                        ? ` ${captureOutcome}.`
                        : activeRunTerminal
                          ? ""
                          : " Verdicts remain pending until the run finishes."
                    }`
                  )}
                </div>
              )}
              {bacnetBackend &&
                (bacnetBackend.kind === "simulated" ? (
                  <div className="sample-banner warning" role="alert">
                    {bacnetBackend.text}
                  </div>
                ) : (
                  <div className="sample-banner" role="note">
                    {bacnetBackend.text}
                  </div>
                ))}

              {validationJsonDownload.error && (
                <div className="state-panel error" role="alert">
                  <strong>Raw JSON download failed</strong>
                  <span>{validationJsonDownload.error}</span>
                </div>
              )}

              {resultRows.length > 0 && (
                <div className="results-filter-bar">
                  <label className="results-filter-text">
                    Filter results
                    <input
                      onChange={(event) => setResultsTextFilter(event.target.value)}
                      placeholder={
                        resultsTopicColumn
                          ? "Topic path, asset, status — or an MQTT wildcard (+/#)"
                          : "Asset, host, status, or any visible value"
                      }
                      value={resultsTextFilter}
                    />
                  </label>
                  <label className="results-filter-tone">
                    Verdict
                    <select
                      onChange={(event) => setResultsToneFilter(event.target.value)}
                      value={resultsToneFilter}
                    >
                      {resultsToneOptions.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  {/* System is register-backed; observation describes only this run. */}
                  {isUdmiValidation && (
                    <>
                      <label className="results-filter-text results-topic-filter">
                        Topic contains
                        <input
                          onChange={(event) => setResultsTopicContainsFilter(event.target.value)}
                          placeholder="HV/SEC or HV/SEC/02"
                          value={resultsTopicContainsFilter}
                        />
                      </label>
                      <label className="results-filter-facet">
                        System
                        <select
                          onChange={(event) => setResultsSystemFilter(event.target.value)}
                          value={resultsSystemFilter}
                        >
                          <option value="all">All systems</option>
                          {systemOptions.map((system) => (
                            <option key={system} value={system}>
                              {system}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="results-filter-facet">
                        Observation
                        <select
                          onChange={(event) => setResultsObservationFilter(event.target.value)}
                          value={resultsObservationFilter}
                        >
                          <option value="all">Observed or not observed</option>
                          <option value="observed">Observed this run</option>
                          <option value="not-observed">Not observed this run</option>
                        </select>
                      </label>
                      <label className="results-filter-facet">
                        Category
                        <select
                          onChange={(event) =>
                            setResultsCategoryFilter(
                              event.target.value as UdmiReportScopeV1["filters"]["category"],
                            )
                          }
                          value={resultsCategoryFilter}
                        >
                          <option value="all">Expected and unexpected</option>
                          <option value="validation">Expected validation</option>
                          <option value="unexpected-devices">Unexpected devices</option>
                        </select>
                      </label>
                    </>
                  )}
                  <span className="results-filter-count">
                    Showing {visibleResultRows.length} of {resultRows.length}{" "}
                    {resultRows.length === 1 ? "row" : "rows"}
                    {hasUdmiLiveResults
                      ? ` across ${visibleUdmiCounts.expectedAssets} expected ${
                          visibleUdmiCounts.expectedAssets === 1 ? "asset" : "assets"
                        }${
                          visibleUdmiCounts.unexpectedDevices > 0
                            ? ` plus ${visibleUdmiCounts.unexpectedDevices} unexpected ${
                                visibleUdmiCounts.unexpectedDevices === 1 ? "device" : "devices"
                              }`
                            : ""
                        }`
                      : ""}
                  </span>
                  {isResultsFilterActive && (
                    <button
                      className="secondary-button compact"
                      onClick={() => {
                        setResultsTextFilter("");
                        setResultsTopicContainsFilter("");
                        setResultsToneFilter("all");
                        setResultsSystemFilter("all");
                        setResultsObservationFilter("all");
                        setResultsCategoryFilter("all");
                      }}
                      type="button"
                    >
                      Clear filters
                    </button>
                  )}
                </div>
              )}

              <div className="data-table-wrap results-scroll">
                {resultRows.length === 0 ? (
                  <div className="empty-workspace">
                    <strong>
                      {validationEmptyState
                        ? validationEmptyState.title
                        : discoveryEmptyState
                          ? discoveryEmptyState.title
                          : isDiscoveryModule && activeRun && !activeRunTerminal
                            ? "Run in progress..."
                            : "No results yet"}
                    </strong>
                    <span>
                      {validationEmptyState
                        ? validationEmptyState.detail
                        : discoveryEmptyState
                          ? discoveryEmptyState.detail
                          : isDiscoveryModule
                            ? "Run a discovery; observed devices, points, or topics appear here once it completes."
                            : "Run a job to populate results."}
                    </span>
                  </div>
                ) : visibleResultRows.length === 0 ? (
                  // The filter matched nothing. This is a claim about the FILTER,
                  // never the scan — never fall through to the discovery empty state,
                  // whose copy asserts what the network did (ISSUE-4).
                  <div className="empty-workspace">
                    <strong>No rows match the current filters</strong>
                    <span>
                      Adjust or clear the filters to see the {resultRows.length} captured{" "}
                      {resultRows.length === 1 ? "row" : "rows"}.
                    </span>
                  </div>
                ) : (
                  <table className="data-table">
                    <thead>
                      <tr>
                        {tableColumns.map((column) => (
                          <th key={column}>{column}</th>
                        ))}
                        <th>Details</th>
                      </tr>
                    </thead>
                    {/* UDMI results group by asset (ITEM-7): one collapsible summary
                    row per asset that expands to its per-payload-type rows,
                    instead of 3-4 flat lines per asset. Row shading (row-tone)
                    is the live UDMI/discovery verdict set on __tone; the summary
                    row carries the asset's worst visible tone. Discovery routes
                    keep the flat render. Child rows are the shared renderResultRow
                    (selection + View unchanged, ISSUE-4). */}
                    {hasUdmiLiveResults ? (
                      <tbody>
                        {udmiRowGroups.map((group) => {
                          const isOpen = expandedResultAssets.has(group.asset);
                          const isUnexpected = group.rows.every(
                            ({ row }) => row.__category === "unexpected-devices",
                          );
                          return (
                            <Fragment key={`group-${group.asset}`}>
                              <tr
                                className={`asset-summary-row${group.worstTone ? ` row-${group.worstTone}` : ""}`}
                              >
                                <td colSpan={tableColumns.length + 1}>
                                  <button
                                    aria-expanded={isOpen}
                                    className="asset-summary-toggle"
                                    onClick={() => toggleResultAsset(group.asset)}
                                    type="button"
                                  >
                                    <span aria-hidden="true" className="asset-summary-caret">
                                      {isOpen ? "▾" : "▸"}
                                    </span>
                                    <strong>{group.asset}</strong>
                                    <span>
                                      {isUnexpected
                                        ? "Unexpected device · excluded from compliance"
                                        : `${group.rows.length} payload type${
                                            group.rows.length === 1 ? "" : "s"
                                          } · ${group.issueTotal} issue${
                                            group.issueTotal === 1 ? "" : "s"
                                          }`}
                                    </span>
                                  </button>
                                </td>
                              </tr>
                              {isOpen && group.rows.map(renderResultRow)}
                            </Fragment>
                          );
                        })}
                      </tbody>
                    ) : (
                      <tbody>{visibleResultRows.map(renderResultRow)}</tbody>
                    )}
                  </table>
                )}
              </div>
            </article>

            {module.route === "bacnet-scanner" && !runAccessClosed && activeRunAuthoritativelyTerminal && bacnetRouters !== null && (
              <section className="surface" aria-labelledby="bacnet-routers-heading">
                <div className="surface-heading">
                  <div>
                    <h3 id="bacnet-routers-heading">Routers / BBMDs</h3>
                    <p className="section-copy">
                      BACnet/IP routers and BBMDs that answered Who-Is-Router during discovery, and the
                      remote network numbers they advertise.
                    </p>
                  </div>
                  <span className="results-filter-count">
                    {`${bacnetRouters.length} router${bacnetRouters.length === 1 ? "" : "s"}`}
                  </span>
                </div>
                {bacnetRouters.length === 0 ? (
                  <div className="empty-workspace">
                    <strong>No BACnet routers responded</strong>
                    <span>
                      No Who-Is-Router replies were heard during discovery — a recorded result, not an
                      error.
                    </span>
                  </div>
                ) : (
                  <div className="data-table-wrap results-scroll">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th scope="col">Router Address</th>
                          <th scope="col">Reachable Networks</th>
                        </tr>
                      </thead>
                      <tbody>
                        {bacnetRouters.map((router) => (
                          <tr key={router.address}>
                            <td>{router.address}</td>
                            <td>{router.networks || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            )}

            {(module.route === "bacnet-scanner" || module.route === "bacnet-discovery-sct") && !runAccessClosed && activeRunAuthoritativelyTerminal && (
              <section className="surface" aria-labelledby="bacnet-points-heading">
                <div className="surface-heading">
                  <div>
                    <h3 id="bacnet-points-heading">Points / Live Data</h3>
                    <p className="section-copy">
                      Seal-verified point rows. Search and paging stay bounded on the server.
                    </p>
                  </div>
                  <span className="results-filter-count">
                    {bacnetPointsQuery.data
                      ? `${bacnetPointsQuery.data.total} total`
                      : "Loading point count"}
                  </span>
                </div>
                <label className="field-label" htmlFor="bacnet-points-search">
                  Search points
                  <input
                    id="bacnet-points-search"
                    onChange={(event) => {
                      setBacnetPointsSearch(event.target.value);
                      setBacnetPointsCursor(null);
                    }}
                    value={bacnetPointsSearch}
                  />
                </label>
                {bacnetPointsQuery.isError ? (
                  <div className="state-panel error" role="alert">
                    <strong>Point view unavailable</strong>
                    <span>
                      {bacnetPointsQuery.error instanceof Error
                        ? bacnetPointsQuery.error.message
                        : "The sealed point page could not be read."}
                    </span>
                  </div>
                ) : bacnetPointsQuery.isLoading ? (
                  <div className="state-panel" role="status">
                    <strong>Loading point page</strong>
                    <span>The first bounded page is being verified.</span>
                  </div>
                ) : bacnetPointsQuery.data?.points.length === 0 ? (
                  <div className="empty-workspace">
                    <strong>{bacnetPointsSearch ? "No points match" : "No point rows"}</strong>
                    <span>
                      {bacnetPointsSearch
                        ? "Clear the search to inspect the complete sealed page."
                        : "This run did not produce point evidence."}
                    </span>
                  </div>
                ) : (
                  <>
                    <div className="data-table-wrap results-scroll">
                      <table className="data-table">
                        <thead>
                          <tr>
                            <th scope="col">Position</th>
                            <th scope="col">Device</th>
                            <th scope="col">Object</th>
                            <th scope="col">Units</th>
                            <th scope="col">Value</th>
                            <th scope="col">Outcome</th>
                          </tr>
                        </thead>
                        <tbody>
                          {bacnetPointsQuery.data?.points.map((point, index) => {
                            const row = bacnetPointRow(point);
                            return (
                              <tr key={String(point.id ?? row.position ?? index)}>
                                <td>{bacnetPointCell(row.position)}</td>
                                <td>{bacnetPointCell(row.device)}</td>
                                <td>{bacnetPointCell(row.object)}</td>
                                <td>{bacnetPointCell(row.units)}</td>
                                <td>{bacnetPointCell(row.value)}</td>
                                <td>{bacnetPointCell(row.outcome)}</td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                    {bacnetPointsQuery.data?.has_more && bacnetPointsQuery.data.next_cursor && (
                      <div className="detail-actions">
                        <button
                          className="secondary-button compact"
                          onClick={() =>
                            setBacnetPointsCursor(bacnetPointsQuery.data?.next_cursor ?? null)
                          }
                          type="button"
                        >
                          Next point page
                        </button>
                      </div>
                    )}
                  </>
                )}
              </section>
            )}

            {detailRow && !isUdmiValidation && (
              <dialog
                aria-labelledby="result-detail-heading"
                className="result-detail-dialog surface"
                onCancel={(event) => {
                  event.preventDefault();
                  closeResultDetailDialog();
                }}
                onClick={(event) => {
                  if (event.target !== event.currentTarget) {
                    return;
                  }
                  const bounds = event.currentTarget.getBoundingClientRect();
                  const clickedBackdrop =
                    event.clientX < bounds.left ||
                    event.clientX > bounds.right ||
                    event.clientY < bounds.top ||
                    event.clientY > bounds.bottom;
                  if (clickedBackdrop) {
                    closeResultDetailDialog();
                  }
                }}
                ref={detailDialogRef}
                tabIndex={-1}
              >
                <div className="surface-heading">
                  <div>
                    <h3 id="result-detail-heading">Result detail</h3>
                  </div>
                  <button
                    className="secondary-button compact"
                    onClick={closeResultDetailDialog}
                    type="button"
                  >
                    Close
                  </button>
                </div>
                <div className="detail-list">
                  {buildResultDetailItems(
                    module.route,
                    detailRow,
                    usingLiveResults,
                    resultAssetGroups,
                  ).map((item) => (
                    <div className="detail-row" key={item.label}>
                      <span>{item.label}</span>
                      <strong>{item.value}</strong>
                    </div>
                  ))}
                </div>
                {module.route === "bacnet-scanner" && activeRun && activeRunTerminal && (() => {
                  const deviceInstance = Number(detailRow.Instance);
                  const hasInstance = Number.isInteger(deviceInstance) && deviceInstance >= 0;
                  const browsingThis =
                    objectBrowseMutation.variables?.deviceInstance === deviceInstance;
                  const pending = objectBrowseMutation.isPending && browsingThis;
                  const errored = objectBrowseMutation.isError && browsingThis;
                  const result =
                    objectBrowseResult && objectBrowseResult.device_instance === deviceInstance
                      ? objectBrowseResult
                      : null;
                  return (
                    <div className="detail-actions" aria-live="polite">
                      <div className="property-expansion-panel">
                        <strong>Browse live objects</strong>
                        <span>
                          Reads this device&apos;s object list and present values directly from the
                          network. Nothing is persisted; the scan results above are unchanged.
                        </span>
                        <button
                          className="secondary-button compact"
                          disabled={pending || !hasInstance || !scanAuthorized}
                          onClick={() => {
                            if (hasInstance && activeRun) {
                              objectBrowseMutation.mutate({ runId: activeRun.runId, deviceInstance });
                            }
                          }}
                          type="button"
                        >
                          {pending ? "Reading object list…" : "Browse live objects"}
                        </button>
                        {!scanAuthorized && (
                          <span>Tick the scan-authorization checkbox on the Run step first.</span>
                        )}
                        {scanAuthorized && !hasInstance && (
                          <span>This row has no device instance to read.</span>
                        )}
                      </div>
                      {errored && (
                        <div className="state-panel error" role="alert">
                          <strong>Object browse failed</strong>
                          <span>
                            {objectBrowseMutation.error instanceof Error
                              ? objectBrowseMutation.error.message
                              : "The device object list could not be read."}
                          </span>
                        </div>
                      )}
                      {result && (
                        <>
                          <span className="results-filter-count">
                            {`${result.count} object${result.count === 1 ? "" : "s"} on device · showing ${result.objects.length}`}
                            {result.truncated ? " · list truncated at the read cap" : ""}
                          </span>
                          {result.error && (
                            <div className="state-panel" role="status">
                              <strong>Device did not return a full object list</strong>
                              <span>{result.error}</span>
                            </div>
                          )}
                          {result.objects.length > 0 && (
                            <div className="data-table-wrap results-scroll">
                              <table className="data-table">
                                <thead>
                                  <tr>
                                    <th scope="col">Object</th>
                                    <th scope="col">Name</th>
                                    <th scope="col">Present Value</th>
                                    <th scope="col">Units</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {result.objects.map((object) => (
                                    <tr key={`${object.type_name}-${object.instance}`}>
                                      <td>{`${object.type_name}-${object.instance}`}</td>
                                      <td>{object.name || "—"}</td>
                                      <td>{object.present_value || "—"}</td>
                                      <td>{object.units || "—"}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  );
                })()}
                {module.route === "bacnet-discovery-sct" && activeRun && activeRunTerminal && (
                  <div className="detail-actions" aria-live="polite">
                    <div className="property-expansion-panel">
                      <strong>Bounded property read</strong>
                      <span>
                        Select only properties present in the sealed parent ceiling. The parent
                        result remains unchanged.
                      </span>
                      <span>
                        Destination: {detailRow["IP Address"] || "sealed parent destination"}. Caps:{" "}
                        {JSON.stringify(activeRunRecord?.parameters?.effective_throttle ?? {})}.
                      </span>
                      {propertyCeiling.length > 0 ? (
                        <fieldset>
                          <legend>Allowed properties</legend>
                          {propertyCeiling.map((property) => (
                            <label key={property}>
                              <input
                                checked={propertyRequestedReadSet.includes(property)}
                                onChange={(event) => {
                                  setPropertyRequestedReadSet((current) =>
                                    event.target.checked
                                      ? [...current, property]
                                      : current.filter((item) => item !== property),
                                  );
                                }}
                                type="checkbox"
                              />
                              {property}
                            </label>
                          ))}
                        </fieldset>
                      ) : (
                        <span>No property ceiling was published by this parent run.</span>
                      )}
                    </div>
                    <button
                      className="secondary-button compact"
                      disabled={propertyExpansionPending || propertyCeiling.length === 0}
                      onClick={() => {
                        const requestedReadSet = propertyRequestedReadSet.filter((property) =>
                          propertyCeiling.includes(property),
                        );
                        if (requestedReadSet.length === 0) {
                          setPropertyExpansionNotice("Select at least one allowed property.");
                          return;
                        }
                        setPropertyPreviewRunId(null);
                        setPropertyRunId(null);
                        if (activeRunOwner) {
                          propertyExpansionMutation.mutate({
                            owner: activeRunOwner,
                            parentRunId: activeRun.runId,
                            requestedReadSet,
                            row: detailRow,
                          });
                        }
                      }}
                      type="button"
                    >
                      {propertyExpansionPending
                        ? "Creating property preview..."
                        : "Read more properties"}
                    </button>
                    {propertyExpansionNotice && <span>{propertyExpansionNotice}</span>}
                    {propertyPreviewRunId && propertyRequest && (
                      <div className="property-child-controls">
                        <span>
                          Preview: {propertyPreviewRunId}; state {propertyRunState ?? "queued"}.
                        </span>
                        <label htmlFor="property-authorization">Authorization</label>
                        <select
                          id="property-authorization"
                          value={propertyAuthorizationId ?? ""}
                          onChange={(event) =>
                            setPropertyAuthorizationId(event.target.value || null)
                          }
                        >
                          <option value="">Select authorization</option>
                          {(propertyAuthorizationsQuery.data ?? []).map((authorization) => (
                            <option
                              key={authorization.authorization_id}
                              value={authorization.authorization_id}
                            >
                              {authorization.authorization_id} (uses {authorization.use_count}/
                              {authorization.max_uses})
                            </option>
                          ))}
                        </select>
                        <button
                          className="secondary-button compact"
                          disabled={
                            propertyLivePending ||
                            !propertyAuthorizationId ||
                            propertyRunState !== "sealed"
                          }
                          onClick={() => {
                            if (
                              activeRunOwner &&
                              propertyOwner &&
                              propertyRequest &&
                              propertyPreviewRunId &&
                              propertyAuthorizationId &&
                              sameRunEpochOwner(propertyOwner, activeRunOwner)
                            ) {
                              propertyLiveMutation.mutate({
                                authorizationId: propertyAuthorizationId,
                                owner: activeRunOwner,
                                previewRunId: propertyPreviewRunId,
                                request: propertyRequest,
                              });
                            }
                          }}
                          type="button"
                        >
                          {propertyLivePending
                            ? "Starting child..."
                            : "Start child read"}
                        </button>
                        {propertyRunId &&
                          propertyRunId !== propertyPreviewRunId &&
                          !isTerminalStatus(propertyRunQuery.data?.status) && (
                            <button
                              className="secondary-button compact"
                              disabled={propertyCancellingForActiveOwner || propertyCancelPending}
                              onClick={() => {
                                if (
                                  activeRunOwner &&
                                  propertyOwner &&
                                  propertyRunId &&
                                  sameRunEpochOwner(propertyOwner, activeRunOwner)
                                ) {
                                  propertyCancelMutation.mutate({
                                    owner: activeRunOwner,
                                    runId: propertyRunId,
                                  });
                                }
                              }}
                              type="button"
                            >
                              {propertyCancellingForActiveOwner ? "Cancelling..." : "Stop child read"}
                            </button>
                          )}
                        {propertyRunId && propertyRunId !== propertyPreviewRunId && (
                          <span>
                            Child {propertyRunId}: {propertyRunState ?? "queued"}.
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </dialog>
            )}

            <aside className="surface inspector">
              <div className="surface-heading">
                <h3>Inspector</h3>
              </div>

              <>
                <div className="detail-list">
                  {resultDetails.map((item) => (
                    <div className="detail-row" key={item.label}>
                      <span>{item.label}</span>
                      <strong>{item.value}</strong>
                    </div>
                  ))}
                </div>

                {inspectorResult?.__category === "unexpected-devices" ? (
                  <div className="state-panel warning" role="note">
                    <strong>Observed outside the expected register</strong>
                    <span>
                      This publisher is reported separately. It does not enter expected-asset,
                      compliance, payload, fault, or validation-result totals.
                    </span>
                  </div>
                ) : resultAssetGroups ? (
                  <div className="asset-group-list">
                    {payloadViewSource && (
                      <p className="section-copy">
                        {payloadViewSource === "live_capture"
                          ? "Live-captured payloads — expand an asset, then a payload type, to compare expected vs observed."
                          : payloadViewSource === "direct_inputs"
                            ? "Pasted payloads — expand an asset, then a payload type, to compare expected vs observed."
                            : "No payload content for this run (fixture summary only); expand an asset for issue detail per payload type."}
                      </p>
                    )}
                    {!selectedInspectorAssetGroup && (
                      <div className="empty-workspace">
                        <strong>
                          {visibleResultRows.length === 0 && isResultsFilterActive
                            ? "No asset selected in the filtered results"
                            : "No asset selected"}
                        </strong>
                        <span>
                          {visibleResultRows.length === 0 && isResultsFilterActive
                            ? "Adjust or clear the filters, then select a result row to inspect its evidence."
                            : "Select an expanded result row above to inspect that asset's payload evidence."}
                        </span>
                      </div>
                    )}
                    {selectedInspectorAssetGroup &&
                      [selectedInspectorAssetGroup].map((group) => {
                        const isOpen = expandedAsset === group.assetId;
                        const typeSummary = group.payloadTypes
                          .map((entry) => {
                            const parts: string[] = [];
                            if (entry.issues.length > 0) {
                              parts.push(
                                `${entry.issues.length} issue${entry.issues.length === 1 ? "" : "s"}`,
                              );
                            }
                            if (entry.hasPayloadView) {
                              parts.push("payload");
                            }
                            return `${entry.payloadType} (${parts.join(", ") || "ok"})`;
                          })
                          .join(", ");
                        return (
                          <div
                            className={`asset-group${isOpen ? " open" : ""}`}
                            key={group.assetId}
                          >
                            <button
                              aria-expanded={isOpen}
                              className="asset-group-toggle"
                              onClick={() => setExpandedAsset(isOpen ? null : group.assetId)}
                              type="button"
                            >
                              <strong>{group.assetId}</strong>
                              <span>
                                {group.issues.length} issue{group.issues.length === 1 ? "" : "s"} ·{" "}
                                {typeSummary}
                              </span>
                            </button>
                            {isOpen && (
                              <div className="asset-group-detail">
                                {group.payloadTypes.map((entry) => {
                                  const payloadKey = `${group.assetId}:${entry.payloadType}`;
                                  const payloadOpen = expandedPayloadKey === payloadKey;
                                  // Same shared (issues-gated) verdict as the
                                  // results-table row for this asset x payload type,
                                  // so scrolling the sections draws the eye to red
                                  // without re-reading the table. "Not received"
                                  // and a pending/failed issues fetch stay neutral.
                                  const sectionVerdict = gatedUdmiVerdict(
                                    entry.issues,
                                    entry.observedPresent,
                                    notObservedAssets.has(group.assetId),
                                  );
                                  const sectionTone = udmiVerdictTone(sectionVerdict.verdict);
                                  return (
                                    <div
                                      className={`payload-type-group${sectionTone ? ` section-${sectionTone}` : ""}`}
                                      key={entry.payloadType}
                                      ref={(el) => {
                                        // Register/deregister this payload group so a
                                        // row control can scroll straight to it (ITEM-D).
                                        if (el) {
                                          payloadGroupRefs.current.set(payloadKey, el);
                                        } else {
                                          payloadGroupRefs.current.delete(payloadKey);
                                        }
                                      }}
                                      tabIndex={-1}
                                    >
                                      <div className="payload-type-heading">
                                        <h5>{entry.payloadType}</h5>
                                        {entry.hasPayloadView &&
                                          entry.issues.length >= LONG_PAYLOAD_ISSUE_THRESHOLD && (
                                            <button
                                              aria-label={`Jump to ${entry.payloadType} expected versus observed comparison`}
                                              className="secondary-button compact"
                                              onClick={() => jumpToPayloadComparison(payloadKey)}
                                              type="button"
                                            >
                                              Jump to payload comparison
                                            </button>
                                          )}
                                      </div>
                                      <p className={`payload-verdict ${sectionTone ?? "neutral"}`}>
                                        {sectionVerdict.verdict === "pass"
                                          ? "PASS: UDMI Compliant"
                                          : sectionVerdict.verdict === "pass-notes"
                                            ? "PASS WITH NOTES: minor issues below"
                                            : sectionVerdict.verdict === "offline"
                                              ? "NOT OBSERVED THIS RUN: no payload arrived during the capture window"
                                              : sectionVerdict.verdict === "fail"
                                                ? "NON-COMPLIANT: please see details below"
                                                : sectionVerdict.label === "Not received"
                                                  ? "NOT RECEIVED: no payload arrived for this payload type"
                                                  : sectionVerdict.label}
                                      </p>
                                      {entry.issues.map((issue) => (
                                        <IssueCard
                                          key={issue.id}
                                          context={issue.area}
                                          issue={issue}
                                        />
                                      ))}
                                      {entry.hasPayloadView && (
                                        <div className="payload-evidence">
                                          <button
                                            aria-expanded={payloadOpen}
                                            className="secondary-button compact"
                                            onClick={() =>
                                              setExpandedPayloadKey(payloadOpen ? null : payloadKey)
                                            }
                                            ref={(el) => {
                                              if (el) {
                                                payloadComparisonControlRefs.current.set(
                                                  payloadKey,
                                                  el,
                                                );
                                              } else {
                                                payloadComparisonControlRefs.current.delete(
                                                  payloadKey,
                                                );
                                              }
                                            }}
                                            type="button"
                                          >
                                            {payloadOpen ? "Hide" : "Show"} expected vs observed
                                            payload
                                          </button>
                                          {payloadOpen && (
                                            <PayloadComparePanels
                                              expected={entry.expected}
                                              issues={entry.issues}
                                              observed={entry.observed}
                                              observedPresent={entry.observedPresent}
                                            />
                                          )}
                                        </div>
                                      )}
                                    </div>
                                  );
                                })}
                              </div>
                            )}
                          </div>
                        );
                      })}
                  </div>
                ) : (module.route === "mqtt-scanner" || module.route === "mqtt-discovery-sct") ? (
                  // Real captured payload for the selected topic, replacing the old
                  // fabricated sample issue-cards on this discovery route.
                  selectedMqttTopic ? (
                    <MqttPayloadPanel topic={selectedMqttTopic} />
                  ) : (
                    <div className="empty-workspace">
                      <strong>No topic selected</strong>
                      <span>Select a captured topic to inspect its last payload.</span>
                    </div>
                  )
                ) : isDiscoveryModule ? (
                  // Other discovery routes (ip/bacnet): a neutral note in place of
                  // the old sample issue-cards — discovery observes, it does not
                  // produce register-comparison findings.
                  <div className="empty-workspace">
                    <strong>No findings here</strong>
                    <span>Findings are produced by validation runs, not discovery.</span>
                  </div>
                ) : (
                  <div className="issue-list compact-list">
                    {visibleIssues.length > 0 ? (
                      visibleIssues.map((issue) => (
                        <IssueCard key={issue.id} context={issue.assetId} issue={issue} />
                      ))
                    ) : (
                      <div className="empty-workspace">
                        <strong>No active findings</strong>
                        <span>
                          {liveIssues
                            ? "This validation run reported no issues."
                            : "Run a validation to surface live findings here."}
                        </span>
                      </div>
                    )}
                  </div>
                )}
              </>
            </aside>
            {isUdmiValidation && displayedValidationSummary ? (
              <div className="udmi-result-supplementary">
                {displayedAssetTopicDiscovery ? (
                  <AssetTopicDiscoveryPanel
                    discovery={displayedAssetTopicDiscovery}
                    filtered={summaryFiltersActive}
                  />
                ) : null}
                {(displayedValidationSummary.wrong_topic_assets ?? []).length > 0 ? (
                  <WrongTopicAssetsPanel
                    assets={displayedValidationSummary.wrong_topic_assets ?? []}
                  />
                ) : null}
              </div>
            ) : null}
          </section>
        )}

        {/* Second instance of the report controls, at the END of the Results step
          (field engineer's 2026-07-15 walkthrough: a run finishes, the step auto-advances
          to Results, and the run monitor's copy — which lives in the "setup run"
          stepgroup — vanishes with it, so the operator had to click back a step
          to generate the report they just earned). Both instances render the
          same stateless ReportFromRunControls wired to the one lifted
          reportExportFormat state, so they can never disagree.

          MUST stay a DIRECT child of .module-steps: the gate is
          `.module-steps > [data-stepgroup]` (electracom-theme.css:1302).
          Nesting it inside the results grid above would leave it ungated (and
          add a stray third column). jsdom never applies the theme CSS, so no
          visibility assertion can protect this — the tests pin the parent node
          and the data-stepgroup attribute instead.

          The `module.route !== "reports"` clause is DEFENSIVE, not a live fix:
          the reports head's run actions are all kind:"report", and report
          actions never setActiveRun (see runMutation.onSuccess), so activeRun is
          already always null there. It is here because that route's own section
          renders reportToast across every step group — the day a report run does
          attach itself to the monitor, this card would toast twice on one
          screen. Cheap insurance, unreachable today; don't read it as evidence
          the case exists. */}
        {module.route !== "reports" &&
          canEngineer &&
          activeRun &&
          activeRunAuthoritativelyTerminal &&
          runController.phase !== "submitting" && (
          <section className="surface" data-stepgroup="results">
            <div className="surface-heading">
              <div>
                <h3>Generate Report</h3>
              </div>
            </div>
            <div className="inline-actions">
              <ReportFromRunControls
                format={reportExportFormat}
                isUdmiRun={
                  activeRun.kind === "validation" &&
                  validationRunQuery.data?.job_type === "udmi_validation"
                }
                udmiVariant={udmiReportVariant}
                onUdmiVariantChange={setUdmiReportVariant}
                onFormatChange={setReportExportFormat}
                onGenerate={handleGenerateReportFromRun}
                pending={reportMutationPending}
              />
            </div>
            {reportToast && (
              <div
                className={`state-panel ${reportToastWarning ? "warning" : "success"}`}
                role="status"
              >
                <strong>
                  {reportToastWarning
                    ? "Report generation incomplete"
                    : generatedAllReportIds
                      ? "Reports ready"
                      : "Report generated"}
                </strong>
                <span>{reportToast}</span>
                {renderGeneratedAllReportDownload()}
              </div>
            )}
            {reportMutationError && (
              <span className="error-text">{reportMutationError.message}</span>
            )}
          </section>
        )}
        {reportDialogOpen && (
          <dialog
            aria-describedby={
              reportScopeSnapshot ? "report-title-help report-scope-help" : "report-title-help"
            }
            aria-labelledby="report-title-heading"
            className="report-title-dialog"
            onCancel={(event) => {
              event.preventDefault();
              closeReportDialog();
            }}
            ref={reportDialogRef}
          >
            <form onSubmit={handleReportDialogSubmit}>
              <div className="report-title-dialog-heading">
                <h3 id="report-title-heading">Name this validation report</h3>
                <p id="report-title-help">
                  Use a title that identifies the site, systems, or capture window. It will appear
                  in{" "}
                  {reportIntents?.length === ALL_REPORT_FORMATS.length
                    ? "all four generated reports"
                    : `the generated ${(
                        reportIntents?.[0]?.format ??
                        (reportExportFormat === "all" ? "PDF" : reportExportFormat)
                      ).toUpperCase()} report`}
                  , the Reports list, and{" "}
                  {reportIntents?.length === ALL_REPORT_FORMATS.length
                    ? "each download filename"
                    : "the download filename"}
                  .
                </p>
              </div>
              {reportScopeSnapshot && (
                <p className="report-scope-summary" id="report-scope-help">
                  {reportScopeSnapshot.filtered ? "Filtered" : "Full run"} scope locked when this
                  dialog opened: {reportScopeSnapshot.expectedAssets} expected{" "}
                  {reportScopeSnapshot.expectedAssets === 1 ? "asset" : "assets"},{" "}
                  {reportScopeSnapshot.expectedPayloads} expected{" "}
                  {reportScopeSnapshot.expectedPayloads === 1 ? "payload" : "payloads"}, and{" "}
                  {reportScopeSnapshot.unexpectedDevices} unexpected{" "}
                  {reportScopeSnapshot.unexpectedDevices === 1 ? "device" : "devices"}. This report
                  keeps that locked scope even if the filters change behind the dialog.
                </p>
              )}
              <label>
                Report title
                <input
                  maxLength={160}
                  onChange={(event) => setReportTitle(event.target.value)}
                  ref={reportTitleInputRef}
                  required
                  value={reportTitle}
                />
              </label>
              <small>{reportTitle.length}/160 characters</small>
              {reportMutationError && (
                <span className="error-text" role="alert">
                  {reportMutationError.message}
                </span>
              )}
              <div className="inline-actions report-title-dialog-actions">
                <button
                  className="secondary-button compact"
                  disabled={reportMutationPending}
                  onClick={closeReportDialog}
                  type="button"
                >
                  Cancel
                </button>
                <button
                  className="primary-button compact"
                  disabled={reportMutationPending || reportTitle.trim().length === 0}
                  type="submit"
                >
                  {reportMutationPending ? "Generating..." : "Generate report"}
                </button>
              </div>
            </form>
          </dialog>
        )}
      </div>
    </div>
  );
}

// Segmented Setup / Run / Results control. Gates the module's sections (via the
// data-step / data-stepgroup CSS in electracom-theme.css) so only the active
// stage's panels render — replacing one long scroll with one screen per task.
function StepNav({
  step,
  onStep,
  hasRun,
  terminal,
}: {
  step: ModuleStep;
  onStep: (next: ModuleStep) => void;
  hasRun: boolean;
  terminal: boolean;
}) {
  const steps: { id: ModuleStep; label: string }[] = [
    { id: "setup", label: "Setup" },
    { id: "run", label: "Run" },
    { id: "results", label: "Results" },
  ];
  return (
    <nav aria-label="Module steps" className="step-nav">
      {steps.map((entry, index) => {
        const done = (entry.id === "setup" && hasRun) || (entry.id === "run" && terminal);
        return (
          <button
            aria-current={step === entry.id ? "step" : undefined}
            className={step === entry.id ? "active" : undefined}
            key={entry.id}
            onClick={() => onStep(entry.id)}
            type="button"
          >
            <span className={`step-num${done ? " step-done" : ""}`}>{done ? "✓" : index + 1}</span>
            {entry.label}
          </button>
        );
      })}
    </nav>
  );
}

// Report format picker + "Generate report from this run" button. Rendered twice
// — once in the run monitor ("setup run" stepgroup) and once at the end of the
// Results step — because the step the run leaves you on is not the step the
// control used to live on.
//
// Deliberately stateless: the format lives in ModulePage's single
// reportExportFormat state and the guards (canEngineer / activeRun / terminal)
// stay at the call sites. Give this component its own useState and the two
// instances would silently drift apart — the picker you changed would not be
// the one the POST reads.
function ReportFromRunControls({
  format,
  isUdmiRun,
  udmiVariant,
  onUdmiVariantChange,
  onFormatChange,
  onGenerate,
  pending,
}: {
  format: ReportFormatSelection;
  isUdmiRun: boolean;
  udmiVariant: UdmiReportVariant;
  onUdmiVariantChange: (next: UdmiReportVariant) => void;
  onFormatChange: (next: ReportFormatSelection) => void;
  onGenerate: (opener: HTMLButtonElement) => void;
  pending: boolean;
}) {
  return (
    <div className="report-from-run-controls">
      {isUdmiRun && (
        <label className="report-format-picker">
          Report product
          <select
            aria-label="UDMI report product"
            onChange={(event) => onUdmiVariantChange(event.target.value as UdmiReportVariant)}
            value={udmiVariant}
          >
            <option value="client">Condensed metrics-only</option>
            <option value="technical">Technical evidence</option>
          </select>
        </label>
      )}
      <label className="report-format-picker">
        Report format
        <select
          aria-label="Report format"
          onChange={(event) => onFormatChange(event.target.value as ReportFormatSelection)}
          value={format}
        >
          <option value="pdf">PDF (.pdf)</option>
          <option value="docx">Word (.docx)</option>
          <option value="xlsx">Excel (.xlsx)</option>
          <option value="zip">Evidence pack (.zip)</option>
          <option value="all">Generate All</option>
        </select>
      </label>
      <button
        className="secondary-button compact"
        disabled={pending}
        onClick={(event) => onGenerate(event.currentTarget)}
        title="Generate a report for this run type, then find it in the Reports tab."
        type="button"
      >
        {pending ? "Generating..." : "Generate report from this run"}
      </button>
    </div>
  );
}

type UdmiSummaryDisplay = UdmiValidationSummaryV1;

type SummaryMetric = {
  label: string;
  value: number;
};

function hasNumericFields(value: unknown, fields: string[]): boolean {
  return (
    isRecord(value) &&
    fields.every((field) => typeof value[field] === "number" && Number.isFinite(value[field]))
  );
}

function readValidationSummary(
  resultSummary: Record<string, unknown> | undefined,
): UdmiValidationSummaryV1 | null {
  const candidate = resultSummary?.validation_summary_v1;
  if (
    !isRecord(candidate) ||
    (candidate.schema_version !== "1.0" && candidate.schema_version !== "1.1")
  ) {
    return null;
  }
  const valid =
    hasNumericFields(candidate.asset_metrics, [
      "expected",
      "observed",
      "not_observed",
      "with_issues",
      "successfully_validated",
    ]) &&
    hasNumericFields(candidate.payload_metrics, [
      "expected",
      "received",
      "with_issues",
      "successfully_validated",
    ]) &&
    hasNumericFields(candidate.fault_metrics, [
      "payload_formatting_issues",
      "missing_points",
      "point_naming_issues",
      "additional_points",
      "stale_or_cadence",
      "other_issues",
    ]) &&
    hasNumericFields(candidate.issue_metrics, ["blocking", "warning"]) &&
    Array.isArray(candidate.system_metrics) &&
    Array.isArray(candidate.asset_results) &&
    Array.isArray(candidate.fault_rows);
  return valid ? (candidate as unknown as UdmiValidationSummaryV1) : null;
}

const ASSET_TOPIC_DISCOVERY_STATUS_LABELS: Record<UdmiAssetTopicDiscoveryStatus, string> = {
  expected_topic_observed: "Expected topic observed",
  alternate_topic_observed: "Alternate topic observed",
  no_matching_asset_id_topic_observed: "No matching asset-ID topic observed",
  capture_incomplete: "Capture incomplete",
  ambiguous_asset_id: "Ambiguous asset ID",
  missing_asset_id: "Asset ID missing from the register row",
  scope_unavailable: "Discovery scope unavailable",
  scope_configuration_error: "Scope configuration error",
};

function isAssetTopicDiscoveryStatus(value: unknown): value is UdmiAssetTopicDiscoveryStatus {
  return (
    typeof value === "string" &&
    Object.prototype.hasOwnProperty.call(ASSET_TOPIC_DISCOVERY_STATUS_LABELS, value)
  );
}

function readAssetTopicObservation(value: unknown): UdmiAssetTopicObservation | null {
  if (
    !isRecord(value) ||
    typeof value.topic !== "string" ||
    typeof value.message_count !== "number" ||
    !Number.isFinite(value.message_count) ||
    value.message_count < 0 ||
    typeof value.last_seen !== "string"
  ) {
    return null;
  }
  return {
    last_seen: value.last_seen,
    message_count: value.message_count,
    topic: value.topic,
  };
}

function readAssetTopicDiscoveryAssetResult(
  value: unknown,
): UdmiAssetTopicDiscoveryAssetResult | null {
  if (
    !isRecord(value) ||
    typeof value.asset_id !== "string" ||
    typeof value.system !== "string" ||
    typeof value.expected_topic_root !== "string" ||
    !Array.isArray(value.expected_topics) ||
    !value.expected_topics.every((topic) => typeof topic === "string") ||
    !Array.isArray(value.observed_expected_topics) ||
    !Array.isArray(value.observed_alternate_topics) ||
    typeof value.matched_message_count !== "number" ||
    !Number.isFinite(value.matched_message_count) ||
    value.matched_message_count < 0 ||
    typeof value.topic_limit_reached !== "boolean" ||
    !isAssetTopicDiscoveryStatus(value.status)
  ) {
    return null;
  }
  const expected = value.observed_expected_topics.map(readAssetTopicObservation);
  const alternate = value.observed_alternate_topics.map(readAssetTopicObservation);
  if (expected.some((topic) => topic === null) || alternate.some((topic) => topic === null)) {
    return null;
  }
  return {
    asset_id: value.asset_id,
    expected_topic_root: value.expected_topic_root,
    expected_topics: value.expected_topics,
    matched_message_count: value.matched_message_count,
    observed_alternate_topics: alternate as UdmiAssetTopicObservation[],
    observed_expected_topics: expected as UdmiAssetTopicObservation[],
    status: value.status,
    system: value.system,
    topic_limit_reached: value.topic_limit_reached,
  };
}

function readAssetTopicDiscovery(
  resultSummary: Record<string, unknown> | undefined,
): UdmiAssetTopicDiscovery | null {
  const candidate = resultSummary?.asset_topic_discovery;
  if (
    !isRecord(candidate) ||
    candidate.enabled !== true ||
    (candidate.scope !== null && typeof candidate.scope !== "string") ||
    (candidate.scope_source !== "register_common_ancestor" &&
      candidate.scope_source !== "all" &&
      candidate.scope_source !== "invalid" &&
      candidate.scope_source !== "unavailable" &&
      candidate.scope_source !== "disabled") ||
    (candidate.scope_error !== null && typeof candidate.scope_error !== "string") ||
    typeof candidate.topic_limit_per_asset !== "number" ||
    !Number.isFinite(candidate.topic_limit_per_asset) ||
    candidate.topic_limit_per_asset < 0 ||
    typeof candidate.capture_complete !== "boolean" ||
    typeof candidate.capture_status !== "string" ||
    !isRecord(candidate.status_counts) ||
    !Array.isArray(candidate.asset_results)
  ) {
    return null;
  }
  if (
    !Object.entries(candidate.status_counts).every(
      ([status, count]) =>
        isAssetTopicDiscoveryStatus(status) &&
        typeof count === "number" &&
        Number.isFinite(count) &&
        count >= 0,
    )
  ) {
    return null;
  }
  const assetResults = candidate.asset_results.map(readAssetTopicDiscoveryAssetResult);
  if (assetResults.some((asset) => asset === null)) {
    return null;
  }
  return {
    asset_results: assetResults as UdmiAssetTopicDiscoveryAssetResult[],
    capture_complete: candidate.capture_complete,
    capture_status: candidate.capture_status,
    enabled: true,
    scope: candidate.scope,
    scope_error: candidate.scope_error,
    scope_source: candidate.scope_source,
    status_counts: candidate.status_counts as Partial<
      Record<UdmiAssetTopicDiscoveryStatus, number>
    >,
    topic_limit_per_asset: candidate.topic_limit_per_asset,
  };
}

function filterAssetTopicDiscovery(
  discovery: UdmiAssetTopicDiscovery | null,
  visibleAssets: readonly SummaryAssetResult[],
  filtersActive: boolean,
): UdmiAssetTopicDiscovery | null {
  if (!discovery || !filtersActive) {
    return discovery;
  }
  const visibleAssetIds = new Set(visibleAssets.map((asset) => asset.asset_id));
  return {
    ...discovery,
    asset_results: discovery.asset_results.filter((asset) => visibleAssetIds.has(asset.asset_id)),
  };
}

function buildValidationSummaryDisplay(
  summary: UdmiValidationSummaryV1 | null,
  retiredUnexpectedIssueIds: ReadonlySet<string>,
): UdmiSummaryDisplay | null {
  if (!summary) {
    return null;
  }
  const unexpectedDevices = summary.unexpected_devices ?? [];
  const wrongTopicAssets = summary.wrong_topic_assets ?? [];
  const retiredFaults = summary.fault_rows.filter((fault) =>
    retiredUnexpectedIssueIds.has(fault.issue_id),
  );
  const retainedFaults = summary.fault_rows.filter(
    (fault) => !retiredUnexpectedIssueIds.has(fault.issue_id),
  );
  const canonicalFaultCategory = (
    category: string,
  ): keyof UdmiValidationSummaryV1["fault_metrics"] => {
    const key = category.trim().toLocaleLowerCase().replace(/[- ]/g, "_");
    if (key === "payload_formatting" || key === "payload_formatting_issue") {
      return "payload_formatting_issues";
    }
    if (key === "missing_point") return "missing_points";
    if (key === "point_naming" || key === "point_naming_issue") {
      return "point_naming_issues";
    }
    if (key === "additional_point") return "additional_points";
    if (key === "stale" || key === "cadence") return "stale_or_cadence";
    return key === "payload_formatting_issues" ||
      key === "missing_points" ||
      key === "point_naming_issues" ||
      key === "additional_points" ||
      key === "stale_or_cadence"
      ? key
      : "other_issues";
  };
  const withoutRetiredFaults = (
    faultMetrics: UdmiValidationSummaryV1["fault_metrics"],
    issueMetrics: UdmiValidationSummaryV1["issue_metrics"],
    faults: UdmiValidationSummaryV1["fault_rows"],
  ) => {
    const adjustedFaults = { ...faultMetrics };
    const adjustedIssues = { ...issueMetrics };
    for (const fault of faults) {
      const category = canonicalFaultCategory(fault.category);
      adjustedFaults[category] = Math.max(0, adjustedFaults[category] - 1);
      const severity = fault.severity.toLocaleLowerCase();
      const issueKey = ["critical", "high", "medium", "blocking"].includes(severity)
        ? "blocking"
        : "warning";
      adjustedIssues[issueKey] = Math.max(0, adjustedIssues[issueKey] - 1);
    }
    return { fault_metrics: adjustedFaults, issue_metrics: adjustedIssues };
  };
  const adjustedOverall = withoutRetiredFaults(
    summary.fault_metrics,
    summary.issue_metrics,
    retiredFaults,
  );
  const normalisePayloadMetrics = (
    metrics: UdmiValidationSummaryV1["payload_metrics"],
    assets: UdmiValidationSummaryV1["asset_results"],
  ) => {
    const expectedRows = assets.flatMap((asset) =>
      asset.payload_results.filter((payload) => payload.expected),
    );
    const receivedRows = expectedRows.filter((payload) => payload.received);
    const exactRowsRetained =
      expectedRows.length === metrics.expected && receivedRows.length === metrics.received;
    const withIssues =
      summary.schema_version === "1.0"
        ? exactRowsRetained
          ? receivedRows.filter((payload) => payload.has_issues).length
          : Math.min(metrics.with_issues, metrics.received)
        : metrics.with_issues;
    const successfullyValidated =
      summary.schema_version === "1.0"
        ? Math.min(metrics.successfully_validated, metrics.received)
        : metrics.successfully_validated;
    return {
      ...metrics,
      not_received: metrics.not_received ?? Math.max(0, metrics.expected - metrics.received),
      with_issues: withIssues,
      successfully_validated: successfullyValidated,
    };
  };
  return {
    ...summary,
    asset_metrics: {
      ...summary.asset_metrics,
      // A populated device list is the row-level evidence and wins over a
      // provisional/stale scalar. Keep the scalar fallback only for older
      // snapshots that omitted unexpected_devices altogether.
      unexpected: Array.isArray(summary.unexpected_devices)
        ? unexpectedDevices.length
        : (summary.asset_metrics.unexpected ?? 0),
      wrong_topic: Array.isArray(summary.wrong_topic_assets)
        ? wrongTopicAssets.length
        : (summary.asset_metrics.wrong_topic ?? 0),
    },
    payload_metrics: normalisePayloadMetrics(summary.payload_metrics, summary.asset_results),
    system_metrics: summary.system_metrics.map((system) => ({
      ...system,
      asset_metrics: {
        ...system.asset_metrics,
        wrong_topic: Array.isArray(summary.wrong_topic_assets)
          ? wrongTopicAssets.filter((asset) => (asset.system || "Unspecified") === system.system)
              .length
          : (system.asset_metrics.wrong_topic ?? 0),
      },
      ...withoutRetiredFaults(
        system.fault_metrics,
        system.issue_metrics,
        retiredFaults.filter((fault) => (fault.system || "Unspecified") === system.system),
      ),
      payload_metrics: normalisePayloadMetrics(
        system.payload_metrics,
        summary.asset_results.filter((asset) => asset.system === system.system),
      ),
    })),
    ...adjustedOverall,
    fault_rows: retainedFaults,
    unexpected_devices: unexpectedDevices,
    wrong_topic_assets: wrongTopicAssets,
  };
}

function formatMetricCount(value: number): string {
  return value.toLocaleString();
}

function formatMetricPercent(successful: number, expected: number): string {
  if (expected === 0) {
    return "N/A";
  }
  return `${Math.round((successful / expected) * 100)}%`;
}

type SummaryAssetResult = UdmiValidationSummaryV1["asset_results"][number];
type SummaryFaultRow = UdmiValidationSummaryV1["fault_rows"][number];

function summaryMetricsForAssets(
  assets: SummaryAssetResult[],
  faults: SummaryFaultRow[],
  scopeIssuesToFaults: boolean,
  unexpected = 0,
  wrongTopic = 0,
) {
  const payloads = assets.flatMap((asset) => asset.payload_results);
  const expectedPayloads = payloads.filter((payload) => payload.expected);
  const faultMetrics = {
    payload_formatting_issues: 0,
    missing_points: 0,
    point_naming_issues: 0,
    additional_points: 0,
    stale_or_cadence: 0,
    other_issues: 0,
  };
  for (const fault of faults) {
    const category = fault.category.toLocaleLowerCase();
    if (category.includes("missing") && category.includes("point")) {
      faultMetrics.missing_points += 1;
    } else if (category.includes("naming") || category.includes("point_name")) {
      faultMetrics.point_naming_issues += 1;
    } else if (
      category.includes("additional") ||
      category.includes("extra_point") ||
      category.includes("unexpected_point")
    ) {
      faultMetrics.additional_points += 1;
    } else if (
      category.includes("stale") ||
      category.includes("cadence") ||
      category.includes("reporting_interval")
    ) {
      faultMetrics.stale_or_cadence += 1;
    } else if (category.includes("format") || category.includes("schema")) {
      faultMetrics.payload_formatting_issues += 1;
    } else {
      faultMetrics.other_issues += 1;
    }
  }
  const blocking = scopeIssuesToFaults
    ? faults.filter((fault) =>
        ["critical", "high", "medium", "blocking"].includes(fault.severity.toLocaleLowerCase()),
      ).length
    : assets.reduce((total, asset) => total + asset.blocking_issue_count, 0);
  const issueCount = scopeIssuesToFaults
    ? faults.length
    : assets.reduce((total, asset) => total + asset.issue_count, 0);
  return {
    asset_metrics: {
      expected: assets.length,
      observed: assets.filter((asset) => asset.observed).length,
      not_observed: assets.filter((asset) => !asset.observed).length,
      with_issues: assets.filter((asset) => asset.issue_count > 0).length,
      successfully_validated: assets.filter((asset) => asset.successfully_validated).length,
      unexpected,
      wrong_topic: wrongTopic,
    },
    payload_metrics: {
      expected: expectedPayloads.length,
      received: expectedPayloads.filter((payload) => payload.received).length,
      not_received: expectedPayloads.filter((payload) => !payload.received).length,
      with_issues: expectedPayloads.filter((payload) => payload.received && payload.has_issues)
        .length,
      successfully_validated: expectedPayloads.filter((payload) => payload.successfully_validated)
        .length,
    },
    fault_metrics: faultMetrics,
    issue_metrics: {
      blocking,
      warning: Math.max(0, issueCount - blocking),
    },
  };
}

function filterValidationSummary(
  summary: UdmiSummaryDisplay | null,
  scope: UdmiReportScopeV1 | null,
  assetFacts: ReadonlyMap<string, AssetFacts>,
): UdmiSummaryDisplay | null {
  if (!summary) {
    return null;
  }

  // Direct payload views are the strongest evidence for observation/system.
  // Reconcile the persisted summary before filtering so the cards, system table,
  // results rows, and inspector cannot report different answers for one facet.
  const reconciledAssets = summary.asset_results.map((asset) => {
    const facts = assetFacts.get(asset.asset_id);
    return facts && (facts.observed !== asset.observed || facts.system !== asset.system)
      ? { ...asset, observed: facts.observed, system: facts.system }
      : asset;
  });
  const expectedFaultRows = summary.fault_rows.filter(
    (fault) => !fault.category.toLocaleLowerCase().includes("unexpected_device"),
  );
  const unexpectedDevices = summary.unexpected_devices ?? [];
  const unexpectedCount = summary.asset_metrics.unexpected ?? unexpectedDevices.length;
  const wrongTopicAssets = summary.wrong_topic_assets ?? [];
  const wrongTopicCount = summary.asset_metrics.wrong_topic ?? wrongTopicAssets.length;
  const summaryChanged =
    reconciledAssets.some((asset, index) => asset !== summary.asset_results[index]) ||
    expectedFaultRows.length !== summary.fault_rows.length;
  let reconciledSummary = summary;
  if (summaryChanged) {
    const overall = summaryMetricsForAssets(
      reconciledAssets,
      expectedFaultRows,
      false,
      unexpectedCount,
      wrongTopicCount,
    );
    const systems = Array.from(
      new Set(reconciledAssets.map((asset) => asset.system || "Unspecified")),
    ).sort();
    const systemMetrics = systems.map((system) => {
      const systemAssets = reconciledAssets.filter(
        (asset) => (asset.system || "Unspecified") === system,
      );
      const systemAssetIds = new Set(systemAssets.map((asset) => asset.asset_id));
      const systemFaults = expectedFaultRows.filter(
        (fault) => fault.asset_id !== null && systemAssetIds.has(fault.asset_id),
      );
      const systemWrongTopicCount = wrongTopicAssets.filter(
        (asset) => (asset.system || "Unspecified") === system,
      ).length;
      return {
        system,
        ...summaryMetricsForAssets(systemAssets, systemFaults, false, 0, systemWrongTopicCount),
      };
    });
    reconciledSummary = {
      ...summary,
      ...overall,
      asset_results: reconciledAssets,
      fault_rows: expectedFaultRows,
      system_metrics: systemMetrics,
    };
  }
  if (!scope) {
    return reconciledSummary;
  }

  const selectedPayloadKeys = new Set(
    scope.selected_payloads.map((payload) => `${payload.asset_id}\u0000${payload.payload_type}`),
  );
  const selectedAssetIds = new Set(scope.selected_payloads.map((payload) => payload.asset_id));
  const selectedPayloadTypesByAsset = new Map<string, Set<string>>();
  for (const payload of scope.selected_payloads) {
    const selectedTypes = selectedPayloadTypesByAsset.get(payload.asset_id) ?? new Set<string>();
    selectedTypes.add(payload.payload_type);
    selectedPayloadTypesByAsset.set(payload.asset_id, selectedTypes);
  }
  const fullySelectedAssetIds = new Set(
    reconciledSummary.asset_results.flatMap((asset) => {
      const availableTypes = new Set(
        asset.payload_results
          .filter(
            (payload) =>
              payload.expected &&
              (payload.payload_type === "state" ||
                payload.payload_type === "metadata" ||
                payload.payload_type === "pointset"),
          )
          .map((payload) => payload.payload_type),
      );
      const selectedTypes = selectedPayloadTypesByAsset.get(asset.asset_id);
      return availableTypes.size > 0 &&
        selectedTypes?.size === availableTypes.size &&
        Array.from(availableTypes).every((payloadType) => selectedTypes.has(payloadType))
        ? [asset.asset_id]
        : [];
    }),
  );
  const availableExpectedPayloadKeys = new Set(
    reconciledSummary.asset_results.flatMap((asset) =>
      asset.payload_results
        .filter((payload) => payload.expected)
        .map((payload) => `${asset.asset_id}\u0000${payload.payload_type}`),
    ),
  );
  const fullSourceSelected =
    availableExpectedPayloadKeys.size > 0 &&
    availableExpectedPayloadKeys.size === selectedPayloadKeys.size &&
    Array.from(availableExpectedPayloadKeys).every((key) => selectedPayloadKeys.has(key));
  const selectedAssets = reconciledSummary.asset_results.flatMap((asset) => {
    const payloadResults = asset.payload_results.filter((payload) =>
      selectedPayloadKeys.has(`${asset.asset_id}\u0000${payload.payload_type}`),
    );
    if (payloadResults.length > 0) {
      return [{ ...asset, payload_results: payloadResults }];
    }
    // Fixture-era summaries can name an asset-level issue row while omitting
    // payload_results entirely. Retain that selected asset as a compatibility
    // fallback; current 1.1 snapshots always take the exact payload branch.
    return asset.payload_results.length === 0 && selectedAssetIds.has(asset.asset_id)
      ? [asset]
      : [];
  });
  const assetIds = new Set(selectedAssets.map((asset) => asset.asset_id));
  const faultRows = reconciledSummary.fault_rows.filter((fault) => {
    if (!fault.asset_id) {
      return fullSourceSelected;
    }
    if (!assetIds.has(fault.asset_id)) {
      return false;
    }
    const payloadType = fault.payload_type?.trim() ?? "";
    return payloadType
      ? selectedPayloadKeys.has(`${fault.asset_id}\u0000${payloadType}`)
      : fullySelectedAssetIds.has(fault.asset_id);
  });
  const assets = selectedAssets.map((asset) => {
    const assetFaults = faultRows.filter((fault) => fault.asset_id === asset.asset_id);
    const expectedPayloads = asset.payload_results.filter((payload) => payload.expected);
    const receivedPayloads = expectedPayloads.filter((payload) => payload.received);
    const observedTimes = receivedPayloads
      .flatMap((payload) => {
        if (!payload.received_at) return [];
        const instant = Date.parse(payload.received_at);
        return Number.isNaN(instant) ? [] : [{ instant, value: payload.received_at }];
      })
      .sort((left, right) => right.instant - left.instant);
    const blockingIssueCount = assetFaults.filter((fault) =>
      ["critical", "high", "medium", "blocking"].includes(fault.severity.toLocaleLowerCase()),
    ).length;
    const issueCount = assetFaults.length;
    return {
      ...asset,
      observed: receivedPayloads.length > 0,
      expected_payloads: expectedPayloads.length,
      received_payloads: expectedPayloads.filter((payload) => payload.received).length,
      all_expected_payloads_received:
        expectedPayloads.length > 0 && expectedPayloads.every((payload) => payload.received),
      all_received_payloads_successfully_validated:
        receivedPayloads.length > 0 &&
        receivedPayloads.every((payload) => payload.successfully_validated),
      successfully_validated:
        expectedPayloads.length > 0 &&
        expectedPayloads.every((payload) => payload.received) &&
        blockingIssueCount === 0,
      issue_count: issueCount,
      blocking_issue_count: blockingIssueCount,
      last_observed_at: observedTimes[0]?.value ?? null,
    };
  });
  const selectedUnexpectedIds = new Set(scope.unexpected_device_ids);
  const scopedUnexpectedDevices = unexpectedDevices.filter((device) =>
    selectedUnexpectedIds.has(device.id),
  );
  const scopedWrongTopicAssets = wrongTopicAssets.flatMap((asset) => {
    if (!assetIds.has(asset.asset_id)) {
      return [];
    }
    const payloads = asset.payloads.filter((payload) =>
      selectedPayloadKeys.has(`${asset.asset_id}\u0000${payload.payload_type}`),
    );
    return payloads.length > 0 ? [{ ...asset, payloads }] : [];
  });
  const overall = summaryMetricsForAssets(
    assets,
    faultRows,
    true,
    scopedUnexpectedDevices.length,
    scopedWrongTopicAssets.length,
  );
  const systems = Array.from(new Set(assets.map((asset) => asset.system || "Unspecified"))).sort();
  const systemMetrics = systems.map((system) => {
    const systemAssets = assets.filter((asset) => (asset.system || "Unspecified") === system);
    const systemAssetIds = new Set(systemAssets.map((asset) => asset.asset_id));
    const systemFaults = faultRows.filter(
      (fault) => fault.asset_id !== null && systemAssetIds.has(fault.asset_id),
    );
    const systemWrongTopicCount = scopedWrongTopicAssets.filter(
      (asset) => (asset.system || "Unspecified") === system,
    ).length;
    return {
      system,
      ...summaryMetricsForAssets(systemAssets, systemFaults, true, 0, systemWrongTopicCount),
    };
  });
  return {
    ...reconciledSummary,
    ...overall,
    asset_results: assets,
    fault_rows: faultRows,
    system_metrics: systemMetrics,
    unexpected_devices: scopedUnexpectedDevices,
    wrong_topic_assets: scopedWrongTopicAssets,
  };
}

function SummaryMetricGroup({
  metrics,
  title,
  tone,
}: {
  metrics: SummaryMetric[];
  title: string;
  tone: "assets" | "faults";
}) {
  return (
    <section className={"udmi-metric-group udmi-metric-" + tone}>
      <h4>{title}</h4>
      <dl className="udmi-metric-table">
        {metrics.map((metric) => (
          <div key={metric.label}>
            <dt>{metric.label}</dt>
            <dd>{formatMetricCount(metric.value)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function PayloadMetricGroup({ summary }: { summary: UdmiSummaryDisplay }) {
  const metrics: SummaryMetric[] = [
    { label: "Expected payloads", value: summary.payload_metrics.expected },
    { label: "Received payloads", value: summary.payload_metrics.received },
    {
      label: "Not received",
      value:
        summary.payload_metrics.not_received ??
        Math.max(0, summary.payload_metrics.expected - summary.payload_metrics.received),
    },
    { label: "Payloads with issues", value: summary.payload_metrics.with_issues },
    { label: "Successfully validated", value: summary.payload_metrics.successfully_validated },
  ];
  const expected = summary.payload_metrics.expected;
  const correct = summary.payload_metrics.successfully_validated;
  const incorrect = Math.max(0, expected - correct);
  return (
    <section className="udmi-metric-group udmi-metric-payloads">
      <h4>Payload metrics</h4>
      <dl className="udmi-metric-table">
        {metrics.map((metric) => (
          <div key={metric.label}>
            <dt>{metric.label}</dt>
            <dd>{formatMetricCount(metric.value)}</dd>
          </div>
        ))}
      </dl>
      <dl className="udmi-payload-rates">
        <div>
          <dt>Payloads correct</dt>
          <dd>
            {formatMetricPercent(correct, expected)}
            <small>
              {formatMetricCount(correct)} / {formatMetricCount(expected)} expected
            </small>
          </dd>
        </div>
        <div>
          <dt>Payloads incorrect</dt>
          <dd>
            {formatMetricPercent(incorrect, expected)}
            <small>
              {formatMetricCount(incorrect)} / {formatMetricCount(expected)} expected
            </small>
          </dd>
        </div>
      </dl>
      <p className="udmi-metric-basis">
        Expected payloads are the denominator. Unexpected received payloads are excluded.
      </p>
    </section>
  );
}

function assetTopicDiscoveryStatusLabel(status: UdmiAssetTopicDiscoveryStatus): string {
  return ASSET_TOPIC_DISCOVERY_STATUS_LABELS[status];
}

function assetTopicDiscoveryScopeSourceLabel(
  source: UdmiAssetTopicDiscovery["scope_source"],
): string {
  switch (source) {
    case "register_common_ancestor":
      return "Register common ancestor";
    case "all":
      return "Approved all-topic scope";
    case "invalid":
      return "Invalid";
    case "unavailable":
      return "Bounded scope unavailable";
    case "disabled":
      return "Disabled";
  }
}

function assetTopicDiscoveryCaptureStatusLabel(status: string): string {
  if (status === "completed") return "Completed";
  if (status === "cancelled") return "Stopped";
  if (status === "primary_topic_limit_reached") return "Primary topic limit reached";
  if (status === "primary_byte_limit_reached") return "Primary byte limit reached";
  return status.replace(/_/g, " ");
}

function formatAssetTopicDiscoveryStatusCounts(
  statusCounts: UdmiAssetTopicDiscovery["status_counts"],
): string {
  const counts = Object.entries(statusCounts)
    .filter(
      ([status, count]) =>
        isAssetTopicDiscoveryStatus(status) && typeof count === "number" && count > 0,
    )
    .map(
      ([status, count]) =>
        `${formatMetricCount(count)} ${assetTopicDiscoveryStatusLabel(
          status as UdmiAssetTopicDiscoveryStatus,
        )}`,
    );
  return counts.length > 0 ? counts.join("; ") : "No asset status counts reported.";
}

function formatAssetTopicObservations(observations: readonly UdmiAssetTopicObservation[]): string {
  if (observations.length === 0) {
    return "None";
  }
  return observations
    .map((observation) => {
      const count = `${formatMetricCount(observation.message_count)} message${
        observation.message_count === 1 ? "" : "s"
      }`;
      const lastSeen = observation.last_seen
        ? `; last seen ${formatAbsoluteTime(observation.last_seen)}`
        : "";
      return `${observation.topic} (${count}${lastSeen})`;
    })
    .join("; ");
}

function AssetTopicDiscoveryPanel({
  discovery,
  filtered,
}: {
  discovery: UdmiAssetTopicDiscovery;
  filtered: boolean;
}) {
  return (
    <section
      className="udmi-system-summary udmi-asset-topic-discovery"
      aria-labelledby="udmi-asset-topic-discovery-heading"
    >
      <div>
        <h4 id="udmi-asset-topic-discovery-heading">Asset topic discovery</h4>
        <p>
          Asset IDs are matched against case-sensitive MQTT topic segments within the approved
          scope. Payload content is not inspected.
          {filtered ? " Rows reflect the active result filters." : ""}
        </p>
      </div>
      <dl className="udmi-summary-run-meta udmi-topic-discovery-meta">
        <div>
          <dt>Discovery scope</dt>
          <dd>{discovery.scope ?? "Not available"}</dd>
        </div>
        <div>
          <dt>Scope source</dt>
          <dd>{assetTopicDiscoveryScopeSourceLabel(discovery.scope_source)}</dd>
        </div>
        <div>
          <dt>Capture status</dt>
          <dd>{assetTopicDiscoveryCaptureStatusLabel(discovery.capture_status)}</dd>
        </div>
        <div>
          <dt>Topic limit per asset</dt>
          <dd>{formatMetricCount(discovery.topic_limit_per_asset)}</dd>
        </div>
      </dl>
      <p className="section-copy">
        Status totals: {formatAssetTopicDiscoveryStatusCounts(discovery.status_counts)}
      </p>
      {!discovery.capture_complete ? (
        <p className="section-copy">
          This capture is incomplete. Topic matches only cover messages retained before it ended.
        </p>
      ) : null}
      {discovery.scope_error ? (
        <p className="section-copy">
          Scope configuration: {discovery.scope_error.replace(/_/g, " ")}.
        </p>
      ) : null}
      {discovery.asset_results.length > 0 ? (
        <div className="data-table-wrap udmi-topic-discovery-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Asset</th>
                <th>System</th>
                <th>Discovery status</th>
                <th>Expected topic root</th>
                <th>Expected topics observed</th>
                <th>Alternate topics observed</th>
                <th>Matched messages</th>
              </tr>
            </thead>
            <tbody>
              {discovery.asset_results.map((asset) => (
                <tr key={asset.asset_id}>
                  <td>{asset.asset_id}</td>
                  <td>{asset.system || "Unspecified"}</td>
                  <td>
                    <strong>{assetTopicDiscoveryStatusLabel(asset.status)}</strong>
                    {asset.topic_limit_reached ? (
                      <span>Topic limit reached for this asset.</span>
                    ) : null}
                  </td>
                  <td>
                    {asset.expected_topic_root || "Not recorded"}
                    <span>
                      {formatMetricCount(asset.expected_topics.length)} expected topic
                      {asset.expected_topics.length === 1 ? "" : "s"}
                    </span>
                  </td>
                  <td>{formatAssetTopicObservations(asset.observed_expected_topics)}</td>
                  <td>{formatAssetTopicObservations(asset.observed_alternate_topics)}</td>
                  <td>{formatMetricCount(asset.matched_message_count)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="section-copy">
          No asset-topic discovery rows match the current result filters.
        </p>
      )}
    </section>
  );
}

function WrongTopicAssetsPanel({
  assets,
}: {
  assets: readonly NonNullable<UdmiValidationSummaryV1["wrong_topic_assets"]>[number][];
}) {
  return (
    <section
      className="surface udmi-system-summary udmi-wrong-topic-summary"
      aria-labelledby="udmi-wrong-topic-summary-heading"
    >
      <div>
        <h4 id="udmi-wrong-topic-summary-heading">Registered assets on wrong topics</h4>
        <p>
          These assets were received and identified, but their observed MQTT topic roots do not
          match the register.
        </p>
      </div>
      <div className="data-table-wrap udmi-wrong-topic-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Asset</th>
              <th>System</th>
              <th>Expected topic root</th>
              <th>Observed topic root</th>
              <th>Affected payloads</th>
              <th>Last seen</th>
            </tr>
          </thead>
          <tbody>
            {assets.map((asset) => (
              <tr key={`${asset.asset_id}:${asset.actual_topic_root}`}>
                <td>{asset.asset_id}</td>
                <td>{asset.system || "Unspecified"}</td>
                <td>{asset.expected_topic_root}</td>
                <td>{asset.actual_topic_root}</td>
                <td>
                  {asset.payloads
                    .map(
                      (payload) =>
                        `${payload.payload_type}: ${payload.expected_topic} → ${payload.actual_topic}`,
                    )
                    .join(", ")}
                </td>
                <td>{asset.last_seen ? formatAbsoluteTime(asset.last_seen) : "Not recorded"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function UdmiSummaryPanel({
  filtered,
  lastRunAt,
  provisional,
  summary,
}: {
  filtered: boolean;
  lastRunAt: string | undefined;
  provisional: boolean;
  summary: UdmiSummaryDisplay;
}) {
  const assets: SummaryMetric[] = [
    { label: "Expected assets", value: summary.asset_metrics.expected },
    { label: "Observed assets", value: summary.asset_metrics.observed },
    { label: "Not observed", value: summary.asset_metrics.not_observed },
    { label: "Assets with issues", value: summary.asset_metrics.with_issues },
    { label: "Successfully validated", value: summary.asset_metrics.successfully_validated },
    { label: "Wrong-topic assets", value: summary.asset_metrics.wrong_topic ?? 0 },
    { label: "Unexpected devices", value: summary.asset_metrics.unexpected ?? 0 },
  ];
  const faults: SummaryMetric[] = [
    { label: "Payload formatting", value: summary.fault_metrics.payload_formatting_issues },
    { label: "Missing points", value: summary.fault_metrics.missing_points },
    { label: "Point naming", value: summary.fault_metrics.point_naming_issues },
    { label: "Additional points", value: summary.fault_metrics.additional_points },
    { label: "Cadence or stale", value: summary.fault_metrics.stale_or_cadence },
    { label: "Other issues", value: summary.fault_metrics.other_issues },
  ];

  return (
    <article className="surface udmi-summary" aria-labelledby="udmi-summary-heading">
      <div className="surface-heading udmi-summary-heading">
        <div>
          <h3 id="udmi-summary-heading">
            {provisional ? "Provisional validation summary" : "Validation summary"}
          </h3>
          {filtered ? (
            <p className="section-copy">
              Metrics, details, and generated reports reflect the exact rows retained by every
              active result filter.
            </p>
          ) : null}
        </div>
        <dl className="udmi-summary-run-meta">
          <div>
            <dt>Overall compliance</dt>
            <dd>
              {formatMetricPercent(
                summary.asset_metrics.successfully_validated,
                summary.asset_metrics.expected,
              )}{" "}
              <span>
                ({formatMetricCount(summary.asset_metrics.successfully_validated)} /{" "}
                {formatMetricCount(summary.asset_metrics.expected)} assets)
              </span>
            </dd>
          </div>
          <div>
            <dt>{provisional ? "Snapshot updated" : "Last validation run"}</dt>
            <dd>{lastRunAt ? formatAbsoluteTime(lastRunAt) : "Not recorded"}</dd>
          </div>
          <div>
            <dt>Issues</dt>
            <dd>
              {formatMetricCount(summary.issue_metrics.blocking + summary.issue_metrics.warning)}{" "}
              <span>({formatMetricCount(summary.issue_metrics.warning)} warnings)</span>
            </dd>
          </div>
        </dl>
      </div>

      <div className="udmi-metric-groups">
        <SummaryMetricGroup metrics={assets} title="Asset metrics" tone="assets" />
        <PayloadMetricGroup summary={summary} />
        <SummaryMetricGroup metrics={faults} title="Fault metrics" tone="faults" />
      </div>

      <p className="udmi-metric-basis">
        The Observed assets metric counts expected register assets with at least one retained
        expected payload. Unexpected devices are reported separately and never enter expected,
        observed, compliance, payload, fault, or validation-result totals.{" "}
        {summary.unexpected_devices_measured === true ? (
          <strong>
            Unexpected-device measurement was available
            {summary.unexpected_devices_measurement_scope
              ? ` for ${summary.unexpected_devices_measurement_scope}`
              : " for this run"}
            .
          </strong>
        ) : (
          <strong>
            Unexpected-device measurement was unavailable for this run; the displayed 0 does not
            prove that no unexpected publishers exist.
          </strong>
        )}
      </p>

      <section className="udmi-system-summary" aria-labelledby="udmi-system-summary-heading">
        <div>
          <h4 id="udmi-system-summary-heading">Completion by system</h4>
          <p>Successfully validated assets divided by expected assets from the register.</p>
        </div>
        {summary.system_metrics.length > 0 ? (
          <div className="data-table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>System</th>
                  <th>Completion</th>
                  <th>Observed</th>
                  <th>Issues</th>
                </tr>
              </thead>
              <tbody>
                {summary.system_metrics.map((system) => (
                  <tr key={system.system}>
                    <td>{system.system || "Unspecified"}</td>
                    <td>
                      <strong>
                        {formatMetricPercent(
                          system.asset_metrics.successfully_validated,
                          system.asset_metrics.expected,
                        )}
                      </strong>{" "}
                      <span>
                        ({formatMetricCount(system.asset_metrics.successfully_validated)} /{" "}
                        {formatMetricCount(system.asset_metrics.expected)} assets)
                      </span>
                    </td>
                    <td>
                      {formatMetricCount(system.asset_metrics.observed)} /{" "}
                      {formatMetricCount(system.asset_metrics.expected)}
                    </td>
                    <td>
                      {formatMetricCount(
                        system.issue_metrics.blocking + system.issue_metrics.warning,
                      )}{" "}
                      issues ({formatMetricCount(system.issue_metrics.warning)} warnings)
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="section-copy">No system values were stored for this run.</p>
        )}
      </section>
    </article>
  );
}

function defaultReportTitle(
  run: { created_at?: string; job_type?: string; site_id?: string } | null | undefined,
): string {
  const label =
    run?.job_type === "udmi_validation"
      ? "UDMI Validation Report"
      : run?.job_type?.includes("discovery")
        ? "Discovery Report"
        : "Commissioning Report";
  const parsed = run?.created_at ? new Date(run.created_at) : new Date();
  const validDate = Number.isNaN(parsed.getTime()) ? new Date() : parsed;
  const date = new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(validDate);
  const site = run?.site_id && run.site_id !== "demo-site" ? ` - ${run.site_id}` : "";
  return `${label}${site} - ${date}`;
}

function udmiResultsEmptyState(input: {
  error: unknown;
  hasRun: boolean;
  loading: boolean;
  status: string | undefined;
}): { title: string; detail: string } {
  if (input.error) {
    return {
      title: "Could not load validation results",
      detail: input.error instanceof Error ? input.error.message : "The validation request failed.",
    };
  }
  if (!input.hasRun) {
    return {
      title: "No validation run yet",
      detail: "Start a UDMI validation to populate asset, payload, and fault results.",
    };
  }
  if (input.loading) {
    return { title: "Loading validation results...", detail: "Fetching the stored run snapshot." };
  }
  if (input.status === "queued" || input.status === "running") {
    return {
      title: "Validation in progress...",
      detail: "Result rows will appear as the run snapshot is updated.",
    };
  }
  if (input.status === "failed") {
    return {
      title: "Validation failed with no result rows",
      detail:
        "Check the run error above. Any stored summary evidence remains available for export.",
    };
  }
  if (input.status === "cancelled") {
    return {
      title: "Validation was stopped",
      detail: "No payload rows were stored before the run stopped.",
    };
  }
  return {
    title: "Validation completed with no result rows",
    detail: "The run finished, but its stored snapshot contains no asset or payload rows.",
  };
}

function scanPortSpecification(ports: ScanPort[]): string {
  return ports
    .map((entry) => ({ port: entry.port.trim(), protocol: entry.protocol }))
    .filter((entry) => entry.port)
    .map((entry) => `${entry.port}/${entry.protocol}`)
    .join(", ");
}

// Builds discovery run parameters, attaching the authorization contract for
// real scans and the dry_run flag for previews. IP scans also carry the port
// specification. Mirrors the backend safety contract (parameters.authorized).
function buildDiscoveryParameters(
  action: Extract<ModuleRunAction, { kind: "discovery" }>,
  options: {
    authorized: boolean;
    dryRun: boolean;
    scanPorts: ScanPort[];
    targetRows?: IpTargetRow[];
    exclusionRows?: IpTargetRow[];
    provider?: IPDiscoveryProvider;
    nmapProfile?: NmapProfileName;
    captureTopicFilter?: string;
    captureSeconds?: string;
    target?: string;
  },
): Record<string, unknown> {
  const parameters: Record<string, unknown> = {};
  if (options.dryRun) {
    parameters.dry_run = true;
  } else {
    // Boolean shorthand only — the backend stamps the real authenticated
    // principal, so the frontend never fabricates a scan_authorization block.
    parameters.authorized = options.authorized;
  }
  if (action.runKind === "ip") {
    parameters.provider = options.provider ?? "builtin_tcp_connect";
    if (parameters.provider === "operator_managed_nmap") {
      parameters.nmap_profile = options.nmapProfile ?? "tcp_connect_inventory";
    }
    if (
      parameters.provider !== "operator_managed_nmap" ||
      options.nmapProfile !== "host_discovery"
    ) {
      parameters.port_specification = scanPortSpecification(options.scanPorts);
    }
    const targetRows = options.targetRows ?? [];
    const exclusionRows = options.exclusionRows ?? [];
    if (targetRows.length > 0 || exclusionRows.length > 0) {
      const expressions = serializeIpTargetRows(targetRows, exclusionRows);
      parameters.target_expressions = expressions.target_expressions;
      parameters.exclusions = expressions.exclusions;
      // A register-driven scan may still carry exclusions. The backend requires
      // this explicit opt-in before it expands registered addresses, rather than
      // treating an empty target editor as permission to scan them.
      if (expressions.target_expressions.length === 0) {
        parameters.use_register_addresses = true;
      }
      return parameters;
    }
    // Compatibility fallback for existing deep links and saved drafts. A blank
    // target list deliberately scans the imported IP register, but the backend
    // requires that intent on the wire before it will expand those addresses.
    const target = options.target?.trim();
    if (target) {
      if (target.includes("/")) {
        parameters.cidr = target;
      } else if (target.includes("-")) {
        // Split once on the first "-" so the operator's input reaches the
        // backend intact (JS split(limit) would drop any trailing segment).
        const dash = target.indexOf("-");
        parameters.start = target.slice(0, dash).trim();
        parameters.end = target.slice(dash + 1).trim();
      } else {
        parameters.addresses = [target];
      }
    } else {
      parameters.use_register_addresses = true;
    }
  }
  // MQTT discovery: forward the operator's topic filter and capture window so
  // the engine subscribes to the requested topics for the requested duration
  // (mq9nhbzu). The backend reads topic_filter + capture_seconds.
  if (action.runKind === "mqtt") {
    const filter = options.captureTopicFilter?.trim();
    if (filter) {
      parameters.topic_filter = filter;
    }
    // Blank => 0, the backend's "indefinite" sentinel: run until stopped (Stop
    // run) or the message cap. A positive value is a bounded capture window.
    // Anything else ("45s", "abc", "-5") is REJECTED at submit, mirroring the
    // UDMI run-time path — silently coercing it to 0 would turn an intended
    // bounded window into an unbounded background capture with no warning
    // (mq9nhbzu). The thrown Error surfaces through the runMutation error panel.
    const raw = (options.captureSeconds ?? "").trim();
    const seconds = Number(raw);
    if (raw !== "" && !(Number.isFinite(seconds) && seconds > 0)) {
      throw new Error(
        "Run time must be a positive number, or blank to capture until you press Stop run.",
      );
    }
    parameters.capture_seconds = raw === "" ? 0 : seconds;
  }
  return parameters;
}

function buildUdmiValidationParameters(input: {
  captureSeconds: string;
  expectedSchedule: string;
  metadataPayload: string;
  metadataTopic: string;
  pointsetPayload: string;
  pointsetTopic: string;
  statePayload: string;
  stateTopic: string;
  topicDiscoveryAllScopeConfirmed: boolean;
  topicDiscoveryEnabled: boolean;
  topicDiscoveryScope: "bounded" | "all";
  useLiveBroker: boolean;
  useRegister: boolean;
}): Record<string, unknown> {
  // Blank => 0, the backend's "indefinite" sentinel: run until every expected
  // topic has reported a payload, Cancel, or the message cap. A positive value
  // bounds the run to that many seconds. Anything else ("45s", "abc", "-5") is
  // rejected at submit — silently coercing it to the indefinite sentinel would
  // turn an intended 45-second run into an unbounded one with no warning. The
  // thrown Error surfaces through the same runMutation error panel as the
  // parseJsonObject failures below.
  const rawSeconds = input.captureSeconds.trim();
  const parsedSeconds = Number(rawSeconds);
  if (rawSeconds !== "" && !(Number.isFinite(parsedSeconds) && parsedSeconds > 0)) {
    throw new Error(
      "Run time must be a positive number of seconds, or blank to run until all expected topics are captured.",
    );
  }
  const captureSeconds = rawSeconds === "" ? 0 : parsedSeconds;
  const topicDiscoveryParameters: Record<string, unknown> = {};
  if (input.useLiveBroker && input.useRegister && input.topicDiscoveryEnabled) {
    if (input.topicDiscoveryScope === "all" && !input.topicDiscoveryAllScopeConfirmed) {
      throw new Error("Confirm the broader all-topic MQTT scope before starting topic discovery.");
    }
    topicDiscoveryParameters.topic_discovery_enabled = true;
    topicDiscoveryParameters.topic_discovery_scope = input.topicDiscoveryScope;
    topicDiscoveryParameters.topic_discovery_all_scope_confirmed =
      input.topicDiscoveryScope === "all" && input.topicDiscoveryAllScopeConfirmed;
  }
  if (input.useRegister) {
    // Register-driven run: send no pasted schedule/payloads/topics so the
    // backend builds one expected asset per imported mqtt_register row (its
    // wildcard topic, points, units, and Expected schema version). use_register
    // makes the backend refuse (400) when no register import exists, instead of
    // silently validating the packaged sample fixture.
    return {
      capture_seconds: captureSeconds,
      ...topicDiscoveryParameters,
      use_live_broker: input.useLiveBroker,
      use_register: true,
    };
  }
  return {
    capture_seconds: captureSeconds,
    expected_schedule: parseJsonObject(input.expectedSchedule, "Expected schedule JSON"),
    metadata_payload: parseJsonObject(input.metadataPayload, "Metadata payload JSON"),
    metadata_topic: input.metadataTopic,
    pointset_payload: parseJsonObject(input.pointsetPayload, "Pointset payload JSON"),
    pointset_topic: input.pointsetTopic,
    state_payload: parseJsonObject(input.statePayload, "State payload JSON"),
    state_topic: input.stateTopic,
    ...topicDiscoveryParameters,
    use_live_broker: input.useLiveBroker,
  };
}

function parseJsonObject(value: string, label: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(value) as unknown;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch (error) {
    // eslint-disable-next-line preserve-caught-error -- caught message is embedded in the thrown error text; `{ cause }` needs the ES2022 Error lib, beyond this tsconfig's ES2020 target.
    throw new Error(
      `${label} is not valid JSON: ${error instanceof Error ? error.message : "parse failed"}`,
    );
  }
  throw new Error(`${label} must be a JSON object.`);
}

// Structured issue card (ITEM-9): the description reads as the headline, then the
// expected/observed comparison, any status detail, and the suggested action sit
// on their own readable lines — instead of one run-on <strong> string. `context`
// is the eyebrow's secondary label (the payload area, or the asset id). field engineer's
// own word "empty" for a present-but-blank value survives inside expectedObserved
// (built in toIssueRow) byte-identical.
function IssueCard({ issue, context }: { issue: IssueRow; context: string }) {
  const headline = (issue.description ?? "").trim() || issue.message;
  return (
    <div className={`issue-card ${issue.severity}`}>
      <div className="issue-card-body">
        <span>{context ? `${issue.id} · ${context}` : issue.id}</span>
        <strong>{headline}</strong>
        {issue.mismatch && <em className="issue-mismatch">Mismatch</em>}
        {issue.expectedObserved && <small>{issue.expectedObserved}</small>}
        {issue.statusDetail && <small>Status: {issue.statusDetail}</small>}
        {issue.suggestedAction && (
          <small className="issue-suggestion">{issue.suggestedAction}</small>
        )}
      </div>
    </div>
  );
}

// The MQTT discovery inspector's payload panel: the real last_payload OBJECT for
// the selected topic (never a re-parse of the stringified "Raw Payload" cell).
// Mirrors the UDMI observed-payload block (pre + Explore JSON tree). Honesty:
// a non-JSON payload is stored as a presence marker, so we say exactly that and
// render no tree; a JSON scalar/list is wrapped by the engine under `_value`,
// so we unwrap it before display.
function MqttPayloadPanel({ topic }: { topic: DiscoveryRowRecord }) {
  const payload = topic.last_payload;
  const topicName = String(topic.topic ?? "topic");
  const isObject = payload !== null && typeof payload === "object";
  const rawPresent = isObject && (payload as Record<string, unknown>)._raw_present === true;
  const hasValueWrap = isObject && "_value" in (payload as Record<string, unknown>);
  const display = hasValueWrap ? (payload as Record<string, unknown>)._value : payload;
  return (
    <div className="payload-inspector">
      <h4>Last payload on {topicName}</h4>
      {rawPresent ? (
        <p className="section-copy">
          Non-JSON payload observed. The engine stores a presence marker, not the raw bytes.
        </p>
      ) : (
        <>
          <pre className="payload-cell">{JSON.stringify(display, null, 2)}</pre>
          <details className="json-inspector">
            <summary>Explore JSON tree</summary>
            <JsonTree value={display} />
          </details>
        </>
      )}
    </div>
  );
}

// One aligned compare cell: a single JSON line coloured into syntax spans, with
// the presence-diff mark class (only-expected amber / only-observed red) and, on
// an engine-flagged point row, the red flagged tint. A null line is a filler that
// keeps the two panels row-aligned. textContent stays the full line text, so the
// mark-class assertions in the tests still read the key names off each cell.
function AlignedDiffCell({ line, flagged }: { line: AlignedRow["expected"]; flagged: boolean }) {
  const markClass = line?.mark ? ` ${line.mark}` : "";
  return (
    <div className={`payload-diff-line${markClass}${flagged ? " flagged" : ""}`}>
      {line
        ? tokenizeJsonLine(line.text).map((token, index) => (
            <span className={`json-${token.kind}`} key={index}>
              {token.text}
            </span>
          ))
        : ""}
    </div>
  );
}

function ExpectedTemplateContext() {
  return (
    <p className="section-copy payload-template-context">
      The expected timestamp is a schema-valid template value created when this result view was
      built. Freshness checks use the observed payload timestamp against this tool&apos;s receive
      time. The expected side keeps its own build value and never borrows broker data. Registered
      values are shown where known.
    </p>
  );
}

// Expected-vs-observed UDMI payload panels (ITEM-8). When a payload was observed,
// the two sides are aligned LINE-FOR-LINE inside ONE scroll container (so they
// scroll together), JSON-syntax-coloured, with the presence diff (amber =
// expected-only key, red = observed-only key) and an honest red highlight on rows
// whose exact evidence path the engine flagged (or the UI conservatively derived). VALUES
// are never diffed — the expected side is a template of sentinels — so a healthy
// payload is never painted red. When nothing was observed there is no comparison
// to make (an observation-shaped claim would be dishonest), so it falls back to a
// plain expected panel.
function PayloadComparePanels({
  expected,
  observed,
  observedPresent,
  issues,
}: {
  expected: unknown;
  observed: unknown;
  observedPresent: boolean;
  issues: IssueRow[];
}) {
  // Red rows come only from an exact evidence path. New issues may carry that
  // pointer directly; older unit issues derive conservative `units` candidates
  // from match_basis + point_name so the parent point row stays unhighlighted.
  const flaggedPaths = new Set<string>();
  for (const issue of issues) {
    const exactPath = normaliseEvidencePath(issue.evidencePath);
    if (exactPath) {
      flaggedPaths.add(exactPath);
      continue;
    }
    if (!issue.pointName) {
      continue;
    }
    const point = issue.pointName.replace(/~/g, "~0").replace(/\//g, "~1");
    const suffix = issue.matchBasis?.toLocaleLowerCase().includes("unit") ? "/units" : "";
    for (const prefix of [
      "/pointset/points",
      "/points",
      "/metadata/pointset/points",
      "/metadata/points",
    ]) {
      flaggedPaths.add(`${prefix}/${point}${suffix}`);
    }
  }
  const aligned =
    observedPresent && isPlainObject(expected) && isPlainObject(observed)
      ? alignPayloadDiff(expected, observed, flaggedPaths)
      : null;

  if (!aligned) {
    return (
      <div className="payload-compare">
        <div>
          <h6>Expected UDMI template</h6>
          <ExpectedTemplateContext />
          <pre className="payload-cell">{expected ? JSON.stringify(expected, null, 2) : "—"}</pre>
        </div>
        <div>
          <h6>Observed</h6>
          {/* Only claim "not captured" when nothing WAS observed. A payload can
              be present while the aligned diff is unavailable (e.g. the expected
              template facet is null / empty), and hiding real captured evidence
              behind a false "not captured" would contradict the row's Observed:
              Yes. Show the observed JSON in that case. */}
          {observedPresent && observed !== null && observed !== undefined ? (
            <pre className="payload-cell">{JSON.stringify(observed, null, 2)}</pre>
          ) : (
            <pre className="payload-cell">not captured</pre>
          )}
        </div>
      </div>
    );
  }

  const hasFlagged = aligned.some((row) => row.flagged);
  return (
    <>
      <ExpectedTemplateContext />
      <div className="payload-compare-aligned">
        <div className="payload-compare-grid">
          <div className="payload-compare-head">Expected UDMI template</div>
          <div className="payload-compare-head">Observed</div>
          {aligned.map((row, index) => (
            <Fragment key={index}>
              <AlignedDiffCell flagged={row.flagged} line={row.expected} />
              <AlignedDiffCell flagged={row.flagged} line={row.observed} />
            </Fragment>
          ))}
        </div>
      </div>
      {observed !== null && observed !== undefined && (
        <details className="json-inspector">
          <summary>Explore observed JSON tree</summary>
          <JsonTree value={observed} />
        </details>
      )}
      <p className="section-copy payload-diff-legend">
        Highlights mark keys present on only one side (amber = expected only, red = observed only).
        {hasFlagged ? " Rows in red match exact validation evidence paths." : ""} Values are not
        compared here — expected values are template sentinels; see the issues above for value
        checks.
      </p>
    </>
  );
}

function normaliseEvidencePath(path: string | null | undefined): string | null {
  const trimmed = path?.trim();
  if (!trimmed) {
    return null;
  }
  if (trimmed.startsWith("/")) {
    return trimmed;
  }
  const withoutRoot = trimmed.replace(/^\$\.?/, "");
  const segments = withoutRoot.split(".").filter(Boolean);
  return segments.length > 0
    ? `/${segments.map((segment) => segment.replace(/~/g, "~0").replace(/\//g, "~1")).join("/")}`
    : null;
}

function JsonTree({ value }: { value: unknown }) {
  if (value === null || typeof value !== "object") {
    return <span>{JSON.stringify(value)}</span>;
  }
  return (
    <ul className="json-tree">
      {Object.entries(value).map(([key, child]) => (
        <li key={key}>
          {child !== null && typeof child === "object" ? (
            <details>
              <summary>{key}</summary>
              <JsonTree value={child} />
            </details>
          ) : (
            <>
              <strong>{key}</strong>: {JSON.stringify(child)}
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

// A present-but-empty expected/observed value ("") is flagged as the explicit
// word "empty" (field engineer's own word, ISSUE-10) rather than rendering as blank; an
// absent value (null/undefined) stays "n/a". Keeps the comparison segment
// whenever EITHER side is present so an all-empty pair no longer drops the
// whole clause and leaves a dangling "observed " before the suggested action.
function issueDisplayValue(value: string | null | undefined): string {
  return value === "" ? "empty" : (value ?? "n/a");
}

function toIssueRow(issue: ValidationIssueRecord): IssueRow {
  const expectedObserved =
    issue.expected_value != null || issue.observed_value != null
      ? `Expected ${issueDisplayValue(issue.expected_value)}, observed ${issueDisplayValue(issue.observed_value)}`
      : undefined;
  const mismatch =
    (issue.expected_value != null || issue.observed_value != null) &&
    String(issue.expected_value ?? "") !== String(issue.observed_value ?? "");
  // The joined one-liner is still built for the Inspector detail list;
  // the same fragments are also carried structured so the issue CARDS can render
  // them as separate lines instead of one run-on string (ITEM-9).
  const details = [
    issue.description,
    issue.status_detail ? `Status: ${issue.status_detail}` : null,
    expectedObserved ?? null,
    issue.suggested_action,
  ]
    .filter(Boolean)
    .join(" ");
  return {
    area: issue.issue_type.replace(/_/g, " "),
    assetId: issue.asset_id ?? "Unknown asset",
    id: issue.issue_id,
    message: details,
    severity: toIssueSeverity(issue.severity),
    description: issue.description,
    statusDetail: issue.status_detail ?? null,
    expectedObserved,
    mismatch,
    suggestedAction: issue.suggested_action ?? null,
    pointName: issue.point_name ?? null,
    matchBasis: issue.match_basis ?? null,
    evidencePath: issue.evidence_path ?? null,
  };
}

function toIssueSeverity(severity: ValidationIssueRecord["severity"]): IssueRow["severity"] {
  if (severity === "critical") {
    return "critical";
  }
  if (severity === "high" || severity === "medium") {
    return "major";
  }
  return "minor";
}

function formatSummaryValue(value: unknown): string {
  if (typeof value === "number" || typeof value === "string") {
    return String(value);
  }
  return "Pending";
}

// Seconds elapsed since `startIso` (the run's created_at). While `running`, a 1s
// interval re-renders so the value ticks; once stopped it freezes to
// frozenEndIso - startIso (updated_at - created_at) with no interval. Clamped at
// 0 so client/server clock skew can never show a negative timer. On the portable
// exe both clocks are the same host, so skew is not a practical concern (ITEM-6).
function useElapsedSeconds(
  startIso: string | undefined,
  running: boolean,
  frozenEndIso: string | undefined,
): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) {
      return;
    }
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running]);
  if (!startIso) {
    return 0;
  }
  const start = Date.parse(startIso);
  if (Number.isNaN(start)) {
    return 0;
  }
  const frozenEnd = frozenEndIso ? Date.parse(frozenEndIso) : Number.NaN;
  const end = running || Number.isNaN(frozenEnd) ? now : frozenEnd;
  return Math.max(0, Math.floor((end - start) / 1000));
}

// h:mm:ss for the run monitor's Elapsed entry.
function formatElapsed(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const minutes = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const seconds = String(s % 60).padStart(2, "0");
  return `${Math.floor(s / 3600)}:${minutes}:${seconds}`;
}

// The capture window a UDMI run actually used, from result_summary
// capture_mode + capture_window_seconds (stamped by the engine at run end).
// NOT formatSummaryValue: a null capture_window_seconds means an INDEFINITE
// window, not a pending value. Returns null ("render nothing") when no capture
// was attempted or the summary has not landed yet.
function formatCaptureWindow(summary: Record<string, unknown>): string | null {
  const mode = summary.capture_mode;
  const seconds = summary.capture_window_seconds;
  if (mode === "indefinite") {
    return "until all topics reported (indefinite)";
  }
  if (typeof seconds !== "number") {
    return null;
  }
  // The inline fallback rewrites a blank/0 (indefinite) request to its safety
  // ceiling BEFORE the engine runs, so capture_mode reads "bounded" — the
  // indefinite_bounded_inline flag is the only honest record of the cap.
  if (summary.indefinite_bounded_inline === true) {
    return `capped at ${seconds} s (indefinite requested; inline run)`;
  }
  if (mode === "bounded") {
    return `${seconds} s (bounded)`;
  }
  if (mode === "indefinite_bounded_no_cancel") {
    return `capped at ${seconds} s (indefinite requested; no cancel path)`;
  }
  return null;
}

function formatCaptureOutcome(
  status: string | undefined,
  summary: Record<string, unknown>,
): string | null {
  if (summary.broker_capture_attempted !== true) {
    return null;
  }
  const terminationReason =
    typeof summary.termination_reason === "string" ? summary.termination_reason : null;
  const eligible =
    typeof summary.acceptance_eligible === "boolean"
      ? status === "succeeded" && summary.acceptance_eligible
      : status === "succeeded" &&
        summary.validation_incomplete !== true &&
        summary.window_completed === true &&
        terminationReason === "window_elapsed";
  if (eligible) {
    return "Cadence acceptance: eligible";
  }
  const reason = terminationReason ? humanizeStage(terminationReason) : "capture incomplete";
  return `Cadence acceptance: ineligible (${reason})`;
}

function renderCell(
  row: Record<string, string>,
  column: string,
  onCopyPayload: (payload: string, label: string) => void,
) {
  if (column === "Raw Payload" && row[column]) {
    return (
      <button
        className="secondary-button compact"
        onClick={() => onCopyPayload(row[column], row.Asset ?? row.Topic ?? "Selected")}
        type="button"
      >
        Copy payload
      </button>
    );
  }
  if (column === "Detailed Status") {
    const forbidden = forbiddenOpenPorts(row[column]);
    const unexpected = unexpectedOpenPorts(row[column]);
    const missing = missingExpectedPorts(row[column]);
    const expectedOk = expectedPortsOk(row[column]);
    // A register-listed host that answered nothing: amber/inconclusive, never a
    // red "offline" claim — a TCP-connect miss is not proof the host is absent.
    const expectedSilent = expectedByRegisterSilent(row[column]);
    if (forbidden || unexpected || missing || expectedOk || expectedSilent) {
      return (
        <>
          {row[column]}
          {forbidden && <span className="chip red"> Forbidden ports open: {forbidden}</span>}
          {unexpected && <span className="chip amber"> Unexpected ports open: {unexpected}</span>}
          {missing && <span className="chip red"> Missing expected ports: {missing}</span>}
          {expectedOk && <span className="chip green"> Expected ports {expectedOk}</span>}
          {expectedSilent && (
            <span className="chip amber"> Expected by register — no response</span>
          )}
        </>
      );
    }
  }
  return row[column];
}

function parsePublishValue(value: string): string | number | boolean {
  const trimmed = value.trim();
  if (trimmed === "true") {
    return true;
  }
  if (trimmed === "false") {
    return false;
  }
  const numeric = Number(trimmed);
  return Number.isFinite(numeric) && trimmed !== "" ? numeric : trimmed;
}

// Composes one config payload containing the primary point/value plus any extra
// pairs (mq9n11wi). Starts from the operator's base payload JSON (so any extra
// structure they typed is preserved) and merges every pair under
// pointset.points.<name> = { set_value }. Falls back to a fresh object if the
// base payload is not valid JSON. The backend confirm path still verifies only
// the primary point.
function buildMultiPointPayload(
  basePayload: string,
  primaryPoint: string,
  primaryValue: string,
  extras: PointValuePair[],
): string {
  const pairs = [{ point: primaryPoint, value: primaryValue }, ...extras].filter(
    (pair) => pair.point.trim() !== "",
  );
  // No extra pairs and the base payload already carries the single point: leave
  // the operator's payload untouched (preserves the original single-point flow).
  if (extras.every((pair) => pair.point.trim() === "")) {
    return basePayload;
  }
  let root: Record<string, unknown>;
  try {
    const parsed = JSON.parse(basePayload) as unknown;
    root =
      parsed && typeof parsed === "object" && !Array.isArray(parsed)
        ? (parsed as Record<string, unknown>)
        : {};
  } catch {
    root = {};
  }
  const pointset = isRecord(root.pointset) ? { ...root.pointset } : {};
  const points = isRecord(pointset.points) ? { ...pointset.points } : {};
  for (const pair of pairs) {
    points[pair.point.trim()] = { set_value: parsePublishValue(pair.value) };
  }
  pointset.points = points;
  root.pointset = pointset;
  return JSON.stringify(root);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

// One latest-payload-per-topic row for the MQTT Explorer-like capture panel.
type CaptureRow = {
  topic: string;
  asset: string;
  lastSeen: string;
  messageCount: string;
  payload: string;
};

function mqttCaptureRow(topic: DiscoveryRowRecord): CaptureRow {
  const attributes = (topic.attributes as Record<string, unknown> | undefined) ?? {};
  const lastPayload = topic.last_payload;
  const payload =
    lastPayload && typeof lastPayload === "object" && Object.keys(lastPayload).length > 0
      ? JSON.stringify(lastPayload)
      : "";
  return {
    asset: stringOrDash(attributes.device_ref),
    lastSeen: topic.created_at ? String(topic.created_at) : "—",
    messageCount: stringOrDash(topic.message_count),
    payload,
    topic: stringOrDash(topic.topic),
  };
}

function stringOrDash(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  return typeof value === "string" ? value : String(value);
}

function captureRowsToCsv(rows: CaptureRow[]): string {
  const header = ["Topic", "Asset", "Last Seen", "Message Count", "Latest Payload"];
  const escape = (value: string): string => `"${value.replace(/"/g, '""')}"`;
  const lines = [header.map(escape).join(",")];
  for (const row of rows) {
    lines.push(
      [row.topic, row.asset, row.lastSeen, row.messageCount, row.payload].map(escape).join(","),
    );
  }
  return lines.join("\r\n");
}

/**
 * Drives an authenticated file download. Plain `<a download href>` anchors
 * navigate outside fetch(), so they cannot carry the X-API-Key header and
 * 401 in hosted deployments; this routes downloads through downloadFile().
 */
function useFileDownload(apiClient: SessionBoundApiClient) {
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const generationRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      generationRef.current += 1;
      controllerRef.current?.abort();
      controllerRef.current = null;
    },
    [],
  );

  const download = useCallback(
    async ({
      fallbackFilename,
      init,
      isCurrent = () => true,
      key,
      path,
    }: {
      fallbackFilename: string;
      init?: RequestInit;
      isCurrent?: () => boolean;
      key: string;
      path: string;
    }) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      const generation = generationRef.current + 1;
      generationRef.current = generation;
      setPendingKey(key);
      setError(null);
      try {
        const { blob, filename } = await downloadFile(path, init, {
          client: apiClient,
          signal: controller.signal,
        });
        if (generation !== generationRef.current || !isCurrent()) {
          return;
        }
        triggerBlobDownload(blob, filename ?? fallbackFilename);
      } catch (cause) {
        if (generation === generationRef.current && !controller.signal.aborted && isCurrent()) {
          setError(cause instanceof Error ? cause.message : "Download failed.");
        }
      } finally {
        if (generation === generationRef.current) {
          controllerRef.current = null;
          setPendingKey(null);
        }
      }
    },
    [apiClient],
  );

  const reset = useCallback(() => {
    generationRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
    setPendingKey(null);
    setError(null);
  }, []);

  return { download, error, pendingKey, reset };
}

function triggerBlobDownload(blob: Blob, filename: string): void {
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
}

function buildResultDetailItems(
  route: string,
  row: Record<string, string>,
  live: boolean,
  // Live UDMI only: the merged issue/payload groups the row was built from, so
  // the detail can show the actual issue text instead of a bare count.
  assetGroups: MergedAssetGroup[] | null = null,
): DetailItem[] {
  if (route === "ip-scanner" || route === "ip-scanner-sct") {
    // The per-host detail surfaced by the results "View" button. MAC/Hostname are
    // best-effort enrichment: the engine emits "—" (blank) when no ARP entry or
    // PTR record exists, so a blank here is honest, never fabricated.
    const items: DetailItem[] = [
      { label: "Asset", value: row.Asset ?? "—" },
      { label: "Observed IP", value: row["Observed IP"] ?? "—" },
      { label: "MAC Address", value: row["MAC Address"] ?? "—" },
      { label: "Hostname", value: row.Hostname ?? "—" },
      { label: "Open ports", value: row.Ports ?? "—" },
      { label: "Match basis", value: row["Match Basis"] ?? "—" },
      { label: "Last seen", value: row["Last Seen"] ?? "—" },
      { label: "Detailed status", value: row["Detailed Status"] ?? "—" },
    ];
    // Surface any policy-flagged ports the engine stamped into status_detail,
    // mirroring the table cell chips so the detail view is self-contained.
    const forbidden = forbiddenOpenPorts(row["Detailed Status"]);
    const unexpected = unexpectedOpenPorts(row["Detailed Status"]);
    const missing = missingExpectedPorts(row["Detailed Status"]);
    const expectedOk = expectedPortsOk(row["Detailed Status"]);
    if (forbidden) {
      items.push({ label: "Forbidden ports open", value: forbidden });
    }
    if (unexpected) {
      items.push({ label: "Unexpected ports open", value: unexpected });
    }
    if (missing) {
      items.push({ label: "Missing expected ports", value: missing });
    }
    if (expectedOk) {
      items.push({ label: "Expected ports", value: expectedOk });
    }
    return items;
  }

  if (route === "bacnet-scanner" || route === "bacnet-discovery-sct") {
    return [
      { label: "Device", value: row.Device ?? "Selected BACnet device" },
      { label: "Instance", value: row.Instance ?? "Unknown" },
      { label: "Address", value: row.Address ?? "—" },
      { label: "IP Address", value: row["IP Address"] ?? "—" },
      { label: "Network Number", value: row["Network Number"] ?? "—" },
      { label: "Vendor", value: row.Vendor ?? "—" },
      { label: "Objects indexed", value: row.Objects ?? "Pending" },
      {
        label: "Last discovered",
        value: row.Discovered ?? row["Device Last Discovered"] ?? "Not recorded",
      },
      {
        label: live ? "Note" : "Object drilldown",
        value: live
          ? "Object-level present values are in the per-run points endpoint; comparison verdicts come from a validation run."
          : "Show object type, instance, object name, present value, units, reliability, status flags, priority array, and timestamp.",
      },
    ];
  }

  if (route === "mqtt-scanner" || route === "mqtt-discovery-sct") {
    // Per-message metadata rides hidden row keys (see mqttRowsFromResults).
    // Honesty-rule wording is load-bearing: NEVER label a timestamp "Published"
    // (MQTT 3.1.1 has no publish time on the wire), and state that delivery QoS
    // is capped by our subscription QoS. Old runs carry no keys -> "Not recorded".
    const retained =
      row.__retained === "yes"
        ? "Yes — replayed from the broker's retained store"
        : row.__retained === "no"
          ? "No — arrived live during the capture window"
          : "Not recorded (run predates metadata capture)";
    const deliveryQos = row.__qos
      ? `${row.__qos} (broker-to-tool delivery; capped by this tool's subscription QoS${
          row.__subscribeQos ? ` ${row.__subscribeQos}` : ""
        } — the publisher's QoS may be higher)`
      : "Not recorded";
    const receivedAt = row.__receivedAt
      ? `${new Date(row.__receivedAt).toLocaleString()} (this tool's clock — MQTT 3.1.1 carries no broker publish timestamp)`
      : "Not recorded";
    return [
      { label: "Topic", value: row.Topic ?? "State, metadata, or pointset topic" },
      { label: "Asset", value: row.Asset ?? "—" },
      { label: "Messages", value: row["Message Count"] ?? "Pending" },
      { label: "Last payload seen", value: row["Last Payload Seen"] ?? "Not recorded" },
      { label: "Retained", value: retained },
      { label: "Delivery QoS", value: deliveryQos },
      { label: "Received at", value: receivedAt },
      { label: "Connection status", value: row["Detailed Status"] ?? "Pending" },
      {
        label: "Note",
        value: live
          ? "Raw payloads are captured as observed. Type/interval verdicts come from a validation run, not discovery."
          : "Show decoded JSON, extracted point names, present values, units, timestamp freshness, and schema warnings together.",
      },
    ];
  }

  if (route === "udmi-validation") {
    if (live) {
      if (row.__category === "unexpected-devices") {
        return [
          { label: "Publisher", value: row.Asset ?? "Unknown publisher" },
          { label: "Device ID", value: row.__unexpectedId ?? "Not recorded" },
          { label: "Topic root", value: row.Topic ?? "Not recorded" },
          { label: "Observed topics", value: row.__topics || row.Topic || "Not recorded" },
          {
            label: "Last seen",
            value: row.__lastSeen ? formatAbsoluteTime(row.__lastSeen) : "Not recorded",
          },
          { label: "Category", value: "Unexpected device (outside expected register)" },
        ];
      }
      // The row only carries formatted strings; the actual issue text lives in
      // the merged groups. Rows were built as Asset=assetId and
      // Payload=`UDMI ${payloadType}`, so both joins are exact-match safe.
      const issues =
        assetGroups
          ?.find((group) => group.assetId === row.Asset)
          ?.payloadTypes.find((entry) => `UDMI ${entry.payloadType}` === row.Payload)?.issues ?? [];
      // 1-2 issues: show the text inline so a View answers "what failed" without
      // more digging. More: point at the per-asset issue detail below the table.
      const issueItems: DetailItem[] =
        issues.length === 0
          ? [
              {
                label: "Live data view",
                value:
                  "Derived from the validation run's real payload views and issues — expand the asset in the issues panel for expected-vs-observed detail.",
              },
            ]
          : issues.length <= 2
            ? issues.map((issue) => ({ label: issue.id, value: issue.message }))
            : [
                {
                  label: "Issue detail",
                  value: `${issues.length} issues — see the issue details below the table.`,
                },
              ];
      return [
        { label: "Asset", value: row.Asset ?? "Selected MQTT asset" },
        { label: "Payload type", value: row.Payload ?? "—" },
        { label: "Observed", value: row.Observed ?? "—" },
        { label: "Issues", value: row.Issues ?? "0" },
        { label: "Result", value: row.Result ?? "Pending" },
        ...issueItems,
      ];
    }
    return [
      { label: "Asset", value: row.Asset ?? "Selected MQTT asset" },
      { label: "Topic", value: row.Topic ?? "State, metadata, or pointset topic" },
      { label: "Last payload", value: row["Payload Last Seen"] ?? "Not recorded" },
      { label: "Messages", value: row["Message Count"] ?? "Pending" },
      { label: "Result", value: row.Result ?? "Pending" },
      {
        label: "Live data view",
        value:
          "Run-level results and live issues come from the validation run; the per-asset rows below are a labelled sample.",
      },
    ];
  }

  if (route === "data-validation") {
    return [
      { label: "Asset", value: row.Asset ?? "Selected asset" },
      { label: "Point", value: row.Point ?? "Selected point" },
      { label: "BACnet value", value: row.BACnet ?? "Not available" },
      { label: "MQTT value", value: row.MQTT ?? "Not available" },
      {
        label: "Comparison logic",
        value:
          "Comparison verdicts live in the validation run result_summary and issues. The rows below are a labelled sample.",
      },
    ];
  }

  if (route === "reports") {
    return [
      { label: "Report", value: row.Report ?? "Selected report" },
      { label: "Source", value: row.Source ?? "Selected source" },
      { label: "Status", value: row.Status ?? "Pending" },
      { label: "File", value: row.File ?? "Not generated" },
      {
        label: "Outputs",
        value: "Excel report for filtering and Word report for formal handover.",
      },
    ];
  }

  return Object.entries(row).map(([label, value]) => ({ label, value }));
}
