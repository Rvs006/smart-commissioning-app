import { ChangeEvent, useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  cancelRun,
  createImport,
  createReport,
  downloadFile,
  getDiscoveryResults,
  getDiscoveryRun,
  getDiscoveryTopics,
  getDiscoveryTopicsXlsxPath,
  getValidationIssues,
  getValidationRun,
  getImportErrors,
  getImportTemplatePath,
  getReportDownloadPath,
  ImportBatchSummary,
  ImportErrorReport,
  ImportType,
  JobStatus,
  listImportProfiles,
  listReports,
  rollbackMqttConfigPublish,
  startMqttConfigPublishRun,
  startDiscoveryRun,
  startValidationRun,
  DiscoveryRowRecord,
  ReportSummary,
  ReportType,
  UdmiAssetPayloadView,
  ValidationIssueRecord,
} from "../../api/client";
import { getModuleByRoute, type ModuleRunAction } from "./moduleData";
import { groupIssuesByAsset, mergeAssetGroups, moduleWorkspaces, type IssueRow } from "./operatorData";
import {
  discoveryMetrics,
  discoveryViewFor,
  forbiddenOpenPorts,
  matchesTopicFilter,
  unexpectedOpenPorts,
  validationMetrics,
} from "./discoveryRows";
import { isTerminalStatus } from "./runFormat";
import { useRunEvents } from "./useRunEvents";
import { ENGINEER_REQUIRED_TOOLTIP, useSession } from "../../app/sessionContext";

type ModulePageProps = {
  moduleRoute: string;
};

type ImportOutcome = {
  summary: ImportBatchSummary;
  errors: ImportErrorReport | null;
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
type ActiveRun = {
  runId: string;
  kind: "discovery" | "validation";
};

// The module page is split into three stages so the operator works one screen
// at a time instead of scrolling a single long page of every control at once.
type ModuleStep = "setup" | "run" | "results";

const DISCOVERY_ROUTES = new Set(["ip-scanner", "bacnet-discovery", "mqtt-discovery"]);

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

const defaultScanPorts: ScanPort[] = [
  { port: "47808", protocol: "udp" },
  { port: "80", protocol: "tcp" },
  { port: "443", protocol: "tcp" },
];
const defaultExpectedSchedule = JSON.stringify(
  {
    asset_id: "AHU-1000001",
    guid: "ifc://expected-ahu-1000001",
    manufacturer: "Schneider",
    model: "PM5111",
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
      operation: {
        operational: true,
      },
    },
    timestamp: "2026-04-01T10:47:38.697+01:00",
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
        },
      },
    },
    timestamp: "2026-04-01T10:48:00.000+01:00",
  },
  null,
  2,
);
const defaultPointsetPayload = JSON.stringify(
  {
    pointset: {
      points: {
        supply_air_temperature_setpoint: {
          present_value: 22,
        },
      },
    },
    timestamp: "2026-04-01T10:48:56.312+01:00",
  },
  null,
  2,
);

