import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { createReport, getHealth, listImportProfiles } from "../../api/client";
import { issueRows, projectSummary, runRows, workflowStages } from "./operatorData";

const statusLabels = {
  failed: "Fail",
  queued: "Queued",
  ready: "Ready",
  running: "Running",
  warning: "Warning",
};

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

export function DashboardPage() {
  const healthQuery = useQuery({
    queryFn: getHealth,
    queryKey: ["health"],
    refetchInterval: 15000,
  });
  const profilesQuery = useQuery({
    queryFn: listImportProfiles,
    queryKey: ["import-profiles"],
  });
  const reportMutation = useMutation({
    mutationFn: () => createReport({ reportType: "evidence_pack" }),
  });

  return (
    <div className="app-page dashboard-page">
      <section className="home-overview">
        <div className="overview-copy">
          <span className="eyebrow">{projectSummary.project}</span>
          <h2>{projectSummary.site}</h2>
          <p>Track the current commissioning state and jump into the next validation task.</p>
        </div>

        <div className="readiness-panel" aria-label={`${projectSummary.readiness}% ready`}>
          <div className="readiness-row">
            <span>Readiness</span>
            <strong>{projectSummary.readiness}%</strong>
          </div>
          <div className="progress-track">
            <div style={{ width: `${projectSummary.readiness}%` }} />
          </div>
          <small>
            API {healthQuery.data?.status ?? "offline"} · {profilesQuery.data?.length ?? "..."} import profiles
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
          disabled={reportMutation.isPending}
          onClick={() => reportMutation.mutate()}
          type="button"
        >
          {reportMutation.isPending ? "Queueing..." : "Queue evidence pack"}
        </button>
      </section>

      <section className="kpi-strip compact-kpis">
        <article>
          <span>Total assets</span>
          <strong>{projectSummary.assets}</strong>
        </article>
        <article>
          <span>Online assets</span>
          <strong>{projectSummary.onlineAssets}</strong>
        </article>
        <article className="danger">
          <span>Open issues</span>
          <strong>{projectSummary.openIssues}</strong>
        </article>
      </section>

      <section className="app-grid two-col home-main-grid">
        <article className="surface">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Workflow</span>
              <h3>Current Stage</h3>
            </div>
          </div>
          <div className="workflow-board">
            {workflowStages.slice(0, 4).map((stage) => (
              <div className={`workflow-stage ${stage.state}`} key={stage.name}>
                <div>
                  <strong>{stage.name}</strong>
                  <p>{stage.summary}</p>
                </div>
                <span>{stage.action}</span>
              </div>
            ))}
          </div>
        </article>

        <article className="surface">
          <div className="surface-heading">
            <div>
              <span className="eyebrow">Jobs</span>
              <h3>Recent Runs</h3>
            </div>
          </div>
          <div className="run-list">
            {runRows.slice(0, 2).map((run) => (
              <div className="run-card" key={run.id}>
                <div>
                  <strong>{run.type}</strong>
                  <span>{run.stage}</span>
                </div>
                <div className="progress-block">
                  <span className={`status-token ${run.status}`}>{statusLabels[run.status]}</span>
                  <div className="progress-track">
                    <div style={{ width: `${run.progress}%` }} />
                  </div>
                  <small>{run.updated}</small>
                </div>
              </div>
            ))}
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
            {issueRows.slice(0, 1).map((issue) => (
              <div className={`issue-card ${issue.severity}`} key={issue.id}>
                <div>
                  <span>{issue.id}</span>
                  <strong>{issue.message}</strong>
                  <small>
                    {issue.assetId} · {issue.area}
                  </small>
                </div>
                <em>{issue.owner}</em>
              </div>
            ))}
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
            <strong>{projectSummary.evidencePacks}</strong>
            <span>Evidence packs generated from stored runs.</span>
            <Link className="secondary-button compact" to="/reports">
              Open reports
            </Link>
          </div>
        </article>
      </section>
    </div>
  );
}
