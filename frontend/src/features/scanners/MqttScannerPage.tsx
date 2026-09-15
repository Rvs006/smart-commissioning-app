import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  getConfiguration,
  getDiscoveryTopicsXlsxPath,
  getRawEvidenceDownloadPath,
  saveMqttLiveAsRegister,
} from "../../api/client";
import { mutationKeys, queryKeys } from "../../api/queryKeys";
import { ENGINEER_REQUIRED_TOOLTIP, useSession } from "../../app/sessionContext";
import { MqttFocusedDetail } from "../workflow/MqttFocusedDetail";
import { MqttLiveTopicTree } from "../workflow/MqttLiveTopicTree";
import { MqttPublishModal } from "../workflow/MqttPublishModal";
import { captureRowsToCsv, mqttRegisterCompareNote, type CaptureRow } from "../workflow/discoveryRows";
import { triggerBlobDownload, useFileDownload } from "../workflow/fileDownload";
import { useMqttLiveSession } from "../workflow/useMqttLiveSession";
import { ScannerScreen, type SetupCell } from "./ScannerScreen";
import type { ScannerRow } from "./scannerRows";
import { ScannerSidePanel } from "./ScannerSidePanel";
import { useScannerRun, useStoredPanelWidth } from "./useScannerRun";

// The scanner capture lane is bounded at 15 minutes by the sidecar adapter; the
// setup card refuses a longer window rather than letting the adapter clamp it
// silently. Same number and wording as the v0.1.58 module page.
const MQTT_CAPTURE_CAP_SECONDS = 900;
const CAPTURE_UNIT_SECONDS = { hours: 3600, minutes: 60, seconds: 1 } as const;

type CaptureUnit = keyof typeof CAPTURE_UNIT_SECONDS;

