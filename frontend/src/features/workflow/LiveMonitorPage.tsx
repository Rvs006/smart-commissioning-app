import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";
import { getHealth, getValidationIssues, getValidationRun, type RunRecord } from "../../api/client";
import { formatRelativeTime, humanizeStage, toHealthState } from "./runFormat";

const POLL_MS = 30_000;
const MAX_SAMPLES = 40;

type Sample = Readonly<{
  capturedAt: string;
  progress: number;
  stage: string;
  updatedAt: string;
}>;

function isTerminal(status: RunRecord["status"]): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

export function LiveMonitorPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const runId = searchParams.get("runId")?.trim() ?? "";
  const [draftRunId, setDraftRunId] = useState(runId);
  const [samples, setSamples] = useState<readonly Sample[]>([]);

  useEffect(() => {
    setDraftRunId(runId);
    setSamples([]);
  }, [runId]);

  const runQuery = useQuery({
    enabled: Boolean(runId),
    queryFn: ({ signal }) => getValidationRun(runId, { signal }),
    queryKey: ["live-monitor", "run", runId],
    refetchInterval: (query) =>
      isTerminal(query.state.data?.status ?? "succeeded") ? false : POLL_MS,
  });
  const issuesQuery = useQuery({
    enabled: Boolean(runId),
    queryFn: ({ signal }) => getValidationIssues(runId, { signal }),
    queryKey: ["live-monitor", "issues", runId],
    refetchInterval: runQuery.data && !isTerminal(runQuery.data.status) ? POLL_MS : false,
  });
  const healthQuery = useQuery({
    queryFn: ({ signal }) => getHealth({ signal }),
    queryKey: ["live-monitor", "health"],
    refetchInterval: POLL_MS,
  });

  useEffect(() => {
    const run = runQuery.data;
    if (!run) return;
    setSamples((previous) => {
      const next: Sample = {
        capturedAt: new Date().toISOString(),
        progress: run.progress_percent,
        stage: run.stage,
        updatedAt: run.updated_at,
      };
      return [...previous, next].slice(-MAX_SAMPLES);
    });
  }, [runQuery.data]);

  const points = useMemo(() => {
    if (samples.length === 0) return "";
    return samples
      .map((sample, index) => {
        const x = samples.length === 1 ? 0 : (index / (samples.length - 1)) * 100;
        return `${x},${100 - sample.progress}`;
      })
      .join(" ");
  }, [samples]);
  const run = runQuery.data;

  return (
    <div className="app-page dashboard-page live-monitor-page">
      <section className="home-overview">
        <div className="overview-copy">
          <span className="eyebrow">Read-only monitoring</span>
          <h2>Live run monitor</h2>
          <p>Polls the validation API every 30 seconds while the selected run is active.</p>
        </div>
        <div className="readiness-panel" aria-label="API status">
          <div className="readiness-row">
            <span>API status</span>
            <strong>{healthQuery.data?.status ?? (healthQuery.isLoading ? "…" : "offline")}</strong>
          </div>
          <small>
            Last checked{" "}
            {healthQuery.data ? formatRelativeTime(healthQuery.data.timestamp) : "not yet"}
          </small>
        </div>
      </section>

      <form
        className="surface live-monitor-form"
        onSubmit={(event) => {
          event.preventDefault();
          const next = draftRunId.trim();
          setSearchParams(next ? { runId: next } : {});
        }}
      >
        <label>
          Validation run ID
          <input onChange={(event) => setDraftRunId(event.target.value)} value={draftRunId} />
        </label>
        <button className="secondary-button" type="submit">
          Monitor run
        </button>
        <Link className="link-button" to="/udmi-validation">
          Open UDMI Workbench
        </Link>
      </form>

      {!runId ? (
        <div className="empty-workspace">
          <strong>No run selected</strong>
          <span>Paste the run ID shown by the UDMI Workbench to begin a read-only live view.</span>
        </div>
      ) : runQuery.isError ? (
        <div className="state-panel error" role="alert">
          <strong>Could not load this validation run</strong>
          <span>
            {runQuery.error instanceof Error ? runQuery.error.message : "Request failed."}
          </span>
        </div>
      ) : run ? (
        <>
          <section className="kpi-strip compact-kpis" aria-label="Current run metrics">
            <article>
              <span>Status</span>
              <strong className={`status-token ${toHealthState(run.status)}`}>{run.status}</strong>
            </article>
            <article>
              <span>Stage</span>
              <strong>{humanizeStage(run.stage) || "Waiting"}</strong>
            </article>
            <article>
              <span>Progress</span>
              <strong>{run.progress_percent}%</strong>
            </article>
            <article className={issuesQuery.data?.issues.length ? "danger" : undefined}>
              <span>Issues</span>
              <strong>{issuesQuery.data?.issues.length ?? "…"}</strong>
            </article>
          </section>

          <section className="app-grid two-col home-main-grid">
            <article className="surface">
              <div className="surface-heading">
                <div>
                  <span className="eyebrow">Timeline</span>
                  <h3>Progress snapshots</h3>
                </div>
              </div>
              {samples.length > 1 ? (
                <svg
                  aria-label="Run progress over recent 30-second checks"
                  className="live-progress-chart"
                  role="img"
                  viewBox="0 0 100 100"
                  preserveAspectRatio="none"
                >
                  <line x1="0" x2="100" y1="0" y2="0" />
                  <line x1="0" x2="100" y1="50" y2="50" />
                  <line x1="0" x2="100" y1="100" y2="100" />
                  <polyline fill="none" points={points} />
                </svg>
              ) : (
                <div className="empty-workspace">
                  <strong>Collecting baseline</strong>
                  <span>The trend appears after the next 30-second check.</span>
                </div>
              )}
              <small>
                {samples.length} retained snapshot{samples.length === 1 ? "" : "s"}; last run update{" "}
                {formatRelativeTime(run.updated_at)}.
              </small>
            </article>
            <article className="surface">
              <div className="surface-heading">
                <div>
                  <span className="eyebrow">Evidence</span>
                  <h3>Current validation state</h3>
                </div>
              </div>
              <dl className="summary-grid">
                <div>
                  <dt>Run ID</dt>
                  <dd>{run.run_id}</dd>
                </div>
                <div>
                  <dt>Created</dt>
                  <dd>{new Date(run.created_at).toLocaleString()}</dd>
                </div>
                <div>
                  <dt>Updated</dt>
                  <dd>{new Date(run.updated_at).toLocaleString()}</dd>
                </div>
                <div>
                  <dt>Terminal</dt>
                  <dd>{isTerminal(run.status) ? "Yes" : "No"}</dd>
                </div>
              </dl>
              {issuesQuery.isError && (
                <div className="state-panel error">
                  <strong>Issues unavailable</strong>
                  <span>The run is still monitored; issue details could not be read.</span>
                </div>
              )}
            </article>
          </section>
        </>
      ) : (
        <div className="empty-workspace">
          <strong>Loading run…</strong>
          <span>Connecting to the read-only validation endpoint.</span>
        </div>
      )}
    </div>
  );
}
