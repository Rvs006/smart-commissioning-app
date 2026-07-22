import { ChangeEvent, Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  cancelRun,
  createImport,
  createReport,
  deleteUdmiSchemaSet,
  downloadFile,
  getDiscoveryResults,
  getDiscoveryRun,
  getDiscoveryTopics,
  getDiscoveryTopicsXlsxPath,
  getImportErrors,
  getLatestImport,
  getValidationIssues,
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
  startDiscoveryRun,
  startValidationRun,
  uploadUdmiSchemaSet,
  DiscoveryRowRecord,
  ReportSummary,
  ReportFormat,
  ReportType,
  UdmiAssetPayloadView,
  ValidationIssueRecord,
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
  isTerminalStatus,
  runPollInterval,
  toHealthState,
} from "./runFormat";
import { alignPayloadDiff, isPlainObject, tokenizeJsonLine, type AlignedRow } from "./payloadDiff";
import { useRunEvents } from "./useRunEvents";
import { ENGINEER_REQUIRED_TOOLTIP, useSession } from "../../app/sessionContext";

type ModulePageProps = {
  moduleRoute: string;
};

type CopyFeedback = {
  message: string;
  severity: "success" | "warning";
};

type DetailItem = {
  label: string;
  value: string;
};

type ScanPort = {
  port: string;
  protocol: "tcp" | "udp";
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
  runId: string;
  kind: "discovery" | "validation";
  restored?: boolean;
};

// The module page is split into three stages so the operator works one screen
// at a time instead of scrolling a single long page of every control at once.
type ModuleStep = "setup" | "run" | "results";

const DISCOVERY_ROUTES = new Set(["ip-scanner", "bacnet-discovery", "mqtt-discovery"]);

