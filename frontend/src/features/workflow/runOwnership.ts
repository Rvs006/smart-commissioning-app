import { useCallback, useEffect, useMemo, useRef } from "react";
import { ApiError, type JobStatus } from "../../api/client";
import type { RunRef, SessionScopeId, WorkspaceRef } from "../../app/sessionScope";
import { isTerminalStatus } from "./runFormat";
import type {
  EvidenceRequirement,
  RunControllerAction,
  RunControllerState,
} from "./runIsolation";

/**
 * Run ownership and the terminal evidence barrier, shared by ModulePage (built-in
 * discovery, UDMI validation, reports) and the native scanner pages.
 *
 * Both screens must answer the same two questions the same way, or a late async
 * callback can write one run's evidence into another run's view:
 *   1. does this callback still belong to the run on screen (epoch + workspace)?
 *   2. has the run's access scope been closed under us (SSE said denied)?
 * They were literal copies until this module; the only per-screen difference is
 * WHICH queries carry the evidence, which is why the barrier takes its refetch
 * and confirmation steps as callbacks.
 */

/**
 * The identity of one submission. `epoch` is monotonic per page, so a re-run of
 * the same run id is still a different owner, and the workspace/session pair
 * keeps a run from a different project or site from ever matching.
 */
export type RunEpochOwner = {
  epoch: number;
  runId: string;
  sessionScopeId: SessionScopeId;
  workspaceRef: WorkspaceRef;
};

/** The read boundary an SSE "closed" frame denies: one module in one workspace. */
export type RunAccessScope = {
  moduleRoute: string;
  sessionScopeId: SessionScopeId;
  workspaceRef: WorkspaceRef;
};

export function sameRunEpochOwner(
  left: RunEpochOwner | null | undefined,
  right: RunEpochOwner | null | undefined,
): boolean {
  return Boolean(
    left &&
      right &&
      left.runId === right.runId &&
      left.epoch === right.epoch &&
      left.sessionScopeId === right.sessionScopeId &&
      left.workspaceRef.projectId === right.workspaceRef.projectId &&
      left.workspaceRef.siteId === right.workspaceRef.siteId,
  );
}

export function sameRunAccessScope(
  left: RunAccessScope | null | undefined,
  right: RunAccessScope | null | undefined,
): boolean {
  return Boolean(
    left &&
      right &&
      left.moduleRoute === right.moduleRoute &&
      left.sessionScopeId === right.sessionScopeId &&
      left.workspaceRef.projectId === right.workspaceRef.projectId &&
      left.workspaceRef.siteId === right.workspaceRef.siteId,
  );
}

/**
 * A run-status refresh that failed for a reason worth retrying: anything that is
 * not an HTTP answer, plus the timeout / rate-limit / server-fault statuses. A
 * definitive 4xx is not retried — it is the server's real answer.
 */
export function isTransientRunStatusError(error: unknown): boolean {
  return (
    !(error instanceof ApiError) ||
    error.status === 408 ||
    error.status === 429 ||
    error.status >= 500
  );
}

/** Back-off between run-status re-reads while the record catches up to SSE. */
export const TERMINAL_RUN_STATUS_RETRY_DELAYS_MS = [300, 600, 1_000] as const;

export type RunOwnershipInput = {
  /** The run on screen. Any object carrying the submission's id and epoch. */
  activeRun: { runId: string; epoch: number } | null;
  moduleRoute: string;
  sessionScopeId: SessionScopeId;
  workspaceRef: WorkspaceRef;
  /** `runEvents.runRef` — the run the SSE hook is streaming, if any. */
  runEventRunRef: RunRef | null | undefined;
  /** `runEvents.connectionState`. "closed" is the access denial we latch. */
  runEventConnectionState: string;
};

export type RunOwnership = {
  activeRunOwner: RunEpochOwner | null;
  /** True when a late callback's owner is still the run on screen AND readable. */
  ownsActiveRun: (owner: RunEpochOwner | null | undefined) => boolean;
  runAccessClosed: boolean;
  /** Clear the latched denial — call from the route/workspace reset effect. */
  resetRunAccessScope: () => void;
  /**
   * Force the owner a callback will be compared against BEFORE the next render
   * lands. ModulePage's reserved-live-submission fencing needs this: it bumps
   * the epoch inside flushSync so evidence from the preview run that shares the
   * live run's id is dropped, and the comparison must move with it immediately.
   */
  overrideActiveRunOwner: (owner: RunEpochOwner | null) => void;
};

