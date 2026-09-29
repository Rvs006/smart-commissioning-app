import { useCallback, useEffect, useRef, useState } from "react";
import {
  connectMqttLiveSession,
  disconnectMqttLiveSession,
  focusMqttLiveAsset,
  getMqttLiveStatus,
  searchMqttLive,
  streamMqttLiveEvents,
  subscribeMqttLive,
  type MqttLiveControlName,
  type MqttLiveFrame,
  type MqttLiveSessionInfo,
  type MqttLiveSnapshot,
  type MqttLiveStatusResponse,
  type SessionBoundApiClient,
} from "../../api/client";
import type { WorkspaceRef } from "../../app/sessionScope";

// The live session's lifecycle phase. Content is always visible in every phase
// (the section never waits on a frame to render): idle/checking show a starting
// state, occupied shows the holder + a take-over, live shows the tree.
export type MqttLivePhase =
  | "idle"
  | "checking"
  | "no_session"
  | "occupied"
  | "connecting"
  | "live"
  | "reconnecting"
  | "unavailable"
  | "ended"
  | "error";

export type MqttLiveSessionState = {
  phase: MqttLivePhase;
  session: MqttLiveSessionInfo | null;
  status: MqttLiveStatusResponse | null;
  snapshot: MqttLiveSnapshot | null;
  lastActivity: { paths: string[]; at: number } | null;
  error: string | null;
  /**
   * Consecutive stream reopens since the last frame arrived. 0 while the stream
   * is delivering. The page reports it so "reconnecting" is a count the operator
   * can watch rather than a spinner that never resolves.
   */
  reconnectAttempts: number;
  start: (opts?: { takeOver?: boolean }) => Promise<void>;
  stop: () => Promise<void>;
  focus: (asset: string) => Promise<void>;
  subscribe: (rootFilter: string, qos?: number) => Promise<void>;
  search: (q: string, matchedOnly?: boolean) => Promise<void>;
  refreshStatus: () => Promise<void>;
};

const RETRY_DELAYS_MS = [1_000, 2_000, 5_000, 10_000, 15_000] as const;

// A sidecar that accepts the session but never delivers a frame used to be
// retried forever, leaving the page saying "reconnecting" with nothing to show
// and no way out. After this many consecutive reopens with no frame, stop and
// report the session as unavailable so the operator gets Stop / Start back.
const RECONNECT_ATTEMPT_CAP = 5;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The live session request failed.";
}

function statusCode(error: unknown): number | null {
  const value = (error as { status?: unknown } | null)?.status;
  return typeof value === "number" ? value : null;
}

/**
 * Drives the single MQTT live broker session: checks occupancy on enable, opens
 * a session (start), streams the topic tree, and stops it (stop). Stream
 * callbacks are guarded by the session generation, so a stale reconnect cannot
 * write into a newer session, and by a per-open stream token, so a replaced or
 * disposed stream (the client still fires onClose after abort) cannot schedule a
 * reconnect or overwrite the snapshot. Unmount aborts the stream WITHOUT
 * disconnecting (a route change or StrictMode remount must not flap the broker;
 * the backend idle reaper reclaims the lease).
 */
