import type {
  DiscoveryObservationPage,
  DiscoveryObservationRecord,
  DiscoveryObservationTerminal,
  JobStatus,
  JobSummary,
  JobType,
  ReportFormat,
  ReportType,
  UdmiReportVariant,
  UdmiReportScopeV1,
} from "../../api/client";
import type { RunRef, SessionScopeId, WorkspaceRef } from "../../app/sessionScope";

export type RunControllerPhase = "idle" | "submitting" | "active" | "terminal-sync" | "settled";

export type EvidenceRequirement = "run" | "issues" | "results" | "topics";

export type RunControllerState = Readonly<{
  phase: RunControllerPhase;
  runRef: RunRef | null;
  epoch: number | null;
  requiredEvidence: readonly EvidenceRequirement[];
  completedEvidence: readonly EvidenceRequirement[];
  evidenceError: string | null;
  acknowledgedObservationCursor: number;
  terminalObservationCursor: number | null;
}>;

export const initialRunControllerState: RunControllerState = Object.freeze({
  phase: "idle",
  runRef: null,
  epoch: null,
  requiredEvidence: [],
  completedEvidence: [],
  evidenceError: null,
  acknowledgedObservationCursor: 0,
  terminalObservationCursor: null,
});

export type ObservationResnapshotReason =
  | "run_mismatch"
  | "attempt_mismatch"
  | "cursor_regression"
  | "event_conflict";

export type ObservationEventIdentity = Readonly<{
  cursor: number;
  payloadSha256: string;
}>;

export type RunEpochOwner = Readonly<{
  runId: string;
  epoch: number;
}>;

export type ObservationFoldState = Readonly<{
  owner: RunEpochOwner;
  attempt: number;
  acknowledgedCursor: number;
  latestCursor: number;
  hasMore: boolean;
  terminal: DiscoveryObservationTerminal | null;
  observationsPruned: boolean;
  observationsQuarantined: boolean;
  events: ReadonlyMap<string, ObservationEventIdentity>;
  entities: ReadonlyMap<string, DiscoveryObservationRecord>;
  resnapshotRequired: boolean;
  resnapshotReason: ObservationResnapshotReason | null;
}>;

export function createObservationFoldState(
  owner: RunEpochOwner | string,
  attempt: number,
  acknowledgedCursor = 0,
): ObservationFoldState {
  const normalizedOwner =
    typeof owner === "string" ? { runId: owner, epoch: 0 } : Object.freeze({ ...owner });
  return {
    owner: normalizedOwner,
    attempt,
    acknowledgedCursor,
    latestCursor: acknowledgedCursor,
    hasMore: false,
    terminal: null,
    observationsPruned: false,
    observationsQuarantined: false,
    events: new Map(),
    entities: new Map(),
    resnapshotRequired: false,
    resnapshotReason: null,
  };
}

export function isObservationTerminalSynchronized(state: ObservationFoldState): boolean {
  return state.terminal !== null && state.acknowledgedCursor >= state.terminal.terminal_cursor;
}

const observationNamespace = (runId: string, attempt: number, key: string): string =>
  `${runId}\u0000${attempt}\u0000${key}`;

const observationEntityKey = (
  runId: string,
  attempt: number,
  observation: DiscoveryObservationRecord,
): string =>
  observationNamespace(runId, attempt, `${observation.entity_kind}\u0000${observation.entity_key}`);

const requireObservationResnapshot = (
  state: ObservationFoldState,
  reason: ObservationResnapshotReason,
): ObservationFoldState => ({
  ...state,
  resnapshotRequired: true,
  resnapshotReason: reason,
});

const sameObservationTerminal = (
  first: DiscoveryObservationTerminal | null,
  second: DiscoveryObservationTerminal | null,
): boolean =>
  first === second ||
  (first !== null &&
    second !== null &&
    first.status === second.status &&
    first.terminal_cursor === second.terminal_cursor);

