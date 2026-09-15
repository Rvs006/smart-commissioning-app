import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MqttLiveSessionState } from "../workflow/useMqttLiveSession";
import {
  clearApiKey,
  setApiKey,
  type MqttLiveFocused,
  type MqttLiveSessionInfo,
  type MqttLiveSnapshot,
} from "../../api/client";
import { MqttScannerPage } from "./MqttScannerPage";
import { scannerRowsFromResults } from "./scannerRows";
import { scannerProviders } from "./scannerTestHarness";

// The live session is driven entirely by the module mock: each test sets the
// phase + snapshot it needs and asserts what the page does with it. Nothing here
// opens a real broker session or an EventSource.
const liveMock = {
  start: vi.fn(async () => {}),
  stop: vi.fn(async () => {}),
  focus: vi.fn(async () => {}),
  subscribe: vi.fn(async () => {}),
  search: vi.fn(async () => {}),
  refreshStatus: vi.fn(async () => {}),
};
let liveState: Partial<MqttLiveSessionState> = {};
// What the page asked the hook for, so the tests can prove the workspace,
// authorization and root filter reach the session and are not defaulted.
let liveHookCalls: Array<{ enabled: unknown; input: unknown }> = [];

vi.mock("../workflow/useMqttLiveSession", () => ({
  useMqttLiveSession: (enabled: unknown, input: unknown) => {
    liveHookCalls.push({ enabled, input });
    return {
      error: null,
      lastActivity: null,
      phase: "no_session",
      session: null,
      snapshot: null,
      status: null,
      ...liveMock,
      ...liveState,
    };
  },
}));

const RUN_ID = "run-mqtt-scanner-1";

function jsonResponse(payload: unknown): Response {
  return { ok: true, status: 200, statusText: "OK", json: async () => payload } as unknown as Response;
}

const terminalRun = {
  run_id: RUN_ID,
  job_type: "mqtt_scanner",
  status: "succeeded",
  stage: "register_comparison",
  progress_percent: 100,
  created_at: "2026-09-14T09:00:00Z",
  updated_at: "2026-09-14T09:01:00Z",
  project_id: "demo-project",
  site_id: "demo-site",
  parameters: {},
  result_summary: {
    topics_discovered: 3,
    assets_discovered: 2,
    register_matches: 2,
    register_rogue: 1,
    subscribe_qos: 1,
    raw_evidence_artifact_id: "art-mqtt-1",
  },
  error_message: null,
};

const results = {
  run_id: RUN_ID,
  job_type: "mqtt_scanner",
  status: "succeeded",
  result_summary: terminalRun.result_summary,
  discovered_assets: [],
  devices: [],
  points: [],
  register_comparison: {
    register_available: true,
    matched_count: 2,
    unmatched_count: 1,
    unobserved_filters: [{ filter: "example/AHU-02/#" }],
  },
  topics: [
    {
      topic: "example/AHU-01/pointset",
      message_count: 12,
      last_payload: { temp: 18.4 },
      created_at: "2026-09-14T09:00:30Z",
      attributes: {
        device_ref: "AHU-01",
        register_match: "matched",
        register_matched_filter: "example/AHU-01/#",
        last_retained: true,
        last_qos: 1,
        last_received_at: "2026-09-14T09:00:59Z",
        status_detail: "observed",
      },
    },
    {
      topic: "example/ROGUE-09/state",
      message_count: 3,
      last_payload: { status: "on" },
      created_at: "2026-09-14T09:00:40Z",
      attributes: {
        device_ref: "ROGUE-09",
        register_match: "unmatched",
        last_retained: false,
        last_qos: 0,
        status_detail: "observed",
      },
    },
  ],
};

let startBody: Record<string, unknown> | null = null;
let configuration: Record<string, { values: Record<string, string>; status: string }> = {};

function mqttConfiguration(brokerHost: string) {
  return {
    mqtt: {
      values: {
        "MQTT Broker FQDN or IP Address": brokerHost,
        Port: "8883",
        "Use TLS": "Enabled",
        "Client ID": "sct-gateway-01",
        QoS: "1 - At least once",
      },
      status: "Not checked",
    },
  };
}

