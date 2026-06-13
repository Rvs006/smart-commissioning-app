import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { listRuns, type JobStatus, type JobType, type ListRunsParams } from "../../api/client";
import {
  formatRelativeTime,
  humanizeJobType,
  humanizeStage,
  isTerminalStatus,
  statusTokenLabels,
  toHealthState,
} from "./runFormat";

// The hosted-hub operator view: a single cross-project run table backed by the
// real GET /runs endpoint with its server-side filters. Each run summary carries
// edge_id (null for a run created on the local edge, populated for runs ingested
// from another edge), which is surfaced honestly as the run's attribution.
//
// NOTE on project/site columns: the list endpoint returns JobSummary rows, which
// do NOT include project_id/site_id per row (only the full per-run RunRecord
// does). project_id/site_id are therefore exposed as FILTER inputs (passed to
// the backend) and echoed as the active scope, rather than fabricated per row.

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

type HubFilters = {
  projectId: string;
  siteId: string;
  edgeId: string;
  jobType: JobType | "";
  status: JobStatus | "";
};

const INITIAL_FILTERS: HubFilters = {
  edgeId: "",
  jobType: "",
  projectId: "",
  siteId: "",
  status: "",
};

export function HubPage() {
  const [filters, setFilters] = useState<HubFilters>(INITIAL_FILTERS);

  // Only send filters the operator actually set. project_id/site_id default on
  // the backend to demo-project/demo-site, so omitting them keeps the default
  // scope; an explicit value narrows it.
  const params = useMemo<ListRunsParams>(() => {
    const next: ListRunsParams = { limit: 100 };
    if (filters.projectId.trim()) {
      next.projectId = filters.projectId.trim();
    }
    if (filters.siteId.trim()) {
      next.siteId = filters.siteId.trim();
    }
    if (filters.edgeId.trim()) {
      next.edgeId = filters.edgeId.trim();
    }
    if (filters.jobType) {
      next.jobType = filters.jobType;
    }
    if (filters.status) {
      next.status = filters.status;
    }
    return next;
  }, [filters]);

  const runsQuery = useQuery({
    queryFn: () => listRuns(params),
    queryKey: ["hub-runs", params],
    // Reuse the terminal-status polling cadence: poll fast while any listed run
    // is still in flight, otherwise settle to a slower refresh.
    refetchInterval: (query) =>
      query.state.data?.runs?.some((run) => !isTerminalStatus(run.status)) ? 2000 : 15000,
  });

  const runs = runsQuery.data?.runs ?? [];
  const isFiltered =
    Boolean(params.projectId || params.siteId || params.edgeId || params.jobType || params.status);

  const updateFilter = <K extends keyof HubFilters>(key: K, value: HubFilters[K]) => {
    setFilters((current) => ({ ...current, [key]: value }));
  };

  return (
    <div className="app-page hub-page">
      <section className="module-hero">
        <div>
          <span className="eyebrow">Hosted hub</span>
          <h2>Multi-project run activity</h2>
          <p>
            Every discovery, validation, and report run across the connected edges, with honest edge
            attribution. Local runs show no edge; ingested runs carry their originating edge id.
          </p>
        </div>
        <div className="module-metrics">
          <article>
            <strong>{runsQuery.isLoading ? "..." : runs.length}</strong>
            <span>runs in view</span>
          </article>
          <article>
            <strong>
              {runsQuery.isLoading
                ? "..."
                : runs.filter((run) => !isTerminalStatus(run.status)).length}
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
              onClick={() => setFilters(INITIAL_FILTERS)}
              type="button"
            >
              Clear filters
            </button>
          )}
        </div>

        <div className="hub-filter-grid">
          <label>
            Project
            <input
              onChange={(event) => updateFilter("projectId", event.target.value)}
              placeholder="demo-project"
              value={filters.projectId}
            />
          </label>
          <label>
            Site
            <input
              onChange={(event) => updateFilter("siteId", event.target.value)}
              placeholder="demo-site"
              value={filters.siteId}
            />
          </label>
          <label>
            Edge
            <input
              onChange={(event) => updateFilter("edgeId", event.target.value)}
              placeholder="any edge"
              value={filters.edgeId}
            />
          </label>
          <label>
            Job type
            <select
              onChange={(event) => updateFilter("jobType", event.target.value as JobType | "")}
              value={filters.jobType}
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
              onChange={(event) => updateFilter("status", event.target.value as JobStatus | "")}
              value={filters.status}
            >
              <option value="">All statuses</option>
              {statusOptions.map((status) => (
                <option key={status} value={status}>
                  {statusTokenLabels[toHealthState(status)]}
                </option>
              ))}
            </select>
          </label>
        </div>
        <p className="section-copy">
          Scope: project <strong>{params.projectId ?? "demo-project"}</strong>, site{" "}
          <strong>{params.siteId ?? "demo-site"}</strong>
          {params.edgeId ? (
            <>
              , edge <strong>{params.edgeId}</strong>
            </>
          ) : null}
          . Project and site are server-side filters; per-run rows expose edge attribution, job type,
          and status.
        </p>
      </section>

      <section className="surface">
        <div className="surface-heading">
          <div>
            <span className="eyebrow">Runs</span>
            <h3>Cross-project activity</h3>
          </div>
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
              <span>Fetching cross-project activity.</span>
            </div>
          ) : runs.length > 0 ? (
            <table className="data-table hub-table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Job type</th>
                  <th>Edge</th>
                  <th>Status</th>
                  <th>Stage</th>
                  <th>Progress</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const state = toHealthState(run.status);
                  return (
                    <tr key={run.run_id}>
                      <td>{run.run_id}</td>
                      <td>{humanizeJobType(run.job_type)}</td>
                      <td>
                        {run.edge_id ? (
                          <span className="edge-chip">{run.edge_id}</span>
                        ) : (
                          <span className="edge-local">Local edge</span>
                        )}
                      </td>
                      <td>
                        <span className={`status-token ${state}`}>{statusTokenLabels[state]}</span>
                      </td>
                      <td>{humanizeStage(run.stage) || "—"}</td>
                      <td>{run.progress_percent}%</td>
                      <td>{formatRelativeTime(run.updated_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="empty-workspace">
              <strong>No runs match this view</strong>
              <span>
                {isFiltered
                  ? "No runs match the current filters. Widen the scope or clear filters."
                  : "No runs have been recorded for this scope yet."}
              </span>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