export function foldObservationPage(
  state: ObservationFoldState,
  page: DiscoveryObservationPage,
): ObservationFoldState {
  if (state.resnapshotRequired) {
    return state;
  }
  if (page.run_id !== state.owner.runId) {
    return requireObservationResnapshot(state, "run_mismatch");
  }
  if (page.attempt !== state.attempt) {
    return requireObservationResnapshot(state, "attempt_mismatch");
  }
  if ((page.observations_pruned || page.observations_quarantined) && page.terminal) {
    const terminal = sameObservationTerminal(state.terminal, page.terminal)
      ? state.terminal
      : page.terminal;
    const latestCursor = Math.max(
      state.latestCursor,
      page.latest_cursor,
      page.terminal.terminal_cursor,
    );
    const events =
      state.events.size === 0 ? state.events : new Map<string, ObservationEventIdentity>();
    const entities =
      state.entities.size === 0 ? state.entities : new Map<string, DiscoveryObservationRecord>();
    if (
      state.acknowledgedCursor === page.terminal.terminal_cursor &&
      state.latestCursor === latestCursor &&
      state.hasMore === false &&
      terminal === state.terminal &&
      state.observationsPruned === Boolean(page.observations_pruned) &&
      state.observationsQuarantined === Boolean(page.observations_quarantined) &&
      events === state.events &&
      entities === state.entities
    ) {
      return state;
    }
    return {
      ...state,
      acknowledgedCursor: page.terminal.terminal_cursor,
      latestCursor,
      hasMore: false,
      terminal,
      observationsPruned: Boolean(page.observations_pruned),
      observationsQuarantined: Boolean(page.observations_quarantined),
      events,
      entities,
    };
  }
  if (page.next_cursor < state.acknowledgedCursor) {
    return requireObservationResnapshot(state, "cursor_regression");
  }

  let events: Map<string, ObservationEventIdentity> | null = null;
  let entities: Map<string, DiscoveryObservationRecord> | null = null;

  for (const observation of page.observations) {
    if (observation.run_id !== state.owner.runId) {
      return requireObservationResnapshot(state, "run_mismatch");
    }
    if (observation.attempt !== state.attempt) {
      return requireObservationResnapshot(state, "attempt_mismatch");
    }
    const eventKey = observationNamespace(state.owner.runId, state.attempt, observation.event_key);
    const priorEvent = (events ?? state.events).get(eventKey);
    if (
      (observation.cursor < state.acknowledgedCursor && !priorEvent) ||
      observation.cursor > page.next_cursor
    ) {
      return requireObservationResnapshot(state, "cursor_regression");
    }
    if (priorEvent && priorEvent.payloadSha256 !== observation.payload_sha256) {
      return requireObservationResnapshot(state, "event_conflict");
    }
    if (priorEvent) {
      continue;
    }
    events ??= new Map(state.events);
    events.set(eventKey, {
      cursor: observation.cursor,
      payloadSha256: observation.payload_sha256,
    });
    const entityKey = observationEntityKey(state.owner.runId, state.attempt, observation);
    const current = (entities ?? state.entities).get(entityKey);
    if (!current || observation.entity_version > current.entity_version) {
      entities ??= new Map(state.entities);
      entities.set(entityKey, observation);
    }
  }

  const terminal = page.terminal
    ? sameObservationTerminal(state.terminal, page.terminal)
      ? state.terminal
      : page.terminal
    : state.terminal;
  const latestCursor = Math.max(state.latestCursor, page.latest_cursor, page.next_cursor);
  if (
    state.acknowledgedCursor === page.next_cursor &&
    state.latestCursor === latestCursor &&
    state.hasMore === page.has_more &&
    terminal === state.terminal &&
    events === null &&
    entities === null
  ) {
    return state;
  }
  return {
    ...state,
    acknowledgedCursor: page.next_cursor,
    latestCursor,
    hasMore: page.has_more,
    terminal,
    events: events ?? state.events,
    entities: entities ?? state.entities,
  };
}

export type RunControllerAction =
  | { type: "reset" }
  | { type: "submitting" }
  | { type: "accepted"; runRef: RunRef; epoch: number }
  | { type: "restored"; runRef: RunRef; status: JobStatus; epoch: number }
  | { type: "terminal-observed"; runId: string; epoch: number; terminalCursor?: number | null }
  | { type: "observation-cursor-acknowledged"; runId: string; epoch: number; cursor: number }
  | {
      type: "evidence-succeeded";
      runId: string;
      epoch: number;
      requirements: readonly EvidenceRequirement[];
    }
  | { type: "evidence-failed"; runId: string; epoch: number; error: string };

export function evidenceRequirementsFor(runRef: RunRef): readonly EvidenceRequirement[] {
  if (runRef.family === "validation") {
    return ["run", "issues"];
  }
  // The terminal discovery result is the sealed snapshot. MQTT topic polling
  // is a supplementary live-capture view, and a transient failure there must
  // never hide topics already persisted in that result from the operator.
  return ["run", "results"];
}

