import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router";
import {
  ApiError,
  browseBacnetScannerObjects,
  cancelRun,
  getDiscoveryComparison,
  getDiscoveryResults,
  getDiscoveryRun,
  listRuns,
  saveBacnetScanRunAsRegister,
  saveIpScanRunAsRegister,
  saveMqttScanRunAsRegister,
  startDiscoveryRun,
  type BacnetObjectBrowseResponse,
  type ImportBatchSummary,
  type ScanRegisterRoute,
} from "../../api/client";
import { mutationKeys, queryKeys } from "../../api/queryKeys";
import { useSession } from "../../app/sessionContext";
import type { RunRef } from "../../app/sessionScope";
import { buildDiscoveryParameters } from "../workflow/buildDiscoveryParameters";
import { getModuleByRoute, type ModuleRunAction } from "../workflow/moduleData";
import { isTerminalStatus, runPollInterval } from "../workflow/runFormat";
import {
  initialRunControllerState,
  latestAttachableRun,
  runControllerReducer,
  toRunRef,
} from "../workflow/runIsolation";
import {
  useRunOwnership,
  useTerminalEvidenceBarrier,
  type RunEpochOwner,
} from "../workflow/runOwnership";
import { useRunEvents } from "../workflow/useRunEvents";
import {
  SCANNER_PANEL_DEFAULT_WIDTH,
  SCANNER_PANEL_WIDTH_STORAGE_KEY,
  clampPanelWidth,
} from "./scannerRows";

/**
 * The side panel's width, remembered per browser (plan section 4.5). Shared by
 * every scanner screen so the operator's drag survives a lane switch; a private
 * window that refuses storage still gets a working, default-width panel.
 */
export function useStoredPanelWidth() {
  const [width, setWidth] = useState(() => {
    try {
      const stored = window.localStorage.getItem(SCANNER_PANEL_WIDTH_STORAGE_KEY);
      return stored ? clampPanelWidth(Number(stored)) : SCANNER_PANEL_DEFAULT_WIDTH;
    } catch {
      return SCANNER_PANEL_DEFAULT_WIDTH;
    }
  });
  useEffect(() => {
    try {
      window.localStorage.setItem(SCANNER_PANEL_WIDTH_STORAGE_KEY, String(width));
    } catch {
      // A private window can refuse storage; the panel still works this session.
    }
  }, [width]);
  return [width, setWidth] as const;
}

export type ScannerLane = "ip" | "bacnet" | "mqtt";

export const SCANNER_LANE_ROUTES: Record<ScannerLane, ScanRegisterRoute> = {
  ip: "ip-scanner",
  bacnet: "bacnet-scanner",
  mqtt: "mqtt-scanner",
};

/** Every per-run operator input the three native scanner setup cards expose. */
export type ScannerRunInputs = {
  ignoreRegister: boolean;
  // IP lane
  scanRangeStart?: string;
  scanRangeEnd?: string;
  probeTimeout?: string;
  // BACnet lane
  bacnetInstanceLow?: string;
  bacnetInstanceHigh?: string;
  bacnetDiscoverMs?: string;
  // MQTT lane (the capture run; the live explorer holds no run)
  captureTopicFilter?: string;
  captureSeconds?: string;
};

type ActiveScannerRun = {
  epoch: number;
  runId: string;
  restored?: boolean;
  ref: RunRef;
};

export type { RunEpochOwner };

/** The scanner lanes always settle on the run record plus its results. */
const DISCOVERY_EVIDENCE_REQUIREMENTS = ["run", "results"] as const;

/**
 * The sidecar (native scanner) run lifecycle, lifted verbatim out of ModulePage
 * so the dedicated IP / BACnet pages get the same run ownership guarantees
 * without the 11k-line component.
 *
 * What is deliberately NOT here, because these lanes never used it: the sealed
 * dry-run preview + scan-authorization ceremony (runKind "ip"/"bacnet" only),
 * the reserved-live-epoch fencing that ceremony needs, progressive observation
 * folding (gated to job types ip_discovery / bacnet_discovery), and validation
 * runs. What IS kept unchanged: the monotonic epoch, the owner comparison that
 * makes a late async callback drop rather than write into a newer submission,
 * the SSE run-access-closed scope, and the terminal evidence barrier that holds
 * results back until the run record and the results response name the same run.
 */
