import {
  ApiError,
  AUTH_REQUIRED_MESSAGE,
  cancelRun,
  clearApiKey,
  downloadFile,
  formatApiDetail,
  getApiKey,
  getDiscoveryResults,
  getDiscoveryRun,
  getHealth,
  getMe,
  listReports,
  listRuns,
  parseSseBuffer,
  roleAtLeast,
  rollbackMqttConfigPublish,
  setApiKey,
  streamRunEvents,
  validateConfiguration,
  type ConfigurationSnapshot,
  type RunEvent,
  type RunEventName,
} from "./client";

describe("formatApiDetail", () => {
  it("returns string details unchanged", () => {
    expect(formatApiDetail("Broker unreachable.")).toBe("Broker unreachable.");
  });

  it("stringifies primitive details", () => {
    expect(formatApiDetail(404)).toBe("404");
    expect(formatApiDetail(false)).toBe("false");
  });

  it("formats FastAPI validation errors as location-prefixed messages", () => {
    const detail = {
      loc: ["body", "mqtt", "Broker Port"],
      msg: "value is not a valid integer",
      type: "type_error.integer",
    };
    expect(formatApiDetail(detail)).toBe("mqtt.Broker Port: value is not a valid integer");
  });

  it("falls back to a generic message for null or undefined details", () => {
    expect(formatApiDetail(null)).toBe("Unknown API error.");
    expect(formatApiDetail(undefined)).toBe("Unknown API error.");
  });
});

const healthPayload = { status: "ok", timestamp: "2026-06-11T00:00:00Z" };

const sectionFixture = { status: "complete", values: {} };

const configurationFixture: ConfigurationSnapshot = {
  backups: sectionFixture,
  bacnet: sectionFixture,
  certificates: sectionFixture,
  device: sectionFixture,
  logging: sectionFixture,
  mqtt: sectionFixture,
  time: sectionFixture,
};

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => payload,
  } as unknown as Response;
}

function errorResponse(status: number, statusText: string, payload: unknown): Response {
  return {
    ok: false,
    status,
    statusText,
    json: async () => payload,
  } as unknown as Response;
}

function stubFetch(response: Response) {
  const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
    async () => response,
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("API key helpers", () => {
  afterEach(() => {
    clearApiKey();
    vi.unstubAllEnvs();
  });

  it("returns null when no key is stored or configured", () => {
    expect(getApiKey()).toBeNull();
  });

  it("reads the key stored in localStorage", () => {
    setApiKey("stored-key");
    expect(getApiKey()).toBe("stored-key");
  });

  it("prefers the localStorage key over VITE_API_KEY", () => {
    vi.stubEnv("VITE_API_KEY", "env-key");
    setApiKey("stored-key");
    expect(getApiKey()).toBe("stored-key");
  });

  it("falls back to VITE_API_KEY when localStorage has no key", () => {
    vi.stubEnv("VITE_API_KEY", "env-key");
    expect(getApiKey()).toBe("env-key");
  });

  it("clearApiKey removes the stored key", () => {
    setApiKey("stored-key");
    clearApiKey();
    expect(getApiKey()).toBeNull();
  });
});

describe("request authentication", () => {
  afterEach(() => {
    clearApiKey();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("attaches the X-API-Key header when localStorage has a key", async () => {
    setApiKey("stored-key");
    const fetchMock = stubFetch(jsonResponse(healthPayload));

    await expect(getHealth()).resolves.toEqual(healthPayload);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/health");
    expect(new Headers(init?.headers).get("X-API-Key")).toBe("stored-key");
  });

  it("attaches the VITE_API_KEY env key when localStorage has none", async () => {
    vi.stubEnv("VITE_API_KEY", "env-key");
    const fetchMock = stubFetch(jsonResponse(healthPayload));

    await getHealth();

    const [, init] = fetchMock.mock.calls[0];
    expect(new Headers(init?.headers).get("X-API-Key")).toBe("env-key");
  });

  it("omits the X-API-Key header when no key is configured", async () => {
    const fetchMock = stubFetch(jsonResponse(healthPayload));

    await getHealth();

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/health");
    // The request init passes through untouched so existing callers and
    // fetch stubs (e.g. App.test.tsx asserting `fetch(url, undefined)`) hold.
    expect(init).toBeUndefined();
  });

  it("preserves existing request headers when attaching the key", async () => {
    setApiKey("stored-key");
    const fetchMock = stubFetch(jsonResponse({ errors: [], valid: true }));

    await validateConfiguration(configurationFixture);

    const [, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("X-API-Key")).toBe("stored-key");
  });

  it("turns a 401 response into an auth-specific ApiError", async () => {
    stubFetch(errorResponse(401, "Unauthorized", { detail: "API key required." }));

    const failure = getHealth();
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      message: AUTH_REQUIRED_MESSAGE,
      name: "ApiError",
      status: 401,
    });
  });

  it("keeps backend detail messages for non-auth failures", async () => {
    stubFetch(errorResponse(422, "Unprocessable Entity", { detail: "Broker unreachable." }));

    await expect(getHealth()).rejects.toMatchObject({
      message: "Broker unreachable.",
      name: "ApiError",
      status: 422,
    });
  });
});

function blobResponse(blob: Blob, headers: Record<string, string> = {}): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    blob: async () => blob,
    headers: new Headers(headers),
  } as unknown as Response;
}