export function useRunOwnership({
  activeRun,
  moduleRoute,
  sessionScopeId,
  workspaceRef,
  runEventRunRef,
  runEventConnectionState,
}: RunOwnershipInput): RunOwnership {
  const currentRunAccessScope: RunAccessScope = { moduleRoute, sessionScopeId, workspaceRef };
  const currentRunAccessScopeRef = useRef(currentRunAccessScope);
  currentRunAccessScopeRef.current = currentRunAccessScope;
  const runAccessClosedScopeRef = useRef<RunAccessScope | null>(null);
  const runEventAccessScope: RunAccessScope | null = runEventRunRef
    ? {
        moduleRoute: runEventRunRef.module,
        sessionScopeId: runEventRunRef.sessionScopeId,
        workspaceRef: runEventRunRef.workspace,
      }
    : null;
  // Latched during render, because `runAccessClosed` below is read in the same
  // render that observes the closed stream: a reserved epoch must not be able to
  // make an already-denied workspace readable again.
  if (
    runEventConnectionState === "closed" &&
    sameRunAccessScope(runEventAccessScope, currentRunAccessScope)
  ) {
    runAccessClosedScopeRef.current = runEventAccessScope;
  }
  const runAccessClosed = sameRunAccessScope(
    runAccessClosedScopeRef.current,
    currentRunAccessScope,
  );

  // Memoised because consumers return it from their own useMemo: a fresh object
  // every render would defeat that memo and re-render every child.
  const activeRunOwner: RunEpochOwner | null = useMemo(
    () =>
      activeRun
        ? { epoch: activeRun.epoch, runId: activeRun.runId, sessionScopeId, workspaceRef }
        : null,
    [activeRun, sessionScopeId, workspaceRef],
  );
  const activeRunOwnerRef = useRef<RunEpochOwner | null>(activeRunOwner);
  activeRunOwnerRef.current = activeRunOwner;

  // Reads refs only, so it is stable and always sees the CURRENT run — which is
  // the point: it is called from callbacks that resolve after a run change.
  const ownsActiveRun = useCallback(
    (owner: RunEpochOwner | null | undefined) =>
      !sameRunAccessScope(runAccessClosedScopeRef.current, currentRunAccessScopeRef.current) &&
      sameRunEpochOwner(owner, activeRunOwnerRef.current),
    [],
  );

  const resetRunAccessScope = useCallback(() => {
    runAccessClosedScopeRef.current = null;
  }, []);

  const overrideActiveRunOwner = useCallback((owner: RunEpochOwner | null) => {
    activeRunOwnerRef.current = owner;
  }, []);

  return {
    activeRunOwner,
    overrideActiveRunOwner,
    ownsActiveRun,
    runAccessClosed,
    resetRunAccessScope,
  };
}

/** The shape of a react-query `refetch()` result this module needs. */
type RefetchOutcome = {
  data?: { run_id?: string; status?: JobStatus } | undefined;
  error?: unknown;
  isError: boolean;
};

export type TerminalEvidenceBarrierInput<TRun extends { runId: string; epoch: number }> = {
  activeRun: TRun | null;
  /** Access denied for this workspace: hold the barrier rather than re-read. */
  blocked: boolean;
  dispatchRun: (action: RunControllerAction) => void;
  runController: RunControllerState;
  /** Re-read the run record. */
  refetchRunStatus: (run: TRun) => Promise<RefetchOutcome>;
  /** Re-read the run's evidence and THROW if it does not name this run. */
  confirmEvidence: (run: TRun) => Promise<void>;
  /** Which evidence the settled phase requires for this run. */
  requirementsFor: (run: TRun) => readonly EvidenceRequirement[];
};

/**
 * Holds a terminal run at "terminal-sync" until the run record AND its evidence
 * both name the same run. SSE can report terminal before the record is written,
 * so the status re-read backs off a few times; anything that is not a definitive
 * server answer is retried, a mismatched run id fails the barrier outright.
 */
