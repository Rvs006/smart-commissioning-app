import { act, renderHook, waitFor } from "@testing-library/react";
import { useRunEvents } from "./useRunEvents";

// Build a streaming Response whose ReadableStream reader yields the given SSE
// chunks in order — the shape streamRunEvents (via fetch) consumes.
function sseStreamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  let index = 0;
  const reader = {
    read: async () => {
      if (index >= chunks.length) {
        return { done: true, value: undefined };
      }
      const value = encoder.encode(chunks[index]);
      index += 1;
      return { done: false, value };
    },
  };
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: new Headers({ "Content-Type": "text/event-stream" }),
    body: { getReader: () => reader },
    json: async () => ({}),
  } as unknown as Response;
}

function sseFrame(payload: Record<string, unknown>, event?: string): string {
  const lines = [];
  if (event) {
    lines.push(`event: ${event}`);
  }
  lines.push(`data: ${JSON.stringify(payload)}`);
  return `${lines.join("\n")}\n\n`;
}

function stubFetch(response: Response | (() => Response)) {
  const fetchMock = vi.fn(async () => (typeof response === "function" ? response() : response));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function controlledSseStream() {
  const encoder = new TextEncoder();
  const queued: Array<{ done: boolean; value?: Uint8Array }> = [];
  let pending: ((value: { done: boolean; value?: Uint8Array }) => void) | null = null;
  let rejectPending: ((error: unknown) => void) | null = null;
  const deliver = (value: { done: boolean; value?: Uint8Array }) => {
    if (pending) {
      const resolve = pending;
      pending = null;
      rejectPending = null;
      resolve(value);
    } else {
      queued.push(value);
    }
  };
  const response = {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: new Headers({ "Content-Type": "text/event-stream" }),
    body: {
      getReader: () => ({
        read: () => {
          const next = queued.shift();
          if (next) {
            return Promise.resolve(next);
          }
          return new Promise<{ done: boolean; value?: Uint8Array }>((resolve, reject) => {
            pending = resolve;
            rejectPending = reject;
          });
        },
      }),
    },
    json: async () => ({}),
  } as unknown as Response;
  return {
    close: () => deliver({ done: true }),
    fail: (error: unknown) => {
      if (!rejectPending) {
        throw new Error("The controlled SSE stream is not waiting for a read.");
      }
      const reject = rejectPending;
      pending = null;
      rejectPending = null;
      reject(error);
    },
    push: (frame: string) => deliver({ done: false, value: encoder.encode(frame) }),
    response,
  };
}

describe("useRunEvents", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("live-updates status/stage/progress from the SSE stream and marks terminal", async () => {
    stubFetch(
      sseStreamResponse([
        sseFrame({ run_id: "r1", status: "running", stage: "scanning", progress_percent: 45 }),
        sseFrame(
          {
            run_id: "r1",
            status: "succeeded",
            progress_percent: 100,
            observation_attempt: 2,
            latest_observation_cursor: 19,
            progressive_counts: { planned: 20, observed: 19 },
          },
          "terminal",
        ),
      ]),
    );

    const { result } = renderHook(() => useRunEvents("r1", true));

    await waitFor(() => expect(result.current.reachedTerminal).toBe(true));

    expect(result.current.sseActive).toBe(true);
    expect(result.current.event).toMatchObject({ status: "succeeded", progress_percent: 100 });
    expect(result.current).toMatchObject({
      connectionState: "terminal",
      observationAttempt: 2,
      latestObservationCursor: 19,
      progressiveCounts: { planned: 20, observed: 19 },
    });
    expect(result.current.lastEventAt).toEqual(expect.any(Number));
  });

  it("keeps progressive evidence in memory instead of browser storage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    stubFetch(
      sseStreamResponse([
        sseFrame(
          {
            run_id: "r1",
            status: "succeeded",
            observation_attempt: 1,
            latest_observation_cursor: 4,
            progressive_counts: { observed: 4 },
          },
          "terminal",
        ),
      ]),
    );

    const { result } = renderHook(() => useRunEvents("r1", true));
    await waitFor(() => expect(result.current.reachedTerminal).toBe(true));

    expect(result.current.latestObservationCursor).toBe(4);
    expect(setItem).not.toHaveBeenCalled();
  });

  it("falls back to polling and retries errors at 1s, 2s, 5s, then a 15s cap", async () => {
    vi.useFakeTimers();
    const fetchMock = stubFetch({
      ok: false,
      status: 503,
      statusText: "Unavailable",
      headers: new Headers(),
      json: async () => ({ detail: "Try again." }),
    } as unknown as Response);

    const { result, unmount } = renderHook(() => useRunEvents("r1", true));

    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(result.current.sseActive).toBe(false);
    expect(result.current.connectionState).toBe("reconnecting");
    // No terminal was observed over the stream — the caller resumes polling.
    expect(result.current.reachedTerminal).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    expect(fetchMock).toHaveBeenCalledTimes(4);
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(fetchMock).toHaveBeenCalledTimes(5);
    await act(async () => vi.advanceTimersByTimeAsync(15_000));
    expect(fetchMock).toHaveBeenCalledTimes(6);
    await act(async () => vi.advanceTimersByTimeAsync(15_000));
    expect(fetchMock).toHaveBeenCalledTimes(7);
    unmount();
  });

  it("does not attach a delayed event from another run to the active stream", async () => {
    stubFetch(
      sseStreamResponse([
        sseFrame({ run_id: "run-a", status: "succeeded", progress_percent: 100 }, "terminal"),
      ]),
    );

    const { result } = renderHook(() => useRunEvents("run-b", true));

    await waitFor(() => expect(result.current.sseActive).toBe(false));
    expect(result.current.event).toBeNull();
    expect(result.current.reachedTerminal).toBe(false);
  });

  it("reconnects after an explicit timeout and keeps polling active until the new stream speaks", async () => {
    vi.useFakeTimers();
    const responses = [
      sseStreamResponse([
        sseFrame({ run_id: "r1", status: "running", latest_observation_cursor: 7 }, "timeout"),
      ]),
      sseStreamResponse([
        sseFrame({ run_id: "r1", status: "succeeded", latest_observation_cursor: 8 }, "terminal"),
      ]),
    ];
    let responseIndex = 0;
    const fetchMock = stubFetch(() => responses[responseIndex++] ?? responses[1]);
    const { result, unmount } = renderHook(() => useRunEvents("r1", true));

    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(result.current).toMatchObject({
      connectionState: "reconnecting",
      latestObservationCursor: 7,
      sseActive: false,
    });
    await act(async () => vi.advanceTimersByTimeAsync(999));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current).toMatchObject({
      connectionState: "terminal",
      latestObservationCursor: 8,
      reachedTerminal: true,
    });
    unmount();
  });

  it("retains the last event across EOF, enables polling, and reconnects", async () => {
    vi.useFakeTimers();
    const responses = [
      sseStreamResponse([
        sseFrame({ run_id: "r1", status: "running", stage: "probing", progress_percent: 30 }),
      ]),
      sseStreamResponse([
        sseFrame({ run_id: "r1", status: "succeeded", progress_percent: 100 }, "terminal"),
      ]),
    ];
    let responseIndex = 0;
    const fetchMock = stubFetch(() => responses[responseIndex++] ?? responses[1]);
    const { result, unmount } = renderHook(() => useRunEvents("r1", true));

    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(result.current.event).toMatchObject({ stage: "probing", progress_percent: 30 });
    expect(result.current).toMatchObject({ connectionState: "reconnecting", sseActive: false });
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.event).toMatchObject({ status: "succeeded", progress_percent: 100 });
    unmount();
  });

  it("retains the last event when an established stream errors", async () => {
    vi.useFakeTimers();
    const stream = controlledSseStream();
    stubFetch(stream.response);
    const { result, unmount } = renderHook(() => useRunEvents("r1", true));
    await act(async () => vi.advanceTimersByTimeAsync(0));
    await act(async () => {
      stream.push(sseFrame({ run_id: "r1", status: "running", stage: "scanning" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    const lastEventAt = result.current.lastEventAt;

    await act(async () => {
      stream.fail(new Error("connection reset"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current).toMatchObject({
      connectionState: "reconnecting",
      event: expect.objectContaining({ stage: "scanning" }),
      lastEventAt,
      sseActive: false,
    });
    unmount();
  });

  it.each([401, 403, 404])(
    "closes and clears scoped state when reconnect receives a concealed %s",
    async (status) => {
      vi.useFakeTimers();
      const responses = [
        sseStreamResponse([
          sseFrame({
            run_id: "r1",
            status: "running",
            observation_attempt: 3,
            latest_observation_cursor: 12,
            progressive_counts: { observations: 12 },
          }),
        ]),
        {
          ok: false,
          status,
          statusText: "Unavailable",
          headers: new Headers(),
          json: async () => ({ detail: "Run is unavailable." }),
        } as unknown as Response,
      ];
      let responseIndex = 0;
      const fetchMock = stubFetch(() => responses[responseIndex++] ?? responses[1]);
      const { result, unmount } = renderHook(() => useRunEvents("r1", true));

      await act(async () => vi.advanceTimersByTimeAsync(0));
      expect(result.current).toMatchObject({
        connectionState: "reconnecting",
        event: expect.objectContaining({ status: "running" }),
        latestObservationCursor: 12,
      });

      await act(async () => vi.advanceTimersByTimeAsync(1_000));
      expect(result.current).toMatchObject({
        connectionState: "closed",
        event: null,
        observationAttempt: null,
        latestObservationCursor: null,
        progressiveCounts: null,
        lastEventAt: null,
        reachedTerminal: false,
        sseActive: false,
      });
      await act(async () => vi.advanceTimersByTimeAsync(60_000));
      expect(fetchMock).toHaveBeenCalledTimes(2);
      unmount();
    },
  );

  it("stops reconnecting after the backend closes scoped access", async () => {
    vi.useFakeTimers();
    const fetchMock = stubFetch(
      sseStreamResponse([
        sseFrame({
          run_id: "r1",
          status: "running",
          observation_attempt: 3,
          latest_observation_cursor: 12,
          progressive_counts: { observations: 12 },
        }),
        sseFrame({ run_id: "r1", status: "closed" }, "closed"),
      ]),
    );
    const { result, unmount } = renderHook(() => useRunEvents("r1", true));

    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(result.current).toMatchObject({
      connectionState: "closed",
      event: null,
      observationAttempt: null,
      latestObservationCursor: null,
      progressiveCounts: null,
      lastEventAt: null,
      reachedTerminal: false,
      sseActive: false,
    });
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    unmount();
  });

  it("treats backend unavailability as retryable without storing its pseudo-status", async () => {
    vi.useFakeTimers();
    const responses = [
      sseStreamResponse([sseFrame({ run_id: "r1", status: "unavailable" }, "unavailable")]),
      sseStreamResponse([sseFrame({ run_id: "r1", status: "succeeded" }, "terminal")]),
    ];
    let responseIndex = 0;
    const fetchMock = stubFetch(() => responses[responseIndex++] ?? responses[1]);
    const { result, unmount } = renderHook(() => useRunEvents("r1", true));

    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(result.current).toMatchObject({
      connectionState: "unavailable",
      event: null,
      sseActive: false,
    });
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.reachedTerminal).toBe(true);
    unmount();
  });

  it("retries immediately when the browser comes online", async () => {
    vi.useFakeTimers();
    const responses = [
      {
        ok: false,
        status: 503,
        statusText: "Unavailable",
        headers: new Headers(),
        json: async () => ({ detail: "Try again." }),
      } as unknown as Response,
      sseStreamResponse([sseFrame({ run_id: "r1", status: "succeeded" }, "terminal")]),
    ];
    let responseIndex = 0;
    const fetchMock = stubFetch(() => responses[responseIndex++] ?? responses[1]);
    const { result, unmount } = renderHook(() => useRunEvents("r1", true));

    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(result.current.connectionState).toBe("reconnecting");
    await act(async () => {
      window.dispatchEvent(new Event("online"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.reachedTerminal).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    unmount();
  });

  it("retries immediately when a visible window regains focus", async () => {
    vi.useFakeTimers();
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    const responses = [
      {
        ok: false,
        status: 503,
        statusText: "Unavailable",
        headers: new Headers(),
        json: async () => ({ detail: "Try again." }),
      } as unknown as Response,
      sseStreamResponse([sseFrame({ run_id: "r1", status: "succeeded" }, "terminal")]),
    ];
    let responseIndex = 0;
    const fetchMock = stubFetch(() => responses[responseIndex++] ?? responses[1]);
    const { result, unmount } = renderHook(() => useRunEvents("r1", true));

    await act(async () => vi.advanceTimersByTimeAsync(0));
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.connectionState).toBe("terminal");
    unmount();
  });

  it("aborts the old run and fences a delayed old-run frame after reattachment", async () => {
    vi.useFakeTimers();
    const runA = controlledSseStream();
    const runB = controlledSseStream();
    const signals: AbortSignal[] = [];
    const removeWindowListener = vi.spyOn(window, "removeEventListener");
    const removeDocumentListener = vi.spyOn(document, "removeEventListener");
    const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      if (init?.signal) {
        signals.push(init.signal);
      }
      return String(url).includes("run-a") ? runA.response : runB.response;
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result, rerender, unmount } = renderHook(({ runId }) => useRunEvents(runId, true), {
      initialProps: { runId: "run-a" },
    });
    await act(async () => vi.advanceTimersByTimeAsync(0));

    rerender({ runId: "run-b" });
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(signals[0].aborted).toBe(true);
    await act(async () => {
      runA.push(sseFrame({ run_id: "run-a", status: "succeeded" }, "terminal"));
      runB.push(sseFrame({ run_id: "run-b", status: "running", stage: "new-run" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.event).toMatchObject({ run_id: "run-b", stage: "new-run" });
    expect(result.current.reachedTerminal).toBe(false);

    unmount();
    expect(signals[1].aborted).toBe(true);
    expect(removeWindowListener).toHaveBeenCalledWith("online", expect.any(Function));
    expect(removeWindowListener).toHaveBeenCalledWith("focus", expect.any(Function));
    expect(removeDocumentListener).toHaveBeenCalledWith("visibilitychange", expect.any(Function));
  });

  it("clears a scheduled retry when the active run changes", async () => {
    vi.useFakeTimers();
    const runB = controlledSseStream();
    const fetchMock = vi.fn(async (url: string | URL | Request) => {
      if (String(url).includes("run-a")) {
        return {
          ok: false,
          status: 503,
          statusText: "Unavailable",
          headers: new Headers(),
          json: async () => ({ detail: "Try again." }),
        } as unknown as Response;
      }
      return runB.response;
    });
    vi.stubGlobal("fetch", fetchMock);
    const { rerender, unmount } = renderHook(({ runId }) => useRunEvents(runId, true), {
      initialProps: { runId: "run-a" },
    });
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    rerender({ runId: "run-b" });
    await act(async () => vi.advanceTimersByTimeAsync(0));
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    unmount();
  });

  it("stays idle (no fetch) when disabled or no run id", () => {
    const fetchMock = stubFetch(sseStreamResponse([]));

    const { result } = renderHook(() => useRunEvents(null, true));
    expect(result.current.event).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();

    const disabled = renderHook(() => useRunEvents("r1", false));
    expect(disabled.result.current.event).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