describe("downloadFile", () => {
  afterEach(() => {
    clearApiKey();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("attaches the X-API-Key header when a key is set", async () => {
    setApiKey("stored-key");
    const blob = new Blob(["spreadsheet-bytes"]);
    const fetchMock = stubFetch(blobResponse(blob));

    const result = await downloadFile("/reports/report-1/download");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/reports/report-1/download");
    expect(new Headers(init?.headers).get("X-API-Key")).toBe("stored-key");
    expect(result.blob).toBe(blob);
  });

  it("parses the filename from the Content-Disposition header", async () => {
    const blob = new Blob(["spreadsheet-bytes"]);
    stubFetch(
      blobResponse(blob, { "Content-Disposition": 'attachment; filename="report.xlsx"' }),
    );

    await expect(downloadFile("/reports/report-1/download")).resolves.toEqual({
      blob,
      filename: "report.xlsx",
    });
  });

  it("falls back to a null filename when Content-Disposition is absent", async () => {
    const blob = new Blob(["spreadsheet-bytes"]);
    stubFetch(blobResponse(blob));

    await expect(downloadFile("/imports/templates/ip_register.csv")).resolves.toEqual({
      blob,
      filename: null,
    });
  });

  it("turns a 401 response into an auth-specific ApiError", async () => {
    stubFetch(errorResponse(401, "Unauthorized", { detail: "API key required." }));

    const failure = downloadFile("/reports/report-1/download");
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      message: AUTH_REQUIRED_MESSAGE,
      name: "ApiError",
      status: 401,
    });
  });

  it("keeps backend detail messages for other download failures", async () => {
    stubFetch(errorResponse(404, "Not Found", { detail: "Report artefact missing." }));

    await expect(downloadFile("/reports/missing/download")).rejects.toMatchObject({
      message: "Report artefact missing.",
      name: "ApiError",
      status: 404,
    });
  });
});

