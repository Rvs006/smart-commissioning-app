import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router";
import {
  downloadFile,
  getDiscoveryComparison,
  getValidationJsonExportPath,
  listRuns,
  type JobStatus,
  type JobSummary,
  type JobType,
} from "../../api/client";
import {
  formatAbsoluteTime,
  formatDuration,
  humanizeJobType,
  humanizeStage,
  isTerminalStatus,
  statusTokenLabels,
  toHealthState,
} from "./runFormat";
import { useSession } from "../../app/sessionContext";
import { queryKeys } from "../../api/queryKeys";
import { moduleRouteForJobType } from "./moduleData";

// Read-only Run History: the full run list from GET /runs as a sortable table
// with ABSOLUTE Started/Finished timestamps and a derived Duration. Where the
// Dashboard shows only the newest few runs with relative time, this shows every
// run — sortable by start time, filterable by status/type, exportable to CSV.
// Pure read view: no backend, schema, or dependency change.
//
// The list endpoint returns JobSummary rows, which do NOT carry project_id/site_id
// (only the full RunRecord does), so those are not columns. Status and job type
// are the offered filters. /runs is paginated via limit+offset; a generous limit
// covers typical history — if a workspace ever exceeds it, offset paginates
// further (not wired here). Finished/Duration are only shown for terminal runs;
// updated_at is the honest finish surrogate (there is no distinct finished_at).

const RUNS_LIMIT = 200;

const jobTypeOptions: { value: JobType; label: string }[] = [
  { label: "IP discovery", value: "ip_discovery" },
  { label: "BACnet discovery", value: "bacnet_discovery" },
  { label: "MQTT discovery", value: "mqtt_discovery" },
  { label: "UDMI validation", value: "udmi_validation" },
  { label: "MQTT config publish", value: "mqtt_config_publish" },
  { label: "BACnet validation", value: "bacnet_validation" },
  { label: "Mapping validation", value: "mapping_validation" },
  { label: "Report generation", value: "report_generation" },
];

const statusOptions: JobStatus[] = ["queued", "running", "succeeded", "failed", "cancelled"];

type SortDirection = "desc" | "asc";

