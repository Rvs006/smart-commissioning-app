import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  createImport,
  getImportErrors,
  getLatestImport,
  getScanRegisterCsvPath,
  listImportProfiles,
  scanRegisterRunIdFromFileName,
  type ImportBatchSummary,
  type ImportType,
  type ScanRegisterRoute,
} from "../../api/client";
import { mutationKeys, queryKeys } from "../../api/queryKeys";
import { ENGINEER_REQUIRED_TOOLTIP } from "../../app/sessionContext";
import { LiveRunConsole } from "../workflow/LiveRunConsole";
import { bacnetBackendLabel, discoveryEmptyStateFor } from "../workflow/discoveryRows";
import { useFileDownload } from "../workflow/fileDownload";
import { formatRelativeTime, humanizeStage } from "../workflow/runFormat";
import { RegisterImportFields } from "../workflow/RegisterImportFields";
import { DeviceDetailPanel } from "./DeviceDetailPanel";
import { GenerateReportCard } from "./GenerateReportCard";
import {
  COLUMN_TITLES,
  bacnetObjectsPill,
  ragFiltersFor,
  rowMatchesRagFilter,
  rowMatchesText,
  scannerColumns,
  scannerRowsFromResults,
  scannerSummaryPills,
  type RagFilter,
  type ScannerRow,
} from "./scannerRows";
import {
  useStoredPanelWidth,
  type ScannerRunController,
  type ScannerRunInputs,
} from "./useScannerRun";
import "./scanners.css";

export type SetupCell = {
  label: string;
  value: string;
  sub?: string;
};

export type ScannerScreenProps = {
  run: ScannerRunController;
  purpose: string;
  startLabel: string;
  /** Read-only values mirrored from Configuration. */
  setupCells: SetupCell[];
  /** The lane's editable per-run inputs. */
  setupFields: ReactNode;
  /** What Start posts. */
  inputs: ScannerRunInputs;
  onIgnoreRegisterChange: (next: boolean) => void;
  /**
   * A lane-specific reason Start must stay disabled. A string is an operator
   * fault (red, role="alert"); the object form lets a lane say the block is an
   * expected state instead, which renders as a neutral status panel — a
   * live-first page would otherwise shout an assertive alert as its resting UI.
   */
  startBlockedReason?:
    | string
    | null
    | { tone: "error" | "status"; title?: string; reason: string };
  /**
   * The side-panel width, when the page owns it. Two panels on one page must
   * not run two states over the same localStorage key. Omit to keep it here.
   */
  panelWidth?: number;
  onPanelWidthChange?: (width: number) => void;
  /** Extra buttons in the results heading (BACnet export assets, MQTT archive). */
  resultsActions?: ReactNode;
  /** Lane-specific evidence cards below the results table (BACnet routers / points). */
  evidenceCards?: ReactNode;
  /** Setup-card heading. MQTT names it "Broker & capture". */
  setupHeading?: string;
  /** Results-card heading. MQTT names it "Captured topics". */
  resultsHeading?: string;
  /**
   * The MQTT lane runs no register comparison from a checkbox: its capture
   * parameters carry no ignore_register key, so the control would be a lie.
   */
  showIgnoreRegister?: boolean;
  /** Extra content in the setup action row, left of the last-run line (MQTT live status). */
  actionRowExtra?: ReactNode;
  /** A whole card between the setup card and the register import card (MQTT live topics). */
  afterSetup?: ReactNode;
  /** A lane-specific note above the results table (the MQTT register comparison). */
  resultsNote?: ReactNode;
};