// A large register can reject hundreds of rows. Render the first N and state the
// honest remainder count rather than building pagination for a pre-1.0 fix:
// fixing the listed rows and re-uploading surfaces the rest.
const IMPORT_ERROR_DISPLAY_CAP = 50;

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
  const { canEngineer } = useSession();
  const queryClient = useQueryClient();
  const module = getModuleByRoute(moduleRoute);
  const workspace = moduleWorkspaces[moduleRoute];
  const isDiscoveryModule = DISCOVERY_ROUTES.has(module.route);
  const [selectedImportType, setSelectedImportType] = useState<ImportType | "">("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [importOutcome, setImportOutcome] = useState<ImportBatchSummary | null>(null);
  const [runOutcome, setRunOutcome] = useState<string | null>(null);
  const [lastReport, setLastReport] = useState<ReportSummary | null>(null);
  const [activeRun, setActiveRun] = useState<ActiveRun | null>(null);
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
  const [publishPointsetTopic, setPublishPointsetTopic] = useState("demo-site/b1/ahu-1000001/events/pointset");
  const [publishWaitSeconds, setPublishWaitSeconds] = useState("5");
  const [scanPorts, setScanPorts] = useState<ScanPort[]>(defaultScanPorts);
  const [scanAuthorized, setScanAuthorized] = useState(false);
  const [scanDryRun, setScanDryRun] = useState(false);
  const [scanTarget, setScanTarget] = useState("");
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
  const [udmiStateTopic, setUdmiStateTopic] = useState("demo-site/b1/ahu-1000001/state");
  const [udmiMetadataTopic, setUdmiMetadataTopic] = useState("demo-site/b1/ahu-1000001/metadata");
  const [udmiPointsetTopic, setUdmiPointsetTopic] = useState("demo-site/b1/ahu-1000001/events/pointset");
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
  const [selectedResultIndex, setSelectedResultIndex] = useState(0);
  // Results-table client-side filter (ISSUE-4): free-text (substring across the
  // visible cells, or an MQTT wildcard against the Topic column) plus a verdict
  // tone filter. Row selection stays positional into the FULL resultRows, so the
  // filtered view preserves original indices (see visibleResultRows).
  const [resultsTextFilter, setResultsTextFilter] = useState("");
  const [resultsToneFilter, setResultsToneFilter] = useState("all");
  // Inspector facet filters (ITEM-10), udmi-validation only: by asset type, by
  // seen/not-seen, and by ONLINE/OFFLINE. Composed on top of the text + verdict
  // filter above and applied to BOTH the results table and the drill-down list.
  const [resultsAssetTypeFilter, setResultsAssetTypeFilter] = useState("all");
  const [resultsSeenFilter, setResultsSeenFilter] = useState("all");
  const [resultsStateFilter, setResultsStateFilter] = useState("all");
  // Per-row "View" opens this result in a modal detail dialog (mqe-view). null =
  // closed; the clicked row's already-formatted cells drive buildResultDetailItems.
  const [detailRow, setDetailRow] = useState<Record<string, string> | null>(null);
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
  // PDF default: the field deliverable is a human-readable handover document
  // (ask 2026-07-14); Word/Excel/zip remain for editable/evidence workflows.
  const [reportExportFormat, setReportExportFormat] = useState<ReportFormat>("pdf");
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
  // Per inspector payload-type-group DOM node, keyed `${assetId}:${payloadType}`,
  // so selecting a live-UDMI results row can expand its asset and scroll straight
  // to that payload's issues (ITEM-D). A ref map, not getElementById: asset ids
  // are arbitrary imported field data, unsafe to trust as DOM element ids.
  const payloadGroupRefs = useRef(new Map<string, HTMLDivElement>());
  const templateDownload = useFileDownload();
  const reportDownload = useFileDownload();
  const exportDownload = useFileDownload();
  const schemaTemplateDownload = useFileDownload();

  const profilesQuery = useQuery({
    queryFn: listImportProfiles,
    queryKey: ["import-profiles"],
  });

  // SSE-first run progress for the active run. status/stage/progress update
  // live from the stream; on stream error/unsupported, sseActive flips false
  // and the queries below resume the proven 1.5s polling (no regression).
  const runEvents = useRunEvents(activeRun?.runId, Boolean(activeRun));
  const sseEvent = runEvents.event;
  const sseDriving = runEvents.sseActive && sseEvent !== null;

  // Validation run monitor — polls until terminal (the proven 1.5s pattern,
  // generalized to any validation run, not only UDMI). While SSE is the live
  // source we pause the interval; if SSE drops we fall back to polling.
  const validationRunQuery = useQuery({
    enabled: Boolean(activeRun) && activeRun?.kind === "validation",
    queryFn: () => getValidationRun(activeRun?.runId ?? ""),
    queryKey: ["validation-run", activeRun?.runId],
    refetchInterval: (query) =>
      runPollInterval({
        reachedTerminal: runEvents.reachedTerminal,
        recordTerminal: isTerminalStatus(query.state.data?.status),
        sseDriving,
      }),
  });

  // Discovery run monitor — same polling contract, against the discovery
  // status endpoint, so queued/running discovery runs update live.
  const discoveryRunQuery = useQuery({
    enabled: Boolean(activeRun) && activeRun?.kind === "discovery",
    queryFn: () => getDiscoveryRun(activeRun?.runId ?? ""),
    queryKey: ["discovery-run", activeRun?.runId],
    refetchInterval: (query) =>
      runPollInterval({
        reachedTerminal: runEvents.reachedTerminal,
        recordTerminal: isTerminalStatus(query.state.data?.status),
        sseDriving,
      }),
  });

  const activeRunRecord =
    activeRun?.kind === "discovery" ? discoveryRunQuery.data : validationRunQuery.data;
  // Prefer the live SSE frame for status/stage/progress; fall back to the
  // polled record for those fields and for everything else (result_summary).
  const activeRunStatus = (sseDriving ? sseEvent?.status : undefined) ?? activeRunRecord?.status;
  const activeRunStage = (sseDriving ? sseEvent?.stage : undefined) ?? activeRunRecord?.stage;
  const activeRunProgress =
    (sseDriving ? sseEvent?.progress_percent : undefined) ?? activeRunRecord?.progress_percent ?? 0;
  const activeRunError = (sseDriving ? sseEvent?.error_message : undefined) ?? activeRunRecord?.error_message;
  const activeRunTerminal = isTerminalStatus(activeRunStatus);

  // Elapsed timer + progress presentation for the active run (ITEM-6). The run
  // monitor renders live now that a run is started in the background (ITEM-4), so
  // a stuck-at-15% bar would otherwise be the face of every run. While running,
  // the timer ticks from the run's created_at; once terminal it freezes to
  // updated_at - created_at. A bounded capture fills over its own window (never
  // claiming 100% before the terminal flip); an indefinite/unknown run shows an
  // active sweep. Clock source is the polled run record, not the SSE frame (the
  // frame carries no created_at).
  const runIsActive = Boolean(activeRun) && !activeRunTerminal;
  const activeRunElapsedSeconds = useElapsedSeconds(
    activeRunRecord?.created_at,
    runIsActive,
    activeRunRecord?.updated_at,
  );
  const captureSecondsParam =
    typeof activeRunRecord?.parameters?.capture_seconds === "number"
      ? (activeRunRecord.parameters.capture_seconds as number)
      : undefined;
  const boundedCapture = runIsActive && captureSecondsParam !== undefined && captureSecondsParam > 0;
  // Fill over the capture window while running, but never past 99% until the run
  // actually reports terminal — the real progress_percent still wins if higher.
  const progressWidth = boundedCapture
    ? Math.max(activeRunProgress, Math.min(99, (activeRunElapsedSeconds / captureSecondsParam) * 100))
    : activeRunProgress;
  const progressIndeterminate = runIsActive && !boundedCapture;

  // Only a run the operator STARTED here this session hard-blocks a second start
  // (ITEM-4: prevent an accidental parallel capture). A REHYDRATED run (restored
  // from a prior visit) still shows its live monitor + Stop control, but must NOT
  // gate Execute: a run fossilized at running/queued — a hosted worker that died
  // with its dispatch markers, so the startup sweep leaves it alone — would
  // otherwise disable Execute forever with no UI escape (the cancel flag it sets
  // is never observed). Gating only freshly-started runs keeps ITEM-4's guard
  // where it belongs and can never fossilize into a permanent lock.
  const startedRunActive = runIsActive && !activeRun?.restored;

  // Validation issues — fetched only once the validation run is terminal.
  const validationIssuesQuery = useQuery({
    enabled:
      Boolean(activeRun) &&
      activeRun?.kind === "validation" &&
      isTerminalStatus(validationRunQuery.data?.status),
    queryFn: () => getValidationIssues(activeRun?.runId ?? ""),
    queryKey: ["validation-issues", activeRun?.runId],
  });

  // Discovery results — fetched only once the discovery run is terminal.
  const discoveryResultsQuery = useQuery({
    enabled:
      Boolean(activeRun) &&
      activeRun?.kind === "discovery" &&
      isTerminalStatus(discoveryRunQuery.data?.status),
    queryFn: () => getDiscoveryResults(activeRun?.runId ?? ""),
    queryKey: ["discovery-results", activeRun?.runId],
  });

  // Live MQTT topic snapshot for the Explorer-like capture panel (mq9nhbzu).
  // Reuses the existing per-run topics endpoint; only enabled for an MQTT
  // discovery run so it never fires on other modules.
  const captureTopicsQuery = useQuery({
    enabled:
      module.route === "mqtt-discovery" && Boolean(activeRun) && activeRun?.kind === "discovery",
    queryFn: () => getDiscoveryTopics(activeRun?.runId ?? ""),
    queryKey: ["discovery-topics", activeRun?.runId],
    // Poll while the run is active so the table refreshes the instant the run
    // goes terminal (topics persist at run end), then stop polling (mq9nhbzu).
    refetchInterval: () =>
      isTerminalStatus(discoveryRunQuery.data?.status) || runEvents.reachedTerminal ? false : 2000,
  });

  // Reports list for the reports page (per-report selection + Export selected).
  const reportsQuery = useQuery({
    enabled: module.route === "reports",
    queryFn: listReports,
    queryKey: ["reports-list"],
  });

  // Uploaded non-published UDMI schema sets, shown on the UDMI workbench only.
  const udmiSchemaSetsQuery = useQuery({
    enabled: module.route === "udmi-validation",
    queryFn: listUdmiSchemaSets,
    queryKey: ["udmi-schema-sets"],
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
    queryFn: () => getImportErrors(importOutcome?.import_id ?? ""),
    queryKey: ["import-errors", importOutcome?.import_id],
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
    queryFn: () => getLatestImport(selectedImportType as ImportType),
    queryKey: ["latest-import", selectedImportType],
  });

  // Run retention: the page state is wiped on every navigation, so arriving at a
  // head used to look like nothing had ever run there. Ask the run store for
  // this head's own runs and re-attach one, so the monitor and results survive
  // navigating away and back.
  //
  // Now that runs execute in the background (ITEM-4), a run can still be
  // RUNNING/QUEUED when the operator refreshes or navigates away. Prefer the
  // newest non-terminal run so its LIVE monitor and Stop control re-attach; fall
  // back to the newest succeeded run otherwise. The seed effect below marks the
  // re-attached run restored:true, so rehydration never hijacks the step — and
  // polling resumes automatically because the run monitor queries key off
  // activeRun.
  //
  // Report actions carry no run lifecycle, so they are excluded — which also
  // naturally exempts the reports route (report-only actions => no query).
  const rehydratableActions = module.runActions.filter(
    (action): action is Exclude<ModuleRunAction, { kind: "report" }> => action.kind !== "report",
  );
  const lastRunQuery = useQuery({
    enabled: rehydratableActions.length > 0,
    // Keyed by route so one head's cached run can never be handed to another.
    queryKey: ["last-attachable-run", module.route],
    queryFn: async () => {
      // Two requests per job type: the newest run of ANY status (catches a live
      // running/queued run) and the newest succeeded run (the terminal fallback).
      const responses = await Promise.all(
        rehydratableActions.flatMap((action) => [
          listRuns({ jobType: action.jobType, limit: 1 }),
          listRuns({ jobType: action.jobType, limit: 1, status: "succeeded" }),
        ]),
      );
      // created_at is ISO-8601 UTC, so it sorts lexicographically.
      const byNewest = (a: { created_at: string }, b: { created_at: string }) =>
        a.created_at < b.created_at ? 1 : -1;
      const all = responses.flatMap((response) => response.runs);
      const live = all.filter((run) => !isTerminalStatus(run.status)).sort(byNewest)[0];
      const succeeded = all.filter((run) => run.status === "succeeded").sort(byNewest)[0];
      return live ?? succeeded ?? null;
    },
  });

  // When SSE reports the run terminal but polling is paused, the polled record
  // (and its result_summary) may still be mid-run. Trigger a single refetch so
  // the terminal-gated issues/results queries fire against fresh data.
  const refetchValidationRun = validationRunQuery.refetch;
  const refetchDiscoveryRun = discoveryRunQuery.refetch;
  useEffect(() => {
    if (!runEvents.reachedTerminal || !activeRun) {
      return;
    }
    if (activeRun.kind === "discovery") {
      void refetchDiscoveryRun();
    } else {
      void refetchValidationRun();
    }
  }, [runEvents.reachedTerminal, activeRun, refetchDiscoveryRun, refetchValidationRun]);

  const resetTemplateDownload = templateDownload.reset;
  const resetReportDownload = reportDownload.reset;
  const resetExportDownload = exportDownload.reset;

  useEffect(() => {
    setSelectedImportType(module.importTypes[0] ?? "");
    setSelectedFile(null);
    setImportOutcome(null);
    setRunOutcome(null);
    setLastReport(null);
    setActiveRun(null);
    setCopyFeedback(null);
    setSelectedResultIndex(0);
    setResultsTextFilter("");
    setResultsToneFilter("all");
    setResultsAssetTypeFilter("all");
    setResultsSeenFilter("all");
    setResultsStateFilter("all");
    setExpandedAsset(null);
    setSelectedReportIds(new Set());
    setReportToast(null);
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
  }, [
    module.route,
    module.importTypes,
    resetTemplateDownload,
    resetReportDownload,
    resetExportDownload,
  ]);

  // Re-attach this head's most recent succeeded run (see lastRunQuery above).
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
    const run = lastRunQuery.data;
    if (!run) {
      return;
    }
    // A LIVE (non-terminal) run always outranks a restored terminal seed. The
    // query cache can hand this effect a stale succeeded run first (mount
    // serves cached data, the refetch lands later with the background run that
    // is actually executing); without this upgrade the activeRun guard below
    // would pin the stale run and the live run's monitor + Stop control would
    // never attach (Codex P1 on PR #88). A session-started run (restored not
    // set) is never replaced.
    const upgradeToLive =
      activeRun?.restored === true &&
      activeRun.runId !== run.run_id &&
      !isTerminalStatus(run.status);
    if (activeRun && !upgradeToLive) {
      return;
    }
    // Belt-and-braces on top of the route-keyed query: only ever seed a run
    // whose job type this head can actually start.
    const action = module.runActions.find(
      (entry) => entry.kind !== "report" && entry.jobType === run.job_type,
    );
    if (!action || action.kind === "report") {
      return;
    }
    setActiveRun({ kind: action.kind, restored: true, runId: run.run_id });
  }, [lastRunQuery.data, activeRun, module.runActions]);

  // Auto-clear the report confirmation toast after a few seconds so a stale
  // "report generated" note does not linger on the page (mqautz9j follow-up).
  useEffect(() => {
    if (!reportToast) {
      return;
    }
    const timer = setTimeout(() => setReportToast(null), 8000);
    return () => clearTimeout(timer);
  }, [reportToast]);

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
    if (activeRunTerminal && activeRunStatus === "succeeded" && activeRun && !activeRun.restored) {
      setStep("results");
    }
  }, [activeRunTerminal, activeRunStatus, activeRun]);

  // Pete's walkthrough ask (2026-07-15): when Results opens, snap to the top of
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
    mutationFn: (input: { importType: ImportType; file: File }) =>
      createImport({
        file: input.file,
        importType: input.importType,
      }),
    onSuccess: (summary) => {
      setImportOutcome(summary);
      // Refresh the "already imported" note so it reflects this upload the next
      // time the file input is empty (ISSUE-5).
      void queryClient.invalidateQueries({ queryKey: ["latest-import"] });
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
    mutationFn: () =>
      uploadUdmiSchemaSet({ files: schemaSetFiles, versionLabel: schemaSetLabel.trim() }),
    onSuccess: () => {
      void udmiSchemaSetsQuery.refetch();
    },
  });

  const schemaDeleteMutation = useMutation({
    mutationFn: (versionLabel: string) => deleteUdmiSchemaSet(versionLabel),
    onSuccess: () => {
      void udmiSchemaSetsQuery.refetch();
    },
  });

  const runMutation = useMutation({
    mutationFn: async (actionIndex: number) => {
      const action = module.runActions[actionIndex];
      if (!action) {
        throw new Error("Unknown run action.");
      }

      if (action.kind === "discovery") {
        // Real (non-dry-run) scans must carry the authorization contract or the
        // backend returns 403. The operator confirms via the checkbox; a dry-run
        // previews the plan with no I/O and needs no authorization.
        return startDiscoveryRun({
          jobType: action.jobType,
          parameters: buildDiscoveryParameters(action, {
            authorized: scanAuthorized,
            captureSeconds: captureSecondsEffective,
            captureTopicFilter,
            dryRun: scanDryRun,
            scanPorts,
            target: scanTarget,
          }),
          runKind: action.runKind,
        });
      }

      if (action.kind === "validation") {
        return startValidationRun({
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
                  useLiveBroker: udmiUseLiveBroker,
                  useRegister: udmiUseRegister,
                })
              : undefined,
          runKind: action.runKind,
        });
      }

      return createReport({ format: action.format ?? "zip", reportType: action.reportType });
    },
    onSuccess: (result, actionIndex) => {
      const action = module.runActions[actionIndex];
      if ("run_id" in result) {
        // The new run has been ACCEPTED and is about to take over the single run
        // monitor. Only now cancel a still-live REHYDRATED run being monitored,
        // so the swap actually replaces it — never before the POST, where an
        // invalid run-time typo or a rejected POST would strand a live capture
        // with no reachable Stop and nothing started (the ITEM-4 orphan guard).
        // Best-effort cooperative cancel: a fossilized run ignores it, a genuinely
        // live one stops cleanly (keeps its partial data, still reports).
        if (
          (action?.kind === "discovery" || action?.kind === "validation") &&
          activeRun?.restored &&
          runIsActive
        ) {
          void cancelRun(activeRun.runId).catch(() => {});
        }
        setRunOutcome(`${result.message} Run ID: ${result.run_id}`);
        setLastReport(null);
        if (action?.kind === "discovery") {
          setActiveRun({ kind: "discovery", runId: result.run_id });
        } else if (action?.kind === "validation") {
          setActiveRun({ kind: "validation", runId: result.run_id });
        }
      } else {
        setLastReport(result);
        setRunOutcome(`Report generated. Report ID: ${result.report_id}, file: ${result.file_name}`);
        // Report linking (mqautz9j): confirm where the report lives and refresh
        // the reports list so the new report is selectable for export.
        setReportToast("Report generated — see the Reports list below to download or export it.");
        setStep("results");
        void reportsQuery.refetch();
      }
    },
  });

  const publishMutation = useMutation({
    mutationFn: () =>
      startMqttConfigPublishRun({
        confirmed: publishConfirmed,
        expectedPoint: publishPoint,
        expectedValue: parsePublishValue(publishValue),
        // Confirm-back now covers every written point (mq9n11wi): pass the
        // primary plus all extra pairs as expected so the backend verifies each.
        expectedPoints: [
          { point: publishPoint, value: parsePublishValue(publishValue) },
          ...publishExtraPoints.map((pair) => ({ point: pair.point, value: parsePublishValue(pair.value) })),
        ],
        // Compose every point/value pair (primary + extras) into one config
        // payload so a single publish writes them all.
        payload: buildMultiPointPayload(publishPayload, publishPoint, publishValue, publishExtraPoints),
        pointsetTopic: publishPointsetTopic,
        topic: publishTopic,
        useLiveBroker: publishUseLiveBroker,
        waitSeconds: Number(publishWaitSeconds) || 5,
      }),
    onSuccess: (result) => {
      setRunOutcome(`${result.message} Run ID: ${result.run_id}`);
      setActiveRun({ kind: "validation", runId: result.run_id });
    },
  });

  const cancelMutation = useMutation({
    mutationFn: (runId: string) => cancelRun(runId),
    onSuccess: () => {
      if (activeRun?.kind === "discovery") {
        void discoveryRunQuery.refetch();
      } else {
        void validationRunQuery.refetch();
      }
    },
  });

  const rollbackMutation = useMutation({
    mutationFn: (runId: string) => rollbackMqttConfigPublish(runId),
    onSuccess: (result) => {
      setRunOutcome(`${result.message} Run ID: ${result.run_id}`);
    },
  });

  // Generate a report off the back of a completed run (mqautz9j), scoped to the
  // originating run via source_run_ids so the report actually traces to it.
  // Format is operator-chosen (field ask 2026-07-14: PDF and Word exports).
  const reportFromRunMutation = useMutation({
    mutationFn: ({ reportType, runId }: { reportType: ReportType; runId: string }) =>
      createReport({ format: reportExportFormat, reportType, sourceRunIds: [runId] }),
    onSuccess: (result) => {
      setReportToast(
        `Report generated from this run — see the Reports tab. Report ID: ${result.report_id}.`,
      );
      // The toast points at the Reports tab, so the list behind it must not be
      // stale. The reports query is disabled off the reports route, so this
      // marks it stale and it refetches when that route enables it.
      void queryClient.invalidateQueries({ queryKey: ["reports-list"] });
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
  // so each card keeps its original index for runMutation.mutate(index).
  const visibleRunActions = module.runActions.filter((action) => !action.hiddenFromRunControls);

  // Index of the UDMI validation run action, used by the Schedule & Payload
  // Evidence "Execute capture" button — the only visible trigger for this run
  // now that the Run Controls card is hidden (mq9n7pbe).
  //
  // Deliberately NOT clamped to 0: a -1 flows into runMutation's "Unknown run
  // action." guard, surfacing a visible error panel on this same Setup step,
  // instead of silently dispatching whatever happens to sit at index 0.
  const udmiRunActionIndex = module.runActions.findIndex(
    (action) => action.kind === "validation" && action.runKind === "udmi",
  );

  // Verdicts derive from the run's issues list, so an empty list only means
  // "no issues" once the issues query has actually SUCCEEDED. Payload views
  // can land first (they ride the run record), so until then — and permanently
  // if the issues fetch fails — every verdict surface (results-table rows, the
  // row View detail, the per-asset payload sections) must stay neutral instead
  // of deriving a false green "Pass" from the empty array. Reuses the 'none'
  // verdict kind, which carries no tone class.
  const udmiIssuesSettled = validationIssuesQuery.isSuccess;
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
  const liveIssues =
    activeRun?.kind === "validation" && validationIssuesQuery.data
      ? validationIssuesQuery.data.issues.map(toIssueRow)
      : null;
  const visibleIssues = liveIssues ?? [];

  // Per-asset / per-payload-type grouping for UDMI live issues (mq9m4bnv).
  // Collapsed shows a cross-payload-type summary per asset; expanding an asset
  // reveals pointset/metadata/state detail. Derived only from real issue data.
  const assetIssueGroups = useMemo(() => {
    if (module.route !== "udmi-validation" || activeRun?.kind !== "validation") {
      return null;
    }
    const records = validationIssuesQuery.data?.issues;
    if (!records || records.length === 0) {
      return null;
    }
    return groupIssuesByAsset(records, toIssueRow);
  }, [module.route, activeRun, validationIssuesQuery.data]);

  // Authoritative per-payload-type expected-vs-observed payloads from the run's
  // result_summary (mq9m4bnv). Real content only (pasted/captured); never faked.
  const payloadViews = useMemo<UdmiAssetPayloadView[] | null>(() => {
    if (module.route !== "udmi-validation" || activeRun?.kind !== "validation") {
      return null;
    }
    const raw = validationRunQuery.data?.result_summary?.payload_views;
    return Array.isArray(raw) ? (raw as UdmiAssetPayloadView[]) : null;
  }, [module.route, activeRun, validationRunQuery.data]);

  // Asset ids a capture attempt found SILENT — the red "offline" set for the
  // RAG scheme (mqf-udmi-rag). Derived from real capture evidence only: issues
  // stamped issue_type "not_publishing" (the complete path — single-asset
  // capture timeouts report silence ONLY as an issue, never in the summary
  // list), unioned with result_summary.not_publishing_devices (the
  // DevicesNotPublishing path) as defensive insurance. Never inferred from
  // observed_present=false alone, so a pasted-payload run (no capture attempted)
  // never paints a device red (honesty rule).
  const offlineAssets = useMemo<Set<string>>(() => {
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

  // Live UDMI results table: one row per asset x payload type, derived only
  // from the terminal run's real payload_views + issues (never fabricated).
  // Replaces the illustrative sample rows that previously showed after a run.
  const udmiLiveResults = useMemo<{ columns: string[]; rows: Array<Record<string, string>> } | null>(() => {
    // job_type guard: mqtt_config_publish runs share this route's run monitor;
    // only a udmi_validation run may populate the per-asset payload table.
    if (
      module.route !== "udmi-validation" ||
      !mergedAssetGroups ||
      validationRunQuery.data?.job_type !== "udmi_validation" ||
      !isTerminalStatus(validationRunQuery.data?.status)
    ) {
      return null;
    }
    const rows = mergedAssetGroups.flatMap((group) =>
      group.payloadTypes.map((entry) => {
        const observed = entry.hasPayloadView ? (entry.observedPresent ? "Yes" : "No") : "—";
        // Shared (issues-gated) verdict helper so the row, its View detail,
        // and the per-asset payload sections can never disagree on the verdict.
        const { label, verdict } = gatedUdmiVerdict(
          entry.issues,
          entry.observedPresent,
          offlineAssets.has(group.assetId),
        );
        return {
          Asset: group.assetId,
          Payload: `UDMI ${entry.payloadType}`,
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
          // so a row click keys straight into the inspector's payload-group refs
          // (ITEM-D). Hidden: not in `columns`, so it never renders as a cell.
          __payloadType: entry.payloadType,
        };
      }),
    );
    if (rows.length === 0) {
      return null;
    }
    return { columns: ["Asset", "Payload", "Observed", "Issues", "Raw Payload", "Result"], rows };
  }, [module.route, mergedAssetGroups, validationRunQuery.data, gatedUdmiVerdict, offlineAssets]);

  // Reset the row selection when the live UDMI view replaces the sample rows so
  // the inspector never shows a stale sample-row selection against live results.
  const hasUdmiLiveResults = udmiLiveResults !== null;
  useEffect(() => {
    if (hasUdmiLiveResults) {
      setSelectedResultIndex(0);
      setDetailRow(null);
      // A fresh result set starts unfiltered so a stale filter never hides new
      // rows behind a "no rows match" note (ISSUE-4). The facet filters (ITEM-10)
      // join the same reset choreography for the same reason.
      setResultsTextFilter("");
      setResultsToneFilter("all");
      setResultsAssetTypeFilter("all");
      setResultsSeenFilter("all");
      setResultsStateFilter("all");
      setExpandedResultAssets(new Set());
    }
  }, [hasUdmiLiveResults]);

  // Live discovery results view (ip/bacnet/mqtt). Built only after a terminal
  // run; until then the table shows its honest "No results yet" empty state.
  const discoveryView = useMemo(() => {
    if (!isDiscoveryModule || !discoveryResultsQuery.data) {
      return null;
    }
    return discoveryViewFor(module.route, discoveryResultsQuery.data);
  }, [isDiscoveryModule, discoveryResultsQuery.data, module.route]);

  // Reset the row selection when the discovery view identity changes (a re-run
  // produces a new view object), mirroring the UDMI reset above, so a re-run
  // never leaves a stale, out-of-range selection pointing at the old rows.
  useEffect(() => {
    if (discoveryView) {
      setSelectedResultIndex(0);
      setResultsTextFilter("");
      setResultsToneFilter("all");
    }
  }, [discoveryView]);

  const liveMetrics = useMemo(() => {
    if (!isDiscoveryModule || !discoveryResultsQuery.data) {
      return null;
    }
    return discoveryMetrics(module.route, discoveryResultsQuery.data);
  }, [isDiscoveryModule, discoveryResultsQuery.data, module.route]);

  // BACnet-only provenance: read result_summary.backend so simulated sample
  // devices are never mistaken for a real on-wire scan. Null for other routes
  // and until a terminal run's results arrive.
  const bacnetBackend = useMemo(() => {
    if (module.route !== "bacnet-discovery" || !discoveryResultsQuery.data) {
      return null;
    }
    return bacnetBackendLabel(discoveryResultsQuery.data);
  }, [module.route, discoveryResultsQuery.data]);

  const usingLiveResults = Boolean(discoveryView) || Boolean(udmiLiveResults);
  const tableColumns = discoveryView?.columns ?? udmiLiveResults?.columns ?? workspace?.columns ?? [];
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
  // selection, the Inspector, and the View modal never point at the wrong row.
  const resultsTopicColumn = tableColumns.includes("Topic") ? "Topic" : undefined;
  // Per-asset facts for the inspector facet filters (ITEM-10), derived from the
  // same merged groups + offline set the verdicts use — so the filters can never
  // claim more than the app observed. Non-udmi routes get an empty map (unused).
  const isUdmiValidation = module.route === "udmi-validation";
  const assetFacts = useMemo(
    () => buildAssetFacts(mergedAssetGroups ?? [], offlineAssets),
    [mergedAssetGroups, offlineAssets],
  );
  const assetTypeOptions = useMemo(() => {
    const types = new Set<string>();
    for (const facts of assetFacts.values()) {
      types.add(facts.type);
    }
    return Array.from(types).sort();
  }, [assetFacts]);
  const facetFilterActive =
    isUdmiValidation &&
    (resultsAssetTypeFilter !== "all" ||
      resultsSeenFilter !== "all" ||
      resultsStateFilter !== "all");
  const isResultsFilterActive =
    resultsTextFilter.trim() !== "" || resultsToneFilter !== "all" || facetFilterActive;
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
          // Facet filters are a claim about the ASSET, so they apply on the
          // udmi-validation route only (other routes have no asset facts).
          return isUdmiValidation
            ? assetMatchesFacetFilter(assetFacts.get(row.Asset), {
                type: resultsAssetTypeFilter,
                seen: resultsSeenFilter,
                state: resultsStateFilter,
              })
            : true;
        }),
    [
      resultRows,
      resultsTextFilter,
      resultsToneFilter,
      resultsTopicColumn,
      isUdmiValidation,
      assetFacts,
      resultsAssetTypeFilter,
      resultsSeenFilter,
      resultsStateFilter,
    ],
  );
  // The drill-down list mirrors the same facet filter so "show me all EMs that
  // are offline" filters the inspector groups too — table and inspector can
  // never disagree (ITEM-10). Text/tone are cell-level, so they are NOT applied
  // here (they filter payload-type rows, not whole assets).
  const visibleAssetGroups = useMemo(() => {
    if (!mergedAssetGroups) {
      return null;
    }
    if (!isUdmiValidation) {
      return mergedAssetGroups;
    }
    return mergedAssetGroups.filter((group) =>
      assetMatchesFacetFilter(assetFacts.get(group.assetId), {
        type: resultsAssetTypeFilter,
        seen: resultsSeenFilter,
        state: resultsStateFilter,
      }),
    );
  }, [
    mergedAssetGroups,
    isUdmiValidation,
    assetFacts,
    resultsAssetTypeFilter,
    resultsSeenFilter,
    resultsStateFilter,
  ]);
  // The selected row, resolved WITHIN the filtered view so the Inspector can
  // never show a row the table is hiding (ISSUE-4): when the active selection is
  // filtered out we fall back to the first visible row, and when NOTHING matches
  // the filter the selection is null so the Inspector renders its own empty state
  // instead of a hidden row's detail (which the table simultaneously denies).
  const selectedResult =
    visibleResultRows.length === 0
      ? null
      : (visibleResultRows.find(({ index }) => index === selectedResultIndex) ?? visibleResultRows[0]).row;
  // In the grouped UDMI table a collapsed asset unmounts its child rows, so the
  // inspector must not keep showing a hidden row's detail (ISSUE-4). Fall back to
  // the empty state until the asset is re-expanded — selectedResult (and its
  // index) is preserved, so re-expanding restores the detail, and the
  // auto-expand-on-select effect still runs off selectedResult, not this.
  const inspectorResult =
    selectedResult && hasUdmiLiveResults && !expandedResultAssets.has(selectedResult.Asset)
      ? null
      : selectedResult;
  const resultDetails = inspectorResult
    ? buildResultDetailItems(module.route, inspectorResult, usingLiveResults, mergedAssetGroups)
    : [];
  // Group the visible UDMI rows by asset for the collapsible summary rows
  // (ITEM-7). Render-only over visibleResultRows, so child rows keep their
  // original index and the ISSUE-4 selection/detail joins are untouched.
  const udmiRowGroups = useMemo(
    () => (hasUdmiLiveResults ? groupUdmiRowsByAsset(visibleResultRows) : []),
    [hasUdmiLiveResults, visibleResultRows],
  );
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
    module.route === "mqtt-discovery"
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
            { label: "Offline", value: "offline" },
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
    if (!visibleResultRows.some(({ index }) => index === selectedResultIndex)) {
      setSelectedResultIndex(visibleResultRows[0].index);
    }
  }, [visibleResultRows, selectedResultIndex]);

  // The structured DiscoveredTopic record for the selected MQTT row, matched by
  // topic (topics are distinct per run by aggregation construction). Yields the
  // real last_payload OBJECT for the inspector's JsonTree — we NEVER re-parse
  // the row's stringified "Raw Payload" cell. Null off the mqtt-discovery route,
  // before results land, or when nothing is selected.
  const selectedMqttTopic = useMemo<DiscoveryRowRecord | null>(() => {
    if (module.route !== "mqtt-discovery" || !discoveryResultsQuery.data || !selectedResult) {
      return null;
    }
    const topic = selectedResult.Topic;
    return (
      discoveryResultsQuery.data.topics.find((record) => String(record.topic) === topic) ?? null
    );
  }, [module.route, discoveryResultsQuery.data, selectedResult]);

  // Terminal empty-state: a discovery run that completed with zero rows must say
  // so explicitly — distinct from "no run yet" / "in flight" / "failed"
  // (Pete 2026-07-15: "it can't find anything, but it doesn't really tell us").
  // Gating on activeRun keeps this composable with run rehydration: a restored
  // terminal run sets activeRun and lights this state up unchanged.
  const discoveryEmptyState =
    isDiscoveryModule && activeRun && activeRunTerminal && resultRows.length === 0
      ? discoveryEmptyStateFor(module.route, discoveryResultsQuery.data, activeRunError)
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
      return liveMetrics;
    }
    if (
      (module.route === "udmi-validation" || module.route === "data-validation") &&
      isTerminalStatus(validationRunQuery.data?.status)
    ) {
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
  }, [liveMetrics, module.route, validationRunQuery.data, reportsQuery.isLoading, reportsQuery.data]);

  const activeStatusClass = activeRunStatus ? toHealthState(activeRunStatus) : "queued";
  // Mid-run device progress for the monitor (BACnet enrichment writes it into
  // result_summary.progress). Read from the polled run record — the SSE frame
  // carries only status/stage/progress_percent — so it updates on the poll
  // cadence; null when absent, so the row simply does not render.
  const runProgressText = formatRunProgress(activeRunRecord?.result_summary);
  // Cancel is an engineer+ mutation; hide it entirely for lower roles so a
  // viewer/reviewer monitoring a run never sees a button that would 403.
  const canCancel =
    canEngineer && Boolean(activeRun) && Boolean(activeRunStatus) && !activeRunTerminal;

  // Export wiring: a report download fits the reports module (uses the last
  // queued report). Elsewhere there is no per-run results download endpoint, so
  // the button is disabled with an explanatory tooltip rather than faked.
  const exportReport = module.route === "reports" ? lastReport : null;
  const exportEnabled = Boolean(exportReport);
  const exportTooltip = exportEnabled
    ? `Download ${exportReport?.file_name ?? "report"}`
    : "Generate a report first to enable a real download. Discovery/validation results have no per-run file export endpoint yet.";

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    setSelectedFile(event.target.files?.[0] ?? null);
    setImportOutcome(null);
    // Chromium fires no change event when the same path is re-picked while the
    // input still holds it, so a corrected CSV saved over the original was
    // silently never re-read (Pete had to rename the file to get it uploaded).
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
    setPublishExtraPoints((current) => current.filter((_entry, entryIndex) => entryIndex !== index));
  };

  const handleExport = () => {
    if (!exportReport) {
      return;
    }
    void exportDownload.download(
      "export",
      getReportDownloadPath(exportReport.report_id),
      exportReport.file_name || `${exportReport.report_id}.${exportReport.output_format}`,
    );
  };

  // Live, downloadable reports from GET /reports. Only succeeded reports have a
  // real file behind getReportDownloadPath, so only those are selectable.
  const liveReports = reportsQuery.data?.reports ?? [];
  const downloadableReports = liveReports.filter((report) => report.status === "succeeded");

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
    const chosen = downloadableReports.filter((report) => selectedReportIds.has(report.report_id));
    if (chosen.length === 0) {
      return;
    }
    if (chosen.length === 1) {
      const [report] = chosen;
      await exportDownload.download(
        `selected-${report.report_id}`,
        getReportDownloadPath(report.report_id),
        report.file_name || `${report.report_id}.${report.output_format}`,
      );
      return;
    }
    await exportDownload.download("selected-zip", REPORTS_EXPORT_PATH, "reports_export.zip", {
      body: JSON.stringify({ report_ids: chosen.map((report) => report.report_id) }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    });
  };

  // "Generate report from this run" affordance shown on a terminal validation/
  // discovery run (mqautz9j). Scopes the report to the originating run id via
  // source_run_ids so the report traces back to it.
  const handleGenerateReportFromRun = () => {
    const runId = activeRun?.runId;
    if (!runId) {
      return;
    }
    const reportType: ReportType =
      activeRun?.kind === "discovery"
        ? ((module.route === "ip-scanner"
            ? "ip_discovery"
            : module.route === "bacnet-discovery"
              ? "bacnet_discovery"
              : "mqtt_discovery") as ReportType)
        : "issue_report";
    reportFromRunMutation.mutate({ reportType, runId });
  };

  // Latest payload per topic for the MQTT Explorer-like capture (mq9nhbzu),
  // filtered by the wildcard topic filter and built from the live topics
  // snapshot. No payloads are fabricated — empty until a real run reports them.
  const captureRows = useMemo(() => {
    const topics = captureTopicsQuery.data?.topics ?? [];
    return topics
      .map((topic) => mqttCaptureRow(topic))
      .filter((row) => matchesTopicFilter(row.topic, captureTopicFilter));
  }, [captureTopicsQuery.data, captureTopicFilter]);

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
    if (captureRows.length === 0 || !activeRun?.runId) {
      return;
    }
    void exportDownload.download(
      "capture-xlsx",
      getDiscoveryTopicsXlsxPath(activeRun.runId, captureTopicFilter),
      `mqtt-capture-${Date.now()}.xlsx`,
    );
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
  const discoveryBlocked = isDiscoveryModule && !scanDryRun && !scanAuthorized;

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

  // Selecting a live-UDMI results row (row click or its View button) opens the
  // matching asset in the inspector and scrolls to that payload type's issues
  // (ITEM-D), so the inspector — now beside the table — surfaces exactly which
  // issues flagged the row. Guarded to live-UDMI rows: they carry an Issues
  // count and a hidden __payloadType; discovery rows have neither and no
  // inspector payload groups, so this no-ops for them.
  const focusInspectorPayload = (row: Record<string, string>) => {
    if (row.Issues === undefined) {
      return;
    }
    setExpandedAsset(row.Asset);
    const key = `${row.Asset}:${row.__payloadType ?? ""}`;
    // The payload-type-group mounts only once its asset expands, so wait a frame
    // for that re-render before scrolling to the freshly-stamped node.
    requestAnimationFrame(() => {
      payloadGroupRefs.current.get(key)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  };

  // One results-table data row. Shared by the flat (discovery) render and the
  // grouped-by-asset render (ITEM-7) so the two can never drift. rowIndex is the
  // ORIGINAL index in resultRows, so selection and detail joins stay correct
  // while the list is filtered (ISSUE-4).
  const renderResultRow = ({ row, index: rowIndex }: { row: Record<string, string>; index: number }) => {
    // Live-UDMI rows carry a real issue count; name it on the View affordance so
    // the button reads as "holds N issues", not a bare "View" (ITEM-D). Honest:
    // the count is only claimed when the row actually has issues.
    const issueCount = row.Issues === undefined ? 0 : Number(row.Issues);
    const viewLabel = issueCount > 0 ? `View ${issueCount} issue${issueCount === 1 ? "" : "s"}` : "View";
    return (
      <tr
        className={`${row.__tone ? `row-${row.__tone}` : ""}${
          selectedResultIndex === rowIndex ? " row-selected" : ""
        }`.trim() || undefined}
        key={rowIndex}
        onClick={() => {
          setSelectedResultIndex(rowIndex);
          focusInspectorPayload(row);
        }}
      >
        {tableColumns.map((column) => (
          <td key={column}>{renderCell(row, column, handleCopyPayload)}</td>
        ))}
        <td>
          <button
            className={`secondary-button compact${selectedResultIndex === rowIndex ? " selected" : ""}`}
            onClick={() => {
              setSelectedResultIndex(rowIndex);
              setDetailRow(row);
              focusInspectorPayload(row);
            }}
            type="button"
          >
            {viewLabel}
          </button>
        </td>
      </tr>
    );
  };

  return (
    <div className="app-page">
      <section className="module-hero" ref={heroRef}>
        <div>
          <span className="eyebrow">{module.backendService}</span>
          <h2>{workspace?.title ?? module.title}</h2>
          <p>{workspace?.headline ?? module.summary}</p>
        </div>
        <div className="module-metrics">
          {metricsView ? (
            <>
              <article>
                <strong>{metricsView.primary}</strong>
                <span>{metricsView.primaryLabel}</span>
              </article>
              <article>
                <strong>{metricsView.secondary}</strong>
                <span>{metricsView.secondaryLabel}</span>
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
      </section>

      <StepNav
        step={step}
        onStep={setStep}
        hasRun={Boolean(activeRun)}
        terminal={activeRunTerminal}
      />

      <div className="module-steps" data-step={step}>
      <section className="app-grid two-col" data-stepgroup="setup run">
        <article className="surface">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Inputs</span>
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
                    {latestImportQuery.data.file_name} — {latestImportQuery.data.accepted_rows} of{" "}
                    {latestImportQuery.data.total_rows} rows accepted,{" "}
                    {formatRelativeTime(latestImportQuery.data.created_at)}. This register is stored
                    and used by runs on this page; upload again only if the file changed.
                  </span>
                </div>
              )}

              <button
                className="primary-button"
                disabled={!selectedFile || !selectedImportType || importMutation.isPending || !canEngineer}
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
                        void templateDownload.download(
                          "template-xlsx",
                          getImportTemplatePath(selectedImportType, "xlsx"),
                          `${selectedImportType}_template.xlsx`,
                        )
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
                        void templateDownload.download(
                          "template-csv",
                          getImportTemplatePath(selectedImportType, "csv"),
                          `${selectedImportType}_template.csv`,
                        )
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
                          <span key={column} className="optional">{column}</span>
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
                    {importOutcome.accepted_rows} accepted ·{" "}
                    {importOutcome.rejected_rows} rejected
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
                    <span>Could not load rejection reasons: {importErrorsQuery.error.message}</span>
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
                      ...and {hiddenImportErrorCount} more rejected rows not shown — fix the rows
                      listed above and re-upload to see the rest.
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
                      <li key={`${warning.row_number ?? "file"}-${warning.field ?? ""}-${index}`}>
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
              <span className="eyebrow">Execution</span>
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
                <label className="confirm-row">
                  <input
                    checked={scanAuthorized}
                    onChange={(event) => setScanAuthorized(event.target.checked)}
                    type="checkbox"
                  />
                  I am authorized to scan this network. Real scans without this are rejected (403).
                </label>
              )}
            </div>
          )}

          <div className="run-list">
            {visibleRunActions.length > 0 ? (
              // Mapped over the FULL list and skipped in place, never
              // filter-then-map: `index` must stay the action's real index in
              // module.runActions or mutate(index) dispatches the wrong action.
              module.runActions.map((action, index) => {
                if (action.hiddenFromRunControls) {
                  return null;
                }
                const scanBlocked = action.kind === "discovery" && discoveryBlocked;
                const mqttOverCapBlocked =
                  mqttCaptureOverCap && action.kind === "discovery" && action.runKind === "mqtt";
                const overCapBlocked =
                  (udmiCaptureOverCap && action.kind === "validation" && action.runKind === "udmi") ||
                  mqttOverCapBlocked;
                // A run started here now executes in the background, so block a
                // second start while one is live (Stop run is the escape) —
                // prevents accidental parallel captures now that POSTs return
                // instantly (ITEM-4). Only a run started THIS session blocks; a
                // rehydrated run never fossilizes into a permanent lock (see
                // startedRunActive).
                const blocked = scanBlocked || !canEngineer || overCapBlocked || startedRunActive;
                // Role gate takes priority in the tooltip; otherwise the existing
                // scan-authorization hint is shown for a blocked real scan.
                const blockedTooltip = !canEngineer
                  ? ENGINEER_REQUIRED_TOOLTIP
                  : scanBlocked
                    ? "Confirm scan authorization (or enable dry run) before starting a real scan."
                    : mqttOverCapBlocked
                      ? "Run time exceeds the 48-hour capture limit."
                      : overCapBlocked
                        ? "Run time exceeds the 48-hour capture limit."
                        : startedRunActive
                          ? "A run is already in progress — stop it before starting another."
                          : undefined;
                return (
                  <div className="run-card" key={action.label}>
                    <div>
                      <strong>{action.label}</strong>
                      <span>{action.helper}</span>
                    </div>
                    <button
                      className="secondary-button compact"
                      disabled={runMutation.isPending || blocked}
                      onClick={() => runMutation.mutate(index)}
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
                  Work through the options below, then start the run with Execute capture under Schedule and Payload
                  Evidence.
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
                    void reportDownload.download(
                      "report",
                      getReportDownloadPath(lastReport.report_id),
                      lastReport.file_name ||
                        `${lastReport.report_id}.${lastReport.output_format}`,
                    )
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
                  <strong>{activeRun.kind === "discovery" ? "Discovery" : "Validation"} run monitor</strong>
                  <span>{activeRun.runId}</span>
                  <Link className="link-button" to="/run-history">
                    Run history
                  </Link>
                </div>
                <span className={`status-token ${activeStatusClass}`}>
                  {activeRunStatus ?? "queued"}
                </span>
              </div>

              <div className={`progress-track${progressIndeterminate ? " indeterminate" : ""}`}>
                <div style={progressIndeterminate ? undefined : { width: `${progressWidth}%` }} />
              </div>

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
                  <dd>{formatSummaryValue(validationRunQuery.data?.result_summary.expected_devices)}</dd>
                </div>
                <div>
                  <dt>Publishing</dt>
                  <dd>{formatSummaryValue(validationRunQuery.data?.result_summary.publishing_seen)}</dd>
                </div>
                <div>
                  <dt>Issues</dt>
                  <dd>{formatSummaryValue(validationRunQuery.data?.result_summary.issue_count)}</dd>
                </div>
                {typeof validationRunQuery.data?.result_summary.blocking_issue_count === "number" && (
                  <div>
                    <dt>Blocking issues</dt>
                    <dd>{formatSummaryValue(validationRunQuery.data.result_summary.blocking_issue_count)}</dd>
                  </div>
                )}
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
                {canEngineer &&
                  activeRun.kind === "validation" &&
                  validationRunQuery.data?.job_type === "mqtt_config_publish" &&
                  activeRunTerminal && (
                    <button
                      className="secondary-button compact"
                      disabled={rollbackMutation.isPending}
                      onClick={() => rollbackMutation.mutate(activeRun.runId)}
                      type="button"
                    >
                      {rollbackMutation.isPending ? "Rolling back..." : "Roll back publish"}
                    </button>
                  )}
                {canEngineer && activeRunTerminal && (
                  <ReportFromRunControls
                    format={reportExportFormat}
                    onFormatChange={setReportExportFormat}
                    onGenerate={handleGenerateReportFromRun}
                    pending={reportFromRunMutation.isPending}
                  />
                )}
              </div>

              {canCancel && (
                <span className="run-monitor-note">
                  Stop run keeps the data collected so far — the stopped run can still generate a
                  report.
                </span>
              )}

              {reportToast && (
                <span className="run-monitor-note">{reportToast}</span>
              )}
              {reportFromRunMutation.isError && (
                <span className="error-text">{reportFromRunMutation.error.message}</span>
              )}

              {cancelMutation.isError && (
                <span className="error-text">{cancelMutation.error.message}</span>
              )}
              {rollbackMutation.isError && (
                <span className="error-text">{rollbackMutation.error.message}</span>
              )}

              {activeRunError && (
                <span className="error-text">{activeRunError}</span>
              )}

              {activeRun.kind === "discovery" && discoveryResultsQuery.isError && (
                <span className="error-text">
                  Could not load discovery results: {discoveryResultsQuery.error instanceof Error
                    ? discoveryResultsQuery.error.message
                    : "request failed"}
                </span>
              )}

              {activeRun.kind === "validation" && validationIssuesQuery.isError && (
                <span className="error-text">
                  Could not load validation issues — verdicts stay pending: {validationIssuesQuery.error instanceof Error
                    ? validationIssuesQuery.error.message
                    : "request failed"}
                </span>
              )}
            </div>
          )}
        </article>
      </section>

      {module.route === "data-validation" && (
        <section className="surface" data-stepgroup="setup">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Validation modes</span>
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

      {module.route === "ip-scanner" && (
        <section className="surface" data-stepgroup="setup">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Scan settings</span>
              <h3>Port and Protocol Selection</h3>
            </div>
            <button className="secondary-button compact" onClick={addScanPort} type="button">
              Add port
            </button>
          </div>
          <p className="field-note">
            The IP scanner runs a TCP connect test only. For BACnet/IP devices (UDP 47808), use BACnet
            Discovery — it sends a real Who-Is broadcast; a TCP probe cannot detect BACnet.
          </p>
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
                    onChange={(event) => changeScanPort(index, "protocol", event.target.value as ScanPort["protocol"])}
                    value={entry.protocol}
                  >
                    {/* TCP only: the sweep is a TCP connect test. UDP (e.g. BACnet
                        47808) is handled by the dedicated BACnet Discovery module. */}
                    <option value="tcp">TCP</option>
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
          <p className="section-copy">
            Sent to the API as <strong>{scanPortSpecification(scanPorts) || "common ports"}</strong>. Leave the
            list empty to use the common TCP fallback (80, 443, 1883, 502). BACnet/IP (UDP 47808) is not probed
            here — use BACnet Discovery.
          </p>
          <div className="publish-grid capture-controls">
            <label>
              Target override (CIDR 10.0.0.0/24, range 10.0.0.1-10.0.0.50, or blank to use the imported IP
              register)
              <input
                onChange={(event) => setScanTarget(event.target.value)}
                placeholder="Blank = imported IP register"
                value={scanTarget}
              />
            </label>
          </div>
        </section>
      )}

      {module.route === "mqtt-discovery" && (
        <section className="surface" data-stepgroup="run">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Live capture</span>
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
                disabled={captureRows.length === 0 || exportDownload.pendingKey !== null}
                onClick={handleCaptureExportXlsx}
                title={
                  captureRows.length === 0
                    ? "No captured topics yet — run an MQTT discovery with this topic filter."
                    : "Download the latest payload per topic as an Excel (XLSX) file."
                }
                type="button"
              >
                {exportDownload.pendingKey === "capture-xlsx" ? "Exporting..." : "Export to XLSX"}
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
                Leave blank to capture every topic (#). Enter a filter with MQTT wildcards
                (+ and #) to narrow the capture, e.g. site/asset-1/#.
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
            Subscribes through an MQTT discovery run and shows the latest payload seen per topic. The live
            broker capture is on-site-untested here; with no broker reachable the run records
            broker_unreachable and this panel stays empty rather than showing fabricated payloads. The
            filter and run time are sent to the run; the run time is{" "}
            <strong>{Number(captureSecondsEffective) > 0 ? `${captureSecondsEffective}s` : "blank (run until you press Stop run)"}</strong>.
            Blank runs until you press Stop run, the 500-distinct-topic cap, or the 48-hour safety limit.
            Closing the app ends the run, which is then marked interrupted at next start. Captured topics
            appear here when the run completes.
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
                  Run an MQTT discovery; the latest payload per topic matching the filter appears here
                  once the run completes. Empty live results stay empty — no sample payloads are shown.
                </span>
              </div>
            )}
          </div>
          {captureTopicsQuery.isError && (
            <span className="error-text">
              Could not load captured topics:{" "}
              {captureTopicsQuery.error instanceof Error ? captureTopicsQuery.error.message : "request failed"}
            </span>
          )}
        </section>
      )}

      {module.route === "udmi-validation" && (
        <section className="surface" data-stepgroup="setup">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Validation inputs</span>
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
                void schemaTemplateDownload.download(
                  "udmi-schema-template",
                  getUdmiSchemaTemplatePath(),
                  "udmi-schema-template-1.5.2.zip",
                )
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
                No non-published schema sets uploaded yet. Canonical published UDMI versions need no
                upload.
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
              <span className="eyebrow">Validation inputs</span>
              <h3>Schedule and Payload Evidence</h3>
            </div>
          </div>
          <label className="confirm-row">
            <input
              checked={udmiUseRegister}
              onChange={(event) => setUdmiUseRegister(event.target.checked)}
              type="checkbox"
            />
            Validate against the imported MQTT register — one expected asset per row (topic, points,
            units, and Expected schema version come from the register). Auto-enabled after an
            accepted register import.
          </label>

          {udmiUseRegister ? (
            <p className="section-copy">
              Register-driven run: the pasted schedule and payload JSON below are ignored. Untick the
              option above to validate the pasted values instead.
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
              onChange={(event) => setUdmiUseLiveBroker(event.target.checked)}
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
                      <input onChange={(event) => setUdmiStateTopic(event.target.value)} value={udmiStateTopic} />
                    </label>
                    <label>
                      Metadata topic
                      <input onChange={(event) => setUdmiMetadataTopic(event.target.value)} value={udmiMetadataTopic} />
                    </label>
                    <label>
                      Pointset topic
                      <input onChange={(event) => setUdmiPointsetTopic(event.target.value)} value={udmiPointsetTopic} />
                    </label>
                  </>
                )}
                <label>
                  Run time (blank = run until all assets/topics seen or until the user stops the run)
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
                Blank runs until every expected asset/topic has reported or you press Stop run — on the portable
                exe as well as the hosted worker. Every capture still ends at the 48-hour safety limit (real-world
                reporting intervals: metadata is often daily), and the completion-driven safety limit is 500 distinct
                concrete topics. Closing the app ends the run, which is then marked interrupted at next start.
              </p>
            </>
          )}

          <div className="inline-actions execute-row">
            <button
              className="primary-button compact"
              disabled={runMutation.isPending || !canEngineer || udmiCaptureOverCap || startedRunActive}
              onClick={() => runMutation.mutate(udmiRunActionIndex)}
              title={
                !canEngineer
                  ? ENGINEER_REQUIRED_TOOLTIP
                  : udmiCaptureOverCap
                    ? "Run time exceeds the 48-hour capture limit."
                    : startedRunActive
                      ? "A run is already in progress — stop it before starting another."
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
              <span className="eyebrow">Controlled publish</span>
              <h3>MQTT Config Payload</h3>
            </div>
          </div>
          <div className="publish-grid">
            <label>
              Config topic
              <input onChange={(event) => setPublishTopic(event.target.value)} value={publishTopic} />
            </label>
            <label>
              Primary point (confirmed)
              <input onChange={(event) => setPublishPoint(event.target.value)} value={publishPoint} />
            </label>
            <label>
              Primary set_value
              <input onChange={(event) => setPublishValue(event.target.value)} value={publishValue} />
            </label>
            <label className="publish-payload">
              Payload JSON
              <textarea onChange={(event) => setPublishPayload(event.target.value)} rows={6} value={publishPayload} />
            </label>
          </div>

          <div className="multi-point-editor">
            <div className="surface-heading compact-heading">
              <div>
                <span className="eyebrow">Additional points</span>
                <h4>Write Multiple Points in One Config</h4>
              </div>
              <button className="secondary-button compact" onClick={addExtraPublishPoint} type="button">
                Add point
              </button>
            </div>
            {publishExtraPoints.length === 0 ? (
              <p className="section-copy">
                Optional. Add extra point/value pairs to write them all in a single config payload alongside
                the primary point above.
              </p>
            ) : (
              <div className="port-editor">
                {publishExtraPoints.map((pair, index) => (
                  <div className="port-row" key={`extra-${index}`}>
                    <label>
                      Point name
                      <input
                        onChange={(event) => changeExtraPublishPoint(index, "point", event.target.value)}
                        placeholder="fan_enable"
                        value={pair.point}
                      />
                    </label>
                    <label>
                      set_value
                      <input
                        onChange={(event) => changeExtraPublishPoint(index, "value", event.target.value)}
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
              All pairs are written into one config payload under <code>pointset.points</code>, and the
              backend confirm/verify step now checks every point/value here — one issue is raised per point
              whose value is not confirmed back. Live-broker confirmation (vs. the local verify) remains
              on-site-untested, the same as for the primary point.
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
              <span className="eyebrow">Reports</span>
              <h3>Generated Reports</h3>
            </div>
            <button
              className="secondary-button compact"
              disabled={selectedReportIds.size === 0 || exportDownload.pendingKey !== null}
              onClick={() => void handleExportSelected()}
              title={
                selectedReportIds.size === 0
                  ? "Tick one or more completed reports to export them."
                  : `Download ${selectedReportIds.size} selected report(s).`
              }
              type="button"
            >
              {exportDownload.pendingKey?.startsWith("selected-") ? "Exporting..." : "Export selected"}
            </button>
          </div>
          <p className="section-copy">
            Every report you generate here is stored against its run and listed below. Generate a report from
            the Run Controls above, or use "Generate report from this run" on a completed discovery or
            validation run elsewhere — it will appear here, traceable to the run it came from.
          </p>
          {reportToast && (
            <div className="state-panel success" role="status">
              <strong>Report generated</strong>
              <span>{reportToast}</span>
            </div>
          )}
          <div className="data-table-wrap">
            {liveReports.length > 0 ? (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Select</th>
                    <th>Report</th>
                    <th>Type</th>
                    <th>Format</th>
                    <th>Status</th>
                    <th>Generated</th>
                    <th>Source runs</th>
                    <th>File</th>
                    <th>Download</th>
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
                            disabled={!downloadable}
                            onChange={() => toggleReportSelection(report.report_id)}
                            title={downloadable ? undefined : "Only completed reports can be exported."}
                            type="checkbox"
                          />
                        </td>
                        <td>{report.report_id}</td>
                        <td>{report.report_type}</td>
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
                              void exportDownload.download(
                                `row-${report.report_id}`,
                                getReportDownloadPath(report.report_id),
                                report.file_name || `${report.report_id}.${report.output_format}`,
                              )
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
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            ) : (
              <div className="empty-workspace">
                <strong>{reportsQuery.isLoading ? "Loading reports..." : "No reports yet"}</strong>
                <span>Generate an Excel or Word report above; it will appear here for selection and export.</span>
              </div>
            )}
          </div>
          {reportsQuery.isError && (
            <span className="error-text">
              Could not load reports:{" "}
              {reportsQuery.error instanceof Error ? reportsQuery.error.message : "request failed"}
            </span>
          )}
          {exportDownload.error && (
            <div className="state-panel error">
              <strong>Export failed</strong>
              <span>{exportDownload.error}</span>
            </div>
          )}
        </section>
      )}

      {/* The inspector sits beside the table (two-col wide-left) only on
          udmi-validation, where it carries live findings/compare content. On the
          discovery/data-validation routes it holds a static empty-state note, so
          keep the table full-width (single column) there. */}
      <section
        className={`app-grid${isUdmiValidation ? " two-col wide-left" : ""}`}
        data-stepgroup="results"
      >
        <article className="surface">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Results</span>
              <h3>{workspace?.tableTitle ?? "Workflow Results"}</h3>
            </div>
            <button
              className="secondary-button compact"
              disabled={!exportEnabled || exportDownload.pendingKey !== null}
              onClick={handleExport}
              title={exportTooltip}
              type="button"
            >
              {exportDownload.pendingKey === "export" ? "Exporting..." : "Export"}
            </button>
          </div>

          {usingLiveResults && (
            <div className="sample-banner" role="note">
              {isDiscoveryModule ? (
                module.route === "ip-scanner" ? (
                  'Live discovery observations. The Result column reports this scan’s response and register-port verdicts; "no response on scanned ports" is inconclusive — a TCP-connect miss is not proof a host is absent.'
                ) : module.route === "mqtt-discovery" &&
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
                `Live validation results — per-asset payload checks from the latest run. Observed payloads were ${
                  payloadViewSource === "live_capture"
                    ? "captured from the MQTT broker"
                    : "supplied directly (pasted), not captured from a broker"
                }.${captureWindow !== null ? ` Capture window: ${captureWindow}.` : ""}`
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
              {/* Facet filters (ITEM-10): asset-type, seen, and online/offline
                  are asset-level claims, so they show on the udmi-validation
                  route only, where each row maps to a real asset with facts. */}
              {isUdmiValidation && (
                <>
                  <label className="results-filter-facet">
                    Asset type
                    <select
                      onChange={(event) => setResultsAssetTypeFilter(event.target.value)}
                      value={resultsAssetTypeFilter}
                    >
                      <option value="all">All types</option>
                      {assetTypeOptions.map((type) => (
                        <option key={type} value={type}>
                          {type}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="results-filter-facet">
                    Seen
                    <select
                      onChange={(event) => setResultsSeenFilter(event.target.value)}
                      value={resultsSeenFilter}
                    >
                      <option value="all">Seen or not</option>
                      <option value="seen">Payload observed</option>
                      <option value="not-seen">Not observed</option>
                    </select>
                  </label>
                  <label className="results-filter-facet">
                    State
                    <select
                      onChange={(event) => setResultsStateFilter(event.target.value)}
                      value={resultsStateFilter}
                    >
                      <option value="all">Any state</option>
                      <option value="online">Online (published this run)</option>
                      <option value="offline">Offline (did not publish)</option>
                    </select>
                  </label>
                </>
              )}
              <span className="results-filter-count">
                Showing {visibleResultRows.length} of {resultRows.length}{" "}
                {resultRows.length === 1 ? "row" : "rows"}
                {hasUdmiLiveResults
                  ? ` across ${udmiRowGroups.length} ${udmiRowGroups.length === 1 ? "asset" : "assets"}`
                  : ""}
              </span>
              {isResultsFilterActive && (
                <button
                  className="secondary-button compact"
                  onClick={() => {
                    setResultsTextFilter("");
                    setResultsToneFilter("all");
                    setResultsAssetTypeFilter("all");
                    setResultsSeenFilter("all");
                    setResultsStateFilter("all");
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
                  {discoveryEmptyState
                    ? discoveryEmptyState.title
                    : isDiscoveryModule && activeRun && !activeRunTerminal
                      ? "Run in progress..."
                      : "No results yet"}
                </strong>
                <span>
                  {discoveryEmptyState
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
                  Adjust or clear the filters to see the {resultRows.length}{" "}
                  captured {resultRows.length === 1 ? "row" : "rows"}.
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
                                  {group.rows.length} payload type{group.rows.length === 1 ? "" : "s"} ·{" "}
                                  {group.issueTotal} issue{group.issueTotal === 1 ? "" : "s"}
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

        {detailRow && (
          <div
            className="modal-overlay"
            role="dialog"
            aria-modal="true"
            aria-label="Result detail"
            onClick={() => setDetailRow(null)}
          >
            <div className="modal-card surface" onClick={(event) => event.stopPropagation()}>
              <div className="surface-heading">
                <div>
                  <span className="eyebrow">Detail</span>
                  <h3>Result detail</h3>
                </div>
                <button
                  className="secondary-button compact"
                  onClick={() => setDetailRow(null)}
                  type="button"
                >
                  Close
                </button>
              </div>
              <div className="detail-list">
                {buildResultDetailItems(module.route, detailRow, usingLiveResults, mergedAssetGroups).map((item) => (
                  <div className="detail-row" key={item.label}>
                    <span>{item.label}</span>
                    <strong>{item.value}</strong>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        <aside className="surface inspector">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Inspector</span>
              <h3>Selected Result Detail</h3>
            </div>
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

              {mergedAssetGroups ? (
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
                  {(visibleAssetGroups ?? []).length === 0 && facetFilterActive && (
                    // A claim about the FILTER, never the scan (ISSUE-4 pattern).
                    <div className="empty-workspace">
                      <strong>No assets match the current filters</strong>
                      <span>Adjust or clear the filters to see the captured assets.</span>
                    </div>
                  )}
                  {(visibleAssetGroups ?? []).map((group) => {
                    const isOpen = expandedAsset === group.assetId;
                    const typeSummary = group.payloadTypes
                      .map((entry) => {
                        const parts: string[] = [];
                        if (entry.issues.length > 0) {
                          parts.push(`${entry.issues.length} issue${entry.issues.length === 1 ? "" : "s"}`);
                        }
                        if (entry.hasPayloadView) {
                          parts.push("payload");
                        }
                        return `${entry.payloadType} (${parts.join(", ") || "ok"})`;
                      })
                      .join(", ");
                    return (
                      <div className={`asset-group${isOpen ? " open" : ""}`} key={group.assetId}>
                        <button
                          aria-expanded={isOpen}
                          className="asset-group-toggle"
                          onClick={() => setExpandedAsset(isOpen ? null : group.assetId)}
                          type="button"
                        >
                          <strong>{group.assetId}</strong>
                          <span>
                            {group.issues.length} issue{group.issues.length === 1 ? "" : "s"} · {typeSummary}
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
                                offlineAssets.has(group.assetId),
                              );
                              const sectionTone = udmiVerdictTone(sectionVerdict.verdict);
                              return (
                                <div
                                  className={`payload-type-group${sectionTone ? ` section-${sectionTone}` : ""}`}
                                  key={entry.payloadType}
                                  ref={(el) => {
                                    // Register/deregister this payload group so a
                                    // row click can scroll straight to it (ITEM-D).
                                    if (el) {
                                      payloadGroupRefs.current.set(payloadKey, el);
                                    } else {
                                      payloadGroupRefs.current.delete(payloadKey);
                                    }
                                  }}
                                >
                                  <h5>{entry.payloadType}</h5>
                                  {sectionTone && (
                                    <p className={`payload-verdict ${sectionTone}`}>
                                      {sectionVerdict.verdict === "pass"
                                        ? "PASS — UDMI Compliant"
                                        : sectionVerdict.verdict === "pass-notes"
                                          ? "PASS WITH NOTES — minor issues below"
                                          : sectionVerdict.verdict === "offline"
                                            ? "OFFLINE — device did not publish during the capture window"
                                            : "NON-COMPLIANT — please see details below"}
                                    </p>
                                  )}
                                  {entry.issues.map((issue) => (
                                    <IssueCard key={issue.id} context={issue.area} issue={issue} />
                                  ))}
                                  {entry.hasPayloadView && (
                                    <div className="payload-evidence">
                                      <button
                                        aria-expanded={payloadOpen}
                                        className="secondary-button compact"
                                        onClick={() =>
                                          setExpandedPayloadKey(payloadOpen ? null : payloadKey)
                                        }
                                        type="button"
                                      >
                                        {payloadOpen ? "Hide" : "Show"} expected vs observed payload
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
              ) : module.route === "mqtt-discovery" ? (
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

              <div className="evidence-list">
                <h4>Evidence outputs</h4>
                {activeRun?.kind === "validation" &&
                  Boolean(validationRunQuery.data?.result_summary.source_fixture) && (
                    <span>Validation fixture summary</span>
                  )}
                {(workspace?.evidence ?? []).map((item) => (
                  <span key={item}>{item}</span>
                ))}
              </div>
            </>
        </aside>
      </section>

      {/* Second instance of the report controls, at the END of the Results step
          (Pete's 2026-07-15 walkthrough: a run finishes, the step auto-advances
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
      {module.route !== "reports" && canEngineer && activeRun && activeRunTerminal && (
        <section className="surface" data-stepgroup="results">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Reporting</span>
              <h3>Generate Report</h3>
            </div>
          </div>
          <div className="inline-actions">
            <ReportFromRunControls
              format={reportExportFormat}
              onFormatChange={setReportExportFormat}
              onGenerate={handleGenerateReportFromRun}
              pending={reportFromRunMutation.isPending}
            />
          </div>
          {reportToast && (
            <div className="state-panel success" role="status">
              <strong>Report generated</strong>
              <span>{reportToast}</span>
            </div>
          )}
          {reportFromRunMutation.isError && (
            <span className="error-text">{reportFromRunMutation.error.message}</span>
          )}
        </section>
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
  onFormatChange,
  onGenerate,
  pending,
}: {
  format: ReportFormat;
  onFormatChange: (next: ReportFormat) => void;
  onGenerate: () => void;
  pending: boolean;
}) {
  return (
    <>
      <label className="report-format-picker">
        Report format
        <select
          aria-label="Report format"
          onChange={(event) => onFormatChange(event.target.value as ReportFormat)}
          value={format}
        >
          <option value="pdf">PDF (.pdf)</option>
          <option value="docx">Word (.docx)</option>
          <option value="xlsx">Excel (.xlsx)</option>
          <option value="zip">Evidence pack (.zip)</option>
        </select>
      </label>
      <button
        className="secondary-button compact"
        disabled={pending}
        onClick={onGenerate}
        title="Generate a report for this run type, then find it in the Reports tab."
        type="button"
      >
        {pending ? "Generating..." : "Generate report from this run"}
      </button>
    </>
  );
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
    parameters.port_specification = scanPortSpecification(options.scanPorts);
    // Optional ad-hoc target override. Blank sends nothing so the backend falls
    // back to the imported IP register exactly as before. "/" => CIDR; "-" =>
    // start/end range; otherwise a single address. No UI validation — the
    // backend returns a clear 400/failed run for malformed input.
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
  if (input.useRegister) {
    // Register-driven run: send no pasted schedule/payloads/topics so the
    // backend builds one expected asset per imported mqtt_register row (its
    // wildcard topic, points, units, and Expected schema version). use_register
    // makes the backend refuse (400) when no register import exists, instead of
    // silently validating the packaged sample fixture.
    return {
      capture_seconds: captureSeconds,
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
    throw new Error(`${label} is not valid JSON: ${error instanceof Error ? error.message : "parse failed"}`);
  }
  throw new Error(`${label} must be a JSON object.`);
}

// Structured issue card (ITEM-9): the description reads as the headline, then the
// expected/observed comparison, any status detail, and the suggested action sit
// on their own readable lines — instead of one run-on <strong> string. `context`
// is the eyebrow's secondary label (the payload area, or the asset id). Pete's
// own word "empty" for a present-but-blank value survives inside expectedObserved
// (built in toIssueRow) byte-identical.
function IssueCard({ issue, context }: { issue: IssueRow; context: string }) {
  const headline = (issue.description ?? "").trim() || issue.message;
  return (
    <div className={`issue-card ${issue.severity}`}>
      <div className="issue-card-body">
        <span>{context ? `${issue.id} · ${context}` : issue.id}</span>
        <strong>{headline}</strong>
        {issue.expectedObserved && <small>{issue.expectedObserved}</small>}
        {issue.statusDetail && <small>Status: {issue.statusDetail}</small>}
        {issue.suggestedAction && <small className="issue-suggestion">{issue.suggestedAction}</small>}
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

// Expected-vs-observed UDMI payload panels (ITEM-8). When a payload was observed,
// the two sides are aligned LINE-FOR-LINE inside ONE scroll container (so they
// scroll together), JSON-syntax-coloured, with the presence diff (amber =
// expected-only key, red = observed-only key) and an honest red highlight on rows
// whose point name the engine actually flagged in its validation issues. VALUES
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
  // Red rows come ONLY from the engine's flagged point names (name-based,
  // best-effort): an issue with no point_name highlights no row, and topic /
  // cadence / schema-level issues stay the authority of the issue cards above.
  const flaggedPoints = new Set<string>();
  for (const issue of issues) {
    if (issue.pointName) {
      flaggedPoints.add(issue.pointName);
    }
  }
  const aligned =
    observedPresent && isPlainObject(expected) && isPlainObject(observed)
      ? alignPayloadDiff(expected, observed, flaggedPoints)
      : null;

  if (!aligned) {
    return (
      <div className="payload-compare">
        <div>
          <h6>Expected UDMI template</h6>
          <p className="section-copy">Registered values are shown where known; schema-valid sentinel values identify device-supplied fields and are not observed data.</p>
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
      <p className="section-copy">Registered values are shown where known; schema-valid sentinel values identify device-supplied fields and are not observed data.</p>
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
        {hasFlagged ? " Rows in red are points flagged by the validation issues above." : ""} Values are
        not compared here — expected values are template sentinels; see the issues above for value
        checks.
      </p>
    </>
  );
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
            <><strong>{key}</strong>: {JSON.stringify(child)}</>
          )}
        </li>
      ))}
    </ul>
  );
}

// A present-but-empty expected/observed value ("") is flagged as the explicit
// word "empty" (Pete's own word, ISSUE-10) rather than rendering as blank; an
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
  // The joined one-liner is still built (the row View modal reads issue.message);
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
    suggestedAction: issue.suggested_action ?? null,
    pointName: issue.point_name ?? null,
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

function renderCell(
  row: Record<string, string>,
  column: string,
  onCopyPayload: (payload: string, label: string) => void,
) {
  if (column === "Raw Payload" && row[column]) {
    return (
      <button
        className="secondary-button compact"
        onClick={(event) => {
          // Copy sits inside the results row; without this the click also fires
          // the row's focusInspectorPayload, collapsing/scrolling the inspector.
          event.stopPropagation();
          onCopyPayload(row[column], row.Asset ?? row.Topic ?? "Selected");
        }}
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
          {expectedSilent && <span className="chip amber"> Expected by register — no response</span>}
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
  const pairs = [
    { point: primaryPoint, value: primaryValue },
    ...extras,
  ].filter((pair) => pair.point.trim() !== "");
  // No extra pairs and the base payload already carries the single point: leave
  // the operator's payload untouched (preserves the original single-point flow).
  if (extras.every((pair) => pair.point.trim() === "")) {
    return basePayload;
  }
  let root: Record<string, unknown>;
  try {
    const parsed = JSON.parse(basePayload) as unknown;
    root = parsed && typeof parsed === "object" && !Array.isArray(parsed)
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
function useFileDownload() {
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const download = useCallback(
    async (key: string, path: string, fallbackFilename: string, init?: RequestInit) => {
      setPendingKey(key);
      setError(null);
      try {
        const { blob, filename } = await downloadFile(path, init);
        triggerBlobDownload(blob, filename ?? fallbackFilename);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Download failed.");
      } finally {
        setPendingKey(null);
      }
    },
    [],
  );

  const reset = useCallback(() => {
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
  if (route === "ip-scanner") {
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

  if (route === "bacnet-discovery") {
    return [
      { label: "Device", value: row.Device ?? "Selected BACnet device" },
      { label: "Instance", value: row.Instance ?? "Unknown" },
      { label: "Address", value: row.Address ?? "—" },
      { label: "IP Address", value: row["IP Address"] ?? "—" },
      { label: "Network Number", value: row["Network Number"] ?? "—" },
      { label: "Vendor", value: row.Vendor ?? "—" },
      { label: "Objects indexed", value: row.Objects ?? "Pending" },
      { label: "Last discovered", value: row.Discovered ?? row["Device Last Discovered"] ?? "Not recorded" },
      {
        label: live ? "Note" : "Object drilldown",
        value: live
          ? "Object-level present values are in the per-run points endpoint; comparison verdicts come from a validation run."
          : "Show object type, instance, object name, present value, units, reliability, status flags, priority array, and timestamp.",
      },
    ];
  }

  if (route === "mqtt-discovery") {
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
      { label: "Outputs", value: "Excel report for filtering and Word report for formal handover." },
    ];
  }

  return Object.entries(row).map(([label, value]) => ({ label, value }));
}
