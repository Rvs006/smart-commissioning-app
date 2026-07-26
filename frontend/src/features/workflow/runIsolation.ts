import type {
  JobStatus,
  JobSummary,
  JobType,
  ReportFormat,
  ReportType,
  UdmiReportScopeV1,
} from "../../api/client";
import type { RunRef, SessionScopeId, WorkspaceRef } from "../../app/sessionScope";

export type RunControllerPhase =
  | "idle"
  | "submitting"
  | "active"
  | "terminal-sync"
  | "settled";

export type EvidenceRequirement = "run" | "issues" | "results" | "topics";

export type RunControllerState = Readonly<{
  phase: RunControllerPhase;
  runRef: RunRef | null;
  requiredEvidence: readonly EvidenceRequirement[];
  completedEvidence: readonly EvidenceRequirement[];
  evidenceError: string | null;
}>;

export const initialRunControllerState: RunControllerState = Object.freeze({
  phase: "idle",
  runRef: null,
  requiredEvidence: [],
  completedEvidence: [],
  evidenceError: null,
});

export type RunControllerAction =
  | { type: "reset" }
  | { type: "submitting" }
  | { type: "accepted"; runRef: RunRef }
  | { type: "restored"; runRef: RunRef; status: JobStatus }
  | { type: "terminal-observed"; runId: string }
  | {
      type: "evidence-succeeded";
      runId: string;
      requirements: readonly EvidenceRequirement[];
    }
  | { type: "evidence-failed"; runId: string; error: string };

export function evidenceRequirementsFor(runRef: RunRef): readonly EvidenceRequirement[] {
  if (runRef.family === "validation") {
    return ["run", "issues"];
  }
  return runRef.jobType === "mqtt_discovery"
    ? ["run", "results", "topics"]
    : ["run", "results"];
}

export function runControllerReducer(
  state: RunControllerState,
  action: RunControllerAction,
): RunControllerState {
  if (action.type === "reset") {
    return initialRunControllerState;
  }
  if (action.type === "submitting") {
    return { ...initialRunControllerState, phase: "submitting" };
  }
  if (action.type === "accepted") {
    return {
      phase: "active",
      runRef: action.runRef,
      requiredEvidence: evidenceRequirementsFor(action.runRef),
      completedEvidence: [],
      evidenceError: null,
    };
  }
  if (action.type === "restored") {
    const requiredEvidence = evidenceRequirementsFor(action.runRef);
    return {
      phase: isTerminal(action.status) ? "terminal-sync" : "active",
      runRef: action.runRef,
      requiredEvidence,
      completedEvidence: [],
      evidenceError: null,
    };
  }
  if (!state.runRef || action.runId !== state.runRef.runId) {
    return state;
  }
  if (action.type === "terminal-observed") {
    if (state.phase === "settled") {
      return state;
    }
    return { ...state, phase: "terminal-sync", evidenceError: null };
  }
  if (action.type === "evidence-failed") {
    return { ...state, phase: "terminal-sync", evidenceError: action.error };
  }

  const completed = new Set(state.completedEvidence);
  for (const requirement of action.requirements) {
    if (state.requiredEvidence.includes(requirement)) {
      completed.add(requirement);
    }
  }
  const completedEvidence = state.requiredEvidence.filter((requirement) => completed.has(requirement));
  const settled = state.requiredEvidence.every((requirement) => completed.has(requirement));
  return {
    ...state,
    phase: settled ? "settled" : "terminal-sync",
    completedEvidence,
    evidenceError: null,
  };
}

export function toRunRef(
  sessionScopeId: SessionScopeId,
  workspace: WorkspaceRef,
  module: string,
  run: Pick<JobSummary, "run_id" | "job_type">,
  origin: RunRef["origin"] = "restored",
): RunRef {
  return Object.freeze({
    sessionScopeId,
    workspace,
    module,
    runId: run.run_id,
    family: isDiscoveryJob(run.job_type) ? "discovery" : "validation",
    jobType: run.job_type,
    origin,
  });
}

export function latestAttachableRun(runs: readonly JobSummary[]): JobSummary | null {
  let newestActive: JobSummary | null = null;
  let newestTerminal: JobSummary | null = null;
  for (const candidate of runs) {
    if (!isTerminal(candidate.status)) {
      if (!newestActive || candidate.created_at > newestActive.created_at) {
        newestActive = candidate;
      }
    } else if (!newestTerminal || candidate.created_at > newestTerminal.created_at) {
      newestTerminal = candidate;
    }
  }
  return newestActive ?? newestTerminal;
}

export type ReportIntent = Readonly<{
  runId: string;
  reportType: ReportType;
  format: ReportFormat;
  udmiScope?: UdmiReportScopeV1;
}>;

export function createReportIntent(input: ReportIntent): ReportIntent {
  const udmiScope = input.udmiScope
    ? freezeUdmiScope(input.udmiScope)
    : undefined;
  return Object.freeze({
    runId: input.runId,
    reportType: input.reportType,
    format: input.format,
    ...(udmiScope ? { udmiScope } : {}),
  });
}

function freezeUdmiScope(scope: UdmiReportScopeV1): UdmiReportScopeV1 {
  const selectedPayloads = scope.selected_payloads.map((payload) => Object.freeze({ ...payload }));
  const filters = Object.freeze({ ...scope.filters });
  return Object.freeze({
    schema_version: scope.schema_version,
    selected_payloads: Object.freeze(selectedPayloads) as unknown as UdmiReportScopeV1["selected_payloads"],
    unexpected_device_ids: Object.freeze([...scope.unexpected_device_ids]) as unknown as string[],
    filters,
  });
}

const evidenceIdentity = (kind: string, parts: readonly (string | undefined)[]): string =>
  `${kind}\u0000${parts.map((part) => part ?? "").join("\u0000")}`;

/** Stable asset identity, independent of array position and response ordering. */
export function assetIdentity(
  route: string,
  row: Readonly<Record<string, string>>,
): string {
  return route === "bacnet-discovery"
    ? evidenceIdentity("asset", [route, row.Asset, row.Device, row.Object, row["Object ID"], row.Address])
    : evidenceIdentity("asset", [route, row.Asset, row["IP Address"], row.Host, row.Address]);
}

/** Stable payload identity keeps a selected UDMI observation bound to its source evidence. */
export function payloadIdentity(row: Readonly<Record<string, string>>): string {
  return evidenceIdentity("payload", [
    row.__category,
    row.__unexpectedId || row.Asset,
    row.__payloadType || row.Payload,
  ]);
}

/** Stable topic identity keeps an MQTT selection attached across refreshes. */
export function topicIdentity(row: Readonly<Record<string, string>>): string {
  return evidenceIdentity("topic", [row.Topic]);
}

/** Route-aware evidence identity used by selection and filtering state. */
export function resultIdentity(route: string, row: Readonly<Record<string, string>>): string {
  return route === "udmi-validation"
    ? payloadIdentity(row)
    : route === "mqtt-discovery"
      ? topicIdentity(row)
      : assetIdentity(route, row);
}

function isDiscoveryJob(jobType: JobType | string): boolean {
  return jobType === "ip_discovery" || jobType === "bacnet_discovery" || jobType === "mqtt_discovery";
}

function isTerminal(status: JobStatus): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}