export function useTerminalEvidenceBarrier<TRun extends { runId: string; epoch: number }>({
  activeRun,
  blocked,
  confirmEvidence,
  dispatchRun,
  refetchRunStatus,
  requirementsFor,
  runController,
}: TerminalEvidenceBarrierInput<TRun>): { resetEvidenceSync: () => void } {
  // The three callbacks are latched in a ref and read through it inside the
  // async body, so they are NOT effect dependencies. Asking every caller to
  // memoise them would have been a trap: an unmemoised callback re-runs the
  // effect mid-flight, the cleanup sets `disposed`, and the re-run then returns
  // early on the `evidenceSyncRef` guard, so the barrier never settles and the
  // run hangs at "terminal-sync" with no error. The sequence always calls the
  // newest callbacks, which is what a caller re-rendering with fresh closures
  // means anyway.
  const callbacksRef = useRef({ confirmEvidence, refetchRunStatus, requirementsFor });
  callbacksRef.current = { confirmEvidence, refetchRunStatus, requirementsFor };
  const evidenceSyncRef = useRef<number | null>(null);
  useEffect(() => {
    evidenceSyncRef.current = null;
  }, [activeRun?.epoch]);

  useEffect(() => {
    const run = activeRun;
    if (
      blocked ||
      !run ||
      runController.phase !== "terminal-sync" ||
      runController.runRef?.runId !== run.runId ||
      runController.epoch !== run.epoch ||
      evidenceSyncRef.current === run.epoch
    ) {
      return;
    }
    evidenceSyncRef.current = run.epoch;
    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let resolveRetry: (() => void) | null = null;

    const waitForRunStatusRetry = (delay: number) =>
      new Promise<void>((resolve) => {
        resolveRetry = resolve;
        retryTimer = setTimeout(() => {
          retryTimer = null;
          resolveRetry = null;
          resolve();
        }, delay);
      });

    void (async () => {
      try {
        let terminalRunConfirmed = false;
        for (let attempt = 0; attempt <= TERMINAL_RUN_STATUS_RETRY_DELAYS_MS.length; attempt += 1) {
          const runResult = await callbacksRef.current.refetchRunStatus(run);
          if (disposed) {
            return;
          }
          if (runResult.data?.run_id && runResult.data.run_id !== run.runId) {
            throw new Error("Final run evidence did not match the active run.");
          }
          if (!runResult.isError && runResult.data?.run_id === run.runId) {
            if (isTerminalStatus(runResult.data.status)) {
              terminalRunConfirmed = true;
              break;
            }
          } else if (!isTransientRunStatusError(runResult.error)) {
            throw runResult.error ?? new Error("Final run status could not be refreshed.");
          }
          const delay = TERMINAL_RUN_STATUS_RETRY_DELAYS_MS[attempt];
          if (delay === undefined) {
            throw runResult.error ?? new Error("Final run status did not reach a terminal state.");
          }
          await waitForRunStatusRetry(delay);
          if (disposed) {
            return;
          }
        }
        if (!terminalRunConfirmed || disposed) {
          return;
        }
        await callbacksRef.current.confirmEvidence(run);
        if (!disposed) {
          dispatchRun({
            type: "evidence-succeeded",
            runId: run.runId,
            epoch: run.epoch,
            requirements: callbacksRef.current.requirementsFor(run),
          });
        }
      } catch (cause) {
        if (!disposed) {
          dispatchRun({
            type: "evidence-failed",
            runId: run.runId,
            epoch: run.epoch,
            error: cause instanceof Error ? cause.message : "Final evidence refresh failed.",
          });
        }
      }
    })();

    return () => {
      disposed = true;
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      resolveRetry?.();
      resolveRetry = null;
    };
  }, [
    activeRun,
    blocked,
    dispatchRun,
    runController.phase,
    runController.epoch,
    runController.runRef?.runId,
  ]);

  // A preview and its authorized live run can share an id in local/test
  // adapters; clearing the marker makes each submission a fresh barrier even
  // when the backend reuses that identifier.
  const resetEvidenceSync = useCallback(() => {
    evidenceSyncRef.current = null;
  }, []);

  return { resetEvidenceSync };
}