describe("run and discovery API functions", () => {
  afterEach(() => {
    clearApiKey();
    vi.unstubAllGlobals();
  });

  it("listRuns builds the query string and attaches the API key", async () => {
    setApiKey("stored-key");
    const payload = { runs: [{ run_id: "r1", job_type: "ip_discovery", status: "succeeded" }] };
    const fetchMock = stubFetch(jsonResponse(payload));

    await expect(
      listRuns({ jobType: "ip_discovery", limit: 25, projectId: "demo-project" }),
    ).resolves.toEqual(payload);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/runs?project_id=demo-project&job_type=ip_discovery&limit=25");
    expect(new Headers(init?.headers).get("X-API-Key")).toBe("stored-key");
  });

  it("listRuns omits the query string when no params are supplied", async () => {
    const fetchMock = stubFetch(jsonResponse({ runs: [] }));

    await listRuns();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/runs");
  });

  it("listRuns passes the new edge_id and status filters and reads edge_id off each run", async () => {
    setApiKey("stored-key");
    const payload = {
      runs: [
        {
          run_id: "r-local",
          job_type: "ip_discovery",
          status: "succeeded",
          stage: "done",
          progress_percent: 100,
          created_at: "2026-06-11T09:00:00Z",
          updated_at: "2026-06-11T09:05:00Z",
          edge_id: null,
        },
        {
          run_id: "r-edge",
          job_type: "mqtt_discovery",
          status: "running",
          stage: "subscribing",
          progress_percent: 40,
          created_at: "2026-06-11T09:10:00Z",
          updated_at: "2026-06-11T09:11:00Z",
          edge_id: "edge-west-2",
        },
      ],
    };
    const fetchMock = stubFetch(jsonResponse(payload));

    const result = await listRuns({
      edgeId: "edge-west-2",
      jobType: "mqtt_discovery",
      status: "running",
    });

    const [url, init] = fetchMock.mock.calls[0];
    // job_type, edge_id, and status all ride the query string.
    expect(url).toBe(
      "/api/v1/runs?job_type=mqtt_discovery&edge_id=edge-west-2&status=running",
    );
    expect(new Headers(init?.headers).get("X-API-Key")).toBe("stored-key");
    // edge_id is exposed on each run (null for a local run, populated otherwise).
    expect(result.runs[0].edge_id).toBeNull();
    expect(result.runs[1].edge_id).toBe("edge-west-2");
  });

  it("cancelRun POSTs to the cancel endpoint", async () => {
    const payload = { run_id: "r1", job_type: "ip_discovery", status: "cancelled" };
    const fetchMock = stubFetch(jsonResponse(payload));

    await expect(cancelRun("r1")).resolves.toEqual(payload);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/runs/r1/cancel");
    expect(init?.method).toBe("POST");
  });

  it("getDiscoveryRun and getDiscoveryResults hit the discovery routes", async () => {
    const runPayload = { run_id: "r1", status: "running" };
    let fetchMock = stubFetch(jsonResponse(runPayload));
    await getDiscoveryRun("r1");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/discovery/runs/r1");

    const resultsPayload = { run_id: "r1", status: "succeeded", devices: [], points: [], topics: [], discovered_assets: [] };
    fetchMock = stubFetch(jsonResponse(resultsPayload));
    await expect(getDiscoveryResults("r1")).resolves.toEqual(resultsPayload);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/discovery/runs/r1/results");
  });

  it("listReports hits the reports list endpoint", async () => {
    const fetchMock = stubFetch(jsonResponse({ reports: [] }));
    await listReports();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/reports");
  });

  it("rollbackMqttConfigPublish POSTs to the rollback route", async () => {
    const fetchMock = stubFetch(
      jsonResponse({ run_id: "r1", job_type: "mqtt_config_publish", status: "succeeded", message: "ok" }),
    );
    await rollbackMqttConfigPublish("r1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/validation/mqtt-config/runs/r1/rollback");
    expect(init?.method).toBe("POST");
  });

  it("turns a 401 on a run call into an auth-specific ApiError", async () => {
    stubFetch(errorResponse(401, "Unauthorized", { detail: "API key required." }));

    await expect(listRuns()).rejects.toMatchObject({
      message: AUTH_REQUIRED_MESSAGE,
      name: "ApiError",
      status: 401,
    });
  });
});

describe("identity (getMe + roleAtLeast)", () => {
  afterEach(() => {
    clearApiKey();
    vi.unstubAllGlobals();
  });

  it("getMe hits /api/v1/me, attaches the key, and returns the principal", async () => {
    setApiKey("stored-key");
    const payload = { username: "site-eng", role: "engineer", source: "user_key" };
    const fetchMock = stubFetch(jsonResponse(payload));

    await expect(getMe()).resolves.toEqual(payload);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/me");
    expect(new Headers(init?.headers).get("X-API-Key")).toBe("stored-key");
  });

  it("roleAtLeast respects the viewer<reviewer<engineer<admin ordering", () => {
    expect(roleAtLeast("engineer", "engineer")).toBe(true);
    expect(roleAtLeast("admin", "engineer")).toBe(true);
    expect(roleAtLeast("viewer", "engineer")).toBe(false);
    expect(roleAtLeast("reviewer", "engineer")).toBe(false);
    // Reviewer outranks viewer but not engineer.
    expect(roleAtLeast("reviewer", "reviewer")).toBe(true);
    expect(roleAtLeast("viewer", "reviewer")).toBe(false);
    // Unknown/undefined roles fail closed (treated as below any minimum).
    expect(roleAtLeast(undefined, "viewer")).toBe(false);
    expect(roleAtLeast("superuser", "viewer")).toBe(false);
  });
});

