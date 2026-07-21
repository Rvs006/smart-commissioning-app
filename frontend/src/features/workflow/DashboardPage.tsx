import { useEffect, useMemo } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  createReport,
  getHealth,
  getValidationIssues,
  listImportProfiles,
  listReports,
  listRuns,
  listValidationRuns,
  type JobSummary,
  type ValidationIssueRecord,
} from "../../api/client";
import { derivePayloadType } from "./operatorData";
import {
  formatRelativeTime,
  humanizeJobType,
  humanizeStage,
  isTerminalStatus,
  statusTokenLabels,
  toHealthState,
} from "./runFormat";
import { useRunEvents } from "./useRunEvents";
import { ENGINEER_REQUIRED_TOOLTIP, useSession } from "../../app/sessionContext";

const briefSteps = [
  {
    body: "Confirm gateway identity, BACnet network, MQTT broker, certificates, time, backups, and logging.",
    label: "Configure",
    number: "1",
    to: "/configuration",
  },
  {
    body: "Find reachable IP devices, BACnet objects, and MQTT topics before deeper validation starts.",
    label: "Discover",
    number: "2",
    to: "/ip-scanner",
  },
  {
    body: "Check UDMI state and pointset payloads, point quality, mappings, tolerances, and issue severity.",
    label: "Validate",
    number: "3",
    to: "/udmi-validation",
  },
  {
    body: "Create repeatable evidence packs and issue reports instead of relying on screenshots.",
    label: "Report",
    number: "4",
    to: "/reports",
  },
];

// Picks the single highest-severity issue across recent terminal validation
// runs to surface as the dashboard's "Blocking Finding". Severity rank mirrors
// the backend ValidationIssueRecord ordering.
const severityRank: Record<string, number> = {
  critical: 4,
  high: 3,
  medium: 2,
  low: 1,
};

function severityClass(severity: string): "critical" | "major" | "minor" {
  if (severity === "critical") {
    return "critical";
  }
  if (severity === "high" || severity === "medium") {
    return "major";
  }
  return "minor";
}

// Reformats a single blocking finding into two readable lines instead of one
// run-on string. Reuses derivePayloadType so the homepage labels payloads
// ("UDMI pointset") the same way the validation module does.
//   headline: asset + payload type, e.g. "MDB5-00-043-BLR-1 · UDMI pointset"
//   detail:   the point problem, e.g. "fault_status — expected STRING but received NUMBER"
// The made-up ISS-#### id is intentionally dropped in favour of this framing.
type ParsedFinding = {
  headline: string;
  detail: string;
};

function parseBlockingFinding(issue: ValidationIssueRecord): ParsedFinding {
  const asset = issue.asset_id?.trim() || "Unknown asset";
  const payloadType = derivePayloadType(issue);
  const headline = payloadType === "other" ? asset : `${asset} · UDMI ${payloadType}`;

  const point = issue.point_name?.trim() || null;
  const expected = issue.expected_value?.trim();
  const observed = issue.observed_value?.trim();

  // A present-but-empty value reads as the explicit word "empty" (ISSUE-10),
  // matching ModulePage's issue detail; an absent value stays "n/a". `undefined`
  // means the field was absent, "" means present-but-blank (after trim).
  const showValue = (value: string | undefined): string =>
    value === undefined ? "n/a" : value === "" ? "empty" : value;

  // Prefer a structured expected/observed comparison; fall back to the
  // human description, then to the issue type humanized. The comparison is used
  // whenever EITHER side is present (including present-but-empty), so an empty
  // value is flagged rather than silently dropping the whole clause.
  let problem: string;
  if (expected !== undefined || observed !== undefined) {
    problem = `expected ${showValue(expected)} but received ${showValue(observed)}`;
  } else if (issue.description?.trim()) {
    problem = issue.description.trim();
  } else {
    problem = issue.issue_type.replace(/_/g, " ");
  }

  const detail = point ? `${point} — ${problem}` : problem;
  return { detail, headline };
}