function stubFetch({ runs = [terminalRun] as unknown[] } = {}) {
  startBody = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/runs?")) {
        return jsonResponse({ runs });
      }
      if (url.endsWith("/api/v1/imports/profiles")) {
        return jsonResponse([
          {
            import_type: "mqtt_scanner_register",
            description: "Expected MQTT assets.",
            required_columns: ["asset_id", "topic"],
            duplicate_key_fields: ["asset_id"],
          },
        ]);
      }
      if (url.includes("/api/v1/imports/latest")) {
        return jsonResponse({
          import_id: "imp-m1",
          import_type: "mqtt_scanner_register",
          file_name: "mqtt-register.csv",
          status: "accepted",
          total_rows: 2,
          accepted_rows: 2,
          rejected_rows: 0,
          missing_columns: [],
          warnings: [],
          created_at: "2026-09-14T08:00:00Z",
        });
      }
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configuration);
      }
      if (url.includes(`/api/v1/discovery/runs/${RUN_ID}/results`)) {
        return jsonResponse(results);
      }
      if (url.includes(`/api/v1/discovery/runs/${RUN_ID}`)) {
        return jsonResponse(terminalRun);
      }
      if (url.endsWith("/api/v1/discovery/mqtt_sidecar/runs") && init?.method === "POST") {
        startBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return jsonResponse({
          run_id: RUN_ID,
          job_type: "mqtt_scanner",
          status: "queued",
          message: "MQTT capture accepted.",
        });
      }
      if (url.includes("/discovery/mqtt_sidecar/live/save-as-register")) {
        return jsonResponse({
          import_id: "imp-live-1",
          import_type: "mqtt_scanner_register",
          file_name: "mqtt-live-register.csv",
          status: "accepted",
          total_rows: 4,
          accepted_rows: 4,
          rejected_rows: 0,
          missing_columns: [],
          warnings: [],
          created_at: "2026-09-14T09:10:00Z",
        });
      }
      if (url.includes("/save-as-register")) {
        return jsonResponse({
          import_id: "imp-saved-m1",
          import_type: "mqtt_scanner_register",
          file_name: `mqtt-scan-register-${RUN_ID}.csv`,
          status: "accepted",
          total_rows: 2,
          accepted_rows: 2,
          rejected_rows: 0,
          missing_columns: [],
          warnings: [],
          created_at: "2026-09-14T09:06:00Z",
        });
      }
      if (url.includes("/events")) {
        return jsonResponse({});
      }
      throw new Error(`Unexpected fetch in test: ${url}`);
    }),
  );
}

const liveSession: MqttLiveSessionInfo = {
  session_id: "sess-1",
  owner: "engineer-1",
  project_id: "demo-project",
  site_id: "demo-site",
  since: "2026-09-14T09:00:00Z",
};

const liveSnapshot: MqttLiveSnapshot = {
  type: "snapshot",
  status: {
    status: "connected",
    host: "broker.example.test",
    port: 8883,
    tls: true,
    rootFilter: "#",
    qos: 1,
    error: "",
    since: 1_757_000_000_000,
  },
  stats: {
    expectedAssets: 26,
    subscribedAssets: 24,
    liveAssets: 24,
    topicsDiscovered: 1842,
    issues: 2,
    totalMessages: 90210,
  },
  totalTopics: 1842,
  treeShown: 12,
  filtered: false,
  tree: [
    {
      n: "example",
      p: "example",
      t: 1842,
      m: 90210,
      r: 5,
      mt: 0,
      ch: [
        { n: "AHU-01", p: "example/AHU-01", t: 3, m: 120, r: 2, mt: 1, a: "AHU-01", leaf: 1 },
      ],
    },
  ],
  focused: null,
};

const focusedAsset: MqttLiveFocused = {
  asset: "AHU-01",
  key: "AHU-01",
  matched: true,
  schema: "pointset",
  rate: 2,
  count: 120,
  topics: ["example/AHU-01/pointset"],
  topicsDetail: [
    {
      topic: "example/AHU-01/pointset",
      schema: "pointset",
      count: 120,
      rate: 2,
      retained: true,
      history: [{ ts: 1_757_000_000_000, raw: "{}" }],
    },
  ],
  lastTopic: "example/AHU-01/pointset",
  lastPayload: '{"temp":18.4}',
  livePoints: [],
  issues: 0,
  comparison: {
    matched: 3,
    missing: 0,
    extra: 0,
    matchedNames: [],
    missingNames: [],
    extraNames: [],
    expected: 3,
  },
  meta: null,
  udmi: {},
  configTopic: "example/AHU-01/config",
  configPayload: '{"version":"1.5.2"}',
};

