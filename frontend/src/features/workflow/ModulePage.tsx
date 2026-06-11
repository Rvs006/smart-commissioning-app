import { ChangeEvent, useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  createImport,
  createReport,
  getValidationIssues,
  getValidationRun,
  getImportErrors,
  getImportTemplateUrl,
  getReportDownloadUrl,
  ImportBatchSummary,
  ImportErrorReport,
  ImportProfileSummary,
  ImportType,
  JobStatus,
  listImportProfiles,
  startMqttConfigPublishRun,
  startDiscoveryRun,
  startValidationRun,
  ReportSummary,
  ValidationIssueRecord,
} from "../../api/client";
import { getModuleByRoute } from "./moduleData";
import { moduleWorkspaces, type IssueRow } from "./operatorData";

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

const terminalStatuses: JobStatus[] = ["succeeded", "failed", "cancelled"];
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
  const module = getModuleByRoute(moduleRoute);
  const workspace = moduleWorkspaces[moduleRoute];
  const [selectedImportType, setSelectedImportType] = useState<ImportType | "">("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [importOutcome, setImportOutcome] = useState<ImportOutcome | null>(null);
  const [runOutcome, setRunOutcome] = useState<string | null>(null);
  const [lastReport, setLastReport] = useState<ReportSummary | null>(null);
  const [activeUdmiRunId, setActiveUdmiRunId] = useState<string | null>(null);
  const [copyFeedback, setCopyFeedback] = useState<CopyFeedback | null>(null);
  const [publishTopic, setPublishTopic] = useState("334os/b1/ahu-1000001/config");
  const [publishPayload, setPublishPayload] = useState(
    '{"pointset":{"points":{"supply_air_temperature_setpoint":{"set_value":22}}}}',
  );
  const [publishPoint, setPublishPoint] = useState("supply_air_temperature_setpoint");
  const [publishValue, setPublishValue] = useState("22");
  const [publishConfirmed, setPublishConfirmed] = useState(false);
  const [publishUseLiveBroker, setPublishUseLiveBroker] = useState(false);
  const [publishPointsetTopic, setPublishPointsetTopic] = useState("334os/b1/ahu-1000001/events/pointset");
  const [publishWaitSeconds, setPublishWaitSeconds] = useState("5");
  const [scanPorts, setScanPorts] = useState<ScanPort[]>(defaultScanPorts);
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

  const profilesQuery = useQuery({
    queryFn: listImportProfiles,
    queryKey: ["import-profiles"],
  });

  const validationRunQuery = useQuery({
    enabled: Boolean(activeUdmiRunId),
    queryFn: () => getValidationRun(activeUdmiRunId ?? ""),
    queryKey: ["validation-run", activeUdmiRunId],
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && terminalStatuses.includes(status) ? false : 1500;
    },
  });

  const validationIssuesQuery = useQuery({
    enabled:
      Boolean(activeUdmiRunId) &&
      Boolean(validationRunQuery.data?.status) &&
      terminalStatuses.includes(validationRunQuery.data?.status as JobStatus),
    queryFn: () => getValidationIssues(activeUdmiRunId ?? ""),
    queryKey: ["validation-issues", activeUdmiRunId],
  });

  useEffect(() => {
    setSelectedImportType(module.importTypes[0] ?? "");
    setSelectedFile(null);
    setImportOutcome(null);
    setRunOutcome(null);
    setLastReport(null);
    setActiveUdmiRunId(null);
    setCopyFeedback(null);
    setSelectedResultIndex(0);
  }, [module.route, module.importTypes]);

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
        return startDiscoveryRun({
          jobType: action.jobType,
          parameters:
            action.runKind === "ip"
              ? { port_specification: scanPortSpecification(scanPorts) }
              : undefined,
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
        if (action?.kind === "validation" && action.runKind === "udmi") {
          setActiveUdmiRunId(result.run_id);
        }
      } else {
        setLastReport(result);
        setRunOutcome(`Report queued. Report ID: ${result.report_id}, file: ${result.file_name}`);
      }
    },
  });

  const publishMutation = useMutation({
    mutationFn: () =>
      startMqttConfigPublishRun({
        confirmed: publishConfirmed,
        expectedPoint: publishPoint,
        expectedValue: parsePublishValue(publishValue),
        payload: publishPayload,
        pointsetTopic: publishPointsetTopic,
        topic: publishTopic,
        useLiveBroker: publishUseLiveBroker,
        waitSeconds: Number(publishWaitSeconds) || 5,
      }),
    onSuccess: (result) => {
      setRunOutcome(`${result.message} Run ID: ${result.run_id}`);
      setActiveUdmiRunId(result.run_id);
    },
  });

  const availableProfiles =
    profilesQuery.data?.filter((profile) => module.importTypes.includes(profile.import_type)) ?? [];

  const selectedProfile = availableProfiles.find(
    (profile) => profile.import_type === selectedImportType,
  );
  const liveIssues =
    module.route === "udmi-validation" && activeUdmiRunId && validationIssuesQuery.data
      ? validationIssuesQuery.data.issues.map(toIssueRow)
      : null;
  const visibleIssues = liveIssues ?? workspace?.issues ?? [];
  const validationRun = validationRunQuery.data;
  const validationStatusClass = validationRun ? toStatusClass(validationRun.status) : "queued";
  const resultRows = workspace?.rows ?? [];
  const selectedResult = resultRows[selectedResultIndex] ?? resultRows[0] ?? null;
  const resultDetails = selectedResult ? buildResultDetailItems(module.route, selectedResult) : [];

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

  return (
    <div className="app-page">
      <section className="module-hero">
        <div>
          <span className="eyebrow">{module.backendService}</span>
          <h2>{workspace?.title ?? module.title}</h2>
          <p>{workspace?.headline ?? module.summary}</p>
        </div>
        <div className="module-metrics">
          <article>
            <strong>{workspace?.primaryMetric ?? "Ready"}</strong>
            <span>{workspace?.primaryMetricLabel ?? module.integrationStatus}</span>
          </article>
          <article>
            <strong>{workspace?.secondaryMetric ?? module.importTypes.length}</strong>
            <span>{workspace?.secondaryMetricLabel ?? "accepted inputs"}</span>
          </article>
        </div>
      </section>

      <section className="app-grid two-col">
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
                disabled={!selectedFile || !selectedImportType || importMutation.isPending}
                onClick={handleImport}
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
                    <a
                      className="secondary-button compact"
                      download
                      href={getImportTemplateUrl(selectedImportType, "xlsx")}
                    >
                      Download XLSX
                    </a>
                    <a
                      className="secondary-button compact"
                      download
                      href={getImportTemplateUrl(selectedImportType, "csv")}
                    >
                      Download CSV
                    </a>
                  </div>
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
          <div className="run-list">
            {module.runActions.length > 0 ? (
              module.runActions.map((action, index) => (
                <div className="run-card" key={action.label}>
                  <div>
                    <strong>{action.label}</strong>
                    <span>{action.helper}</span>
                  </div>
                  <button
                    className="secondary-button compact"
                    disabled={runMutation.isPending}
                    onClick={() => runMutation.mutate(index)}
                    type="button"
                  >
                    {runMutation.isPending ? "Queueing..." : "Queue"}
                  </button>
                </div>
              ))
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
                <a className="secondary-button compact inline-link-button" download href={getReportDownloadUrl(lastReport.report_id)}>
                  Download {lastReport.output_format.toUpperCase()}
                </a>
              )}
            </div>
          )}

          {copyFeedback && (
            <div className={`state-panel ${copyFeedback.severity}`}>
              <strong>Payload copy</strong>
              <span>{copyFeedback.message}</span>
            </div>
          )}

          {activeUdmiRunId && (
            <div className="state-panel run-monitor">
              <div className="run-monitor-heading">
                <div>
                  <strong>UDMI run monitor</strong>
                  <span>{activeUdmiRunId}</span>
                </div>
                <span className={`status-token ${validationStatusClass}`}>
                  {validationRun?.status ?? "queued"}
                </span>
              </div>

              <div className="progress-track">
                <div style={{ width: `${validationRun?.progress_percent ?? 0}%` }} />
              </div>

              <dl className="summary-grid">
                <div>
                  <dt>Stage</dt>
                  <dd>{validationRun?.stage?.replace(/_/g, " ") ?? "Waiting for first update"}</dd>
                </div>
                <div>
                  <dt>Expected</dt>
                  <dd>{formatSummaryValue(validationRun?.result_summary.expected_devices)}</dd>
                </div>
                <div>
                  <dt>Publishing</dt>
                  <dd>{formatSummaryValue(validationRun?.result_summary.publishing_seen)}</dd>
                </div>
                <div>
                  <dt>Issues</dt>
                  <dd>{formatSummaryValue(validationRun?.result_summary.issue_count)}</dd>
                </div>
              </dl>

              {validationRun?.error_message && (
                <span className="error-text">{validationRun.error_message}</span>
              )}
            </div>
          )}
        </article>
      </section>

      {module.route === "data-validation" && (
        <section className="surface">
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
        <section className="surface">
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

      {module.route === "udmi-validation" && (
        <section className="surface">
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
        </section>
      )}

      {module.route === "udmi-validation" && (
        <section className="surface">
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
              Expected point
              <input onChange={(event) => setPublishPoint(event.target.value)} value={publishPoint} />
            </label>
            <label>
              Expected next present_value
              <input onChange={(event) => setPublishValue(event.target.value)} value={publishValue} />
            </label>
            <label className="publish-payload">
              Payload JSON
              <textarea onChange={(event) => setPublishPayload(event.target.value)} rows={6} value={publishPayload} />
            </label>
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
            disabled={publishMutation.isPending || !publishConfirmed}
            onClick={() => publishMutation.mutate()}
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

      <section className="app-grid two-col wide-left">
        <article className="surface">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Results</span>
              <h3>{workspace?.tableTitle ?? "Workflow Results"}</h3>
            </div>
            <button className="secondary-button compact" type="button">
              Export
            </button>
          </div>
          <div className="data-table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  {(workspace?.columns ?? []).map((column) => (
                    <th key={column}>{column}</th>
                  ))}
                  {resultRows.length > 0 && <th>Details</th>}
                </tr>
              </thead>
              <tbody>
                {resultRows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {(workspace?.columns ?? []).map((column) => (
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
          </div>
        </article>

        <aside className="surface inspector">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Inspector</span>
              <h3>{module.route === "ip-scanner" ? "Default Template" : "Selected Result Detail"}</h3>
            </div>
          </div>

          {module.route === "ip-scanner" ? (
            <DefaultTemplateInspector selectedImportType={selectedImportType || "ip_register"} selectedProfile={selectedProfile} />
          ) : (
            <>
              <div className="detail-list">
                {resultDetails.map((item) => (
                  <div className="detail-row" key={item.label}>
                    <span>{item.label}</span>
                    <strong>{item.value}</strong>
                  </div>
                ))}
              </div>

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
                    <span>This module has no blocking issue in the current demo data.</span>
                  </div>
                )}
              </div>

              <div className="evidence-list">
                <h4>Evidence outputs</h4>
                {activeUdmiRunId && Boolean(validationRun?.result_summary.source_fixture) && (
                  <span>UDMI fixture summary</span>
                )}
                {(workspace?.evidence ?? []).map((item) => (
                  <span key={item}>{item}</span>
                ))}
              </div>
            </>
          )}
        </aside>
      </section>
    </div>
  );
}

function scanPortSpecification(ports: ScanPort[]): string {
  return ports
    .map((entry) => ({ port: entry.port.trim(), protocol: entry.protocol }))
    .filter((entry) => entry.port)
    .map((entry) => `${entry.port}/${entry.protocol}`)
    .join(", ");
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
      <button className="secondary-button compact" onClick={() => onCopyPayload(row[column], row.Asset ?? "Selected")} type="button">
        Copy payload
      </button>
    );
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

function DefaultTemplateInspector({
  selectedImportType,
  selectedProfile,
}: {
  selectedImportType: ImportType;
  selectedProfile?: ImportProfileSummary;
}) {
  return (
    <div className="template-inspector">
      <p className="section-copy">
        IP Discovery should start from a default register template, not from network-level issue
        evidence. Upload this template after filling in the expected devices for the project.
      </p>
      <div className="inline-actions">
        <a className="primary-button compact" download href={getImportTemplateUrl(selectedImportType, "xlsx")}>
          Download default XLSX
        </a>
        <a className="secondary-button compact" download href={getImportTemplateUrl(selectedImportType, "csv")}>
          Download CSV copy
        </a>
      </div>
      <div className="evidence-list template-fields">
        <h4>Template columns</h4>
        {(selectedProfile?.required_columns ?? []).map((column) => (
          <span key={column}>{column}</span>
        ))}
      </div>
    </div>
  );
}

function buildResultDetailItems(route: string, row: Record<string, string>): DetailItem[] {
  if (route === "bacnet-discovery") {
    return [
      { label: "Device", value: row.Device ?? "Selected BACnet device" },
      { label: "Instance", value: row.Instance ?? "Unknown" },
      { label: "Objects indexed", value: row.Objects ?? "Pending" },
      { label: "Discovery status", value: row.Result ?? "Pending" },
      { label: "Last discovered", value: row["Device Last Discovered"] ?? "Not recorded" },
      {
        label: "Object drilldown",
        value:
          "Show object type, instance, object name, present value, units, reliability, status flags, priority array, and timestamp.",
      },
      {
        label: "Readable grouping",
        value:
          "Group by equipment, then point family; highlight missing, stale, unreliable, and unit-mismatch points first.",
      },
    ];
  }

  if (route === "mqtt-discovery" || route === "udmi-validation") {
    return [
      { label: "Asset", value: row.Asset ?? "Selected MQTT asset" },
      { label: "Topic", value: row.Topic ?? "State, metadata, or pointset topic" },
      { label: "Last payload", value: row["Payload Last Seen"] ?? "Not recorded" },
      { label: "Messages", value: row["Message Count"] ?? "Pending" },
      { label: "Result", value: row.Result ?? "Pending" },
      {
        label: "Live data view",
        value:
          "Show decoded JSON, extracted point names, present values, units, timestamp freshness, and schema warnings together.",
      },
      {
        label: "Operator summary",
        value:
          "Start with plain English status, then allow expanding raw payload evidence only when someone needs to audit it.",
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
          "Show mapped source fields, unit conversion, tolerance, pass/warn/fail state, and the latest timestamps from both protocols.",
      },
      {
        label: "Human display",
        value:
          "Use one row per point with expandable evidence so engineers can scan mismatches before reading raw values.",
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