// Minimal RFC-4180 quoting: wrap a cell only if it contains a comma, quote, or
// newline (toLocaleString timestamps embed a comma), doubling any inner quotes.
function csvCell(value: string): string {
  return /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

// Serialise the currently-visible rows to CSV and save via a transient object-URL
// anchor — the same client-side file-save idiom as downloadConfigurationEnvelope,
// swapped to text/csv. No CSV library: stdlib string join.
function downloadRunsCsv(rows: JobSummary[]): void {
  const header = ["Run", "Type", "Status", "Started", "Finished", "Duration"];
  const lines = rows.map((run) => {
    const terminal = isTerminalStatus(run.status);
    return [
      run.run_id,
      humanizeJobType(run.job_type),
      statusTokenLabels[toHealthState(run.status)],
      formatAbsoluteTime(run.created_at),
      terminal ? formatAbsoluteTime(run.updated_at) : "In progress",
      terminal ? formatDuration(run.created_at, run.updated_at) : "—",
    ]
      .map(csvCell)
      .join(",");
  });
  const csv = [header.join(","), ...lines].join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `run-history-${new Date().toISOString().replace(/[:.]/g, "-")}.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function RunHistoryPage() {
  const { apiClient, sessionScopeId, workspace } = useSession();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [jobType, setJobType] = useState<JobType | "">("");
  const [status, setStatus] = useState<JobStatus | "">("");
  const [sortDir, setSortDir] = useState<SortDirection>("desc");
  const [jsonPendingRunId, setJsonPendingRunId] = useState<string | null>(null);
  const [jsonDownloadError, setJsonDownloadError] = useState<string | null>(null);
  const [baselineRunId, setBaselineRunId] = useState(searchParams.get("baseline") ?? "");
  const [candidateRunId, setCandidateRunId] = useState(searchParams.get("candidate") ?? "");

  // One fetch of the full history; filter + sort + export happen client-side over
  // that array, so the exported rows are exactly what the table shows. Reuse the
  // Hub's terminal-status polling cadence: poll fast while any run is in flight.
  const runsQuery = useQuery({
    queryFn: ({ signal }) =>
      listRuns(
        {
          limit: RUNS_LIMIT,
          projectId: workspace.projectId,
          siteId: workspace.siteId,
        },
        { client: apiClient, signal },
      ),
    queryKey: queryKeys.runs(sessionScopeId, workspace, `history-${RUNS_LIMIT}`),
    refetchInterval: (query) =>
      query.state.data?.runs?.some((run) => !isTerminalStatus(run.status)) ? 2000 : 15000,
  });

  const isFiltered = Boolean(jobType || status);

  const visibleRuns = useMemo(() => {
    const filtered = (runsQuery.data?.runs ?? []).filter(
      (run) => (!jobType || run.job_type === jobType) && (!status || run.status === status),
    );
    const dir = sortDir === "desc" ? -1 : 1;
    // Sort by Started (created_at); default newest-first.
    return [...filtered].sort(
      (a, b) => dir * (Date.parse(a.created_at) - Date.parse(b.created_at)),
    );
  }, [runsQuery.data, jobType, status, sortDir]);

  const comparableRuns = useMemo(
    () =>
      visibleRuns.filter(
        (run) =>
          isTerminalStatus(run.status) &&
          run.status === "succeeded" &&
          [
            "ip_discovery",
            "bacnet_discovery",
            "mqtt_discovery",
            "ip_scanner",
            "bacnet_scanner",
            "mqtt_scanner",
          ].includes(run.job_type),
      ),
    [visibleRuns],
  );

  const compareQuery = useQuery({
    enabled: Boolean(baselineRunId && candidateRunId && baselineRunId !== candidateRunId),
    queryKey: [
      "discovery-comparison",
      sessionScopeId,
      workspace.projectId,
      workspace.siteId,
      baselineRunId,
      candidateRunId,
    ],
    queryFn: ({ signal }) =>
      getDiscoveryComparison(candidateRunId, baselineRunId, {
        client: apiClient,
        signal,
      }),
  });

  const updateComparisonPair = (baseline: string, candidate: string) => {
    setBaselineRunId(baseline);
    setCandidateRunId(candidate);
    const next = new URLSearchParams(searchParams);
    if (baseline) next.set("baseline", baseline);
    else next.delete("baseline");
    if (candidate) next.set("candidate", candidate);
    else next.delete("candidate");
    setSearchParams(next);
  };

  const openComparison = () => {
    if (!compareQuery.data?.compatible) return;
    const jobTypeValue = comparableRuns.find((run) => run.run_id === candidateRunId)?.job_type;
    const route = jobTypeValue && moduleRouteForJobType(jobTypeValue);
    if (!route) return;
    navigate(
      `/${route}?run=${encodeURIComponent(candidateRunId)}&compare=${encodeURIComponent(baselineRunId)}`,
    );
  };

  const downloadValidationJson = async (runId: string) => {
    setJsonPendingRunId(runId);
    setJsonDownloadError(null);
    try {
      const { blob, filename } = await downloadFile(getValidationJsonExportPath(runId), undefined, {
        client: apiClient,
      });
      saveBlob(blob, filename ?? `udmi-validation-${runId}.json`);
    } catch (cause) {
      setJsonDownloadError(cause instanceof Error ? cause.message : "Raw JSON download failed.");
    } finally {
      setJsonPendingRunId(null);
    }
  };

  return (
    <div className="app-page hub-page">
      <section className="module-hero">
        <div>
          <span className="eyebrow">Operate</span>
          <h2>Run history</h2>
          <p>
            Every commissioning run recorded for this workspace, with absolute start and finish
            times and a derived duration. Read-only — sort by start time, filter by status or type,
            and export the visible rows to CSV.
          </p>
        </div>
        <div className="module-metrics">
          <article>
            <strong>{runsQuery.isLoading ? "..." : visibleRuns.length}</strong>
            <span>runs in view</span>
          </article>
          <article>
            <strong>
              {runsQuery.isLoading
                ? "..."
                : visibleRuns.filter((run) => !isTerminalStatus(run.status)).length}
            </strong>
            <span>still active</span>
          </article>
        </div>
      </section>

      <section className="surface">
        <div className="surface-heading">
          <div>
            <span className="eyebrow">Filters</span>
            <h3>Scope the run list</h3>
          </div>
          {isFiltered && (
            <button
              className="secondary-button compact"
              onClick={() => {
                setJobType("");
                setStatus("");
              }}
              type="button"
            >
              Clear filters
            </button>
          )}
        </div>

        <div className="hub-filter-grid">
          <label>
            Job type
            <select
              onChange={(event) => setJobType(event.target.value as JobType | "")}
              value={jobType}
            >
              <option value="">All job types</option>
              {jobTypeOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Status
            <select
              onChange={(event) => setStatus(event.target.value as JobStatus | "")}
              value={status}
            >
              <option value="">All statuses</option>
              {statusOptions.map((option) => (
                <option key={option} value={option}>
                  {statusTokenLabels[toHealthState(option)]}
                </option>
              ))}
            </select>
          </label>
          <label>
            Sort
            <select
              onChange={(event) => setSortDir(event.target.value as SortDirection)}
              value={sortDir}
            >
              <option value="desc">Newest first</option>
              <option value="asc">Oldest first</option>
            </select>
          </label>
        </div>
      </section>

      <section className="surface" aria-label="Sealed run comparison">
        <div className="surface-heading">
          <div>
            <span className="eyebrow">Compare</span>
            <h3>Pair two sealed runs</h3>
          </div>
          <button
            className="secondary-button compact"
            disabled={!compareQuery.data?.compatible}
            onClick={openComparison}
            type="button"
          >
            Open in module
          </button>
        </div>
        <div className="hub-filter-grid">
          <label>
            Baseline
            <select
              aria-label="Comparison baseline"
              onChange={(event) => updateComparisonPair(event.target.value, candidateRunId)}
              value={baselineRunId}
            >
              <option value="">Select baseline</option>
              {comparableRuns.map((run) => (
                <option
                  disabled={run.run_id === candidateRunId}
                  key={run.run_id}
                  value={run.run_id}
                >
                  {run.run_id} · {humanizeJobType(run.job_type)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Candidate
            <select
              aria-label="Comparison candidate"
              onChange={(event) => updateComparisonPair(baselineRunId, event.target.value)}
              value={candidateRunId}
            >
              <option value="">Select candidate</option>
              {comparableRuns.map((run) => (
                <option disabled={run.run_id === baselineRunId} key={run.run_id} value={run.run_id}>
                  {run.run_id} · {humanizeJobType(run.job_type)}
                </option>
              ))}
            </select>
          </label>
        </div>
        {compareQuery.isError && (
          <div className="state-panel error" role="alert">
            <strong>Comparison unavailable</strong>
            <span>
              {compareQuery.error instanceof Error ? compareQuery.error.message : "Request failed."}
            </span>
          </div>
        )}
        {compareQuery.data && !compareQuery.data.compatible && (
          <div className="state-panel" role="status">
            <strong>Runs cannot be compared</strong>
            <span>{compareQuery.data.reason ?? "The sealed runs are incompatible."}</span>
          </div>
        )}
        {compareQuery.data?.compatible && (
          <div className="comparison-summary" aria-live="polite">
            <span>{compareQuery.data.additions.length} additions</span>
            <span>{compareQuery.data.removals.length} removals</span>
            <span>{compareQuery.data.changes.length} changes</span>
          </div>
        )}
      </section>

      <section className="surface">
        <div className="surface-heading">
          <div>
            <span className="eyebrow">Runs</span>
            <h3>Full run history</h3>
          </div>
          <button
            className="secondary-button compact"
            disabled={visibleRuns.length === 0}
            onClick={() => downloadRunsCsv(visibleRuns)}
            type="button"
          >
            Export CSV
          </button>
        </div>

        {jsonDownloadError && (
          <div className="state-panel error" role="alert">
            <strong>Raw JSON download failed</strong>
            <span>{jsonDownloadError}</span>
          </div>
        )}

        <div className="data-table-wrap">
          {runsQuery.isError ? (
            <div className="state-panel error">
              <strong>Could not load runs</strong>
              <span>
                {runsQuery.error instanceof Error ? runsQuery.error.message : "Request failed."}
              </span>
            </div>
          ) : runsQuery.isLoading ? (
            <div className="empty-workspace">
              <strong>Loading runs...</strong>
              <span>Fetching run history.</span>
            </div>
          ) : visibleRuns.length > 0 ? (
            <table className="data-table hub-table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Type</th>
                  <th>Status</th>
                  <th>Started</th>
                  <th>Finished</th>
                  <th>Duration</th>
                  <th>Evidence</th>
                </tr>
              </thead>
              <tbody>
                {visibleRuns.map((run) => {
                  const state = toHealthState(run.status);
                  const terminal = isTerminalStatus(run.status);
                  return (
                    <tr key={run.run_id}>
                      <td>{run.run_id}</td>
                      <td>{humanizeJobType(run.job_type)}</td>
                      <td>
                        <span className={`status-token ${state}`}>{statusTokenLabels[state]}</span>
                      </td>
                      <td>{formatAbsoluteTime(run.created_at)}</td>
                      <td>{terminal ? formatAbsoluteTime(run.updated_at) : "—"}</td>
                      <td>{terminal ? formatDuration(run.created_at, run.updated_at) : "—"}</td>
                      <td>
                        {terminal && run.job_type === "udmi_validation" ? (
                          <div className="run-history-evidence">
                            <button
                              aria-label={`Download raw JSON for ${run.run_id}`}
                              className="secondary-button compact"
                              disabled={jsonPendingRunId !== null}
                              onClick={() => void downloadValidationJson(run.run_id)}
                              type="button"
                            >
                              {jsonPendingRunId === run.run_id ? "Downloading..." : "Raw JSON"}
                            </button>
                            {run.acceptance_eligible === false ? (
                              <small>Cadence: ineligible</small>
                            ) : run.acceptance_eligible === true ? (
                              <small>Cadence: eligible</small>
                            ) : null}
                            {run.termination_reason ? (
                              <small>
                                Ended: {humanizeStage(run.termination_reason ?? undefined)}
                              </small>
                            ) : null}
                          </div>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="empty-workspace">
              <strong>No runs to show</strong>
              <span>
                {isFiltered
                  ? "No runs match the current filters. Widen the scope or clear filters."
                  : "No runs have been recorded yet."}
              </span>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