export function ScannerScreen({
  run,
  purpose,
  startLabel,
  setupCells,
  setupFields,
  inputs,
  onIgnoreRegisterChange,
  startBlockedReason = null,
  panelWidth: panelWidthProp,
  onPanelWidthChange,
  resultsActions,
  evidenceCards,
  setupHeading = "Scan setup",
  resultsHeading = "Results",
  showIgnoreRegister = true,
  actionRowExtra,
  afterSetup,
  resultsNote,
}: ScannerScreenProps) {
  const {
    activeRun,
    activeRunError,
    activeRunProgress,
    activeRunRecord,
    activeRunStage,
    activeRunStatus,
    activeRunTerminal,
    apiClient,
    authorizationEnforced,
    canCancel,
    canEngineer,
    lane,
    module,
    results,
    runAccessClosed,
    runAttachmentNotice,
    runController,
    runOutcome,
    saveableDeviceCount,
    savedRegister,
    scanAuthorized,
    scanAuthorizedChecked,
    sessionScopeId,
    setScanAuthorizedChecked,
    startedRunActive,
    workspaceRef,
  } = run;

  const queryClient = useQueryClient();
  const columns = scannerColumns(lane);
  const rows = useMemo(() => scannerRowsFromResults(lane, results), [lane, results]);
  const pills = useMemo(
    () => scannerSummaryPills(lane, results?.result_summary),
    [lane, results],
  );
  // BACnet also shows the exported-object count beside the six RAG counters
  // (the full-app artboard's "128 objects").
  const objectsPill = useMemo(
    () => (lane === "bacnet" ? bacnetObjectsPill(results?.result_summary) : null),
    [lane, results],
  );
  const backendNote = useMemo(
    () => (lane === "bacnet" && results ? bacnetBackendLabel(results) : null),
    [lane, results],
  );
  const emptyState = useMemo(
    () => discoveryEmptyStateFor(module.route, results ?? undefined, activeRunError),
    [activeRunError, module.route, results],
  );

  const [textFilter, setTextFilter] = useState("");
  const [ragFilter, setRagFilter] = useState<RagFilter>("all");
  const [selectedRowId, setSelectedRowId] = useState<string | null>(null);
  const [panelExpanded, setPanelExpanded] = useState(false);
  const [ownPanelWidth, setOwnPanelWidth] = useStoredPanelWidth();
  const panelWidth = panelWidthProp ?? ownPanelWidth;
  const setPanelWidth = onPanelWidthChange ?? setOwnPanelWidth;
  const rowRefs = useRef(new Map<string, HTMLTableRowElement>());
  const filterInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    setSelectedRowId(null);
    setPanelExpanded(false);
    setTextFilter("");
    setRagFilter("all");
  }, [activeRun?.runId, activeRun?.epoch]);

  const visibleRows = useMemo(
    () => rows.filter((row) => rowMatchesRagFilter(row, ragFilter) && rowMatchesText(row, textFilter)),
    [ragFilter, rows, textFilter],
  );
  // The panel only ever describes a row the operator can still see. A selection
  // filtered out of the table would otherwise leave the panel open with no row
  // to return focus to when it closes, dropping focus to <body>.
  useEffect(() => {
    if (selectedRowId && !visibleRows.some((row) => row.id === selectedRowId)) {
      setSelectedRowId(null);
      setPanelExpanded(false);
    }
  }, [selectedRowId, visibleRows]);

  const selectedRow: ScannerRow | null =
    visibleRows.find((row) => row.id === selectedRowId) ?? null;
  const browsingSelectedDevice =
    selectedRow?.deviceInstance !== undefined &&
    run.objectBrowseMutation.variables?.deviceInstance === selectedRow.deviceInstance;

  const closePanel = () => {
    const focusTarget = selectedRowId ? rowRefs.current.get(selectedRowId) : null;
    setSelectedRowId(null);
    setPanelExpanded(false);
    // The row is still on screen (see the effect above), so focus goes back to
    // where it came from; the filter box is the fallback if it has gone.
    if (focusTarget) {
      focusTarget.focus();
    } else {
      filterInputRef.current?.focus();
    }
  };

  // ---- Register import card -------------------------------------------------
  const importType = (module.importTypes[0] ?? "") as ImportType | "";
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [importOutcome, setImportOutcome] = useState<ImportBatchSummary | null>(null);
  const templateDownload = useFileDownload(apiClient);
  const registerCsvDownload = useFileDownload(apiClient);

  const profilesQuery = useQuery({
    queryFn: ({ signal }) => listImportProfiles({ client: apiClient, signal }),
    queryKey: queryKeys.importProfiles(sessionScopeId, workspaceRef),
  });
  const selectedProfile = (profilesQuery.data ?? []).find(
    (profile) => profile.import_type === importType,
  );
  const latestImportQuery = useQuery({
    enabled: importType !== "",
    queryFn: ({ signal }) =>
      getLatestImport(importType as ImportType, workspaceRef.projectId, workspaceRef.siteId, {
        client: apiClient,
        signal,
      }),
    queryKey: queryKeys.latestImport(sessionScopeId, workspaceRef, importType),
  });
  const importErrorsQuery = useQuery({
    enabled: Boolean(importOutcome && importOutcome.status !== "accepted"),
    queryFn: ({ signal }) =>
      getImportErrors(importOutcome?.import_id ?? "", { client: apiClient, signal }),
    queryKey: queryKeys.importErrors(sessionScopeId, workspaceRef, importOutcome?.import_id),
  });
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
      // The ROOT key is what matches: queryKeys.latestImport ends in the
      // import-type slot, so passing it without one builds a key nothing is
      // stored under and the "Register already imported" note never refreshed.
      void queryClient.invalidateQueries({
        queryKey: queryKeys.latestImportRoot(sessionScopeId, workspaceRef),
      });
    },
  });

  // A project/site switch must not leave the previous workspace's staged file or
  // import verdict on screen: they describe a register that does not apply here.
  useEffect(() => {
    setSelectedFile(null);
    setImportOutcome(null);
  }, [sessionScopeId, workspaceRef.projectId, workspaceRef.siteId]);

  // A register on file that came from "Save scan as register" names the run that
  // produced it, so the same CSV can be downloaded again; an uploaded register
  // has no run to rebuild it from.
  const scanRegisterRoute = module.route as ScanRegisterRoute;
  const latestRegisterFileName = latestImportQuery.data?.file_name ?? "";
  const latestRegisterCsvRunId = scanRegisterRunIdFromFileName(
    scanRegisterRoute,
    latestRegisterFileName,
  );

  const laneNoun = lane === "bacnet" ? "BACnet" : lane === "mqtt" ? "MQTT" : "IP";
  const laneRunNoun = lane === "mqtt" ? "capture" : "scan";

  // ---- run gating -----------------------------------------------------------
  const startBlock =
    typeof startBlockedReason === "string"
      ? { tone: "error" as const, title: undefined, reason: startBlockedReason }
      : startBlockedReason;
  const startBlocked =
    !canEngineer ||
    startedRunActive ||
    runAccessClosed ||
    !scanAuthorized ||
    Boolean(startBlock) ||
    run.startMutation.isPending;
  const startTooltip = !canEngineer
    ? ENGINEER_REQUIRED_TOOLTIP
    : runAccessClosed
      ? "Run access for this workspace is closed. Reopen the page before starting another run."
      : startedRunActive
        ? "A run is already in progress. Stop it before starting another."
        : !scanAuthorized
          ? "Confirm scan authorization before starting this scan."
          : (startBlock?.reason ?? undefined);

  const lastRunLine = activeRunRecord
    ? `Last run ${formatRelativeTime(activeRunRecord.updated_at ?? activeRunRecord.created_at)}`
    : "No run on this page yet";

  const statusClass =
    activeRunStatus === "succeeded"
      ? "ready"
      : activeRunStatus === "failed"
        ? "failed"
        : activeRunStatus === "running"
          ? "running"
          : "queued";

  return (
    <div className="app-page scanner-page">
      <header className="scanner-page-head">
        <p className="scanner-eyebrow">Discover</p>
        <h1>{module.title}</h1>
        <p className="scanner-purpose">{purpose}</p>
      </header>

      {/* ---- Scan setup ---- */}
      <section className="scanner-card" aria-labelledby="scanner-setup-heading">
        <div className="scanner-card-head">
          <h2 id="scanner-setup-heading">{setupHeading}</h2>
          <Link className="scanner-card-link" to="/configuration">
            Edit in Configuration <span aria-hidden="true">→</span>
          </Link>
        </div>
        <div className="scanner-setup-grid">
          {setupCells.map((cell) => (
            <div key={cell.label}>
              <span className="scanner-setup-label">{cell.label}</span>
              <span className="scanner-setup-value">{cell.value}</span>
              {cell.sub && <span className="scanner-setup-sub mono">{cell.sub}</span>}
            </div>
          ))}
        </div>
        <div className="scanner-setup-fields">
          {setupFields}
          {showIgnoreRegister && (
            <label className="confirm-row">
              <input
                checked={inputs.ignoreRegister}
                onChange={(event) => onIgnoreRegisterChange(event.target.checked)}
                type="checkbox"
              />
              Ignore register for this run (scan without RAG comparison)
            </label>
          )}
          {authorizationEnforced && (
            <label className="confirm-row">
              <input
                checked={scanAuthorizedChecked}
                onChange={(event) => setScanAuthorizedChecked(event.target.checked)}
                type="checkbox"
              />
              I am authorized to scan this network.
            </label>
          )}
        </div>
        <div className="scanner-action-row">
          <button
            className="primary-button"
            disabled={startBlocked}
            onClick={() => run.start(inputs)}
            title={startTooltip}
            type="button"
          >
            {run.startMutation.isPending ? "Starting..." : startLabel}
          </button>
          <button
            className="secondary-button"
            disabled={!canCancel || run.cancelMutation.isPending}
            onClick={run.stop}
            type="button"
          >
            {run.cancelMutation.isPending ? "Stopping..." : "Stop"}
          </button>
          {actionRowExtra}
          <p className="scanner-last-run">
            {lastRunLine}
            {activeRunStatus ? (
              <>
                {" · "}
                <strong>{activeRunStatus}</strong>
              </>
            ) : null}
          </p>
        </div>

        {startBlock &&
          (startBlock.tone === "status" ? (
            <div className="state-panel" role="status">
              {startBlock.title && <strong>{startBlock.title}</strong>}
              <span>{startBlock.reason}</span>
            </div>
          ) : (
            <p className="error-text" role="alert">
              {startBlock.reason}
            </p>
          ))}
        {run.startMutation.isError && (
          <div className="state-panel error">
            <strong>Run request failed</strong>
            <span>{run.startMutation.error.message}</span>
          </div>
        )}
        {run.cancelMutation.isError && (
          <div className="state-panel error">
            <strong>Stop failed</strong>
            <span>{run.cancelMutation.error.message}</span>
          </div>
        )}
        {runOutcome && (
          <div className="state-panel success">
            <strong>Accepted by API</strong>
            <span>{runOutcome}</span>
          </div>
        )}
        {runAttachmentNotice && (
          <div className="state-panel" role="note">
            <strong>Run link unavailable</strong>
            <span>{runAttachmentNotice}</span>
          </div>
        )}
        {activeRunError && (
          <div className="state-panel error" role="alert">
            <strong>Run reported an error</strong>
            <span>{activeRunError}</span>
          </div>
        )}

        {activeRun && (
          <div className="state-panel run-monitor">
            <div className="run-monitor-heading">
              <div>
                <strong>Discovery run monitor</strong>
                <span>{activeRun.runId}</span>
                <Link className="link-button" to="/run-history">
                  Run history
                </Link>
              </div>
              <span className={`status-token ${statusClass}`}>{activeRunStatus ?? "queued"}</span>
            </div>
            <div className="progress-track">
              <div style={{ width: `${Math.min(100, Math.max(0, activeRunProgress))}%` }} />
            </div>
            <p className="scanner-stage">
              {humanizeStage(activeRunStage ?? "") || "Waiting for first update"}
              {activeRunProgress > 0 ? ` · ${Math.round(activeRunProgress)}%` : ""}
            </p>
            {/* The one connection note that applies to these lanes: when run
                access closes mid-run, say why the evidence stopped. */}
            {runAccessClosed && (
              <p aria-live="polite" className="scanner-stage" role="status">
                Access changed. Live run evidence is no longer available in this workspace.
              </p>
            )}
            {/* The live console belongs to a run in flight. Once the run is
                terminal the results table below IS the outcome, and the
                console's UDMI/topic panels only render "waiting for evidence"
                placeholders for a lane that never produces them. */}
            {activeRunRecord && !activeRunTerminal && (
              <LiveRunConsole
                key={activeRunRecord.run_id}
                assetTopicDiscovery={null}
                elapsed=""
                issueCount={
                  typeof activeRunRecord.result_summary.issue_count === "number"
                    ? activeRunRecord.result_summary.issue_count
                    : 0
                }
                progress={activeRunProgress}
                run={activeRunRecord}
                stage={activeRunStage ?? ""}
                status={activeRunStatus ?? "queued"}
                validationSummary={null}
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
                    "Results appear after the final run data is confirmed."}
                </span>
              </div>
            )}
          </div>
        )}
      </section>

      {/* The MQTT lane's live explorer sits here: above the register card, so a
          live-first page opens on the tree rather than on an import form. */}
      {afterSetup}

      {/* ---- Register import ---- */}
      <section className="scanner-card" aria-labelledby="scanner-import-heading">
        <div className="scanner-card-head">
          <h2 id="scanner-import-heading">Register import</h2>
          <span className="scanner-setup-sub">{importType.replace(/_/g, " ")}</span>
        </div>
        <div className="scanner-card-body form-stack">
          <RegisterImportFields
            canEngineer={canEngineer}
            importErrorsQuery={importErrorsQuery}
            importOutcome={importOutcome}
            importType={importType}
            latestImport={latestImportQuery.data}
            onFileSelected={(file) => {
              setSelectedFile(file);
              setImportOutcome(null);
            }}
            onUpload={() => {
              if (selectedFile && importType) {
                importMutation.mutate({ file: selectedFile, importType });
              }
            }}
            registerCsv={{
              download: registerCsvDownload,
              fallbackFilename: latestRegisterFileName,
              path: latestRegisterCsvRunId
                ? getScanRegisterCsvPath(scanRegisterRoute, latestRegisterCsvRunId)
                : null,
            }}
            selectedFile={selectedFile}
            selectedProfile={selectedProfile}
            templateDownload={templateDownload}
            uploadError={importMutation.error}
            uploading={importMutation.isPending}
          />
        </div>
      </section>

      {/* ---- Sealed run comparison (?compare=) ---- */}
      {!runAccessClosed && run.comparisonRunId && (
        <section aria-label="Sealed run comparison" className="scanner-card">
          <div className="scanner-card-head">
            <h2>Sealed run against {run.comparisonRunId}</h2>
            <button className="secondary-button compact" onClick={run.clearComparison} type="button">
              Return to current run
            </button>
          </div>
          <div className="scanner-card-body">
            {run.discoveryComparisonQuery.isLoading ? (
              <p>Loading sealed comparison.</p>
            ) : run.discoveryComparisonQuery.isError ? (
              <div className="state-panel error" role="alert">
                <strong>Comparison unavailable</strong>
                <span>
                  {run.discoveryComparisonQuery.error instanceof Error
                    ? run.discoveryComparisonQuery.error.message
                    : "The sealed comparison could not be loaded."}
                </span>
              </div>
            ) : run.discoveryComparisonQuery.data?.compatible ? (
              <div className="comparison-summary" aria-live="polite">
                <span>{run.discoveryComparisonQuery.data.additions.length} additions</span>
                <span>{run.discoveryComparisonQuery.data.removals.length} removals</span>
                <span>{run.discoveryComparisonQuery.data.changes.length} changes</span>
              </div>
            ) : (
              <div className="state-panel" role="status">
                <strong>Runs cannot be compared</strong>
                <span>
                  {run.discoveryComparisonQuery.data?.reason ??
                    "The sealed runs are incompatible."}
                </span>
              </div>
            )}
          </div>
        </section>
      )}

      {/* ---- Results ---- */}
      <section className="scanner-card" aria-labelledby="scanner-results-heading">
        <div className="scanner-card-head">
          <div className="scanner-results-title">
            <h2 id="scanner-results-heading">{resultsHeading}</h2>
            <div className="scanner-pills">
              {pills.map((pill) => (
                <span className={`scanner-chip chip-${pill.chip}`} key={pill.label}>
                  {pill.value} {pill.label}
                </span>
              ))}
              {objectsPill && (
                <span className={`scanner-chip chip-${objectsPill.chip}`} key="objects">
                  {objectsPill.value} {objectsPill.label}
                </span>
              )}
            </div>
          </div>
          <div className="inline-actions">
            <button
              className="secondary-button compact"
              disabled={!canEngineer || !saveableDeviceCount || run.saveRegisterMutation.isPending}
              onClick={run.saveAsRegister}
              title={
                canEngineer
                  ? lane === "mqtt"
                    ? "Turn this capture's discovered assets into an expected-asset MQTT register (one row per asset, with its topic, schema, site and location)."
                    : lane === "bacnet"
                      ? "Turn this scan's discovered devices into an expected-device register (their reported object counts become the expected objects)."
                      : "Turn this scan's responding devices into an expected-device register (their open ports become the expected ports)."
                  : ENGINEER_REQUIRED_TOOLTIP
              }
              type="button"
            >
              {run.saveRegisterMutation.isPending
                ? "Saving register..."
                : `Save ${laneRunNoun} as register (applies to the next ${laneRunNoun})`}
            </button>
            {resultsActions}
          </div>
        </div>

        {/* Only while the panel still describes the run on screen: a save that
            resolved after the operator started another run would otherwise show
            run A file name beside run B download URL. */}
        {savedRegister && savedRegister.runId === activeRun?.runId && (
          <div className="state-panel success" role="status">
            <strong>Saved as register</strong>
            <span>
              {savedRegister.summary.file_name}: {savedRegister.summary.accepted_rows} of{" "}
              {savedRegister.summary.total_rows} rows accepted ({savedRegister.summary.import_id}).
              It is stored here and applies automatically to the next {laneNoun} {laneRunNoun} for
              this project and site. There is nothing to upload. Keep a copy if you want one:
            </span>
            <button
              className="secondary-button compact inline-link-button"
              disabled={registerCsvDownload.pendingKey !== null}
              onClick={() =>
                void registerCsvDownload.download({
                  fallbackFilename: savedRegister.summary.file_name,
                  key: "register-csv",
                  // The run that was SAVED, so the file and the URL always
                  // describe the same run.
                  path: getScanRegisterCsvPath(scanRegisterRoute, savedRegister.runId),
                })
              }
              type="button"
            >
              {registerCsvDownload.pendingKey === "register-csv"
                ? "Downloading..."
                : "Download register CSV"}
            </button>
          </div>
        )}
        {registerCsvDownload.pendingKey === null &&
          registerCsvDownload.error &&
          registerCsvDownload.errorStatus !== 404 && (
            <div className="state-panel error" role="alert">
              <strong>Register CSV download failed</strong>
              <span>{registerCsvDownload.error}</span>
            </div>
          )}
        {run.saveRegisterMutation.isError && (
          <div className="state-panel error" role="alert">
            <strong>Save as register failed</strong>
            <span>
              {run.saveRegisterMutation.error instanceof Error
                ? run.saveRegisterMutation.error.message
                : "The register could not be created."}
            </span>
          </div>
        )}
        {run.discoveryResultsQuery.isError && (
          <div className="state-panel error" role="alert">
            <strong>Results unavailable</strong>
            <span>
              {run.discoveryResultsQuery.error instanceof Error
                ? run.discoveryResultsQuery.error.message
                : "The discovery results could not be loaded."}
            </span>
          </div>
        )}

        {/* Provenance, never decoration: a simulated BACnet backend must never be
            mistaken for a real on-wire scan, and a TCP-connect miss is not proof
            a host is absent. */}
        {backendNote && (
          <div
            className={`sample-banner${backendNote.kind === "simulated" ? " warning" : ""}`}
            role={backendNote.kind === "simulated" ? "alert" : "note"}
          >
            {backendNote.text}
          </div>
        )}
        {lane === "ip" && rows.length > 0 && (
          <div className="sample-banner" role="note">
            Live scan observations. A host with no response on the scanned ports is inconclusive —
            a TCP-connect miss is not proof the host is absent.
          </div>
        )}
        {resultsNote}

        {rows.length > 0 && (
          <div className="results-filter-bar scanner-filter-bar">
            <label className="results-filter-text">
              Filter results
              <input
                onChange={(event) => setTextFilter(event.target.value)}
                placeholder={
                  lane === "mqtt" ? "Topic, asset, payload" : "Address, name, vendor, status"
                }
                ref={filterInputRef}
                value={textFilter}
              />
            </label>
            <div aria-label="Register verdict" className="scanner-chip-filters" role="group">
              {ragFiltersFor(lane).map((filter) => (
                <button
                  aria-pressed={ragFilter === filter.id}
                  className={`scanner-chip-button chip-${filter.chip}${
                    ragFilter === filter.id ? " active" : ""
                  }`}
                  key={filter.id}
                  onClick={() => setRagFilter(filter.id)}
                  type="button"
                >
                  {filter.id === "all" ? null : <span aria-hidden="true">●</span>} {filter.label}
                </button>
              ))}
            </div>
            <span className="results-filter-count">
              Showing {visibleRows.length} of {rows.length} {rows.length === 1 ? "row" : "rows"}
            </span>
          </div>
        )}

        <div
          className={`scanner-results-layout${selectedRow ? " with-panel" : ""}${
            panelExpanded ? " panel-expanded" : ""
          }`}
          style={{ ["--scanner-panel-width" as string]: `${panelWidth}px` }}
        >
          <div className="data-table-wrap results-scroll scanner-table-wrap">
            {rows.length === 0 ? (
              <div className="empty-workspace">
                {/* A failed, stopped or dry run must never read as "looked and
                    found nothing": discoveryEmptyStateFor echoes what the run
                    actually did, and only a run with no diagnosis of its own
                    falls through to the generic copy. */}
                <strong>
                  {emptyState
                    ? emptyState.title
                    : activeRun && !activeRunTerminal
                      ? `${lane === "mqtt" ? "Capture" : "Scan"} in progress...`
                      : lane === "mqtt"
                        ? "No captured topics yet"
                        : "No results yet"}
                </strong>
                <span>
                  {emptyState
                    ? emptyState.detail
                    : activeRun && !activeRunTerminal
                      ? `Rows appear when the ${laneRunNoun} finishes and its evidence is confirmed.`
                      : lane === "mqtt"
                        ? "Record a capture to persist the topics and payloads as run evidence. Empty live results stay empty — no sample payloads are shown."
                      : "Start a scan to populate this table."}
                </span>
              </div>
            ) : (
              <table className="data-table scanner-table">
                <thead>
                  <tr>
                    {columns.map((column) => (
                      <th key={column} scope="col" title={COLUMN_TITLES[column]}>
                        {column}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {visibleRows.map((row) => {
                    const selected = row.id === selectedRowId;
                    return (
                      <tr
                        aria-selected={selected}
                        className={`${row.tone ? `row-${row.tone}` : ""}${
                          selected ? " row-selected" : ""
                        } result-row-selectable`.trim()}
                        key={row.id}
                        onClick={() => setSelectedRowId(row.id)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            setSelectedRowId(row.id);
                          }
                        }}
                        ref={(node) => {
                          if (node) {
                            rowRefs.current.set(row.id, node);
                          } else {
                            rowRefs.current.delete(row.id);
                          }
                        }}
                        tabIndex={0}
                      >
                        {columns.map((column) => {
                          const cell = row.cells[column];
                          if (!cell) {
                            return <td key={column}>—</td>;
                          }
                          return (
                            <td className={cell.mono ? "mono" : undefined} key={column}>
                              {cell.chip ? (
                                <span className={`scanner-chip chip-${cell.chip}`}>{cell.text}</span>
                              ) : (
                                cell.text
                              )}
                              {cell.sub && <span>{cell.sub}</span>}
                            </td>
                          );
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>

          <DeviceDetailPanel
            expanded={panelExpanded}
            lane={lane}
            objectBrowse={
              lane === "bacnet"
                ? {
                    canBrowse: Boolean(activeRun) && activeRunTerminal && scanAuthorized,
                    blockedReason: scanAuthorized
                      ? null
                      : "Confirm scan authorization in Scan setup before reading live objects.",
                    // Scoped to the device actually being browsed: a read
                    // started on one row must not spin (or report a failure) on
                    // the next row the operator clicks.
                    pending: run.objectBrowseMutation.isPending && browsingSelectedDevice,
                    error:
                      run.objectBrowseMutation.isError && browsingSelectedDevice
                        ? run.objectBrowseMutation.error instanceof Error
                          ? run.objectBrowseMutation.error.message
                          : "The device object list could not be read."
                        : null,
                    result: run.objectBrowseResult,
                    onLoad: run.browseObjects,
                  }
                : undefined
            }
            onClose={closePanel}
            onResize={setPanelWidth}
            onToggleExpand={() => setPanelExpanded((current) => !current)}
            row={selectedRow}
            width={panelWidth}
          />
        </div>

        {activeRun && (
          <p className="scanner-footer-line">
            <span aria-hidden="true">✓</span> Saved as <code>{activeRun.ref.jobType}</code> run{" "}
            <code>#{activeRun.runId}</code> ·{" "}
            <Link to="/run-history">View in Run History</Link> ·{" "}
            <Link to="/reports">Open in Reports</Link>
          </p>
        )}
      </section>

      {evidenceCards}

      <GenerateReportCard run={run} />
    </div>
  );
}