export function ModulePage({ moduleRoute }: ModulePageProps) {
  // Discovery/validation/report runs, imports, cancel, publish, and rollback are
  // all engineer+ mutations server-side. A viewer/reviewer sees these controls
  // disabled with an explanatory tooltip rather than letting the click 403.
  const { canEngineer } = useSession();
  const module = getModuleByRoute(moduleRoute);
  const workspace = moduleWorkspaces[moduleRoute];
  const isDiscoveryModule = DISCOVERY_ROUTES.has(module.route);
  const [selectedImportType, setSelectedImportType] = useState<ImportType | "">("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [importOutcome, setImportOutcome] = useState<ImportOutcome | null>(null);
  const [runOutcome, setRunOutcome] = useState<string | null>(null);
  const [lastReport, setLastReport] = useState<ReportSummary | null>(null);
  const [activeRun, setActiveRun] = useState<ActiveRun | null>(null);
  const [copyFeedback, setCopyFeedback] = useState<CopyFeedback | null>(null);
  const [publishTopic, setPublishTopic] = useState("334os/b1/ahu-1000001/config");
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
  const [publishPointsetTopic, setPublishPointsetTopic] = useState("334os/b1/ahu-1000001/events/pointset");
  const [publishWaitSeconds, setPublishWaitSeconds] = useState("5");
  const [scanPorts, setScanPorts] = useState<ScanPort[]>(defaultScanPorts);
  const [scanAuthorized, setScanAuthorized] = useState(false);
  const [scanDryRun, setScanDryRun] = useState(false);
  const [udmiExpectedSchedule, setUdmiExpectedSchedule] = useState(defaultExpectedSchedule);
  const [udmiStatePayload, setUdmiStatePayload] = useState(defaultStatePayload);
  const [udmiMetadataPayload, setUdmiMetadataPayload] = useState(defaultMetadataPayload);
  const [udmiPointsetPayload, setUdmiPointsetPayload] = useState(defaultPointsetPayload);
  const [udmiUseLiveBroker, setUdmiUseLiveBroker] = useState(false);
  const [udmiStateTopic, setUdmiStateTopic] = useState("334os/b1/ahu-1000001/state");
  const [udmiMetadataTopic, setUdmiMetadataTopic] = useState("334os/b1/ahu-1000001/metadata");
  const [udmiPointsetTopic, setUdmiPointsetTopic] = useState("334os/b1/ahu-1000001/events/pointset");
  const [udmiCaptureSeconds, setUdmiCaptureSeconds] = useState("5");
  const [selectedResultIndex, setSelectedResultIndex] = useState(0);
  // Per-asset expansion in the UDMI per-payload-type results view (mq9m4bnv),
  // and the nested expected-vs-observed payload expand keyed `${asset}:${type}`.
  const [expandedAsset, setExpandedAsset] = useState<string | null>(null);
  const [expandedPayloadKey, setExpandedPayloadKey] = useState<string | null>(null);
  // Reports page: which queued reports are ticked for "Export selected" and a
  // one-shot confirmation shown after a report is generated (mqatcqb3/mqautz9j).
  const [selectedReportIds, setSelectedReportIds] = useState<Set<string>>(new Set());
  const [reportToast, setReportToast] = useState<string | null>(null);
  // MQTT Explorer-like capture inputs (mq9nhbzu). The live broker capture itself
  // is on-site-untested; this drives the existing mqtt discovery run + topics.
  const [captureTopicFilter, setCaptureTopicFilter] = useState("#");
  const [captureSeconds, setCaptureSeconds] = useState("10");
  const [step, setStep] = useState<ModuleStep>("setup");
  const templateDownload = useFileDownload();
  const reportDownload = useFileDownload();
  const exportDownload = useFileDownload();
  const allTemplatesDownload = useFileDownload();

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
    refetchInterval: (query) => {
      if (isTerminalStatus(query.state.data?.status) || runEvents.reachedTerminal) {
        return false;
      }
      return runEvents.sseActive ? false : 1500;
    },
  });

  // Discovery run monitor — same polling contract, against the discovery
  // status endpoint, so queued/running discovery runs update live.
  const discoveryRunQuery = useQuery({
    enabled: Boolean(activeRun) && activeRun?.kind === "discovery",
    queryFn: () => getDiscoveryRun(activeRun?.runId ?? ""),
    queryKey: ["discovery-run", activeRun?.runId],
    refetchInterval: (query) => {
      if (isTerminalStatus(query.state.data?.status) || runEvents.reachedTerminal) {
        return false;
      }
      return runEvents.sseActive ? false : 1500;
    },
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
  const resetAllTemplatesDownload = allTemplatesDownload.reset;

  useEffect(() => {
    setSelectedImportType(module.importTypes[0] ?? "");
    setSelectedFile(null);
    setImportOutcome(null);
    setRunOutcome(null);
    setLastReport(null);
    setActiveRun(null);
    setCopyFeedback(null);
    setSelectedResultIndex(0);
    setExpandedAsset(null);
    setSelectedReportIds(new Set());
    setReportToast(null);
    setScanAuthorized(false);
    setScanDryRun(false);
    setStep("setup");
    resetTemplateDownload();
    resetReportDownload();
    resetExportDownload();
    resetAllTemplatesDownload();
  }, [
    module.route,
    module.importTypes,
    resetTemplateDownload,
    resetReportDownload,
    resetExportDownload,
    resetAllTemplatesDownload,
  ]);

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
  useEffect(() => {
    if (activeRun) {
      setStep("run");
    }
  }, [activeRun]);

  // Only a *successful* run advances to Results. A failed/cancelled run is left
  // on the Run step, where the monitor shows the terminal status and
  // activeRunError — otherwise the operator would land on an empty Results view
  // with no clue why the job ended.
  useEffect(() => {
    if (activeRunTerminal && activeRunStatus === "succeeded") {
      setStep("results");
    }
  }, [activeRunTerminal, activeRunStatus]);

  const importMutation = useMutation({
    mutationFn: async (input: { importType: ImportType; file: File }) => {
      const summary = await createImport({
        file: input.file,
        importType: input.importType,
      });
      const errors =
        summary.status !== "accepted" || summary.rejected_rows > 0
          ? await getImportErrors(summary.import_id)
          : null;

      return { errors, summary };
    },
    onSuccess: (outcome) => {
      setImportOutcome(outcome);
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
            captureSeconds,
            captureTopicFilter,
            dryRun: scanDryRun,
            scanPorts,
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
                  captureSeconds: udmiCaptureSeconds,
                  expectedSchedule: udmiExpectedSchedule,
                  metadataPayload: udmiMetadataPayload,
                  metadataTopic: udmiMetadataTopic,
                  pointsetPayload: udmiPointsetPayload,
                  pointsetTopic: udmiPointsetTopic,
                  statePayload: udmiStatePayload,
                  stateTopic: udmiStateTopic,
                  useLiveBroker: udmiUseLiveBroker,
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
  const reportFromRunMutation = useMutation({
    mutationFn: ({ reportType, runId }: { reportType: ReportType; runId: string }) =>
      createReport({ format: "zip", reportType, sourceRunIds: [runId] }),
    onSuccess: (result) => {
      setReportToast(
        `Report generated from this run — see the Reports tab. Report ID: ${result.report_id}.`,
      );
    },
  });

  const availableProfiles =
    profilesQuery.data?.filter((profile) => module.importTypes.includes(profile.import_type)) ?? [];

  const selectedProfile = availableProfiles.find(
    (profile) => profile.import_type === selectedImportType,
  );

  // Index of the UDMI validation run action, used by the Schedule & Payload
  // Evidence "Execute capture" button so it triggers the same run as the Run
  // Controls card (mq9n7pbe). Defaults to 0 if the action shape ever changes.
  const udmiRunActionIndex = Math.max(
    0,
    module.runActions.findIndex(
      (action) => action.kind === "validation" && action.runKind === "udmi",
    ),
  );

  // Live issues for ANY terminal validation run (UDMI, BACnet, mapping), not
  // only UDMI. Falls back to the labelled sample workspace.issues otherwise.
  const liveIssues =
    activeRun?.kind === "validation" && validationIssuesQuery.data
      ? validationIssuesQuery.data.issues.map(toIssueRow)
      : null;
  const visibleIssues = liveIssues ?? workspace?.issues ?? [];

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

  const payloadViewSource =
    activeRun?.kind === "validation"
      ? (validationRunQuery.data?.result_summary?.payload_view_source as string | undefined)
      : undefined;

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

  // Live discovery results view (ip/bacnet/mqtt). Built only after a terminal
  // run; until then the table falls back to labelled sample rows.
  const discoveryView = useMemo(() => {
    if (!isDiscoveryModule || !discoveryResultsQuery.data) {
      return null;
    }
    return discoveryViewFor(module.route, discoveryResultsQuery.data);
  }, [isDiscoveryModule, discoveryResultsQuery.data, module.route]);

  const liveMetrics = useMemo(() => {
    if (!isDiscoveryModule || !discoveryResultsQuery.data) {
      return null;
    }
    return discoveryMetrics(module.route, discoveryResultsQuery.data);
  }, [isDiscoveryModule, discoveryResultsQuery.data, module.route]);

  const usingLiveResults = Boolean(discoveryView);
  const tableColumns = discoveryView?.columns ?? workspace?.columns ?? [];
  const resultRows = discoveryView?.rows ?? workspace?.rows ?? [];
  const selectedResult = resultRows[selectedResultIndex] ?? resultRows[0] ?? null;
  const resultDetails = selectedResult
    ? buildResultDetailItems(module.route, selectedResult, usingLiveResults)
    : [];

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

  const activeStatusClass = activeRunStatus ? toStatusClass(activeRunStatus) : "queued";
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

  // Export selected reports (mqatcqb3): download each ticked report through the
  // authenticated downloadFile path, sequentially so the browser keeps them.
  const handleExportSelected = async () => {
    const chosen = downloadableReports.filter((report) => selectedReportIds.has(report.report_id));
    if (chosen.length === 0) {
      return;
    }
    for (const report of chosen) {
      await exportDownload.download(
        `selected-${report.report_id}`,
        getReportDownloadPath(report.report_id),
        report.file_name || `${report.report_id}.${report.output_format}`,
      );
    }
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

  return (
    <div className="app-page">
      <section className="module-hero">
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
              <span>{module.route === "reports" ? "No reports yet" : "No run yet"}</span>
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
                <div className={`state-panel ${importOutcome.summary.status}`}>
                  <strong>{importOutcome.summary.status.toUpperCase()}</strong>
                  <span>
                    {importOutcome.summary.accepted_rows} accepted ·{" "}
                    {importOutcome.summary.rejected_rows} rejected
                  </span>
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
            {module.runActions.length > 0 ? (
              module.runActions.map((action, index) => {
                const scanBlocked = action.kind === "discovery" && discoveryBlocked;
                const blocked = scanBlocked || !canEngineer;
                // Role gate takes priority in the tooltip; otherwise the existing
                // scan-authorization hint is shown for a blocked real scan.
                const blockedTooltip = !canEngineer
                  ? ENGINEER_REQUIRED_TOOLTIP
                  : scanBlocked
                    ? "Confirm scan authorization (or enable dry run) before starting a real scan."
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
                </div>
                <span className={`status-token ${activeStatusClass}`}>
                  {activeRunStatus ?? "queued"}
                </span>
              </div>

              <div className="progress-track">
                <div style={{ width: `${activeRunProgress}%` }} />
              </div>

              <dl className="summary-grid">
                <div>
                  <dt>Stage</dt>
                  <dd>{activeRunStage?.replace(/_/g, " ") ?? "Waiting for first update"}</dd>
                </div>
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
              </dl>

              <div className="inline-actions">
                {canCancel && (
                  <button
                    className="secondary-button compact"
                    disabled={cancelMutation.isPending}
                    onClick={() => cancelMutation.mutate(activeRun.runId)}
                    type="button"
                  >
                    {cancelMutation.isPending ? "Cancelling..." : "Cancel run"}
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
                  <button
                    className="secondary-button compact"
                    disabled={reportFromRunMutation.isPending}
                    onClick={handleGenerateReportFromRun}
                    title="Generate a report for this run type, then find it in the Reports tab."
                    type="button"
                  >
                    {reportFromRunMutation.isPending ? "Generating..." : "Generate report from this run"}
                  </button>
                )}
              </div>

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
            </div>
          )}
        </article>
      </section>

      {module.importTypes.length > 0 && (
        <section className="surface" data-stepgroup="setup">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Templates</span>
              <h3>Import Templates for This Page</h3>
            </div>
          </div>
          <p className="section-copy">
            Every register and validation template this page accepts, downloadable as XLSX or CSV.
            Each template includes the required columns and one realistic example row.
          </p>
          <div className="template-grid">
            {module.importTypes.map((importType) => (
              <article className="schema-card template-card" key={importType}>
                <div>
                  <strong>{formatImportTypeLabel(importType)}</strong>
                  <p>{importType}.xlsx / {importType}.csv</p>
                </div>
                <div className="inline-actions">
                  <button
                    className="secondary-button compact"
                    disabled={allTemplatesDownload.pendingKey !== null}
                    onClick={() =>
                      void allTemplatesDownload.download(
                        `all-${importType}-xlsx`,
                        getImportTemplatePath(importType, "xlsx"),
                        `${importType}_template.xlsx`,
                      )
                    }
                    type="button"
                  >
                    {allTemplatesDownload.pendingKey === `all-${importType}-xlsx`
                      ? "Downloading..."
                      : "XLSX"}
                  </button>
                  <button
                    className="secondary-button compact"
                    disabled={allTemplatesDownload.pendingKey !== null}
                    onClick={() =>
                      void allTemplatesDownload.download(
                        `all-${importType}-csv`,
                        getImportTemplatePath(importType, "csv"),
                        `${importType}_template.csv`,
                      )
                    }
                    type="button"
                  >
                    {allTemplatesDownload.pendingKey === `all-${importType}-csv`
                      ? "Downloading..."
                      : "CSV"}
                  </button>
                </div>
              </article>
            ))}
          </div>
          {allTemplatesDownload.error && (
            <div className="state-panel error">
              <strong>Template download failed</strong>
              <span>{allTemplatesDownload.error}</span>
            </div>
          )}
        </section>
      )}

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
          <div className="port-editor">
            {scanPorts.map((entry, index) => (
              <div className="port-row" key={`${entry.protocol}-${index}`}>
                <label>
                  Port
                  <input
                    inputMode="numeric"
                    onChange={(event) => changeScanPort(index, "port", event.target.value)}
                    placeholder="47808"
                    value={entry.port}
                  />
                </label>
                <label>
                  Protocol
                  <select
                    onChange={(event) => changeScanPort(index, "protocol", event.target.value as ScanPort["protocol"])}
                    value={entry.protocol}
                  >
                    <option value="udp">UDP</option>
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
            list empty to use the common fallback: 47808/udp, 80/tcp, and 443/tcp.
          </p>
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
                placeholder="334os/+/+/#"
                value={captureTopicFilter}
              />
            </label>
            <label>
              Capture duration (seconds — 0 or blank = run until stopped)
              <input
                inputMode="numeric"
                onChange={(event) => setCaptureSeconds(event.target.value)}
                placeholder="0 = until stopped"
                value={captureSeconds}
              />
            </label>
          </div>
          <p className="section-copy">
            Subscribes through an MQTT discovery run and shows the latest payload seen per topic. The live
            broker capture is on-site-untested here; with no broker reachable the run records
            broker_unreachable and this panel stays empty rather than showing fabricated payloads. The
            filter and duration are sent to the run; set the duration to{" "}
            <strong>{Number(captureSeconds) > 0 ? `${captureSeconds}s` : "0 (run until stopped)"}</strong> —
            an indefinite capture runs until you press Cancel above or the message cap is reached. Captured
            topics appear here when the run completes.
          </p>
          <div className="data-table-wrap">
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
              <h3>Schedule and Payload Evidence</h3>
            </div>
          </div>
          <div className="json-workbench">
            <label>
              Expected schedule JSON
              <textarea
                onChange={(event) => setUdmiExpectedSchedule(event.target.value)}
                rows={9}
                value={udmiExpectedSchedule}
              />
            </label>
            <label>
              State payload JSON
              <textarea
                onChange={(event) => setUdmiStatePayload(event.target.value)}
                rows={9}
                value={udmiStatePayload}
              />
            </label>
            <label>
              Metadata payload JSON
              <textarea
                onChange={(event) => setUdmiMetadataPayload(event.target.value)}
                rows={9}
                value={udmiMetadataPayload}
              />
            </label>
            <label>
              Pointset payload JSON
              <textarea
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
            <div className="publish-grid">
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
              <label>
                Capture window seconds
                <input
                  inputMode="numeric"
                  onChange={(event) => setUdmiCaptureSeconds(event.target.value)}
                  value={udmiCaptureSeconds}
                />
              </label>
            </div>
          )}

          <div className="inline-actions execute-row">
            <button
              className="primary-button compact"
              disabled={runMutation.isPending || !canEngineer}
              onClick={() => runMutation.mutate(udmiRunActionIndex)}
              title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
              type="button"
            >
              {runMutation.isPending ? "Executing..." : "Execute capture"}
            </button>
            <span className="section-copy execute-note">
              {udmiUseLiveBroker
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
        <section className="surface" data-stepgroup="results">
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
                    <th>File</th>
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
                          <span className={`status-token ${toStatusClass(report.status)}`}>
                            {report.status}
                          </span>
                        </td>
                        <td>{report.file_name || "—"}</td>
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

      <section className="app-grid two-col wide-left" data-stepgroup="results">
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

          {!usingLiveResults && resultRows.length > 0 && (
            <div className="sample-banner" role="note">
              {isDiscoveryModule
                ? "Sample preview — run a discovery to replace this with live results."
                : "Sample preview — per-asset result rows are illustrative. The live issues panel and run monitor reflect real run data."}
            </div>
          )}
          {usingLiveResults && (
            <div className="sample-banner" role="note">
              Live discovery observations. Register-comparison verdicts (matched / rogue / missing)
              are produced by validation, not discovery, so no "Result" column is shown here.
            </div>
          )}

          <div className="data-table-wrap">
            {resultRows.length > 0 ? (
              <table className="data-table">
                <thead>
                  <tr>
                    {tableColumns.map((column) => (
                      <th key={column}>{column}</th>
                    ))}
                    <th>Details</th>
                  </tr>
                </thead>
                <tbody>
                  {resultRows.map((row, rowIndex) => (
                    <tr key={rowIndex}>
                      {tableColumns.map((column) => (
                        <td key={column}>{renderCell(row, column, handleCopyPayload)}</td>
                      ))}
                      <td>
                        <button
                          className={`secondary-button compact${selectedResultIndex === rowIndex ? " selected" : ""}`}
                          onClick={() => setSelectedResultIndex(rowIndex)}
                          type="button"
                        >
                          View
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="empty-workspace">
                <strong>
                  {isDiscoveryModule && activeRun && !activeRunTerminal
                    ? "Run in progress..."
                    : "No results yet"}
                </strong>
                <span>
                  {isDiscoveryModule
                    ? "Run a discovery; observed devices, points, or topics appear here once it completes."
                    : "Run a job to populate results."}
                </span>
              </div>
            )}
          </div>
        </article>

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
                  {mergedAssetGroups.map((group) => {
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
                              return (
                                <div className="payload-type-group" key={entry.payloadType}>
                                  <h5>{entry.payloadType}</h5>
                                  {entry.issues.map((issue) => (
                                    <div className={`issue-card ${issue.severity}`} key={issue.id}>
                                      <div className="issue-card-body">
                                        <span>{issue.id}</span>
                                        <strong>{issue.message}</strong>
                                        <small>{issue.area}</small>
                                      </div>
                                    </div>
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
                                        <div className="payload-compare">
                                          <div>
                                            <h6>Expected</h6>
                                            <pre className="payload-cell">
                                              {entry.expected
                                                ? JSON.stringify(entry.expected, null, 2)
                                                : "—"}
                                            </pre>
                                          </div>
                                          <div>
                                            <h6>Observed</h6>
                                            <pre className="payload-cell">
                                              {entry.observedPresent
                                                ? JSON.stringify(entry.observed, null, 2)
                                                : "not captured"}
                                            </pre>
                                          </div>
                                        </div>
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
              ) : (
              <div className="issue-list compact-list">
                {visibleIssues.length > 0 ? (
                  visibleIssues.map((issue) => (
                    <div className={`issue-card ${issue.severity}`} key={issue.id}>
                      <div className="issue-card-body">
                        <span>{issue.id}</span>
                        <strong>{issue.message}</strong>
                        <small>{issue.assetId}</small>
                      </div>
                    </div>
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
  },
): Record<string, unknown> {
  const parameters: Record<string, unknown> = {};
  if (options.dryRun) {
    parameters.dry_run = true;
  } else {
    parameters.authorized = options.authorized;
    parameters.scan_authorization = {
      authorized: options.authorized,
      authorized_by: "frontend-operator",
    };
  }
  if (action.runKind === "ip") {
    parameters.port_specification = scanPortSpecification(options.scanPorts);
  }
  // MQTT discovery: forward the operator's topic filter and capture window so
  // the engine subscribes to the requested topics for the requested duration
  // (mq9nhbzu). The backend reads topic_filter + capture_seconds.
  if (action.runKind === "mqtt") {
    const filter = options.captureTopicFilter?.trim();
    if (filter) {
      parameters.topic_filter = filter;
    }
    // Empty / 0 / non-numeric => 0, the backend's "indefinite" sentinel: run
    // until stopped (Cancel) or the message cap. A positive value is a bounded
    // capture window. >0 => bounded; otherwise indefinite (mq9nhbzu).
    const raw = (options.captureSeconds ?? "").trim();
    const seconds = Number(raw);
    parameters.capture_seconds = raw !== "" && Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
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
}): Record<string, unknown> {
  return {
    capture_seconds: Number(input.captureSeconds) || 5,
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

function toIssueRow(issue: ValidationIssueRecord): IssueRow {
  const details = [
    issue.description,
    issue.status_detail ? `Status: ${issue.status_detail}` : null,
    issue.expected_value || issue.observed_value
      ? `Expected ${issue.expected_value ?? "n/a"}, observed ${issue.observed_value ?? "n/a"}`
      : null,
    issue.suggested_action,
  ]
    .filter(Boolean)
    .join(" ");
  return {
    area: issue.issue_type.replace(/_/g, " "),
    assetId: issue.asset_id ?? "Unknown asset",
    id: issue.issue_id,
    message: details,
    owner: "Commissioning team",
    severity: toIssueSeverity(issue.severity),
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

function toStatusClass(status: JobStatus): string {
  if (status === "succeeded") {
    return "ready";
  }
  if (status === "cancelled") {
    return "warning";
  }
  return status;
}

function formatSummaryValue(value: unknown): string {
  if (typeof value === "number" || typeof value === "string") {
    return String(value);
  }
  return "Pending";
}

function renderCell(
  row: Record<string, string>,
  column: string,
  onCopyPayload: (payload: string, label: string) => void,
) {
  if (column === "Raw Payload" && row[column]) {
    return (
      <button className="secondary-button compact" onClick={() => onCopyPayload(row[column], row.Asset ?? row.Topic ?? "Selected")} type="button">
        Copy payload
      </button>
    );
  }
  if (column === "Detailed Status") {
    const forbidden = forbiddenOpenPorts(row[column]);
    const unexpected = unexpectedOpenPorts(row[column]);
    if (forbidden || unexpected) {
      return (
        <>
          {row[column]}
          {forbidden && <span className="chip red"> Forbidden ports open: {forbidden}</span>}
          {unexpected && <span className="chip amber"> Unexpected ports open: {unexpected}</span>}
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

// Human-readable label for an import type, e.g. "bacnet_points" -> "Bacnet Points".
function formatImportTypeLabel(importType: ImportType): string {
  return importType
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
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

  const download = useCallback(async (key: string, path: string, fallbackFilename: string) => {
    setPendingKey(key);
    setError(null);
    try {
      const { blob, filename } = await downloadFile(path);
      triggerBlobDownload(blob, filename ?? fallbackFilename);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Download failed.");
    } finally {
      setPendingKey(null);
    }
  }, []);

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
): DetailItem[] {
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
    return [
      { label: "Topic", value: row.Topic ?? "State, metadata, or pointset topic" },
      { label: "Asset", value: row.Asset ?? "—" },
      { label: "Messages", value: row["Message Count"] ?? "Pending" },
      { label: "Last payload seen", value: row["Last Payload Seen"] ?? "Not recorded" },
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