// Build a streaming Response whose body yields the given UTF-8 chunks in order
// through a ReadableStream reader — exactly what streamRunEvents consumes via
// fetch(). Mirrors the backend text/event-stream framing.
function sseStreamResponse(chunks: string[], status = 200): Response {
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
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
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

describe("parseSseBuffer", () => {
  it("parses complete frames and returns the partial trailing buffer", () => {
    const buffer = `${sseFrame({ run_id: "r1", status: "running", progress_percent: 40 })}data: {"run_id":"r1",`;
    const { events, rest } = parseSseBuffer(buffer);
    expect(events).toHaveLength(1);
    expect(events[0].name).toBe("message");
    expect(events[0].data).toMatchObject({ run_id: "r1", status: "running", progress_percent: 40 });
    expect(rest).toBe('data: {"run_id":"r1",');
  });

  it("reads the event name from an event: line", () => {
    const { events } = parseSseBuffer(sseFrame({ run_id: "r1", status: "succeeded" }, "terminal"));
    expect(events[0].name).toBe("terminal");
    expect(events[0].data).toMatchObject({ status: "succeeded" });
  });

  it("skips a frame with malformed JSON without throwing", () => {
    const { events } = parseSseBuffer("data: {not-json}\n\n");
    expect(events).toHaveLength(1);
    expect(events[0].data).toBeNull();
  });
});

// Collects events from streamRunEvents and resolves once the stream closes.
function collectRunEvents(runId: string): Promise<{
  events: { event: RunEvent; name: RunEventName }[];
  reachedTerminal: boolean;
  error: unknown;
}> {
  return new Promise((resolve) => {
    const events: { event: RunEvent; name: RunEventName }[] = [];
    let error: unknown;
    streamRunEvents(runId, {
      onClose: (reachedTerminal) => resolve({ error, events, reachedTerminal }),
      onError: (caught) => {
        error = caught;
      },
      onEvent: (event, name) => {
        events.push({ event, name });
      },
    });
  });
}

describe("streamRunEvents", () => {
  afterEach(() => {
    clearApiKey();
    vi.unstubAllGlobals();
  });

  it("attaches the X-API-Key header (fetch-stream) and parses events from the ReadableStream", async () => {
    setApiKey("stored-key");
    const fetchMock = stubFetch(
      sseStreamResponse([
        sseFrame({ run_id: "r1", status: "running", stage: "scanning", progress_percent: 30 }),
        sseFrame({ run_id: "r1", status: "succeeded", progress_percent: 100 }, "terminal"),
      ]),
    );

    const { events, reachedTerminal, error } = await collectRunEvents("r1");

    // X-API-Key rides the fetch (EventSource could not have sent it).
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/runs/r1/events");
    expect(new Headers(init?.headers).get("X-API-Key")).toBe("stored-key");

    expect(error).toBeUndefined();
    expect(reachedTerminal).toBe(true);
    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({ name: "message", event: { status: "running", stage: "scanning" } });
    expect(events[1]).toMatchObject({ name: "terminal", event: { status: "succeeded", progress_percent: 100 } });
  });

  it("treats a status-derived terminal frame as terminal even without the event name", async () => {
    stubFetch(sseStreamResponse([sseFrame({ run_id: "r1", status: "failed", progress_percent: 100 })]));

    const { reachedTerminal, events } = await collectRunEvents("r1");

    expect(reachedTerminal).toBe(true);
    expect(events[0].event.status).toBe("failed");
  });

  it("falls back to polling: a 401 surfaces via onError and closes non-terminal", async () => {
    stubFetch(errorResponse(401, "Unauthorized", { detail: "API key required." }));

    const { error, reachedTerminal, events } = await collectRunEvents("r1");

    expect(events).toHaveLength(0);
    // reachedTerminal=false is the signal the caller (useRunEvents) uses to
    // disable SSE and resume the 1.5s polling.
    expect(reachedTerminal).toBe(false);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ message: AUTH_REQUIRED_MESSAGE, status: 401 });
  });

  it("falls back to polling when the response has no streaming body", async () => {
    const noBody = {
      ok: true,
      status: 200,
      statusText: "OK",
      headers: new Headers(),
      body: null,
      json: async () => ({}),
    } as unknown as Response;
    stubFetch(noBody);

    const { error, reachedTerminal } = await collectRunEvents("r1");

    expect(reachedTerminal).toBe(false);
    expect(error).toBeInstanceOf(Error);
  });
});