export function DashboardPage() {
  // Queueing an evidence pack is a report-create (engineer+). A viewer/reviewer
  // sees the button disabled with an explanatory tooltip rather than a 403.
  const { canEngineer } = useSession();
  const healthQuery = useQuery({
    queryFn: getHealth,
    queryKey: ["health"],
    refetchInterval: 15000,
  });
  const profilesQuery = useQuery({
    queryFn: listImportProfiles,
    queryKey: ["import-profiles"],
  });

  // Recent runs (real). Poll fast while any run is still in flight, otherwise
  // settle to the slower 15s cadence so a queued -> succeeded flip shows live.
  const runsQuery = useQuery({
    queryFn: () => listRuns({ limit: 50 }),
    queryKey: ["runs"],
    refetchInterval: (query) =>
      query.state.data?.runs?.some((run) => !isTerminalStatus(run.status)) ? 1500 : 15000,
  });

  // SSE-first monitoring of the newest in-flight run: the stream drives a fast
  // refetch of the runs list when that run goes terminal, so the dashboard
  // reflects completion without waiting on the 1.5s poll. On SSE error the
  // existing polling above is untouched, so nothing regresses.
  const newestActiveRunId = useMemo(() => {
    const all = runsQuery.data?.runs ?? [];
    return all.find((run) => !isTerminalStatus(run.status))?.run_id ?? null;
  }, [runsQuery.data]);

  const runEvents = useRunEvents(newestActiveRunId, Boolean(newestActiveRunId));
  const refetchRuns = runsQuery.refetch;
  useEffect(() => {
    if (runEvents.reachedTerminal) {
      void refetchRuns();
    }
  }, [runEvents.reachedTerminal, refetchRuns]);

  // Reports (real) — used for the evidence-pack count KPI.
  const reportsQuery = useQuery({
    queryFn: listReports,
    queryKey: ["reports"],
    refetchInterval: (query) =>
      query.state.data?.reports?.some((report) => !isTerminalStatus(report.status)) ? 1500 : 15000,
  });

  // Latest terminal validation run, used to derive the open-issues KPI and the
  // single blocking finding. Issues are only fetched once a run is terminal.
  const validationRunsQuery = useQuery({
    queryFn: listValidationRuns,
    queryKey: ["validation-runs"],
    refetchInterval: (query) =>
      query.state.data?.runs?.some((run) => !isTerminalStatus(run.status)) ? 1500 : 15000,
  });

  const latestTerminalValidationRunId = useMemo(() => {
    const runs = validationRunsQuery.data?.runs ?? [];
    const terminal = runs.find((run) => isTerminalStatus(run.status));
    return terminal?.run_id ?? null;
  }, [validationRunsQuery.data]);

  const issuesQuery = useQuery({
    enabled: Boolean(latestTerminalValidationRunId),
    queryFn: () => getValidationIssues(latestTerminalValidationRunId ?? ""),
    queryKey: ["validation-issues", latestTerminalValidationRunId],
  });

  const reportMutation = useMutation({
    mutationFn: () => createReport({ reportType: "evidence_pack" }),
    onSuccess: () => {
      void reportsQuery.refetch();
    },
  });

  const runs = runsQuery.data?.runs ?? [];
  const recentRuns = runs.slice(0, 4);
  const reports = reportsQuery.data?.reports ?? [];

  const issues = useMemo(() => issuesQuery.data?.issues ?? [], [issuesQuery.data]);
  const sortedIssues = useMemo(
    () =>
      [...issues].sort(
        (a, b) => (severityRank[b.severity] ?? 0) - (severityRank[a.severity] ?? 0),
      ),
    [issues],
  );
  const topIssue = sortedIssues[0] ?? null;

  // Real KPIs derived from live data:
  //  - active runs: runs not yet terminal
  //  - open issues: issues in the latest terminal validation run
  //  - evidence packs: report runs (any report counts as a generated pack)
  const activeRuns = runs.filter((run) => !isTerminalStatus(run.status)).length;
  const openIssues = issues.length;
  const evidencePacks = reports.length;

  const runsLoading = runsQuery.isLoading;
  const runsError = runsQuery.isError ? runsQuery.error : null;

  return (
    <div className="app-page dashboard-page">
      <section className="home-overview">
        <div className="overview-copy">
          {/* Project/site display strings have no backend metadata endpoint. */}
          <span className="eyebrow">Smart Commissioning workspace</span>
          <h2>Commissioning console</h2>
          <p>Track the current commissioning state and jump into the next validation task.</p>
        </div>

        <div className="readiness-panel" aria-label="API status">
          <div className="readiness-row">
            <span>API status</span>
            <strong>{healthQuery.data?.status ?? (healthQuery.isLoading ? "..." : "offline")}</strong>
          </div>
          <small>
            {profilesQuery.data?.length ?? "..."} import profiles · {runs.length} recorded run
            {runs.length === 1 ? "" : "s"}
          </small>
        </div>
      </section>

      <section className="home-brief surface" aria-labelledby="home-brief-heading">
        <div className="brief-copy">
          <span className="eyebrow">How this app is meant to be used</span>
          <h3 id="home-brief-heading">Commissioning evidence workspace for site teams</h3>
          <p>
            Configure the job, import registers, discover what is live, validate protocols and payloads,
            then export handover evidence that can survive review.
          </p>
          <div className="brief-note">
            <strong>Use it to answer</strong>
            <span>What should exist, what is live, what is wrong, and what is ready for handover.</span>
          </div>
        </div>

        <div className="brief-route" aria-label="Recommended commissioning workflow">
          {briefSteps.map((step) => (
            <Link className="brief-step" key={step.label} to={step.to}>
              <b>{step.number}</b>
              <span>{step.label}</span>
              <small>{step.body}</small>
            </Link>
          ))}
        </div>
      </section>

      {reportMutation.isSuccess && (
        <div className="state-panel success">
          <strong>Evidence pack queued</strong>
          <span>
            {reportMutation.data.report_id} will create {reportMutation.data.file_name}.
          </span>
        </div>
      )}

      <section className="home-actions">
        <Link className="primary-button" to="/udmi-validation">
          Continue UDMI validation
        </Link>
        <Link className="secondary-button" to="/ip-scanner">
          Review discovery
        </Link>
        <button
          className="secondary-button"
          disabled={reportMutation.isPending || !canEngineer}
          onClick={() => reportMutation.mutate()}
          title={canEngineer ? undefined : ENGINEER_REQUIRED_TOOLTIP}
          type="button"
        >
          {reportMutation.isPending ? "Queueing..." : "Queue evidence pack"}
        </button>
      </section>

      <section className="kpi-strip compact-kpis">
        <article>
          <span>Recorded runs</span>
          <strong>{runsQuery.isLoading ? "..." : runs.length}</strong>
        </article>
        <article>
          <span>Active runs</span>
          <strong>{runsQuery.isLoading ? "..." : activeRuns}</strong>
        </article>
        <article className={openIssues > 0 ? "danger" : undefined}>
          <span>Open issues</span>
          <strong>{issuesQuery.isLoading ? "..." : openIssues}</strong>
        </article>
        <article>
          <span>Evidence packs</span>
          <strong>{reportsQuery.isLoading ? "..." : evidencePacks}</strong>
        </article>
      </section>

      <section className="app-grid two-col home-main-grid">
        <article className="surface">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Runs</span>
              <h3>Recent runs</h3>
            </div>
            <Link className="link-button" to="/run-history">
              View full run history
            </Link>
          </div>
          <div className="run-list">
            {runsError ? (
              <div className="state-panel error">
                <strong>Could not load runs</strong>
                <span>{runsError instanceof Error ? runsError.message : "Request failed."}</span>
              </div>
            ) : runsLoading ? (
              <div className="empty-workspace">
                <strong>Loading runs...</strong>
                <span>Fetching run history.</span>
              </div>
            ) : recentRuns.length > 0 ? (
              recentRuns.map((run) => <RunSummaryCard key={run.run_id} run={run} />)
            ) : (
              <div className="empty-workspace">
                <strong>No runs yet</strong>
                <span>Queue a discovery or validation run to populate this list.</span>
              </div>
            )}
          </div>
        </article>
      </section>

      <section className="app-grid two-col home-support-grid">
        <article className="surface">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Priority</span>
              <h3>Blocking Finding</h3>
            </div>
          </div>
          <div className="issue-list">
            {issuesQuery.isError ? (
              <div className="state-panel error">
                <strong>Could not load issues</strong>
                <span>
                  {issuesQuery.error instanceof Error
                    ? issuesQuery.error.message
                    : "Request failed."}
                </span>
              </div>
            ) : topIssue ? (
              (() => {
                const finding = parseBlockingFinding(topIssue);
                return (
                  <div className={`issue-card blocking-finding ${severityClass(topIssue.severity)}`}>
                    <div className="issue-card-body">
                      <span>{topIssue.severity} severity</span>
                      <strong>{finding.headline}</strong>
                      <small>{finding.detail}</small>
                    </div>
                    <em>Commissioning team</em>
                  </div>
                );
              })()
            ) : (
              <div className="empty-workspace">
                <strong>No blocking findings</strong>
                <span>
                  {latestTerminalValidationRunId
                    ? "The latest validation run reported no issues."
                    : "Run a validation to surface blocking findings here."}
                </span>
              </div>
            )}
          </div>
        </article>

        <article className="surface">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Reports</span>
              <h3>Evidence</h3>
            </div>
          </div>
          <div className="evidence-summary">
            <strong>{reportsQuery.isLoading ? "..." : evidencePacks}</strong>
            <span>
              {evidencePacks === 1 ? "Report" : "Reports"} generated from stored runs.
            </span>
            <Link className="secondary-button compact" to="/reports">
              Open reports
            </Link>
          </div>
        </article>
      </section>
    </div>
  );
}

function RunSummaryCard({ run }: { run: JobSummary }) {
  const state = toHealthState(run.status);
  return (
    <div className="run-card">
      <div>
        <strong>{humanizeJobType(run.job_type)}</strong>
        <span>{humanizeStage(run.stage) || "Awaiting first update"}</span>
      </div>
      <div className="progress-block">
        <span className={`status-token ${state}`}>{statusTokenLabels[state]}</span>
        <div className="progress-track">
          <div style={{ width: `${run.progress_percent}%` }} />
        </div>
        <small>{formatRelativeTime(run.updated_at)}</small>
      </div>
    </div>
  );
}
