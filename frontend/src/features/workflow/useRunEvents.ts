import { useEffect, useRef, useState } from "react";
import { streamRunEvents, type RunEvent, type SessionBoundApiClient } from "../../api/client";
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
};

const INITIAL_STATE: RunEventsState = {
  event: null,
  runRef: null,
  reachedTerminal: false,
  sseActive: true,
};

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

  useEffect(() => {
    if (!enabled || !runId) {
      activeRunRef.current = null;
      setState(INITIAL_STATE);
      return;
    }

    activeRunRef.current = runId;
    setState({ ...INITIAL_STATE, runRef });

    const dispose = streamRunEvents(runId, {
      onClose: (reachedTerminal) => {
        if (activeRunRef.current !== runId) {
          return;
        }
        // If the stream closed without ever reaching a terminal status (e.g.
        // the 10-minute cap, a network drop, or a proxy timeout), hand back to
        // polling so progress keeps updating.
        if (!reachedTerminal) {
          setState((current) => ({ ...current, sseActive: false }));
        }
      },
      onError: () => {
        if (activeRunRef.current !== runId) {
          return;
        }
        // SSE failed/unsupported: disable it so the caller polls instead.
        setState((current) => ({ ...current, sseActive: false }));
      },
      onEvent: (event, name) => {
        if (activeRunRef.current !== runId || event.run_id !== runId) {
          return;
        }
        setState((current) => ({
          event,
          runRef,
          reachedTerminal: current.reachedTerminal || name === "terminal",
          sseActive: true,
        }));
      },
    }, apiClient ? { client: apiClient } : undefined);

    return () => {
      activeRunRef.current = null;
      dispose();
    };
  }, [apiClient, enabled, runId, runRef]);

  return state;
}