export function useMqttLiveSession(
  enabled: boolean,
  input: { workspace: WorkspaceRef; authorized: boolean; rootFilter?: string },
  apiClient?: SessionBoundApiClient,
): MqttLiveSessionState {
  const [state, setState] = useState<{
    phase: MqttLivePhase;
    session: MqttLiveSessionInfo | null;
    status: MqttLiveStatusResponse | null;
    snapshot: MqttLiveSnapshot | null;
    lastActivity: { paths: string[]; at: number } | null;
    error: string | null;
    reconnectAttempts: number;
  }>({
    phase: "idle",
    session: null,
    status: null,
    snapshot: null,
    lastActivity: null,
    error: null,
    reconnectAttempts: 0,
  });

  // Latest inputs, so start/stop/refresh stay stable callbacks.
  const inputRef = useRef(input);
  inputRef.current = input;
  const apiClientRef = useRef(apiClient);
  apiClientRef.current = apiClient;

  const generationRef = useRef(0);
  const streamIdRef = useRef(0);
  const streamDisposeRef = useRef<(() => void) | null>(null);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retryIndexRef = useRef(0);
  const mountedRef = useRef(true);
  // openStream and scheduleReconnect are mutually recursive; refs break the
  // cycle (and the use-before-define lint) without unstable deps.
  const openStreamRef = useRef<(sessionId: string, generation: number) => void>(() => {});
  const scheduleReconnectRef = useRef<
    (sessionId: string, generation: number, phase: "reconnecting" | "unavailable") => void
  >(() => {});

  const context = useCallback(
    () => (apiClientRef.current ? { client: apiClientRef.current } : undefined),
    [],
  );

  const clearRetry = useCallback(() => {
    if (retryTimerRef.current !== null) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  const disposeStream = useCallback(() => {
    streamIdRef.current += 1;
    streamDisposeRef.current?.();
    streamDisposeRef.current = null;
  }, []);

  const refreshStatus = useCallback(async () => {
    try {
      const status = await getMqttLiveStatus({ context: context() });
      if (!mountedRef.current) {
        return;
      }
      setState((current) => {
        // Do not clobber an active local session view with a status poll.
        if (current.phase === "live" || current.phase === "connecting" || current.phase === "reconnecting") {
          return { ...current, status };
        }
        return { ...current, status, phase: status.session ? "occupied" : "no_session" };
      });
    } catch (error) {
      if (mountedRef.current) {
        setState((current) => ({ ...current, error: errorMessage(error) }));
      }
    }
  }, [context]);

  const openStream = useCallback(
    (sessionId: string, generation: number) => {
      disposeStream();
      const streamId = ++streamIdRef.current;
      const stale = () =>
        generationRef.current !== generation || streamIdRef.current !== streamId || !mountedRef.current;
      streamDisposeRef.current = streamMqttLiveEvents(
        sessionId,
        {
          onFrame: (frame: MqttLiveFrame) => {
            if (stale()) {
              return;
            }
            retryIndexRef.current = 0;
            if (frame.type === "snapshot") {
              setState((current) => ({
                ...current,
                phase: "live",
                snapshot: frame,
                error: null,
                reconnectAttempts: 0,
              }));
            } else {
              setState((current) => ({
                ...current,
                lastActivity: { paths: frame.paths, at: Date.now() },
                reconnectAttempts: 0,
              }));
            }
          },
          onControl: (name: MqttLiveControlName) => {
            if (stale()) {
              return;
            }
            if (name === "closed") {
              // Taken over or reaped by the backend: end and re-read occupancy.
              disposeStream();
              setState((current) => ({ ...current, phase: "ended", session: null, snapshot: null }));
              void refreshStatus();
            } else if (name === "unavailable") {
              scheduleReconnectRef.current(sessionId, generation, "unavailable");
            }
            // "timeout" (wall-clock cap): the onClose below reconnects silently.
          },
          onClose: () => {
            if (stale()) {
              return;
            }
            // The stream ended (cap or upstream close) but the lease may still be
            // held: reconnect with backoff. A gone session surfaces as a 409 on
            // reopen, handled in onError.
            scheduleReconnectRef.current(sessionId, generation, "reconnecting");
          },
          onError: (error: unknown) => {
            if (stale()) {
              return;
            }
            if (statusCode(error) === 409) {
              disposeStream();
              setState((current) => ({ ...current, phase: "ended", session: null, snapshot: null }));
              void refreshStatus();
              return;
            }
            scheduleReconnectRef.current(sessionId, generation, "reconnecting");
          },
        },
        context(),
      );
    },
    [context, disposeStream, refreshStatus],
  );

  const scheduleReconnect = useCallback(
    (sessionId: string, generation: number, phase: "reconnecting" | "unavailable") => {
      if (generationRef.current !== generation || !mountedRef.current || retryTimerRef.current !== null) {
        return;
      }
      const attempt = retryIndexRef.current + 1;
      // Give up rather than retry a stream that has never delivered a frame.
      // The lease is still held, so the operator needs Stop / Start, not a
      // "reconnecting" label that never changes.
      if (attempt > RECONNECT_ATTEMPT_CAP) {
        disposeStream();
        setState((current) => ({
          ...current,
          phase: "unavailable",
          reconnectAttempts: retryIndexRef.current,
          error:
            `The sidecar reports session ${sessionId} as connected, but its event stream closed ` +
            `${retryIndexRef.current} times without sending a topic snapshot. Nothing is being ` +
            "received. Stop the live view and start it again, or check the sidecar.",
        }));
        return;
      }
      setState((current) => ({ ...current, phase, reconnectAttempts: attempt }));
      const delay = RETRY_DELAYS_MS[Math.min(retryIndexRef.current, RETRY_DELAYS_MS.length - 1)] ?? 15_000;
      retryIndexRef.current = attempt;
      retryTimerRef.current = setTimeout(() => {
        retryTimerRef.current = null;
        if (generationRef.current === generation && mountedRef.current) {
          openStreamRef.current(sessionId, generation);
        }
      }, delay);
    },
    [disposeStream],
  );

  openStreamRef.current = openStream;
  scheduleReconnectRef.current = scheduleReconnect;

  const start = useCallback(
    async (opts?: { takeOver?: boolean }) => {
      const generation = ++generationRef.current;
      clearRetry();
      retryIndexRef.current = 0;
      setState((current) => ({ ...current, phase: "connecting", error: null, reconnectAttempts: 0 }));
      try {
        const response = await connectMqttLiveSession({
          workspace: inputRef.current.workspace,
          authorized: inputRef.current.authorized,
          rootFilter: inputRef.current.rootFilter,
          takeOver: opts?.takeOver,
          context: context(),
        });
        if (generationRef.current !== generation || !mountedRef.current) {
          return;
        }
        setState((current) => ({ ...current, session: response.session, error: null }));
        openStream(response.session.session_id, generation);
      } catch (error) {
        if (generationRef.current !== generation || !mountedRef.current) {
          return;
        }
        if (statusCode(error) === 409) {
          // Already held by someone / a capture run is active: show occupancy.
          await refreshStatus();
          setState((current) => ({ ...current, error: errorMessage(error) }));
          return;
        }
        setState((current) => ({ ...current, phase: "error", error: errorMessage(error) }));
      }
    },
    [clearRetry, context, openStream, refreshStatus],
  );

  const stop = useCallback(async () => {
    const sessionId = state.session?.session_id;
    generationRef.current += 1; // invalidate any in-flight stream callbacks
    clearRetry();
    disposeStream();
    setState((current) => ({
      ...current,
      phase: "no_session",
      session: null,
      snapshot: null,
      lastActivity: null,
      error: null,
      reconnectAttempts: 0,
    }));
    try {
      await disconnectMqttLiveSession({ sessionId, context: context() });
    } catch {
      // Stop is always safe; a failed disconnect is reclaimed by the reaper.
    }
    void refreshStatus();
  }, [clearRetry, context, disposeStream, refreshStatus, state.session?.session_id]);

  const focus = useCallback(
    async (asset: string) => {
      const sessionId = state.session?.session_id;
      if (!sessionId || !asset) {
        return;
      }
      try {
        // Fire-and-forget: the focused asset's detail rides the snapshot stream,
        // so we do not need the immediate response.
        await focusMqttLiveAsset({ sessionId, asset, context: context() });
      } catch {
        // Focus is best-effort; a failure leaves the current focus unchanged.
      }
    },
    [context, state.session?.session_id],
  );

  const subscribe = useCallback(
    async (rootFilter: string, qos?: number) => {
      const sessionId = state.session?.session_id;
      if (!sessionId) {
        return;
      }
      try {
        await subscribeMqttLive({ sessionId, rootFilter, qos, context: context() });
      } catch (error) {
        if (mountedRef.current) {
          setState((current) => ({ ...current, error: errorMessage(error) }));
        }
      }
    },
    [context, state.session?.session_id],
  );

  const search = useCallback(
    async (q: string, matchedOnly?: boolean) => {
      const sessionId = state.session?.session_id;
      if (!sessionId) {
        return;
      }
      try {
        // The filtered tree rides the snapshot stream, so ignore the response.
        await searchMqttLive({ sessionId, q, matchedOnly, context: context() });
      } catch {
        // Search is best-effort; a failure leaves the current filter unchanged.
      }
    },
    [context, state.session?.session_id],
  );

  // On enable, read occupancy once. On disable/unmount, abort the stream but do
  // NOT disconnect (see the hook docstring).
  useEffect(() => {
    mountedRef.current = true;
    if (!enabled) {
      return () => {
        mountedRef.current = false;
      };
    }
    setState((current) => (current.phase === "idle" ? { ...current, phase: "checking" } : current));
    void refreshStatus();
    return () => {
      mountedRef.current = false;
      clearRetry();
      disposeStream();
    };
  }, [enabled, refreshStatus, clearRetry, disposeStream]);

  return {
    phase: state.phase,
    session: state.session,
    status: state.status,
    snapshot: state.snapshot,
    lastActivity: state.lastActivity,
    error: state.error,
    reconnectAttempts: state.reconnectAttempts,
    start,
    stop,
    focus,
    subscribe,
    search,
    refreshStatus,
  };
}
