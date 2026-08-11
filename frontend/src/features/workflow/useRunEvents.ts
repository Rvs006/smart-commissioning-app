import { useEffect, useRef, useState } from "react";
import {
  isRunAccessClosedError,
  isRunProgressEvent,
  streamRunEvents,
  type ProgressiveCounts,
  type RunEvent,
  type SessionBoundApiClient,
} from "../../api/client";
import type { RunRef } from "../../app/sessionScope";

// Live run-progress state surfaced by useRunEvents. `event` is the most recent
// SSE progress frame (null until the first frame arrives). `sseActive` is true
// while the SSE stream is the live source; when it flips false the caller
// should fall back to its existing polling (the 1.5s react-query refetch).
export type RunEventsState = {
  event: RunEvent | null;
  runRef: RunRef | null;
  sseActive: boolean;
  // True once the SSE stream observed a terminal status for this run. The
  // caller can use this to stop polling without waiting for a poll round-trip.
  reachedTerminal: boolean;
  connectionState:
    | "idle"
    | "connecting"
    | "connected"
    | "reconnecting"
    | "unavailable"
    | "closed"
    | "terminal";
  observationAttempt: number | null;
  latestObservationCursor: number | null;
  progressiveCounts: ProgressiveCounts | null;
  lastEventAt: number | null;
};

const INITIAL_STATE: RunEventsState = {
  event: null,
  runRef: null,
  reachedTerminal: false,
  sseActive: true,
  connectionState: "idle",
  observationAttempt: null,
  latestObservationCursor: null,
  progressiveCounts: null,
  lastEventAt: null,
};

const RETRY_DELAYS_MS = [1_000, 2_000, 5_000, 10_000, 15_000] as const;

/**
 * Subscribes to the SSE run-progress stream for an active run and live-updates
 * status/stage/progress. Falls back gracefully: on stream error (or an
 * unsupported environment) `sseActive` becomes false so the caller's existing
 * polling takes over and nothing regresses.
 *
 * The stream is opened only when `enabled` and a `runId` are provided, and is
 * re-opened whenever the run id changes. The disposer aborts the in-flight
 * fetch on unmount / run change, so no stream is leaked.
 */
export function useRunEvents(
  run: RunRef | string | null | undefined,
  enabled: boolean,
  apiClient?: SessionBoundApiClient,
): RunEventsState {
  const runId = typeof run === "string" ? run : run?.runId;
  const runRef = typeof run === "string" ? null : (run ?? null);
  const [state, setState] = useState<RunEventsState>(INITIAL_STATE);
  // Track the run id this state belongs to so a stale async callback from a
  // previous stream cannot write into the current run's state.
  const activeRunRef = useRef<string | null>(null);
  const generationRef = useRef(0);

  useEffect(() => {
    if (!enabled || !runId) {
      activeRunRef.current = null;
      setState(INITIAL_STATE);
      return;
    }

    const activeRunId = runId;
    activeRunRef.current = activeRunId;
    const generation = ++generationRef.current;
    let disposed = false;
    let streamDispose: (() => void) | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryIndex = 0;
    let streamGeneration = 0;
    let terminal = false;
    let closed = false;
    setState({ ...INITIAL_STATE, connectionState: "connecting", runRef });

    const isCurrent = (candidateStream?: number): boolean =>
      !disposed &&
      activeRunRef.current === activeRunId &&
      generationRef.current === generation &&
      (candidateStream === undefined || candidateStream === streamGeneration);

    const clearRetry = () => {
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
    };

    const closeAccess = () => {
      if (!isCurrent()) {
        return;
      }
      closed = true;
      clearRetry();
      streamDispose?.();
      setState((current) => ({
        ...current,
        connectionState: "closed",
        event: null,
        lastEventAt: null,
        latestObservationCursor: null,
        observationAttempt: null,
        progressiveCounts: null,
        reachedTerminal: false,
        sseActive: false,
      }));
    };

    const scheduleRetry = (connectionState: "reconnecting" | "unavailable" = "reconnecting") => {
      if (!isCurrent() || terminal || closed || retryTimer !== null) {
        return;
      }
      setState((current) => ({
        ...current,
        connectionState,
        sseActive: false,
      }));
      const delay = RETRY_DELAYS_MS[Math.min(retryIndex, RETRY_DELAYS_MS.length - 1)] ?? 15_000;
      retryIndex += 1;
      retryTimer = setTimeout(() => {
        retryTimer = null;
        connect();
      }, delay);
    };

    function connect(): void {
      if (!isCurrent() || terminal || closed) {
        return;
      }
      const candidateStream = ++streamGeneration;
      streamDispose?.();
      streamDispose = streamRunEvents(
        activeRunId,
        {
          onClose: (reachedTerminal) => {
            if (!isCurrent(candidateStream)) {
              return;
            }
            if (reachedTerminal || terminal) {
              terminal = true;
              clearRetry();
              setState((current) => ({
                ...current,
                connectionState: "terminal",
                reachedTerminal: true,
              }));
              return;
            }
            if (!closed) {
              scheduleRetry();
            }
          },
          onError: (error) => {
            if (isCurrent(candidateStream)) {
              if (isRunAccessClosedError(error)) {
                closeAccess();
              } else {
                scheduleRetry();
              }
            }
          },
          onEvent: (event, name) => {
            if (!isCurrent(candidateStream) || event.run_id !== activeRunId) {
              return;
            }
            if (name === "closed" && event.status === "closed") {
              closeAccess();
              return;
            }
            if (name === "unavailable" && event.status === "unavailable") {
              scheduleRetry("unavailable");
              return;
            }
            if (!isRunProgressEvent(event)) {
              return;
            }
            const reachedTerminal =
              name === "terminal" ||
              event.status === "succeeded" ||
              event.status === "failed" ||
              event.status === "cancelled";
            if (reachedTerminal) {
              terminal = true;
              clearRetry();
            } else {
              retryIndex = 0;
            }
            const reconnecting = name === "timeout" && !reachedTerminal;
            setState((current) => ({
              ...current,
              event,
              runRef,
              reachedTerminal: current.reachedTerminal || reachedTerminal,
              sseActive: !reconnecting,
              connectionState: reachedTerminal
                ? "terminal"
                : reconnecting
                  ? "reconnecting"
                  : "connected",
              observationAttempt: event.observation_attempt ?? current.observationAttempt,
              latestObservationCursor:
                event.latest_observation_cursor ?? current.latestObservationCursor,
              progressiveCounts: event.progressive_counts ?? current.progressiveCounts,
              lastEventAt: Date.now(),
            }));
            if (reconnecting) {
              scheduleRetry();
            }
          },
        },
        apiClient ? { client: apiClient } : undefined,
      );
    }

    const retryImmediately = () => {
      if (!isCurrent() || terminal || closed || retryTimer === null) {
        return;
      }
      clearRetry();
      connect();
    };
    const retryWhenVisible = () => {
      if (document.visibilityState === "visible") {
        retryImmediately();
      }
    };
    window.addEventListener("online", retryImmediately);
    window.addEventListener("focus", retryWhenVisible);
    document.addEventListener("visibilitychange", retryWhenVisible);
    connect();

    return () => {
      disposed = true;
      if (activeRunRef.current === activeRunId) {
      activeRunRef.current = null;
      }
      clearRetry();
      streamDispose?.();
      window.removeEventListener("online", retryImmediately);
      window.removeEventListener("focus", retryWhenVisible);
      document.removeEventListener("visibilitychange", retryWhenVisible);
    };
  }, [apiClient, enabled, runId, runRef]);

  return state;
}
