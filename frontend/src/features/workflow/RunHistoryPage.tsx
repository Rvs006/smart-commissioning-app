import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { listRuns, type JobStatus, type JobSummary, type JobType } from "../../api/client";
import {
  formatAbsoluteTime,
  formatDuration,
  humanizeJobType,
  isTerminalStatus,
  statusTokenLabels,
  toHealthState,
} from "./runFormat";

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

export function RunHistoryPage() {
  const [jobType, setJobType] = useState<JobType | "">("");
  const [status, setStatus] = useState<JobStatus | "">("");
  const [sortDir, setSortDir] = useState<SortDirection>("desc");

  // One fetch of the full history; filter + sort + export happen client-side over
  // that array, so the exported rows are exactly what the table shows. Reuse the
  // Hub's terminal-status polling cadence: poll fast while any run is in flight.
  const runsQuery = useQuery({
    queryFn: () => listRuns({ limit: RUNS_LIMIT }),
    queryKey: ["run-history", RUNS_LIMIT],
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
    return [...filtered].sort((a, b) => dir * (Date.parse(a.created_at) - Date.parse(b.created_at)));
  }, [runsQuery.data, jobType, status, sortDir]);

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
