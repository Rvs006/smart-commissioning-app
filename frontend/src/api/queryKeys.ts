import type { RunRef, SessionScopeId, WorkspaceRef } from "../app/sessionScope";

const workspaceRoot = (scope: SessionScopeId, workspace: WorkspaceRef) =>
  ["session", scope, "workspace", workspace.projectId, workspace.siteId] as const;

const runEvidenceRoot = (scope: SessionScopeId, workspace: WorkspaceRef, run: RunRef | null) =>
  [
    ...workspaceRoot(scope, workspace),
    "run-evidence",
    run?.module,
    run?.family,
    run?.jobType,
    run?.runId,
  ] as const;

/** Central factory for every server-state identity owned by a signed-in session. */
export const queryKeys = {
  session: (scope: SessionScopeId) => ["session", scope] as const,
  workspace: workspaceRoot,
  me: (scope: SessionScopeId) => ["session", scope, "me"] as const,
  health: (scope: SessionScopeId, workspace: WorkspaceRef) =>
    [...workspaceRoot(scope, workspace), "health"] as const,
  configuration: (scope: SessionScopeId, workspace: WorkspaceRef) =>
    [...workspaceRoot(scope, workspace), "configuration"] as const,
  interfaces: (scope: SessionScopeId, workspace: WorkspaceRef) =>
    [...workspaceRoot(scope, workspace), "interfaces"] as const,
  scanAuthorizations: (scope: SessionScopeId, workspace: WorkspaceRef, previewRunId?: string) =>
    [...workspaceRoot(scope, workspace), "scan-authorizations", previewRunId] as const,
  scanAuthorization: (scope: SessionScopeId, workspace: WorkspaceRef, authorizationId: string) =>
    [...workspaceRoot(scope, workspace), "scan-authorization", authorizationId] as const,
  nmapPolicies: (scope: SessionScopeId) => ["session", scope, "nmap", "policies"] as const,
  importProfiles: (scope: SessionScopeId, workspace: WorkspaceRef) =>
    [...workspaceRoot(scope, workspace), "import-profiles"] as const,
  importErrors: (scope: SessionScopeId, workspace: WorkspaceRef, importId: string | undefined) =>
    [...workspaceRoot(scope, workspace), "import-errors", importId] as const,
  latestImport: (scope: SessionScopeId, workspace: WorkspaceRef, importType?: string) =>
    [...workspaceRoot(scope, workspace), "latest-import", importType] as const,
  schemaSets: (scope: SessionScopeId, workspace: WorkspaceRef) =>
    [...workspaceRoot(scope, workspace), "udmi-schema-sets"] as const,
  runs: (scope: SessionScopeId, workspace: WorkspaceRef, variant: string = "all") =>
    [...workspaceRoot(scope, workspace), "runs", variant] as const,
  latestRun: (scope: SessionScopeId, workspace: WorkspaceRef, module: string) =>
    [...workspaceRoot(scope, workspace), "latest-run", module] as const,
  run: (scope: SessionScopeId, workspace: WorkspaceRef, run: RunRef) =>
    [
      ...workspaceRoot(scope, workspace),
      "run",
      run.module,
      run.family,
      run.jobType,
      run.runId,
    ] as const,
  issues: (scope: SessionScopeId, workspace: WorkspaceRef, run: RunRef | null) =>
    [...runEvidenceRoot(scope, workspace, run), "issues"] as const,
  issuesById: (scope: SessionScopeId, workspace: WorkspaceRef, runId?: string | null) =>
    [...workspaceRoot(scope, workspace), "run-evidence", runId, "issues"] as const,
  results: (scope: SessionScopeId, workspace: WorkspaceRef, run: RunRef | null) =>
    [...runEvidenceRoot(scope, workspace, run), "results"] as const,
  topics: (scope: SessionScopeId, workspace: WorkspaceRef, run: RunRef | null) =>
    [...runEvidenceRoot(scope, workspace, run), "topics"] as const,
  reports: (scope: SessionScopeId, workspace: WorkspaceRef) =>
    [...workspaceRoot(scope, workspace), "reports"] as const,
  reportList: (scope: SessionScopeId, workspace: WorkspaceRef) =>
    [...workspaceRoot(scope, workspace), "reports"] as const,
  users: (scope: SessionScopeId, workspace: WorkspaceRef) =>
    [...workspaceRoot(scope, workspace), "users"] as const,
  scopeActivationPreflight: (scope: SessionScopeId, workspace: WorkspaceRef) =>
    [...workspaceRoot(scope, workspace), "scope-activation-preflight"] as const,
  userScopeGrants: (scope: SessionScopeId, workspace: WorkspaceRef, userId: string) =>
    [...workspaceRoot(scope, workspace), "users", userId, "scope-grants"] as const,
  hubRuns: (scope: SessionScopeId, workspace: WorkspaceRef, filters: string) =>
    [...workspaceRoot(scope, workspace), "hub-runs", filters] as const,
};

export const mutationKeys = {
  root: (scope: SessionScopeId) => ["session", scope, "mutation"] as const,
  action: (scope: SessionScopeId, actionId: string) =>
    ["session", scope, "mutation", actionId] as const,
  reports: (scope: SessionScopeId, workspace: WorkspaceRef) =>
    [...queryKeys.reports(scope, workspace), "mutation"] as const,
};
