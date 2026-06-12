import { renderHook, waitFor } from "@testing-library/react";
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

describe("useRunEvents", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("live-updates status/stage/progress from the SSE stream and marks terminal", async () => {
    stubFetch(
      sseStreamResponse([
        sseFrame({ run_id: "r1", status: "running", stage: "scanning", progress_percent: 45 }),
        sseFrame({ run_id: "r1", status: "succeeded", progress_percent: 100 }, "terminal"),
      ]),
    );

    const { result } = renderHook(() => useRunEvents("r1", true));

    await waitFor(() => expect(result.current.reachedTerminal).toBe(true));

    expect(result.current.sseActive).toBe(true);
    expect(result.current.event).toMatchObject({ status: "succeeded", progress_percent: 100 });
  });

  it("falls back to polling (sseActive=false) when the stream errors", async () => {
    stubFetch({
      ok: false,
      status: 401,
      statusText: "Unauthorized",
      headers: new Headers(),
      json: async () => ({ detail: "API key required." }),
    } as unknown as Response);

    const { result } = renderHook(() => useRunEvents("r1", true));

    await waitFor(() => expect(result.current.sseActive).toBe(false));
    // No terminal was observed over the stream — the caller resumes polling.
    expect(result.current.reachedTerminal).toBe(false);
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