/** A persisted attribute rendered as CSV text; an absent value is an empty cell. */
function text(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

/**
 * One captured-topic row as the client-side CSV writes it. Built from the row
 * the TABLE is showing, so the file always matches what the operator filtered
 * down to; the XLSX beside it is the server-rebuilt copy of the whole run.
 */
function captureCsvRow(row: ScannerRow): CaptureRow {
  const a = row.attributes;
  return {
    asset: text(a.asset),
    lastSeen: text(a.last_payload_seen),
    messageCount: text(a.message_count),
    // Compact, unwrapped JSON on ONE line: the same value the table's "Last
    // value" cell shows, not the pretty-printed panel form, so a payload cannot
    // straddle CSV rows.
    payload:
      a.payload_raw_only === true
        ? "non-JSON (not stored)"
        : a.last_payload_value === null || a.last_payload_value === undefined
          ? ""
          : JSON.stringify(a.last_payload_value),
    topic: text(a.topic),
  };
}

/** The phases in which the sidecar's single broker connection is already held. */
const LIVE_HOLDING_PHASES = new Set(["live", "connecting", "reconnecting", "unavailable"]);

export function MqttScannerPage() {
  const {
    apiClient,
    authorizationEnforced,
    canEngineer,
    isLoading: sessionLoading,
    me,
    sessionScopeId,
    workspace: workspaceRef,
  } = useSession();
  const run = useScannerRun("mqtt");
  const queryClient = useQueryClient();

  const [captureTopicFilter, setCaptureTopicFilter] = useState("");
  const [captureSeconds, setCaptureSeconds] = useState("10");
  const [captureUnit, setCaptureUnit] = useState<CaptureUnit>("seconds");
  const [liveSearch, setLiveSearch] = useState("");
  const [liveMatchedOnly, setLiveMatchedOnly] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [publishPrefill, setPublishPrefill] = useState<{ topic: string; payload: string } | null>(
    null,
  );
  // One width for both panels on this page (live focus + captured row). They
  // share a localStorage key, so two independent states would fight over it.
  const [panelWidth, setPanelWidth] = useStoredPanelWidth();
  // The sidecar has no "unfocus" call — focus is server-side session state that
  // only changes when another asset is focused. So Close is a local dismissal
  // rather than a request that would silently do nothing, and it lifts as soon
  // as the operator focuses a different asset.
  const [focusDismissed, setFocusDismissed] = useState(false);
  const archiveDownload = useFileDownload(apiClient);
  const topicsXlsxDownload = useFileDownload(apiClient);

  // Run time is entered in the operator's unit and posted in seconds, exactly as
  // the module page did; a non-numeric value is left alone so the shared builder
  // can reject it with its own message instead of being coerced here.
  const captureSecondsEffective =
    captureSeconds.trim() === "" || !Number.isFinite(Number(captureSeconds))
      ? captureSeconds
      : String(Number(captureSeconds) * CAPTURE_UNIT_SECONDS[captureUnit]);
  const captureOverCap = Number(captureSecondsEffective) > MQTT_CAPTURE_CAP_SECONDS;

  const configurationQuery = useQuery({
    queryFn: ({ signal }) => getConfiguration({ client: apiClient, signal }),
    queryKey: [...queryKeys.workspace(sessionScopeId, workspaceRef), "configuration", "panel-config"],
    staleTime: 30_000,
  });
  const mqttConfig = configurationQuery.data?.mqtt?.values ?? {};
  const brokerHost = (mqttConfig["MQTT Broker FQDN or IP Address"] ?? "").trim();
  const brokerPort = (mqttConfig["Port"] ?? "").trim();
  const brokerTls = (mqttConfig["Use TLS"] ?? "").trim();
  const clientId = (mqttConfig["Client ID"] ?? "").trim();
  const configuredQos = (mqttConfig["QoS"] ?? "").trim();
  // "No broker" is a claim about what Configuration HOLDS, so it may only be made
  // from a read that succeeded. A failed read is a different fact and says so.
  const configurationRead = configurationQuery.isSuccess;
  const brokerConfigured = configurationRead && brokerHost !== "";
  const brokerUnconfigured = configurationRead && brokerHost === "";

  const mqttLive = useMqttLiveSession(
    true,
    {
      workspace: workspaceRef,
      authorized: run.scanAuthorized,
      rootFilter: captureTopicFilter.trim() || undefined,
    },
    apiClient,
  );
  const liveHolding = LIVE_HOLDING_PHASES.has(mqttLive.phase);
  // A 400 from connect when Configuration has no broker. Keyed on the sentence
  // the route returns, which backend/tests/test_mqtt_live_session_api.py pins
  // verbatim (test_connect_without_broker_returns_the_pinned_sentence) so a
  // reworded backend fails there rather than silently here.
  const noBrokerError = Boolean(mqttLive.error?.includes("No MQTT broker is configured"));

  // Live-first (plan section 4.4): open the session on arrival when a broker is
  // configured and nobody else holds it.
  //
  // The guard is spent ONLY by an actual start, or by a manual Start / Stop.
  // Every other condition means "not yet", never "never": burning it on a
  // blocker stranded the page for the rest of the visit, and three of those
  // blockers are ordinary startup states that clear a moment later.
  //  - `me` unresolved: authorizationEnforced fails closed to true while /me is
  //    in flight (session.tsx:133), so scanAuthorized reads false even on a
  //    frictionless portable build, which is the default.
  //  - the consent box not ticked yet, under an enforced build.
  //  - a capture run still in flight; it owns the same broker connection.
  // runRestoreSettled is the one that must be waited on rather than trusted:
  // until the latest-run query answers AND its run is attached, startedRunActive
  // is false because nothing has been ATTACHED, not because nothing is running,
  // and connecting into a live capture 409s.
  const autoStartAttempted = useRef(false);
  const startLive = mqttLive.start;
  // A different project or site is a different broker and a different consent.
  useEffect(() => {
    autoStartAttempted.current = false;
  }, [workspaceRef.projectId, workspaceRef.siteId]);
  useEffect(() => {
    if (
      autoStartAttempted.current ||
      sessionLoading ||
      me === null ||
      !configurationQuery.isFetched ||
      !run.runRestoreSettled ||
      !brokerConfigured ||
      !canEngineer ||
      !run.scanAuthorized ||
      run.startedRunActive ||
      mqttLive.phase !== "no_session"
    ) {
      return;
    }
    autoStartAttempted.current = true;
    void startLive();
  }, [
    brokerConfigured,
    canEngineer,
    configurationQuery.isFetched,
    me,
    mqttLive.phase,
    run.runRestoreSettled,
    run.scanAuthorized,
    run.startedRunActive,
    sessionLoading,
    startLive,
  ]);

  // Save-as-register from the held live session (Track B's route). A sibling of
  // the run-scoped save, never a reuse: there is no run behind it, so its note
  // must not offer a register CSV.
  const saveLiveRegisterMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "mqtt-scanner.save-live-register"),
    mutationFn: (sessionId: string) =>
      saveMqttLiveAsRegister({ context: { client: apiClient }, sessionId }),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.latestImportRoot(sessionScopeId, workspaceRef),
      });
    },
  });
  // The note reports what THIS session saved; a new session (or a stop) makes
  // that claim stale, so it is cleared with the session identity.
  const liveSessionId = mqttLive.session?.session_id ?? null;
  const resetLiveRegisterSave = saveLiveRegisterMutation.reset;
  useEffect(() => {
    resetLiveRegisterSave();
  }, [liveSessionId, resetLiveRegisterSave]);

  const focusedAsset = mqttLive.snapshot?.focused?.asset ?? null;
  useEffect(() => {
    setFocusDismissed(false);
  }, [focusedAsset]);

  const results = run.results;
  const archiveArtifactId =
    typeof results?.result_summary?.raw_evidence_artifact_id === "string"
      ? results.result_summary.raw_evidence_artifact_id
      : null;
  const compareNote = results ? mqttRegisterCompareNote(results) : null;
  const registerAvailable = results?.register_comparison?.register_available === true;

  const unread = configurationQuery.isError ? "could not be read" : "not set";
  const setupCells: SetupCell[] = [
    {
      label: "Broker",
      value: brokerHost || unread,
      sub: brokerHost ? `${brokerPort ? `:${brokerPort}` : ""} · TLS ${brokerTls || "unset"}` : undefined,
    },
    { label: "Client · QoS", value: clientId || unread, sub: configuredQos || undefined },
    { label: "Topic filter", value: captureTopicFilter || "# (every topic)" },
    {
      label: "Capture window",
      value:
        Number(captureSecondsEffective) > 0
          ? `${captureSecondsEffective} s`
          : "60 s (engine default)",
      sub: "latest payload per topic",
    },
  ];

  // Two different things stop a capture, and they must not sound the same. The
  // window being too long is the operator's mistake (red, assertive). The live
  // view holding the one broker connection is the page's normal state under
  // live-first, so it is a neutral status note, as ModulePage's was.
  const startBlocked = captureOverCap
    ? {
        tone: "error" as const,
        reason: "Run time exceeds the 15-minute scanner capture limit — shorten the window.",
      }
    : liveHolding
      ? {
          tone: "status" as const,
          title: "Stop the live view before capturing",
          reason:
            "The live topic tree holds the broker connection. A capture run needs that same connection, so stop the live view above before you record a capture.",
        }
      : null;

  return (
    <ScannerScreen
      actionRowExtra={
        <p className="scanner-live-status" role="status">
          <span
            aria-hidden="true"
            className={`scanner-live-dot${mqttLive.phase === "live" ? " on" : ""}`}
          />
          {mqttLive.phase === "live" && mqttLive.snapshot
            ? `Live · connected · ${mqttLive.snapshot.totalTopics} topics`
            : mqttLive.phase === "connecting"
              ? "Live · connecting"
              : mqttLive.phase === "reconnecting" || mqttLive.phase === "unavailable"
                ? "Live · reconnecting"
                : mqttLive.phase === "occupied"
                  ? "Live · held by another session"
                  : "Live view not running"}
        </p>
      }
      afterSetup={
        <section aria-labelledby="mqtt-live-heading" className="scanner-card">
          <div className="scanner-card-head">
            <div className="scanner-results-title">
              <h2 id="mqtt-live-heading">Live topics</h2>
              {mqttLive.phase === "live" && mqttLive.snapshot && (
                <div className="scanner-pills">
                  {/* Plan section 4.4's five KPIs. Broker leads, because a
                      connection that has dropped explains every other number. */}
                  <span
                    className={`scanner-chip${
                      mqttLive.snapshot.status.status === "connected" ? " chip-pass" : " chip-warn"
                    }`}
                  >
                    Broker {mqttLive.snapshot.status.status}
                    {mqttLive.snapshot.status.error ? ` · ${mqttLive.snapshot.status.error}` : ""}
                  </span>
                  <span className="scanner-chip">
                    {mqttLive.snapshot.stats.topicsDiscovered} Topics
                  </span>
                  <span className="scanner-chip">{mqttLive.snapshot.stats.liveAssets} Live assets</span>
                  <span className="scanner-chip">{mqttLive.snapshot.stats.totalMessages} Messages</span>
                  <span
                    className={`scanner-chip${mqttLive.snapshot.stats.issues > 0 ? " chip-fail" : ""}`}
                  >
                    {mqttLive.snapshot.stats.issues} Issues
                  </span>
                </div>
              )}
            </div>
            <div className="inline-actions">
              {liveHolding ? (
                <button
                  className="secondary-button compact"
                  disabled={!canEngineer}
                  onClick={() => {
                    // An explicit Stop settles it: the page must not reconnect
                    // behind the operator the moment the phase goes idle.
                    autoStartAttempted.current = true;
                    void mqttLive.stop();
                  }}
                  title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
                  type="button"
                >
                  Stop live view
                </button>
              ) : (
                <button
                  className="secondary-button compact"
                  disabled={!canEngineer || !run.scanAuthorized}
                  onClick={() => {
                    autoStartAttempted.current = true;
                    void mqttLive.start();
                  }}
                  title={
                    !canEngineer
                      ? ENGINEER_REQUIRED_TOOLTIP
                      : !run.scanAuthorized
                        ? "Tick the scan-authorization checkbox first."
                        : undefined
                  }
                  type="button"
                >
                  Start live view
                </button>
              )}
            </div>
          </div>

          <div className="scanner-card-body form-stack">
            {/* Any refusal, in any phase except the two that already explain
                themselves. Gating this on phase error/unavailable hid the 409
                from a capture run behind a bare "Live view not running". */}
            {mqttLive.error && mqttLive.phase !== "live" && mqttLive.phase !== "occupied" && (
              <div className="state-panel error" role="alert">
                <strong>{noBrokerError ? "No broker configured" : "Live session problem"}</strong>
                <span>{mqttLive.error}</span>
                {noBrokerError && <Link to="/configuration">Open Configuration</Link>}
              </div>
            )}
            {/* The route already said it, with its own link, when it refused. */}
            {brokerUnconfigured && !noBrokerError && (
              <div className="state-panel" role="status">
                <strong>No broker configured</strong>
                <span>
                  Enter the broker FQDN or IP address on the Configuration page and save it; the live
                  view opens by itself once one is set.
                </span>
                <Link to="/configuration">Open Configuration</Link>
              </div>
            )}
            {configurationQuery.isError && (
              <div className="state-panel error" role="alert">
                <strong>The configuration could not be read</strong>
                <span>
                  {configurationQuery.error instanceof Error
                    ? configurationQuery.error.message
                    : "The broker settings could not be loaded, so what is configured is unknown."}
                </span>
              </div>
            )}

            {mqttLive.phase === "occupied" && mqttLive.status?.session ? (
              <div className="state-panel" role="status">
                <strong>A live session is already open</strong>
                <span>Held by {mqttLive.status.session.owner}. Take over to replace it.</span>
                <div className="detail-actions">
                  <button
                    className="secondary-button compact"
                    disabled={!canEngineer || !run.scanAuthorized}
                    onClick={() => void mqttLive.start({ takeOver: true })}
                    title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
                    type="button"
                  >
                    Take over
                  </button>
                </div>
              </div>
            ) : mqttLive.phase === "live" && mqttLive.snapshot ? (
              <>
                <div className="results-filter-bar scanner-filter-bar">
                  <form
                    className="results-filter-text"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void mqttLive.search(liveSearch.trim(), liveMatchedOnly);
                    }}
                  >
                    <label>
                      Search live topics
                      <input
                        onChange={(event) => setLiveSearch(event.target.value)}
                        placeholder="Topic, asset, or payload text — press Enter to filter"
                        value={liveSearch}
                      />
                    </label>
                    <label className="confirm-row">
                      <input
                        checked={liveMatchedOnly}
                        onChange={(event) => {
                          const next = event.target.checked;
                          setLiveMatchedOnly(next);
                          void mqttLive.search(liveSearch.trim(), next);
                        }}
                        type="checkbox"
                      />
                      Registered assets only
                    </label>
                  </form>
                  <div className="inline-actions">
                    <button
                      className="secondary-button compact"
                      disabled={!canEngineer}
                      onClick={() => void mqttLive.subscribe(captureTopicFilter.trim() || "#")}
                      title={
                        canEngineer
                          ? "Re-subscribe the live session to the topic filter set above."
                          : ENGINEER_REQUIRED_TOOLTIP
                      }
                      type="button"
                    >
                      Apply subscription filter
                    </button>
                    <button
                      className="secondary-button compact"
                      disabled={
                        !canEngineer || !mqttLive.session || saveLiveRegisterMutation.isPending
                      }
                      onClick={() => {
                        const sessionId = mqttLive.session?.session_id;
                        if (sessionId) {
                          saveLiveRegisterMutation.mutate(sessionId);
                        }
                      }}
                      title={
                        canEngineer
                          ? "Turn the assets this live session has discovered into an expected-asset MQTT register. It is stored here, applied automatically to the next capture for this project and site, and pushed back so the tree recolours now."
                          : ENGINEER_REQUIRED_TOOLTIP
                      }
                      type="button"
                    >
                      {saveLiveRegisterMutation.isPending ? "Saving register..." : "Save as register"}
                    </button>
                    <button
                      className="secondary-button compact"
                      disabled={!canEngineer || publishOpen}
                      onClick={() => {
                        setPublishPrefill(null);
                        setPublishOpen(true);
                      }}
                      title={
                        canEngineer
                          ? "Publish one message to a topic (confirm step before it reaches live equipment)."
                          : ENGINEER_REQUIRED_TOOLTIP
                      }
                      type="button"
                    >
                      Publish message…
                    </button>
                  </div>
                </div>

                {saveLiveRegisterMutation.isSuccess && saveLiveRegisterMutation.data && (
                  <div className="state-panel success" role="status">
                    <strong>Saved as register</strong>
                    <span>
                      {saveLiveRegisterMutation.data.accepted_rows} of{" "}
                      {saveLiveRegisterMutation.data.total_rows} rows accepted (
                      {saveLiveRegisterMutation.data.import_id}).{" "}
                      {saveLiveRegisterMutation.data.accepted_rows > 0
                        ? "The live tree now compares against it, and so will the next MQTT capture for this project and site."
                        : "No row was accepted, so nothing changed here and the next capture will not use this import."}
                    </span>
                  </div>
                )}
                {saveLiveRegisterMutation.isError && (
                  <div className="state-panel error" role="alert">
                    <strong>Save as register failed</strong>
                    <span>
                      {saveLiveRegisterMutation.error instanceof Error
                        ? saveLiveRegisterMutation.error.message
                        : "The register could not be created."}
                    </span>
                  </div>
                )}

                <div
                  className={`scanner-live-layout${
                    mqttLive.snapshot.focused && !focusDismissed ? " with-panel" : ""
                  }`}
                  style={{ ["--scanner-panel-width" as string]: `${panelWidth}px` }}
                >
                  <MqttLiveTopicTree
                    lastActivity={mqttLive.lastActivity}
                    onFocus={(asset) => {
                      // Re-focusing the SAME asset after a close leaves the
                      // snapshot unchanged, so the effect below never fires:
                      // clearing here is what makes the second click work.
                      setFocusDismissed(false);
                      void mqttLive.focus(asset);
                    }}
                    totalTopics={mqttLive.snapshot.totalTopics}
                    tree={mqttLive.snapshot.tree}
                    treeShown={mqttLive.snapshot.treeShown}
                    variant="rail"
                  />
                  {mqttLive.snapshot.focused && !focusDismissed ? (
                    <ScannerSidePanel
                      headingId="mqtt-focused-heading"
                      onClose={() => setFocusDismissed(true)}
                      onResize={setPanelWidth}
                      title={mqttLive.snapshot.focused.asset}
                      width={panelWidth}
                    >
                      <MqttFocusedDetail
                        canEngineer={canEngineer}
                        focused={mqttLive.snapshot.focused}
                        titled={false}
                        onWriteConfig={(topic, payload) => {
                          // Prefill the publish lane with the device's config
                          // topic and last-seen config payload, retain on.
                          setPublishPrefill({ payload, topic });
                          setPublishOpen(true);
                        }}
                      />
                    </ScannerSidePanel>
                  ) : (
                    <aside
                      aria-labelledby="mqtt-focused-heading"
                      className="scanner-detail empty"
                      role="complementary"
                    >
                      <h3 id="mqtt-focused-heading">Focused asset</h3>
                      <p className="scanner-detail-empty">
                        Select a topic or asset to inspect its live payload and points.
                      </p>
                    </aside>
                  )}
                </div>
              </>
            ) : mqttLive.phase === "connecting" ? (
              <div className="state-panel" role="status">
                <strong>Opening live session…</strong>
                <span>Connecting to the broker through the sidecar.</span>
              </div>
            ) : (
              brokerConfigured && (
                <div className="state-panel" role="status">
                  <strong>Live view not running</strong>
                  <span>
                    Start a live view to watch broker topics as they arrive. Nothing is persisted;
                    record a capture below when you need saved evidence.
                  </span>
                </div>
              )
            )}

            {publishOpen && (
              <MqttPublishModal
                apiClient={apiClient}
                authorizationEnforced={authorizationEnforced}
                defaultPayload={publishPrefill?.payload}
                // Write config opens exactly as the vendored config editor does:
                // QoS 1, retain on. A blank publish from the toolbar keeps QoS 0.
                defaultQos={publishPrefill ? 1 : undefined}
                defaultRetain={publishPrefill ? true : undefined}
                defaultTopic={publishPrefill?.topic}
                onClose={() => {
                  setPublishOpen(false);
                  setPublishPrefill(null);
                }}
                workspace={workspaceRef}
              />
            )}
          </div>
        </section>
      }
      inputs={{
        captureSeconds: captureSecondsEffective,
        captureTopicFilter,
        ignoreRegister: false,
      }}
      onIgnoreRegisterChange={() => {}}
      onPanelWidthChange={setPanelWidth}
      panelWidth={panelWidth}
      purpose="Subscribe, watch the topic tree, capture retained payloads — native mqtt_scanner run."
      resultsActions={(visibleRows) => (
        <>
          {archiveArtifactId && run.activeRun && (
            <button
              className="secondary-button compact"
              disabled={archiveDownload.pendingKey !== null}
              onClick={() =>
                void archiveDownload.download({
                  fallbackFilename: `mqtt-capture-archive-${run.activeRun?.runId}.zip`,
                  key: "mqtt-archive",
                  path: getRawEvidenceDownloadPath(run.activeRun?.runId ?? "", archiveArtifactId),
                })
              }
              title="Download this capture's raw export archive (per-topic payloads + history), attached to the run as evidence."
              type="button"
            >
              {archiveDownload.pendingKey === "mqtt-archive" ? "Downloading..." : "Export archive"}
            </button>
          )}
          {run.activeRun && visibleRows.length > 0 && (
            <button
              className="secondary-button compact"
              onClick={() => {
                const blob = new Blob([captureRowsToCsv(visibleRows.map(captureCsvRow))], {
                  type: "text/csv;charset=utf-8",
                });
                triggerBlobDownload(blob, `mqtt-capture-${run.activeRun?.runId}.csv`);
              }}
              title="Download the rows this table is currently showing as CSV, built here in the browser. Narrowing the filters narrows the file; 'Export topics (XLSX)' beside it is the whole run, rebuilt server-side."
              type="button"
            >
              Export to CSV
            </button>
          )}
          {run.activeRun && (results?.topics?.length ?? 0) > 0 && (
            <button
              className="secondary-button compact"
              disabled={topicsXlsxDownload.pendingKey !== null}
              onClick={() =>
                void topicsXlsxDownload.download({
                  fallbackFilename: `mqtt-capture-${run.activeRun?.runId}.xlsx`,
                  key: "capture-xlsx",
                  // Deliberately NO topic_filter. It used to pass the setup
                  // card's live input, so editing the filter to line up the next
                  // capture silently narrowed (or emptied) the export of the run
                  // still on screen. The capture already subscribed with its own
                  // filter, so the run's persisted topics ARE the whole run;
                  // re-filtering server-side can only ever remove rows the run
                  // really recorded.
                  path: getDiscoveryTopicsXlsxPath(run.activeRun?.runId ?? ""),
                })
              }
              title="Download every topic this run captured as an Excel (XLSX) file, rebuilt server-side. Filters on screen do not change it; 'Export to CSV' beside it is the filtered view."
              type="button"
            >
              {topicsXlsxDownload.pendingKey === "capture-xlsx"
                ? "Exporting..."
                : "Export topics (XLSX)"}
            </button>
          )}
        </>
      )}
      resultsHeading="Captured topics"
      resultsNote={
        <>
          {archiveDownload.error && (
            <div className="state-panel error" role="alert">
              <strong>Capture archive download failed</strong>
              <span>{archiveDownload.error}</span>
            </div>
          )}
          {topicsXlsxDownload.error && (
            <div className="state-panel error" role="alert">
              <strong>Topic export failed</strong>
              <span>{topicsXlsxDownload.error}</span>
            </div>
          )}
          {results?.result_summary?.indefinite_bounded_inline === true && (
            <span className="error-text">
              This run requested an indefinite capture but was bounded to{" "}
              {String(results.result_summary.capture_seconds)}s because no stop control was available
              for it.
            </span>
          )}
          {results?.register_comparison && (
            <div className="sample-banner" role="note">
              {registerAvailable ? (
                <>
                  Green rows match a topic in the uploaded MQTT register; red rows were observed on
                  the broker but are not in the register.
                  {compareNote ? (
                    <>
                      <br />
                      {compareNote}
                    </>
                  ) : null}
                </>
              ) : (
                "No accepted MQTT register import for this project/site — upload one to compare observed topics against the template."
              )}
            </div>
          )}
        </>
      }
      run={run}
      setupCells={setupCells}
      setupFields={
        <>
          <label>
            Topic filter (MQTT wildcards: + and #)
            <input
              onChange={(event) => setCaptureTopicFilter(event.target.value)}
              placeholder="Blank = capture every topic (#)"
              value={captureTopicFilter}
            />
            <small>
              Leave blank to capture every topic (#). Enter a filter with MQTT wildcards (+ and #) to
              narrow the capture. It also scopes the live view when you apply it.
            </small>
          </label>
          <label>
            Run time (blank = 60-second default window)
            <input
              inputMode="numeric"
              onChange={(event) => setCaptureSeconds(event.target.value)}
              placeholder="blank = 60-second default"
              value={captureSeconds}
            />
            <small>Capped at 15 minutes. Stop ends the capture early.</small>
          </label>
          <label>
            Run time unit
            <select
              onChange={(event) => setCaptureUnit(event.target.value as CaptureUnit)}
              value={captureUnit}
            >
              <option value="seconds">seconds</option>
              <option value="minutes">minutes</option>
              <option value="hours">hours</option>
            </select>
          </label>
        </>
      }
      setupHeading="Broker & capture"
      showIgnoreRegister={false}
      startBlockedReason={startBlocked}
      startLabel="Record capture"
    />
  );
}