function isTerminalSynchronizationComplete(
  requiredEvidence: readonly EvidenceRequirement[],
  completedEvidence: readonly EvidenceRequirement[],
  acknowledgedObservationCursor: number,
  terminalObservationCursor: number | null,
): boolean {
  return (
    requiredEvidence.every((requirement) => completedEvidence.includes(requirement)) &&
    (terminalObservationCursor === null ||
      acknowledgedObservationCursor >= terminalObservationCursor)
  );
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
      epoch: action.epoch,
      requiredEvidence: evidenceRequirementsFor(action.runRef),
      completedEvidence: [],
      evidenceError: null,
      acknowledgedObservationCursor: 0,
      terminalObservationCursor: null,
    };
  }
  if (action.type === "restored") {
    const requiredEvidence = evidenceRequirementsFor(action.runRef);
    return {
      phase: isTerminal(action.status) ? "terminal-sync" : "active",
      runRef: action.runRef,
      epoch: action.epoch,
      requiredEvidence,
      completedEvidence: [],
      evidenceError: null,
      acknowledgedObservationCursor: 0,
      terminalObservationCursor: null,
    };
  }
  if (
    !state.runRef ||
    action.runId !== state.runRef.runId ||
    action.epoch !== state.epoch
  ) {
    return state;
  }
  if (action.type === "terminal-observed") {
    if (state.phase === "settled") {
      if (
        action.terminalCursor === undefined ||
        action.terminalCursor === null ||
        action.terminalCursor <= state.acknowledgedObservationCursor
      ) {
        return state;
      }
    }
    const terminalObservationCursor =
      action.terminalCursor === undefined ? state.terminalObservationCursor : action.terminalCursor;
    return {
      ...state,
      phase: isTerminalSynchronizationComplete(
        state.requiredEvidence,
        state.completedEvidence,
        state.acknowledgedObservationCursor,
        terminalObservationCursor,
      )
        ? "settled"
        : "terminal-sync",
      evidenceError: null,
      terminalObservationCursor,
    };
  }
  if (action.type === "observation-cursor-acknowledged") {
    const acknowledgedObservationCursor = Math.max(
      state.acknowledgedObservationCursor,
      action.cursor,
    );
    const phase =
      state.phase === "terminal-sync" &&
      isTerminalSynchronizationComplete(
        state.requiredEvidence,
        state.completedEvidence,
        acknowledgedObservationCursor,
        state.terminalObservationCursor,
      )
        ? "settled"
        : state.phase;
    if (
      acknowledgedObservationCursor === state.acknowledgedObservationCursor &&
      phase === state.phase
    ) {
      return state;
    }
    return {
      ...state,
      acknowledgedObservationCursor,
      phase,
    };
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
  const completedEvidence = state.requiredEvidence.filter((requirement) =>
    completed.has(requirement),
  );
  return {
    ...state,
    phase: isTerminalSynchronizationComplete(
      state.requiredEvidence,
      completedEvidence,
      state.acknowledgedObservationCursor,
      state.terminalObservationCursor,
    )
      ? "settled"
      : "terminal-sync",
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
  udmiReportVariant?: UdmiReportVariant;
  udmiScope?: UdmiReportScopeV1;
}>;

export function createReportIntent(input: ReportIntent): ReportIntent {
  const udmiScope = input.udmiScope ? freezeUdmiScope(input.udmiScope) : undefined;
  return Object.freeze({
    runId: input.runId,
    reportType: input.reportType,
    format: input.format,
    ...(input.udmiReportVariant ? { udmiReportVariant: input.udmiReportVariant } : {}),
    ...(udmiScope ? { udmiScope } : {}),
  });
}

function freezeUdmiScope(scope: UdmiReportScopeV1): UdmiReportScopeV1 {
  const selectedPayloads = scope.selected_payloads.map((payload) => Object.freeze({ ...payload }));
  const filters = Object.freeze({ ...scope.filters });
  return Object.freeze({
    schema_version: scope.schema_version,
    selected_payloads: Object.freeze(
      selectedPayloads,
    ) as unknown as UdmiReportScopeV1["selected_payloads"],
    unexpected_device_ids: Object.freeze([...scope.unexpected_device_ids]) as unknown as string[],
    filters,
  });
}

const evidenceIdentity = (kind: string, parts: readonly (string | undefined)[]): string =>
  `${kind}\u0000${parts.map((part) => part ?? "").join("\u0000")}`;

/** Stable asset identity, independent of array position and response ordering. */
export function assetIdentity(route: string, row: Readonly<Record<string, string>>): string {
  if (row.__entityKey) {
    return evidenceIdentity("entity", [route, row.__entityKey]);
  }
  return route === "bacnet-scanner" || route === "bacnet-discovery-sct"
    ? evidenceIdentity("asset", [
        route,
        row.Asset,
        row.Device,
        row.Instance,
        row.Object,
        row["Object ID"],
        row.Address,
      ])
    : evidenceIdentity("asset", [
        route,
        row.Asset,
        row["Observed IP"],
        row["IP Address"],
        row.Hostname,
        row.Host,
        row.Address,
      ]);
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
    : route === "mqtt-scanner" || route === "mqtt-discovery-sct"
      ? topicIdentity(row)
      : assetIdentity(route, row);
}

function isDiscoveryJob(jobType: JobType | string): boolean {
  return (
    jobType === "ip_discovery" || jobType === "bacnet_discovery" || jobType === "mqtt_discovery"
  );
}

function isTerminal(status: JobStatus): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}