export function useScannerRun(lane: ScannerLane) {
  const moduleRoute = SCANNER_LANE_ROUTES[lane];
  const {
    apiClient,
    authorizationEnforced,
    canEngineer,
    sessionScopeId,
    workspace: workspaceRef,
  } = useSession();
  const canEngineerRef = useRef(canEngineer);
  canEngineerRef.current = canEngineer;
  const queryClient = useQueryClient();
  const module = getModuleByRoute(moduleRoute);
  const action = module.runActions[0] as Extract<ModuleRunAction, { kind: "discovery" }>;

  const [searchParams, setSearchParams] = useSearchParams();
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

  const [activeRun, setActiveRun] = useState<ActiveScannerRun | null>(null);
  const [runController, dispatchRun] = useReducer(runControllerReducer, initialRunControllerState);
  const [runOutcome, setRunOutcome] = useState<string | null>(null);
  const [runAttachmentNotice, setRunAttachmentNotice] = useState<string | null>(null);
  // The run id travels WITH the summary: the register CSV download is rebuilt
  // from the run that was saved, and reading it off the mutable `activeRun`
  // would hand run B's id to a download labelled with run A's file name.
  const [savedRegister, setSavedRegister] = useState<{
    runId: string;
    summary: ImportBatchSummary;
  } | null>(null);
  const [objectBrowseResult, setObjectBrowseResult] = useState<BacnetObjectBrowseResponse | null>(
    null,
  );
  const activeRunEpochRef = useRef(0);
  const nextActiveRunEpoch = useCallback(() => ++activeRunEpochRef.current, []);

  // Frictionless deployments authorize a live scan by default; enforced ones
  // require the operator's tick. Same derivation ModulePage uses.
  const [scanAuthorizedChecked, setScanAuthorizedChecked] = useState(false);
  const scanAuthorized = authorizationEnforced ? scanAuthorizedChecked : true;

  const runEvents = useRunEvents(
    activeRun?.ref,
    Boolean(activeRun) && runController.phase !== "submitting",
    apiClient,
    activeRun?.epoch ?? 0,
  );

  const { activeRunOwner, ownsActiveRun, runAccessClosed, resetRunAccessScope } = useRunOwnership({
    activeRun,
    moduleRoute,
    runEventConnectionState: runEvents.connectionState,
    runEventRunRef: runEvents.runRef,
    sessionScopeId,
    workspaceRef,
  });

  const sseEvent = runEvents.event;
  const sseDriving =
    runEvents.sseActive &&
    activeRun?.runId === runEvents.runId &&
    activeRun?.epoch === runEvents.epoch &&
    activeRun?.runId === sseEvent?.run_id;

  const runQueryKey = runAccessClosed
    ? ([...queryKeys.workspace(sessionScopeId, workspaceRef), "run", "closed"] as const)
    : activeRun?.ref
      ? ([
          ...queryKeys.run(sessionScopeId, workspaceRef, activeRun.ref),
          "epoch",
          activeRun.epoch,
        ] as const)
      : ([...queryKeys.workspace(sessionScopeId, workspaceRef), "run", moduleRoute, "none"] as const);

  const discoveryRunQuery = useQuery({
    enabled: !runAccessClosed && runController.phase !== "submitting" && Boolean(activeRun),
    queryFn: ({ signal }) => getDiscoveryRun(activeRun?.runId ?? "", { client: apiClient, signal }),
    queryKey: runQueryKey,
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

  const activeRunRecord = runAccessClosed ? undefined : discoveryRunQuery.data;
  const activeRunStatus = (sseDriving ? sseEvent?.status : undefined) ?? activeRunRecord?.status;
  const activeRunStage = (sseDriving ? sseEvent?.stage : undefined) ?? activeRunRecord?.stage;
  const activeRunProgress =
    (sseDriving ? sseEvent?.progress_percent : undefined) ?? activeRunRecord?.progress_percent ?? 0;
  const activeRunError =
    (sseDriving ? sseEvent?.error_message : undefined) ?? activeRunRecord?.error_message;
  const activeRunTerminal = isTerminalStatus(activeRunStatus);
  const activeRunAuthoritativelyTerminal = Boolean(
    activeRun &&
      activeRunRecord?.run_id === activeRun.runId &&
      runController.phase !== "submitting" &&
      runController.runRef?.runId === activeRun.runId &&
      runController.epoch === activeRun.epoch &&
      isTerminalStatus(activeRunRecord.status),
  );

  const discoveryResultsQuery = useQuery({
    enabled:
      !runAccessClosed &&
      Boolean(activeRun) &&
      runController.phase === "settled" &&
      runController.runRef?.runId === activeRun?.runId &&
      runController.epoch === activeRun?.epoch,
    queryFn: ({ signal }) =>
      getDiscoveryResults(activeRun?.runId ?? "", { client: apiClient, signal }),
    queryKey: runAccessClosed
      ? [...queryKeys.workspace(sessionScopeId, workspaceRef), "results", "closed"]
      : activeRun?.ref
        ? [
            ...queryKeys.results(sessionScopeId, workspaceRef, activeRun.ref),
            "epoch",
            activeRun.epoch,
          ]
        : [...queryKeys.workspace(sessionScopeId, workspaceRef), "results", "none"],
  });

  // Sealed run-to-run comparison, opened by a ?compare=<runId> deep link from
  // Run History. Unchanged from ModulePage.
  const discoveryComparisonQuery = useQuery({
    enabled: Boolean(comparisonRunId) && !runAccessClosed && activeRunAuthoritativelyTerminal,
    queryKey: runAccessClosed
      ? [...queryKeys.workspace(sessionScopeId, workspaceRef), "discovery-comparison", "closed"]
      : activeRun?.ref
        ? [
            ...queryKeys.run(sessionScopeId, workspaceRef, activeRun.ref),
            "discovery-comparison",
            "epoch",
            activeRun.epoch,
            comparisonRunId,
          ]
        : [...queryKeys.workspace(sessionScopeId, workspaceRef), "discovery-comparison", "none"],
    queryFn: ({ signal }) =>
      getDiscoveryComparison(activeRun?.runId ?? "", comparisonRunId ?? "", {
        client: apiClient,
        signal,
      }),
  });

  // ---- run re-attachment ---------------------------------------------------
  const requestedRunQuery = useQuery({
    enabled: Boolean(requestedRunId),
    queryFn: ({ signal }) =>
      getDiscoveryRun(requestedRunId ?? "", { client: apiClient, signal }),
    queryKey: [
      ...queryKeys.workspace(sessionScopeId, workspaceRef),
      "requested-run",
      moduleRoute,
      requestedRunId ?? "none",
    ],
    retry: (failureCount, error) =>
      !(error instanceof ApiError && error.status === 404) && failureCount < 2,
  });
  const requestedRunMatches =
    requestedRunQuery.data?.job_type === action.jobType ? requestedRunQuery.data : null;
  const requestedRunUnavailable =
    requestedRunQuery.error instanceof ApiError && requestedRunQuery.error.status === 404;
  const requestedRunIncompatible = requestedRunQuery.isSuccess && requestedRunMatches === null;

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
    enabled: !requestedRunId || requestedRunUnavailable || requestedRunIncompatible,
    queryKey: queryKeys.latestRun(sessionScopeId, workspaceRef, moduleRoute),
    queryFn: async ({ signal }) => {
      const response = await listRuns(
        {
          jobType: action.jobType,
          limit: 20,
          projectId: workspaceRef.projectId,
          siteId: workspaceRef.siteId,
        },
        { client: apiClient, signal },
      );
      return latestAttachableRun(response.runs);
    },
  });

  // True once the page knows whether a run is already in flight. Until then
  // `startedRunActive` is false because nothing has been attached yet, not
  // because nothing is running - a caller that acts on it early (the MQTT
  // page's live auto-connect) would fight a capture run for the broker. The
  // second clause is what makes it honest: the query answering is not enough,
  // the run it returned has to have been seeded into activeRun below.
  const restorableRun = requestedRunId ? requestedRunMatches : lastRunQuery.data;
  const runRestoreSettled =
    (requestedRunId ? requestedRunQuery.isFetched : lastRunQuery.isFetched) &&
    (!restorableRun || activeRun?.runId === restorableRun.run_id);

  // Route/workspace change resets the page's run state BEFORE the seed effect
  // below re-attaches, exactly as in ModulePage. Declaration order is
  // load-bearing: React runs effects in order, so the reset must come first or
  // one workspace's run bleeds into the next.
  useEffect(() => {
    setActiveRun(null);
    setRunOutcome(null);
    setRunAttachmentNotice(null);
    setSavedRegister(null);
    setObjectBrowseResult(null);
    // Consent does not travel between workspaces. The tick said "I am authorized
    // to scan THIS network"; carrying it into another project or site would send
    // authorized:true for a network nobody agreed to probe, with Start still
    // enabled and nothing on screen to show the claim had been reused.
    setScanAuthorizedChecked(false);
    resetRunAccessScope();
    dispatchRun({ type: "reset" });
  }, [moduleRoute, resetRunAccessScope, sessionScopeId, workspaceRef.projectId, workspaceRef.siteId]);

  useEffect(() => {
    const run = requestedRunId ? requestedRunMatches : lastRunQuery.data;
    if (!run || run.job_type !== action.jobType) {
      return;
    }
    // Replace a restored seed whose identity changed (the cache can hand over an
    // older run first), never a run started in this session.
    const replaceRestoredRun = activeRun?.restored === true && activeRun.runId !== run.run_id;
    if (activeRun && !replaceRestoredRun) {
      return;
    }
    const ref = toRunRef(sessionScopeId, workspaceRef, moduleRoute, run, "restored");
    const epoch = nextActiveRunEpoch();
    setActiveRun({ epoch, ref, restored: true, runId: run.run_id });
    dispatchRun({ type: "restored", runRef: ref, status: run.status, epoch });
  }, [
    action.jobType,
    activeRun,
    lastRunQuery.data,
    moduleRoute,
    nextActiveRunEpoch,
    requestedRunId,
    requestedRunMatches,
    sessionScopeId,
    workspaceRef,
  ]);

  // ---- terminal evidence barrier ------------------------------------------
  useEffect(() => {
    if (activeRun && activeRunTerminal) {
      dispatchRun({ type: "terminal-observed", runId: activeRun.runId, epoch: activeRun.epoch });
    }
  }, [activeRun, activeRunTerminal]);

  const refetchDiscoveryRun = discoveryRunQuery.refetch;
  const refetchDiscoveryResults = discoveryResultsQuery.refetch;
  const refetchRunStatus = useCallback(() => refetchDiscoveryRun(), [refetchDiscoveryRun]);
  const confirmEvidence = useCallback(
    async (run: ActiveScannerRun) => {
      const results = await refetchDiscoveryResults();
      if (results.isError || results.data?.run_id !== run.runId) {
        throw new Error("Final discovery evidence did not match the active run.");
      }
    },
    [refetchDiscoveryResults],
  );
  const requirementsFor = useCallback(() => DISCOVERY_EVIDENCE_REQUIREMENTS, []);
  useTerminalEvidenceBarrier({
    activeRun,
    blocked: runAccessClosed,
    confirmEvidence,
    dispatchRun,
    refetchRunStatus,
    requirementsFor,
    runController,
  });

  const finalEvidenceReady =
    runController.phase === "settled" &&
    runController.runRef?.runId === activeRun?.runId &&
    runController.epoch === activeRun?.epoch;

  const results =
    finalEvidenceReady && discoveryResultsQuery.data?.run_id === activeRun?.runId
      ? discoveryResultsQuery.data
      : null;

  // ---- mutations -----------------------------------------------------------
  const startMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, `${moduleRoute}.run`),
    mutationFn: (inputs: ScannerRunInputs) =>
      startDiscoveryRun({
        context: { client: apiClient },
        jobType: action.jobType,
        parameters: buildDiscoveryParameters(action, {
          authorized: scanAuthorized,
          dryRun: false,
          scanPorts: [],
          target: "",
          scanRangeStart: inputs.scanRangeStart,
          scanRangeEnd: inputs.scanRangeEnd,
          probeTimeout: inputs.probeTimeout,
          ignoreRegister: inputs.ignoreRegister,
          bacnetInstanceLow: inputs.bacnetInstanceLow,
          bacnetInstanceHigh: inputs.bacnetInstanceHigh,
          bacnetDiscoverMs: inputs.bacnetDiscoverMs,
          captureTopicFilter: inputs.captureTopicFilter,
          captureSeconds: inputs.captureSeconds,
        }),
        runKind: action.runKind,
        workspace: workspaceRef,
      }),
    onMutate: () => {
      dispatchRun({ type: "submitting" });
    },
    onError: () => {
      if (activeRun) {
        dispatchRun({ type: "accepted", runRef: activeRun.ref, epoch: activeRun.epoch });
      } else {
        dispatchRun({ type: "reset" });
      }
    },
    onSuccess: (result) => {
      const ref = toRunRef(sessionScopeId, workspaceRef, moduleRoute, result, "submitted");
      const epoch = nextActiveRunEpoch();
      setScopedRunUrl(result.run_id);
      setRunOutcome(`${result.message} Run ID: ${result.run_id}`);
      setSavedRegister(null);
      setObjectBrowseResult(null);
      setActiveRun({ epoch, ref, runId: result.run_id });
      dispatchRun({ type: "accepted", runRef: ref, epoch });
    },
  });

  const cancelMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, `${moduleRoute}.cancel`),
    mutationFn: (runId: string) => cancelRun(runId, { client: apiClient }),
    onSuccess: () => {
      void discoveryRunQuery.refetch();
    },
  });

  const saveRegisterMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, `${moduleRoute}.save-register`),
    mutationFn: (runId: string) =>
      lane === "bacnet"
        ? saveBacnetScanRunAsRegister({ context: { client: apiClient }, runId })
        : lane === "mqtt"
          ? saveMqttScanRunAsRegister({ context: { client: apiClient }, runId })
          : saveIpScanRunAsRegister({ context: { client: apiClient }, runId }),
    onSuccess: (summary, runId) => {
      // A save that resolves after the operator switched runs must not repopulate
      // the panel the run-change effect just cleared: the note would describe run
      // A while the page shows run B.
      if (runId === activeRunIdRef.current) {
        setSavedRegister({ runId, summary });
      }
      // Mirror the upload path: refresh the "register on file" note. The ROOT key
      // is what matches — queryKeys.latestImport ends in the import-type slot, so
      // passing it without one produces a key nothing is stored under.
      void queryClient.invalidateQueries({
        queryKey: queryKeys.latestImportRoot(sessionScopeId, workspaceRef),
      });
    },
  });

  // Read by saveRegisterMutation.onSuccess so a late save is compared against the
  // run on screen NOW, not the one that was active when the mutation was issued.
  const activeRunIdRef = useRef(activeRun?.runId);
  activeRunIdRef.current = activeRun?.runId;

  // BACnet only: an ephemeral live read of one device's object list. Starts no
  // child run and persists nothing.
  const objectBrowseMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "bacnet-scanner.object-browse"),
    mutationFn: ({
      runId,
      deviceInstance,
    }: {
      owner: RunEpochOwner;
      runId: string;
      deviceInstance: number;
    }) =>
      browseBacnetScannerObjects({
        context: { client: apiClient },
        runId,
        deviceInstance,
        authorized: scanAuthorized,
      }),
    // A live read is slow enough to outlive the run it was asked for. The panel
    // only checks device_instance, so the same instance in a later run would show
    // the earlier run values as if they had just been read. Drop any response
    // whose owner is no longer the run on screen, and check the run id the server
    // itself echoed rather than trusting the request we sent.
    onSuccess: (result, { owner }) => {
      if (!ownsActiveRun(owner) || result.run_id !== owner.runId) {
        return;
      }
      setObjectBrowseResult(result);
    },
  });

  useEffect(() => {
    setSavedRegister(null);
    setObjectBrowseResult(null);
  }, [activeRun?.runId, activeRun?.epoch]);

  const startedRunActive = Boolean(activeRun) && !activeRunTerminal;
  // Boolean(activeRunStatus) matters: a just-restored run has no record yet, and
  // without it Stop is enabled for a run whose real status may already be
  // terminal — the same guard ModulePage applies.
  const canCancel =
    Boolean(activeRun) &&
    Boolean(activeRunStatus) &&
    !activeRunTerminal &&
    canEngineer &&
    !runAccessClosed &&
    runController.phase !== "submitting";

  const start = useCallback(
    (inputs: ScannerRunInputs) => startMutation.mutate(inputs),
    [startMutation],
  );
  const stop = useCallback(() => {
    if (activeRun) {
      cancelMutation.mutate(activeRun.runId);
    }
  }, [activeRun, cancelMutation]);

  const saveAsRegister = useCallback(() => {
    if (activeRun) {
      saveRegisterMutation.mutate(activeRun.runId);
    }
  }, [activeRun, saveRegisterMutation]);

  const browseObjects = useCallback(
    (deviceInstance: number) => {
      if (activeRun && activeRunOwner) {
        objectBrowseMutation.mutate({
          deviceInstance,
          owner: activeRunOwner,
          runId: activeRun.runId,
        });
      }
    },
    [activeRun, activeRunOwner, objectBrowseMutation],
  );

  const clearComparison = useCallback(() => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.delete("compare");
        return next;
      },
      { replace: true },
    );
  }, [setSearchParams]);

  // Only a succeeded run with recorded evidence has something to save/export.
  // The MQTT capture records topics, not devices, so its gate reads the same way
  // ModulePage's saveableMqttTopicCount did.
  const saveableDeviceCount =
    activeRunStatus !== "succeeded"
      ? 0
      : lane === "mqtt"
        ? (results?.topics?.length ?? 0)
        : (results?.devices?.length ?? 0);

  return useMemo(
    () => ({
      // identity
      module,
      action,
      lane,
      moduleRoute,
      // session
      apiClient,
      authorizationEnforced,
      canEngineer,
      sessionScopeId,
      workspaceRef,
      // run state
      activeRun,
      activeRunError,
      activeRunProgress,
      activeRunRecord,
      activeRunStage,
      activeRunStatus,
      activeRunTerminal,
      activeRunAuthoritativelyTerminal,
      activeRunOwner,
      canCancel,
      ownsActiveRun,
      runAccessClosed,
      runAttachmentNotice,
      runController,
      runOutcome,
      runRestoreSettled,
      startedRunActive,
      // authorization
      scanAuthorized,
      scanAuthorizedChecked,
      setScanAuthorizedChecked,
      // evidence
      comparisonRunId,
      clearComparison,
      discoveryComparisonQuery,
      discoveryResultsQuery,
      results,
      saveableDeviceCount,
      savedRegister,
      // actions
      browseObjects,
      cancelMutation,
      objectBrowseMutation,
      objectBrowseResult,
      saveAsRegister,
      saveRegisterMutation,
      start,
      startMutation,
      stop,
    }),
    [
      action,
      activeRun,
      activeRunAuthoritativelyTerminal,
      activeRunError,
      activeRunProgress,
      activeRunRecord,
      activeRunStage,
      activeRunStatus,
      activeRunTerminal,
      apiClient,
      authorizationEnforced,
      browseObjects,
      activeRunOwner,
      canCancel,
      canEngineer,
      cancelMutation,
      clearComparison,
      comparisonRunId,
      discoveryComparisonQuery,
      discoveryResultsQuery,
      lane,
      module,
      moduleRoute,
      objectBrowseMutation,
      objectBrowseResult,
      ownsActiveRun,
      results,
      runAccessClosed,
      runAttachmentNotice,
      runController,
      runOutcome,
      runRestoreSettled,
      saveAsRegister,
      saveRegisterMutation,
      saveableDeviceCount,
      savedRegister,
      scanAuthorized,
      scanAuthorizedChecked,
      sessionScopeId,
      start,
      startMutation,
      startedRunActive,
      stop,
      workspaceRef,
    ],
  );
}

export type ScannerRunController = ReturnType<typeof useScannerRun>;