const runningRun = { ...terminalRun, status: "running", progress_percent: 40 };

beforeEach(() => {
  setApiKey("engineer-key");
  liveState = {};
  liveHookCalls = [];
  configuration = mqttConfiguration("broker.example.test");
  for (const fn of Object.values(liveMock)) {
    fn.mockClear();
  }
});

afterEach(() => {
  vi.unstubAllGlobals();
  clearApiKey();
  window.localStorage.clear();
});

describe("MqttScannerPage", () => {
  it("opens the live view by itself when a broker is configured", async () => {
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    expect(screen.getByRole("heading", { level: 1, name: "MQTT Discovery" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Broker & capture" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Live topics" })).toBeInTheDocument();
    await waitFor(() => expect(liveMock.start).toHaveBeenCalledTimes(1));

    // The session is opened for THIS workspace, with the page's authorization
    // and its topic filter, not the hook's defaults.
    const last = liveHookCalls[liveHookCalls.length - 1];
    expect(last?.enabled).toBe(true);
    expect(last?.input).toMatchObject({
      authorized: true,
      workspace: { projectId: expect.any(String), siteId: expect.any(String) },
    });
    // Blank filter means "every topic": the hook is given no rootFilter at all
    // rather than a literal "#".
    expect((last?.input as { rootFilter?: string }).rootFilter).toBeUndefined();
  });

  it("passes the topic filter through as the live session's root filter", async () => {
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    fireEvent.change(await screen.findByLabelText(/Topic filter/), {
      target: { value: "example/#" },
    });

    await waitFor(() =>
      expect((liveHookCalls[liveHookCalls.length - 1]?.input as { rootFilter?: string }).rootFilter).toBe("example/#"),
    );
  });

  it("does not auto-start while a capture run is still in flight", async () => {
    // The blocker this guards: until the latest-run query answers, nothing is
    // attached, so startedRunActive is false and a naive auto-connect 409s.
    stubFetch({ runs: [runningRun] });
    render(scannerProviders(<MqttScannerPage />));

    await screen.findByRole("heading", { name: "Live topics" });
    await waitFor(() => expect(screen.getByText(/Discovery run monitor/)).toBeInTheDocument());
    expect(liveMock.start).not.toHaveBeenCalled();
  });

  it("shows the connect refusal instead of a bare 'Live view not running'", async () => {
    liveState = {
      phase: "no_session",
      error: "An MQTT capture run is in progress for this project and site.",
    };
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    expect(
      await screen.findByText("An MQTT capture run is in progress for this project and site."),
    ).toBeInTheDocument();
  });

  it("does not auto-start without scan authorization, or without the engineer role", async () => {
    stubFetch({ runs: [] });
    const enforced = render(
      scannerProviders(<MqttScannerPage />, { authorizationEnforced: true }),
    );
    await screen.findByRole("heading", { name: "Live topics" });
    await waitFor(() => expect(screen.getByLabelText(/Topic filter/)).toBeInTheDocument());
    expect(liveMock.start).not.toHaveBeenCalled();
    enforced.unmount();

    liveHookCalls = [];
    render(scannerProviders(<MqttScannerPage />, { canEngineer: false }));
    await screen.findByRole("heading", { name: "Live topics" });
    await waitFor(() => expect(screen.getByLabelText(/Topic filter/)).toBeInTheDocument());
    expect(liveMock.start).not.toHaveBeenCalled();
  });

  it("attempts the auto-start at most once across re-renders", async () => {
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    await waitFor(() => expect(liveMock.start).toHaveBeenCalledTimes(1));
    // Any state change re-runs the effect; the attempt must not repeat.
    fireEvent.change(await screen.findByLabelText(/Topic filter/), { target: { value: "a/#" } });
    fireEvent.change(screen.getByLabelText(/Topic filter/), { target: { value: "b/#" } });
    await waitFor(() => expect(liveHookCalls.length).toBeGreaterThan(2));
    expect(liveMock.start).toHaveBeenCalledTimes(1);
  });

  it("does not auto-start, and points at Configuration, when no broker is configured", async () => {
    configuration = mqttConfiguration("");
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    expect(await screen.findByText("No broker configured")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Configuration" })).toBeInTheDocument();
    expect(liveMock.start).not.toHaveBeenCalled();
  });

  it("stands down when the live connect reports no broker, instead of retrying", async () => {
    configuration = mqttConfiguration("");
    liveState = {
      phase: "error",
      error:
        "No MQTT broker is configured. Enter the broker FQDN or IP address on the Configuration page and save it.",
    };
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    expect(await screen.findByText(/No MQTT broker is configured/)).toBeInTheDocument();
    expect(liveMock.start).not.toHaveBeenCalled();
  });

  it("puts the topic rail beside the focused panel and drives search, filter and focus", async () => {
    liveState = { phase: "live", session: liveSession, snapshot: liveSnapshot };
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    expect(await screen.findByText("Live topic tree")).toBeInTheDocument();
    expect(screen.getByText("example")).toBeInTheDocument();
    // No focused asset yet: the panel shows the empty state, not a blank box.
    expect(
      screen.getByText("Select a topic or asset to inspect its live payload and points."),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Search live topics"), {
      target: { value: "AHU" },
    });
    fireEvent.submit(screen.getByLabelText("Search live topics").closest("form") as HTMLElement);
    expect(liveMock.search).toHaveBeenCalledWith("AHU", false);

    fireEvent.click(screen.getByLabelText("Registered assets only"));
    expect(liveMock.search).toHaveBeenLastCalledWith("AHU", true);

    fireEvent.click(screen.getByRole("button", { name: "Apply subscription filter" }));
    expect(liveMock.subscribe).toHaveBeenCalledWith("#");

    // Children are only mounted while their branch is open, so expand first —
    // the same collapse behaviour the module page's tree had.
    fireEvent.click(screen.getByRole("button", { name: "Expand example" }));
    // The rail's asset name IS the focus control; copy is a labelled icon.
    fireEvent.click(await screen.findByRole("button", { name: "Focus AHU-01" }));
    expect(liveMock.focus).toHaveBeenCalledWith("AHU-01");
    expect(
      screen.getByRole("button", { name: "Copy topic example/AHU-01" }),
    ).toBeInTheDocument();
  });

  it("closes the focused panel locally, because the sidecar has no unfocus call", async () => {
    liveState = {
      phase: "live",
      session: liveSession,
      snapshot: { ...liveSnapshot, focused: focusedAsset },
    };
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    // Two panels can sit on this page (live focus + captured row), so each is
    // addressed by its own accessible name rather than by role alone.
    const panel = await screen.findByRole("complementary", { name: "AHU-01" });

    fireEvent.click(within(panel).getByRole("button", { name: "Close detail panel" }));

    expect(
      await screen.findByText("Select a topic or asset to inspect its live payload and points."),
    ).toBeInTheDocument();
    // Close is local state, not a request the backend would ignore.
    expect(liveMock.focus).not.toHaveBeenCalled();

    // Re-focusing the SAME asset must reopen it: the snapshot does not change,
    // so nothing but the focus handler can clear the dismissal.
    fireEvent.click(screen.getByRole("button", { name: "Expand example" }));
    fireEvent.click(await screen.findByRole("button", { name: "Focus AHU-01" }));
    expect(await screen.findByRole("complementary", { name: "AHU-01" })).toBeInTheDocument();
    expect(liveMock.focus).toHaveBeenCalledWith("AHU-01");
  });

  it("returns focus to the control that opened the panel", async () => {
    liveState = { phase: "live", session: liveSession, snapshot: liveSnapshot };
    stubFetch({ runs: [] });
    const view = render(scannerProviders(<MqttScannerPage />));

    fireEvent.click(await screen.findByRole("button", { name: "Expand example" }));
    const focusButton = await screen.findByRole("button", { name: "Focus AHU-01" });
    focusButton.focus();
    fireEvent.click(focusButton);

    // The snapshot now carries the focused asset, as the stream would deliver it.
    liveState = {
      phase: "live",
      session: liveSession,
      snapshot: { ...liveSnapshot, focused: focusedAsset },
    };
    view.rerender(scannerProviders(<MqttScannerPage />));

    const panel = await screen.findByRole("complementary", { name: "AHU-01" });
    fireEvent.click(within(panel).getByRole("button", { name: "Close detail panel" }));

    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Focus AHU-01" })),
    );
  });

  it("saves the live session as a register and reports what was accepted", async () => {
    liveState = {
      phase: "live",
      session: liveSession,
      snapshot: { ...liveSnapshot, focused: focusedAsset },
    };
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    fireEvent.click(await screen.findByRole("button", { name: "Save as register" }));

    expect(await screen.findByText(/4 of 4 rows accepted \(imp-live-1\)/)).toBeInTheDocument();
    expect(
      screen.getByText(/The live tree now compares against it, and so will the next MQTT capture/),
    ).toBeInTheDocument();
  });

  it("opens the publish modal prefilled from Write config", async () => {
    liveState = {
      phase: "live",
      session: liveSession,
      snapshot: { ...liveSnapshot, focused: focusedAsset },
    };
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    fireEvent.click(await screen.findByRole("button", { name: "Write config…" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText(/Topic/i)).toHaveValue("example/AHU-01/config");
    expect(within(dialog).getByLabelText(/Payload/i)).toHaveValue('{"version":"1.5.2"}');
  });

  it("offers a take-over instead of stealing an occupied session", async () => {
    liveState = {
      phase: "occupied",
      status: { session: { ...liveSession, owner: "another-operator" }, sidecar_available: true, connection: null, stats: null, register: null },
    };
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    expect(await screen.findByText("A live session is already open")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Take over" }));
    expect(liveMock.start).toHaveBeenCalledWith({ takeOver: true });
  });

  it("posts exactly the capture parameters the sidecar lane expects", async () => {
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    fireEvent.change(await screen.findByLabelText(/Topic filter/), {
      target: { value: "example/#" },
    });
    fireEvent.change(screen.getByLabelText(/Run time \(blank/), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Run time unit"), { target: { value: "minutes" } });

    fireEvent.click(screen.getByRole("button", { name: "Record capture" }));

    await waitFor(() => expect(startBody).not.toBeNull());
    expect(startBody?.job_type).toBe("mqtt_scanner");
    // Exactly what the module page posted for this lane: the authorization
    // shorthand, the window in seconds, the topic filter, and the client's own
    // requested_from stamp. No ignore_register (mqtt_sidecar has no such key).
    expect(startBody?.parameters).toEqual({
      authorized: true,
      capture_seconds: 120,
      requested_from: "frontend-review",
      topic_filter: "example/#",
    });
  });

  it("blocks a capture over the 15-minute window cap", async () => {
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    fireEvent.change(await screen.findByLabelText(/Run time \(blank/), { target: { value: "20" } });
    fireEvent.change(screen.getByLabelText("Run time unit"), { target: { value: "minutes" } });

    const start = screen.getByRole("button", { name: "Record capture" });
    await waitFor(() => expect(start).toBeDisabled());
    // An operator fault: red and assertive.
    expect(screen.getByRole("alert")).toHaveTextContent(/15-minute scanner capture limit/);
  });

  it("says the live view holds the connection as a status, not a red alert", async () => {
    // Under live-first this block is the page's RESTING state, so an assertive
    // alert would shout on every visit.
    liveState = { phase: "live", session: liveSession, snapshot: liveSnapshot };
    stubFetch({ runs: [] });
    render(scannerProviders(<MqttScannerPage />));

    expect(await screen.findByText("Stop the live view before capturing")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record capture" })).toBeDisabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders the captured-topics card with pills, chips, columns and the archive link", async () => {
    stubFetch();
    render(scannerProviders(<MqttScannerPage />));

    const heading = await screen.findByRole("heading", { name: "Captured topics" });
    const card = heading.closest("section") as HTMLElement;
    for (const pill of ["3 Topics", "2 Assets", "2 Match", "1 Rogue"]) {
      expect(await within(card).findByText(pill)).toBeInTheDocument();
    }
    for (const column of ["Topic", "Ret", "QoS", "JSON size", "Last value", "Register Match"]) {
      expect(within(card).getByRole("columnheader", { name: column })).toBeInTheDocument();
    }

    expect(within(card).getByText("example/AHU-01/pointset")).toBeInTheDocument();
    expect(within(card).getByText("In register (wildcard example/AHU-01/#)")).toBeInTheDocument();
    const rogueRow = within(card).getByText("example/ROGUE-09/state").closest("tr") as HTMLElement;
    expect(rogueRow.className).toContain("row-fail");
    expect(within(rogueRow).getByText("Not in register")).toBeInTheDocument();

    expect(within(card).getByRole("button", { name: "Export archive" })).toBeInTheDocument();
    expect(within(card).getByText(/2 topics match the register/)).toBeInTheDocument();

    // Chip filter: "Not in register" leaves only the rogue row.
    fireEvent.click(within(card).getByRole("button", { name: /Not in register/ }));
    await waitFor(() =>
      expect(within(card).queryByText("example/AHU-01/pointset")).not.toBeInTheDocument(),
    );
    expect(within(card).getByText("example/ROGUE-09/state")).toBeInTheDocument();
  });

  it("saves the capture as a register and offers the register CSV", async () => {
    stubFetch();
    render(scannerProviders(<MqttScannerPage />));

    const save = await screen.findByRole("button", {
      name: /Save capture as register \(applies to the next capture\)/,
    });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);

    expect(await screen.findByText(/Saved as register/)).toBeInTheDocument();
    expect(
      screen.getByText(
        /It is stored here and applies automatically to the next MQTT capture for this project and site\./,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download register CSV" })).toBeInTheDocument();
  });

  it("opens a captured topic in the side panel with its metadata and payload", async () => {
    stubFetch();
    render(scannerProviders(<MqttScannerPage />));

    const topic = await screen.findByText("example/AHU-01/pointset");
    fireEvent.click(topic.closest("tr") as HTMLElement);

    const panel = await screen.findByRole("complementary", { name: "example/AHU-01/pointset" });
    expect(within(panel).getByText("AHU-01")).toBeInTheDocument();
    expect(within(panel).getByText("12")).toBeInTheDocument();
    // Delivery QoS (1) and the run's subscription QoS cap (1) are both named.
    expect(within(panel).getAllByText("1").length).toBeGreaterThan(0);
    expect(within(panel).getByText("Yes")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Copy raw" })).toBeInTheDocument();
  });

  it("keeps the register import card and names the run in the footer", async () => {
    stubFetch();
    render(scannerProviders(<MqttScannerPage />));

    expect(await screen.findByRole("heading", { name: "Register import" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload and validate" })).toBeInTheDocument();
    expect(await screen.findByText("mqtt_scanner")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View in Run History" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in Reports" })).toBeInTheDocument();
  });

});

describe("MQTT capture row projection", () => {
  // "JSON size" must describe the same text "Last value" renders: the wrapper
  // the engine stores around a scalar is not what the operator is looking at.
  const project = (lastPayload: unknown) =>
    scannerRowsFromResults("mqtt", {
      ...results,
      topics: [{ ...results.topics[0], last_payload: lastPayload }],
    } as never)[0];

  it("sizes a scalar payload by its unwrapped text, not the stored wrapper", () => {
    const row = project({ _value: 42 });
    expect(row.cells["Last value"].text).toBe("42");
    expect(row.cells["JSON size"].text).toBe("2");
  });

  it("sizes an object payload by its compact JSON", () => {
    const row = project({ temp: 18.4 });
    expect(row.cells["Last value"].text).toBe('{"temp":18.4}');
    expect(row.cells["JSON size"].text).toBe("13");
  });

  it("sizes an empty object as the two braces it renders", () => {
    const row = project({});
    expect(row.cells["Last value"].text).toBe("{}");
    expect(row.cells["JSON size"].text).toBe("2");
  });

  it("reports no size for a non-JSON payload the engine did not store", () => {
    const row = project({ _raw_present: true });
    expect(row.cells["Last value"].text).toBe("non-JSON (not stored)");
    expect(row.cells["JSON size"].text).toBe("—");
  });
});
