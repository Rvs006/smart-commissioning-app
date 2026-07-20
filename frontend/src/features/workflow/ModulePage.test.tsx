import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { clearApiKey, setApiKey } from "../../api/client";
import { SessionProvider } from "../../app/session";
import { ModulePage } from "./ModulePage";

// The engineer-gated controls (Queue, Upload, Publish, Cancel) require a known
// engineer+ role. These wiring tests set a key and stub /me as engineer so the
// existing engineer behaviour is exercised. A separate role test below covers
// the viewer (gated) and engineer (enabled) paths explicitly.
const mePayload = { username: "engineer-1", role: "engineer", source: "user_key" };

const profilesPayload = [
  {
    import_type: "ip_register",
    description: "Expected IP-addressable assets.",
    required_columns: ["asset_id", "ip_address"],
    duplicate_key_fields: ["asset_id"],
  },
];

const acceptedRun = {
  run_id: "run-ip-1",
  job_type: "ip_discovery",
  status: "queued",
  message: "IP discovery accepted.",
};

const terminalRun = {
  run_id: "run-ip-1",
  job_type: "ip_discovery",
  status: "succeeded",
  stage: "register_comparison",
  progress_percent: 100,
  created_at: "2026-06-11T09:00:00Z",
  updated_at: "2026-06-11T09:05:00Z",
  project_id: "demo-project",
  site_id: "demo-site",
  parameters: {},
  result_summary: { hosts_responsive: 1, hosts_scanned: 3 },
  error_message: null,
};

const resultsPayload = {
  run_id: "run-ip-1",
  job_type: "ip_discovery",
  status: "succeeded",
  result_summary: { hosts_responsive: 1, hosts_scanned: 3 },
  discovered_assets: [
    {
      asset_id: null,
      ip_address: "10.10.25.214",
      mac_address: "C0:A6:F3:F2:F3:2F",
      hostname: "plant-controller",
      observed_ports: [{ port: 443, protocol: "tcp", service: "https" }],
      match_basis: "ip",
      last_seen_at: "2026-06-11T09:05:00Z",
      status_detail: "responsive: 443",
    },
  ],
  devices: [],
  points: [],
  topics: [],
};

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => payload,
  } as unknown as Response;
}

function renderModule(route: string) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  // A key is set so the SessionProvider fetches /me; the stubs below return an
  // engineer role, matching the pre-RBAC behaviour these wiring tests assert.
  setApiKey("engineer-key");
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionProvider>
        <MemoryRouter>
          <ModulePage moduleRoute={route} />
        </MemoryRouter>
      </SessionProvider>
    </QueryClientProvider>,
  );
}

describe("ModulePage discovery wiring", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    clearApiKey();
  });

  it("blocks a real scan until authorization is confirmed, then queues and renders live results", async () => {
    // A sweep that now reports every scanned host: the responder plus an
    // unregistered silent host (neutral) and a register-expected silent host
    // (amber/inconclusive). The engine emits "no response on scanned ports" —
    // a TCP-connect miss, never proof a host is absent.
    const liveResultsPayload = {
      ...resultsPayload,
      result_summary: { hosts_responsive: 1, hosts_scanned: 3 },
      discovered_assets: [
        ...resultsPayload.discovered_assets,
        {
          asset_id: null,
          ip_address: "10.10.25.9",
          mac_address: null,
          hostname: null,
          observed_ports: [],
          match_basis: "none",
          last_seen_at: null,
          status_detail: "no response on scanned ports (4 probed)",
        },
        {
          asset_id: "AHU-7",
          ip_address: "10.10.25.11",
          mac_address: null,
          hostname: null,
          observed_ports: [],
          match_basis: "none",
          last_seen_at: null,
          status_detail:
            "no response on scanned ports (2 probed) | EXPECTED BY REGISTER: expected from the " +
            "register import but did not answer this scan — inconclusive, not proof the host is offline",
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(liveResultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner");

    // The real-scan run button is disabled until the operator confirms.
    const queueButton = await screen.findByRole("button", { name: "Run" });
    expect(queueButton).toBeDisabled();

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    // Enabled once the engineer role resolves (/me) and auth is confirmed.
    await waitFor(() => expect(queueButton).toBeEnabled());

    fireEvent.click(queueButton);

    // Run monitor appears and live discovered hosts render from the results payload.
    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();
    // hostname is unique to the live results payload (not present in sample rows);
    // it now appears in both the results table and the selected-result detail aside.
    expect((await screen.findAllByText("plant-controller")).length).toBeGreaterThan(0);
    // Live banner is shown (its ip-scanner copy still opens with this phrase).
    expect(screen.getByText(/Live discovery observations/i)).toBeInTheDocument();
    // The Result column now reports each host's scan verdict.
    expect(screen.getByRole("columnheader", { name: "Result" })).toBeInTheDocument();
    // Both silent hosts surface the honest "no response" copy.
    expect((await screen.findAllByText("No response on scanned ports")).length).toBeGreaterThan(0);
    // jsdom cannot see theme CSS, so assert on classNames only: the register-
    // expected silent host shades amber (warn); the plain responder and the
    // unregistered silent host carry no pass/fail shading.
    expect(document.querySelector("tr.row-warn")).not.toBeNull();
    expect(document.querySelector("tr.row-pass, tr.row-fail")).toBeNull();

    // Headline metric now reflects the real run (hosts_responsive: 1), never the
    // old hardcoded "118" sample.
    expect(await screen.findByText("responsive hosts")).toBeInTheDocument();
    expect(screen.queryByText("118")).not.toBeInTheDocument();

    // A run the operator started here auto-advances to Results on success. Only
    // a *restored* run is exempt (see the run retention suite below).
    await waitFor(() =>
      expect(document.querySelector(".module-steps")).toHaveAttribute("data-step", "results"),
    );
  });

  it("renders import warnings as a non-blocking amber panel distinct from errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/imports") && init?.method === "POST") {
          return jsonResponse({
            import_id: "import-ip-1",
            import_type: "ip_register",
            file_name: "ip_register.csv",
            file_type: "csv",
            project_id: "demo-project",
            site_id: "demo-site",
            total_rows: 1,
            accepted_rows: 1,
            rejected_rows: 0,
            status: "accepted",
            missing_columns: [],
            warnings: [
              {
                row_number: 2,
                field: "Expected services/ports",
                code: "udp_port_not_verified",
                message:
                  "47808/udp is a UDP service — the IP scan verifies TCP ports only. UDP 47808 (BACnet/IP) is verified by the BACnet discovery run.",
              },
            ],
            stored_file_name: "import-ip-1.csv",
            created_at: "2026-07-14T09:00:00Z",
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner");

    fireEvent.change(await screen.findByLabelText(/CSV or XLSX file/i), {
      target: { files: [new File(["reg"], "ip_register.csv")] },
    });
    const upload = screen.getByRole("button", { name: "Upload and validate" });
    await waitFor(() => expect(upload).toBeEnabled());
    fireEvent.click(upload);

    // The import itself stays ACCEPTED; the UDP note arrives as a warning.
    expect(await screen.findByText("ACCEPTED")).toBeInTheDocument();
    const warningPanel = screen
      .getByText(/UDP 47808 \(BACnet\/IP\) is verified by the BACnet discovery run/i)
      .closest(".state-panel");
    expect(warningPanel).toHaveClass("warning");
    expect(warningPanel).not.toHaveClass("error");
    expect(warningPanel).not.toHaveClass("rejected");
    expect(screen.getByText(/Row 2:/)).toBeInTheDocument();
    expect(screen.getByText(/affected rows are still accepted/i)).toBeInTheDocument();
  });

  const latestImportSummary = {
    import_id: "import-ip-9",
    import_type: "ip_register",
    file_name: "ip_register.csv",
    file_type: "csv",
    project_id: "demo-project",
    site_id: "demo-site",
    total_rows: 12,
    accepted_rows: 12,
    rejected_rows: 0,
    status: "accepted",
    missing_columns: [],
    stored_file_name: "import-ip-9.csv",
    created_at: "2026-07-16T09:00:00Z",
  };

  function stubLatestImportFetch(latest: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.includes("/api/v1/imports/latest")) return jsonResponse(latest);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  it("shows a server-truth 'already imported' note when no file is staged (ISSUE-5)", async () => {
    stubLatestImportFetch(latestImportSummary);

    renderModule("ip-scanner");

    // The empty file input no longer implies nothing was uploaded: the note names
    // the stored register and states it is persisted and used by runs here.
    expect(await screen.findByText("Register already imported")).toBeInTheDocument();
    expect(screen.getByText(/12 of 12 rows accepted/i)).toBeInTheDocument();
    expect(screen.getByText(/stored[\s\S]*used by runs on this page/i)).toBeInTheDocument();
  });

  it("hides the 'already imported' note while a new file is staged (ISSUE-5)", async () => {
    stubLatestImportFetch(latestImportSummary);

    renderModule("ip-scanner");

    expect(await screen.findByText("Register already imported")).toBeInTheDocument();
    // Staging a file replaces the server-truth note with the in-session
    // "Selected: ..." line, so the two never both claim the current state.
    fireEvent.change(await screen.findByLabelText(/CSV or XLSX file/i), {
      target: { files: [new File(["reg"], "new_ip_register.csv")] },
    });
    expect(screen.queryByText("Register already imported")).not.toBeInTheDocument();
    expect(screen.getByText(/Selected: new_ip_register\.csv/i)).toBeInTheDocument();
  });

  it("renders nothing extra when the server reports no prior import (ISSUE-5)", async () => {
    // getLatestImport maps a 404 to null; the note must not render on an empty
    // result — an empty file input is the honest state when nothing is on file.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.includes("/api/v1/imports/latest")) {
          return { ok: false, status: 404, statusText: "Not Found", json: async () => ({ detail: "none" }) } as unknown as Response;
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner");

    // The profiles load resolves the card; the note is absent.
    expect(await screen.findByRole("button", { name: "Upload and validate" })).toBeInTheDocument();
    expect(screen.queryByText("Register already imported")).not.toBeInTheDocument();
  });

  it("sends a CIDR target override as parameters.cidr with no addresses key and no fabricated authorization principal", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          postedBody = JSON.parse(String(init.body)) as { parameters: Record<string, unknown> };
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(resultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner");

    // Type an ad-hoc CIDR override and authorize the real scan, mirroring the
    // authorized-scan test above.
    fireEvent.change(screen.getByLabelText(/Target override/i), {
      target: { value: "10.20.0.0/24" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));

    const queueButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(queueButton).toBeEnabled());
    fireEvent.click(queueButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> }).parameters;
    // CIDR override flows through as parameters.cidr; the single-address branch
    // is untouched, so no addresses key is sent.
    expect(parameters.cidr).toBe("10.20.0.0/24");
    expect(parameters).not.toHaveProperty("addresses");
    expect(parameters).not.toHaveProperty("start");
    expect(parameters).not.toHaveProperty("end");
    // Fix 6: only the boolean shorthand is sent; the backend stamps the real
    // authenticated principal, so no fabricated scan_authorization block.
    expect(parameters.authorized).toBe(true);
    expect(parameters).not.toHaveProperty("scan_authorization");
  });

  const mqttAccepted = {
    run_id: "run-mqtt-1",
    job_type: "mqtt_discovery",
    status: "queued",
    message: "MQTT discovery accepted.",
  };

  const mqttTerminal = {
    run_id: "run-mqtt-1",
    job_type: "mqtt_discovery",
    status: "succeeded",
    stage: "capture",
    progress_percent: 100,
    created_at: "2026-07-15T09:00:00Z",
    updated_at: "2026-07-15T09:05:00Z",
    project_id: "demo-project",
    site_id: "demo-site",
    parameters: {},
    result_summary: { topics_discovered: 0, messages_captured: 0 },
    error_message: null,
  };

  function stubMqttRunFetch(onPost: (body: { parameters: Record<string, unknown> }) => void) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          onPost(JSON.parse(String(init.body)) as { parameters: Record<string, unknown> });
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse({ ...resultsPayload, run_id: "run-mqtt-1", discovered_assets: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse(mqttTerminal);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  it("converts an hours capture duration to seconds on the MQTT discovery wire", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    stubMqttRunFetch((body) => {
      postedBody = body;
    });

    renderModule("mqtt-discovery");

    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), {
      target: { value: "2" },
    });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), {
      target: { value: "hours" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));

    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> }).parameters;
    expect(parameters.capture_seconds).toBe(7200);
  });

  it("refuses an MQTT capture duration over the 48-hour cap without posting", async () => {
    let posted = false;
    stubMqttRunFetch(() => {
      posted = true;
    });

    renderModule("mqtt-discovery");

    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), {
      target: { value: "49" },
    });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), {
      target: { value: "hours" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));

    expect(await screen.findByText(/exceeds the 48-hour capture limit/i)).toBeInTheDocument();
    const runButton = screen.getByRole("button", { name: "Run" });
    expect(runButton).toBeDisabled();
    fireEvent.click(runButton);
    expect(posted).toBe(false);
  });

  it("keeps a blank MQTT capture duration as the 0 indefinite sentinel regardless of unit", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    stubMqttRunFetch((body) => {
      postedBody = body;
    });

    renderModule("mqtt-discovery");

    // Clear the default "10" so the duration is blank, then pick an hours unit:
    // the unit multiplier must not turn a blank (indefinite) into a bounded 0.
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), {
      target: { value: "" },
    });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), {
      target: { value: "hours" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));

    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> }).parameters;
    expect(parameters.capture_seconds).toBe(0);
  });

  it("omits topic_filter from the MQTT run when the filter is left blank so the engine captures every topic (#) (2026-07-20 walkthrough ITEM-2)", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    stubMqttRunFetch((body) => {
      postedBody = body;
    });

    renderModule("mqtt-discovery");

    // Do NOT touch the topic filter: it defaults to blank. Root Topic was removed
    // from Configuration, so a blank filter is omitted from the run parameters
    // entirely and the engine falls back to its own "#" default (capture-all) —
    // never a literal "#" on the wire.
    fireEvent.click(await screen.findByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> }).parameters;
    expect(parameters).not.toHaveProperty("topic_filter");
  });

  it("sends an explicit MQTT topic filter verbatim when the operator types one (ISSUE-3)", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    stubMqttRunFetch((body) => {
      postedBody = body;
    });

    renderModule("mqtt-discovery");

    // An operator who wants a full-wildcard or scoped capture types it explicitly;
    // it flows through unchanged as the run's topic_filter override.
    fireEvent.change(await screen.findByLabelText(/Topic filter/i), {
      target: { value: "site/asset-1/#" },
    });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> }).parameters;
    expect(parameters.topic_filter).toBe("site/asset-1/#");
  });

  it("selects an MQTT topic row to inspect its real payload with honest metadata and no fabricated issues", async () => {
    const mqttResultsPayload = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      // subscribe_qos 0 is the delivery-QoS cap the run requested.
      result_summary: { topics_discovered: 2, messages_captured: 5, subscribe_qos: 0 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "udmi/AHU-1/state",
          message_count: 3,
          last_payload: { online: true, firmware: "1.2.3" },
          created_at: "2026-07-15T09:05:00Z",
          attributes: {
            device_ref: "AHU-1",
            position: 0,
            last_retained: true,
            last_qos: 1,
            last_received_at: "2026-07-15T10:00:00+00:00",
          },
        },
        {
          topic: "sensors/raw/blob",
          message_count: 1,
          // Engine presence marker for a non-JSON payload — no raw bytes stored.
          last_payload: { _raw_present: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: {
            device_ref: null,
            position: 1,
            last_retained: false,
            last_qos: 0,
            last_received_at: "2026-07-15T10:01:00+00:00",
          },
        },
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(mqttResultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({ ...mqttTerminal, result_summary: mqttResultsPayload.result_summary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    // Table populates; the inspector defaults to the first row (AHU-1, JSON),
    // so its payload panel (unique heading) and JSON tree are shown up front.
    expect(await screen.findByText(/Last payload on udmi\/AHU-1\/state/)).toBeInTheDocument();
    expect(screen.getByText("Explore JSON tree")).toBeInTheDocument();

    // The fabricated sample MQTT issue must NOT render on this live discovery
    // route. The operatorData issueRows fixture and the workspace?.issues
    // fallback are deleted outright now; this stays as the end-state guard.
    expect(
      screen.queryByText(/Telemetry interval exceeds configured tolerance/i),
    ).not.toBeInTheDocument();

    // Clicking the SECOND row's <tr> (via a cell) moves the inspector to that
    // topic; the non-JSON marker shows an honest sentence and NO json tree.
    // Capture the row before selecting (its topic string becomes ambiguous once
    // the inspector also echoes it).
    const rawCell = screen.getByText("sensors/raw/blob");
    const rawRow = rawCell.closest("tr");
    fireEvent.click(rawCell);
    expect(await screen.findByText(/Non-JSON payload observed/i)).toBeInTheDocument();
    expect(screen.queryByText("Explore JSON tree")).not.toBeInTheDocument();

    // The clicked row carries the selection class (drives the inspector without
    // opening the View modal).
    expect(rawRow?.className).toContain("row-selected");

    // Metadata detail items are present with honesty-rule labels; a timestamp is
    // NEVER labelled "Published" (MQTT 3.1.1 carries no publish time on the wire).
    expect(screen.getByText("Retained")).toBeInTheDocument();
    expect(screen.getByText("Delivery QoS")).toBeInTheDocument();
    expect(screen.getByText("Received at")).toBeInTheDocument();
    expect(screen.queryByText("Published")).not.toBeInTheDocument();
  });

  it("filters the results table by text, preserves selection, and never shows the scan empty state for a filter miss (ISSUE-4)", async () => {
    const mqttResultsPayload = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      result_summary: { topics_discovered: 2, messages_captured: 5 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "udmi/AHU-1/state",
          message_count: 3,
          last_payload: { online: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: "AHU-1", position: 0 },
        },
        {
          topic: "sensors/raw/blob",
          message_count: 1,
          last_payload: { _raw_present: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: null, position: 1 },
        },
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(mqttResultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({ ...mqttTerminal, result_summary: mqttResultsPayload.result_summary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    // Both topic rows land and the count line reports the full set.
    expect(await screen.findByText("sensors/raw/blob")).toBeInTheDocument();
    expect(screen.getByText(/Showing 2 of 2 rows/)).toBeInTheDocument();

    // Filter to the blob topic: the AHU row leaves the table, the count updates,
    // and selection follows to the only visible row (its inspector heading).
    fireEvent.change(screen.getByLabelText(/Filter results/i), { target: { value: "sensors" } });
    expect(screen.getByText(/Showing 1 of 2 rows/)).toBeInTheDocument();
    // Selection follows to the only visible row before asserting the AHU row is
    // fully gone (table cell AND its inspector echo).
    expect(await screen.findByText(/Last payload on sensors\/raw\/blob/)).toBeInTheDocument();
    expect(screen.queryByText("udmi/AHU-1/state")).not.toBeInTheDocument();

    // A filter that matches nothing shows the filter-specific note — NEVER the
    // scan empty state, whose copy would assert something about the network.
    fireEvent.change(screen.getByLabelText(/Filter results/i), { target: { value: "zzz-none" } });
    expect(screen.getByText("No rows match the current filters")).toBeInTheDocument();
    expect(screen.queryByText(/Capture complete/i)).not.toBeInTheDocument();
    expect(screen.queryByText("No results yet")).not.toBeInTheDocument();
    expect(screen.getByText(/Showing 0 of 2 rows/)).toBeInTheDocument();
    // The Inspector must not keep the previously-selected (now hidden) topic's
    // payload on screen while the table reports zero matches: with nothing
    // visible the selection is null and the aside falls back to its own empty
    // state (ISSUE-4).
    expect(screen.queryByText(/Last payload on sensors\/raw\/blob/)).not.toBeInTheDocument();
    expect(screen.getByText("No topic selected")).toBeInTheDocument();

    // Clearing restores every row.
    fireEvent.click(screen.getByRole("button", { name: /Clear filters/i }));
    expect(screen.getByText(/Showing 2 of 2 rows/)).toBeInTheDocument();
    expect(screen.getByText("udmi/AHU-1/state")).toBeInTheDocument();
  });

  it("filters the results table by verdict tone on the MQTT route (ISSUE-4)", async () => {
    const mqttResultsPayload = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      result_summary: { topics_discovered: 2, messages_captured: 5 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "site/asset-1/state",
          message_count: 3,
          last_payload: { online: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: "AHU-1", position: 0, register_match: "matched" },
        },
        {
          topic: "rogue/asset-9/state",
          message_count: 1,
          last_payload: { online: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: null, position: 1, register_match: "unmatched" },
        },
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(mqttResultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({ ...mqttTerminal, result_summary: mqttResultsPayload.result_summary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    expect(await screen.findByText("rogue/asset-9/state")).toBeInTheDocument();

    // "Not in register" keeps only the unmatched (fail-tone) row.
    fireEvent.change(screen.getByLabelText(/Verdict/i), { target: { value: "fail" } });
    expect(screen.getByText(/Showing 1 of 2 rows/)).toBeInTheDocument();
    expect(screen.queryByText("site/asset-1/state")).not.toBeInTheDocument();
    // Present in the table cell (and, since selection follows, the inspector).
    expect(screen.getAllByText("rogue/asset-9/state").length).toBeGreaterThan(0);
  });

  it("shows 'Not recorded' MQTT metadata for a run that predates metadata capture", async () => {
    // An old persisted run: topics carry no last_retained/last_qos/last_received_at
    // and the summary has no subscribe_qos. The inspector must render without
    // crashing and never fabricate values.
    const legacyResults = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      result_summary: { topics_discovered: 1, messages_captured: 2 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "udmi/AHU-1/state",
          message_count: 2,
          last_payload: { online: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: "AHU-1", position: 0 },
        },
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(legacyResults);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({ ...mqttTerminal, result_summary: legacyResults.result_summary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    // The topic appears in both the table cell and the inspector — wait for it.
    expect((await screen.findAllByText("udmi/AHU-1/state")).length).toBeGreaterThan(0);
    // The metadata labels render, and the values honestly read "Not recorded".
    expect(screen.getByText("Retained")).toBeInTheDocument();
    expect(screen.getAllByText(/Not recorded/).length).toBeGreaterThan(0);
  });

  it("shades register-matched and register-foreign MQTT rows and shows the compare banner", async () => {
    const comparedResults = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      result_summary: { topics_discovered: 2, messages_captured: 4 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "334os/b1/ahu-1/state",
          message_count: 3,
          last_payload: { online: true },
          created_at: "2026-07-15T09:05:00Z",
          attributes: {
            device_ref: "AHU-1",
            position: 0,
            register_match: "matched",
            register_matched_filter: "334os/b1/ahu-1/#",
            register_asset_id: "AHU-1",
          },
        },
        {
          topic: "334os/rogue/x/state",
          message_count: 1,
          last_payload: { present_value: 1 },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: null, position: 1, register_match: "unmatched" },
        },
      ],
      register_comparison: {
        register_available: true,
        import_filename: "register.csv",
        matched_count: 1,
        unmatched_count: 1,
        expected_filter_count: 2,
        unobserved_filters: [{ asset_id: "FCU-2", filter: "334os/b1/fcu-2/state" }],
      },
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(comparedResults);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({ ...mqttTerminal, result_summary: comparedResults.result_summary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    // Banner switches to the register-comparison copy (route-aware).
    expect(
      await screen.findByText(/Green rows match a topic in the uploaded MQTT register/),
    ).toBeInTheDocument();
    // The counts note is present on its own line.
    expect(screen.getByText(/1 topic matches the register/)).toBeInTheDocument();
    expect(screen.getByText(/334os\/b1\/fcu-2\/state/)).toBeInTheDocument();

    // The matched row shades green, the foreign row red. Assert on classes only
    // (jsdom cannot see the theme CSS that hides/reveals rows).
    const passRow = document.querySelector("tr.row-pass");
    const failRow = document.querySelector("tr.row-fail");
    expect(passRow?.textContent).toContain("334os/b1/ahu-1/state");
    expect(failRow?.textContent).toContain("334os/rogue/x/state");
  });

  it("prompts to upload a register when no MQTT register import exists", async () => {
    const noRegisterResults = {
      run_id: "run-mqtt-1",
      job_type: "mqtt_discovery",
      status: "succeeded",
      result_summary: { topics_discovered: 1, messages_captured: 1 },
      discovered_assets: [],
      devices: [],
      points: [],
      topics: [
        {
          topic: "334os/rogue/x/state",
          message_count: 1,
          last_payload: { present_value: 1 },
          created_at: "2026-07-15T09:05:00Z",
          attributes: { device_ref: null, position: 0 },
        },
      ],
      register_comparison: { register_available: false },
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/mqtt/runs") && init?.method === "POST") {
          return jsonResponse(mqttAccepted);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/topics")) {
          return jsonResponse({ run_id: "run-mqtt-1", topics: [] });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1/results")) {
          return jsonResponse(noRegisterResults);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-mqtt-1")) {
          return jsonResponse({ ...mqttTerminal, result_summary: noRegisterResults.result_summary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("mqtt-discovery");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    expect(
      await screen.findByText(/No accepted MQTT register import for this project\/site/),
    ).toBeInTheDocument();
    // No register means NO verdicts — never all-red.
    expect(document.querySelector("tr.row-pass, tr.row-fail")).toBeNull();
  });

  it("renders the MAC column and opens a per-host detail dialog from the row View button", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(resultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner");

    const queueButton = await screen.findByRole("button", { name: "Run" });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    await waitFor(() => expect(queueButton).toBeEnabled());
    fireEvent.click(queueButton);

    // The now-populated MAC column renders (header + the live cell value), proving
    // the engine's mac_address flows through to the table.
    expect(await screen.findByRole("columnheader", { name: "MAC Address" })).toBeInTheDocument();
    expect((await screen.findAllByText("C0:A6:F3:F2:F3:2F")).length).toBeGreaterThan(0);

    // Clicking the per-row "View" opens an unmistakable modal dialog whose labeled
    // fields include the real MAC and hostname for that host.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "View" })[0]);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("MAC Address")).toBeInTheDocument();
    expect(within(dialog).getByText("C0:A6:F3:F2:F3:2F")).toBeInTheDocument();
    expect(within(dialog).getByText("Hostname")).toBeInTheDocument();
    expect(within(dialog).getByText("plant-controller")).toBeInTheDocument();

    // Close returns to the table (dialog dismissed).
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("shows a neutral empty-state metric (no hardcoded sample) before any run", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        return jsonResponse({});
      }),
    );

    renderModule("ip-scanner");

    // Before any run the headline metric is a neutral empty state, NOT the old
    // hardcoded sample ("118" / "reachable hosts") that looked like a real scan.
    expect(await screen.findByText("No run yet")).toBeInTheDocument();
    expect(screen.queryByText("118")).not.toBeInTheDocument();
    expect(screen.queryByText("reachable hosts")).not.toBeInTheDocument();
  });

  it("shows a dry-run preview button that needs no authorization", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner");

    fireEvent.click(screen.getByLabelText(/Dry run/i));
    const previewButton = await screen.findByRole("button", { name: "Preview" });
    // Enabled once the engineer role resolves (no scan-auth needed for dry run).
    await waitFor(() => expect(previewButton).toBeEnabled());
  });

  // A scan that completed and genuinely found nothing used to land on the same
  // "No results yet" as a head that had never run (Pete 2026-07-15). These are
  // text-content assertions on the always-in-DOM results section: jsdom applies
  // no theme CSS, so step-gating visibility is not assertable here.
  it("states what was probed when a scan completes and finds nothing", async () => {
    const emptySummary = { hosts_responsive: 0, hosts_scanned: 254 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse({
            ...resultsPayload,
            result_summary: emptySummary,
            discovered_assets: [],
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse({ ...terminalRun, result_summary: emptySummary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const queueButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(queueButton).toBeEnabled());
    fireEvent.click(queueButton);

    expect(await screen.findByText(/Scan complete — no responsive hosts found/i)).toBeInTheDocument();
    expect(screen.getByText(/254 hosts probed/i)).toBeInTheDocument();
    expect(screen.queryByText("No results yet")).not.toBeInTheDocument();
    // Honesty: a succeeded run that observed nothing is a real observation and
    // must never be dressed up as a failure.
    expect(document.querySelector(".empty-workspace")?.textContent).not.toMatch(/fail/i);
  });

  it("labels an empty dry-run preview as a preview, not a negative finding", async () => {
    // The engine stamps hosts_scanned: 0 on a dry run because it sends no
    // packets; without the dry_run gate this would read as "0 hosts were
    // probed" — a network claim about a run that never touched the network.
    const dryRunSummary = { dry_run: true, hosts_responsive: 0, hosts_scanned: 0 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse({
            ...resultsPayload,
            result_summary: dryRunSummary,
            discovered_assets: [],
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse({ ...terminalRun, result_summary: dryRunSummary });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner");

    fireEvent.click(screen.getByLabelText(/Dry run/i));
    const previewButton = await screen.findByRole("button", { name: "Preview" });
    await waitFor(() => expect(previewButton).toBeEnabled());
    fireEvent.click(previewButton);

    expect(await screen.findByText(/Dry run complete — preview only/i)).toBeInTheDocument();
    expect(screen.queryByText(/no responsive hosts found/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/0 hosts were probed/i)).not.toBeInTheDocument();
  });
});

describe("ModulePage BACnet backend provenance", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  // Drives a BACnet discovery run to a terminal, succeeded state whose results
  // carry result_summary.backend, then returns once the live table is showing.
  async function runBacnetWithBackend(backend: string) {
    const bacnetResults = {
      run_id: "run-bacnet-1",
      job_type: "bacnet_discovery",
      status: "succeeded",
      result_summary: { device_count: 1, point_count: 0, backend },
      discovered_assets: [],
      devices: [
        {
          name: "Acme Controls",
          address: "10.0.0.5",
          vendor: "Acme",
          attributes: { device_instance: 1001 },
        },
      ],
      points: [],
      topics: [],
    };
    const bacnetTerminalRun = {
      run_id: "run-bacnet-1",
      job_type: "bacnet_discovery",
      status: "succeeded",
      stage: "discovery",
      progress_percent: 100,
      created_at: "2026-06-11T09:00:00Z",
      updated_at: "2026-06-11T09:05:00Z",
      project_id: "demo-project",
      site_id: "demo-site",
      parameters: {},
      result_summary: { device_count: 1, backend },
      error_message: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/bacnet/runs") && init?.method === "POST") {
          return jsonResponse({
            run_id: "run-bacnet-1",
            job_type: "bacnet_discovery",
            status: "queued",
            message: "BACnet discovery accepted.",
          });
        }
        if (url.endsWith("/api/v1/discovery/runs/run-bacnet-1/results")) {
          return jsonResponse(bacnetResults);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-bacnet-1")) {
          return jsonResponse(bacnetTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("bacnet-discovery");

    const runButton = await screen.findByRole("button", { name: "Run" });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    // The live table renders (Acme Controls only exists in the results payload).
    expect((await screen.findAllByText("Acme Controls")).length).toBeGreaterThan(0);
  }

  it("shows a prominent SIMULATED warning for a simulated backend", async () => {
    await runBacnetWithBackend("simulated");
    const warning = await screen.findByText(/SIMULATED — demo data, not a real BACnet scan\./i);
    expect(warning).toBeInTheDocument();
    // Honesty-critical: it is an assertive alert, styled distinctly (not the
    // neutral amber note), so simulated data cannot pass for a real scan.
    expect(warning).toHaveAttribute("role", "alert");
    expect(warning).toHaveClass("warning");
    expect(screen.queryByText(/Live bacpypes3 scan\./i)).not.toBeInTheDocument();
  });

  it("shows a subtle Live confirmation for a real bacpypes3 backend", async () => {
    await runBacnetWithBackend("bacpypes3");
    expect(await screen.findByText(/Live bacpypes3 scan\./i)).toBeInTheDocument();
    // The alarming simulated warning must NOT appear for a real scan.
    expect(
      screen.queryByText(/SIMULATED — demo data, not a real BACnet scan\./i),
    ).not.toBeInTheDocument();
  });

  // Lives here rather than with the label tests because the results table only
  // exists once a real run has produced rows — there is no sample table to read
  // the columns off any more.
  it("shows IP Address and Network Number columns on the live BACnet results table", async () => {
    await runBacnetWithBackend("bacpypes3");
    expect(await screen.findByRole("columnheader", { name: "IP Address" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Network Number" })).toBeInTheDocument();
  });
});

describe("ModulePage reports wiring", () => {
  // Mirrors the API projection: created_at + source_run_ids come back on every
  // report (GET /reports), which is what the Generated / Source runs columns read.
  const reportsPayload = {
    reports: [
      {
        report_id: "rep-1",
        report_type: "issue_report",
        output_format: "xlsx",
        status: "succeeded",
        file_name: "issue_report.xlsx",
        created_at: "2026-07-15T10:00:00Z",
        source_run_ids: ["run-1"],
      },
      {
        report_id: "rep-2",
        report_type: "evidence_pack",
        output_format: "docx",
        status: "queued",
        file_name: "evidence_pack.docx",
        created_at: "2026-07-15T11:30:00Z",
        source_run_ids: [],
      },
      // A SECOND succeeded report, deliberately not first in the list: the
      // per-row Download tests drive this one, so a row map that ignores its own
      // row and re-downloads liveReports[0] fails instead of passing by accident.
      {
        report_id: "rep-3",
        report_type: "evidence_pack",
        output_format: "zip",
        status: "succeeded",
        file_name: "handover_pack.zip",
        created_at: "2026-07-15T12:45:00Z",
        source_run_ids: ["run-2", "run-3"],
      },
    ],
  };

  // downloadFile() reads .blob() and the Content-Disposition header; the file's
  // jsonResponse helper models neither. Same hand-rolled-Response style.
  function blobResponse(filename: string): Response {
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      blob: async () => new Blob(["x"]),
      headers: {
        get: (name: string) =>
          name.toLowerCase() === "content-disposition"
            ? `attachment; filename="${filename}"`
            : null,
      },
    } as unknown as Response;
  }

  beforeEach(() => {
    // jsdom implements no object-URL APIs; triggerBlobDownload uses them.
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:mock"),
      revokeObjectURL: vi.fn(),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  it("disables Export until a report has been queued", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/reports")) {
          return jsonResponse({ reports: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("reports");

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Export" })).toBeDisabled();
    });
  });

  it("lists generated reports with per-report selection and an Export selected action", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/reports")) {
          return jsonResponse(reportsPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("reports");

    // Both reports listed; only the succeeded one is selectable for export.
    const succeededCheckbox = await screen.findByLabelText(/Select report issue_report\.xlsx/i);
    const queuedCheckbox = screen.getByLabelText(/Select report evidence_pack\.docx/i);
    expect(queuedCheckbox).toBeDisabled();

    const exportSelected = screen.getByRole("button", { name: "Export selected" });
    expect(exportSelected).toBeDisabled();

    fireEvent.click(succeededCheckbox);
    await waitFor(() => expect(exportSelected).toBeEnabled());
  });

  function stubReports(
    onDownload?: (url: string) => void,
    payload: unknown = reportsPayload,
  ) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (/\/api\/v1\/reports\/[^/]+\/download$/.test(url)) {
          onDownload?.(url);
          return blobResponse("issue_report.xlsx");
        }
        if (url.endsWith("/api/v1/reports")) {
          return jsonResponse(payload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  // A report is only traceable evidence if the operator can see WHEN it was cut
  // and WHICH runs fed it — that is the whole point for an ITP handover pack.
  it("shows a Generated timestamp and the source run ids for each report", async () => {
    stubReports();
    renderModule("reports");

    expect(await screen.findByRole("columnheader", { name: "Generated" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Source runs" })).toBeInTheDocument();

    // Assert against the app's own formatter so the check is locale/timezone
    // agnostic (same approach as RunHistoryPage.test.tsx).
    const generated = new Date("2026-07-15T10:00:00Z").toLocaleString();
    expect(screen.getByRole("cell", { name: generated })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "run-1" })).toBeInTheDocument();

    // A report scoped to no runs says so honestly rather than inventing a source.
    const queuedRow = screen.getByLabelText(/Select report evidence_pack\.docx/i).closest("tr")!;
    expect(within(queuedRow).getByRole("cell", { name: "—" })).toBeInTheDocument();
  });

  // Drives rep-3 (succeeded, but NOT the first row) so the click has to resolve
  // its own row's report id rather than falling back to the first one.
  it("downloads a single completed report from its own row", async () => {
    const downloaded: string[] = [];
    stubReports((url) => downloaded.push(url));
    renderModule("reports");

    const row = (await screen.findByLabelText(/Select report handover_pack\.zip/i)).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: "Download" }));

    await waitFor(() => expect(downloaded).toHaveLength(1));
    expect(downloaded[0]).toMatch(/\/api\/v1\/reports\/rep-3\/download$/);
    // The blob actually reached the browser's download path.
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  // Records every reports fetch so the export tests can assert exactly which
  // endpoint (bundle zip vs per-report download) each gesture hit.
  function stubReportsExport(hits: { download: string[]; export: string[] }) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.includes("/api/v1/reports/export?")) {
          hits.export.push(url);
          return blobResponse("reports_export.zip");
        }
        if (/\/api\/v1\/reports\/[^/]+\/download$/.test(url)) {
          hits.download.push(url);
          return blobResponse("issue_report.xlsx");
        }
        if (url.endsWith("/api/v1/reports")) {
          return jsonResponse(reportsPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  // Field bug (2026-07-20 item 13): a per-file download loop tripped the
  // browser's per-gesture throttle and kept only the last file. Several ticked
  // rows must now be ONE bundle request, not one download each.
  it("bundles multiple selected reports into a single export zip request", async () => {
    const hits = { download: [] as string[], export: [] as string[] };
    stubReportsExport(hits);
    renderModule("reports");

    fireEvent.click(await screen.findByLabelText(/Select report issue_report\.xlsx/i));
    fireEvent.click(screen.getByLabelText(/Select report handover_pack\.zip/i));
    fireEvent.click(screen.getByRole("button", { name: "Export selected" }));

    await waitFor(() => expect(hits.export).toHaveLength(1));
    // One request carrying every selected id, and zero per-report downloads.
    expect(hits.export[0]).toMatch(/report_id=rep-1/);
    expect(hits.export[0]).toMatch(/report_id=rep-3/);
    expect(hits.download).toHaveLength(0);
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  // Exactly one ticked report keeps the direct per-report download — a zip of
  // one is needless (their words: "a zip for multiples, direct for one is fine").
  it("downloads a single selected report directly, not through the export zip", async () => {
    const hits = { download: [] as string[], export: [] as string[] };
    stubReportsExport(hits);
    renderModule("reports");

    fireEvent.click(await screen.findByLabelText(/Select report handover_pack\.zip/i));
    fireEvent.click(screen.getByRole("button", { name: "Export selected" }));

    await waitFor(() => expect(hits.download).toHaveLength(1));
    expect(hits.download[0]).toMatch(/\/api\/v1\/reports\/rep-3\/download$/);
    expect(hits.export).toHaveLength(0);
  });

  // Only a succeeded report has real bytes behind it; offering a download for a
  // queued one would hand the operator an error, not a file.
  it("disables the row Download for a report that has not completed", async () => {
    stubReports();
    renderModule("reports");

    const queuedRow = (await screen.findByLabelText(/Select report evidence_pack\.docx/i)).closest(
      "tr",
    )!;
    expect(within(queuedRow).getByRole("button", { name: "Download" })).toBeDisabled();

    const succeededRow = screen.getByLabelText(/Select report issue_report\.xlsx/i).closest("tr")!;
    expect(within(succeededRow).getByRole("button", { name: "Download" })).toBeEnabled();
  });

  // End-state guard, NOT a guard on the fixture itself: item 8 already removed
  // the `workspace?.rows` fallback, so this passes even with the fabricated rows
  // present (verified by mutation). It earns its place by pinning the OUTCOME —
  // no invented report reaches the reports page by ANY route, including a
  // re-introduced fallback. operatorData.test.ts pins the source side: the
  // fixture rows/issues fields are deleted from moduleWorkspaces entirely.
  it("shows an honest empty state, with no fabricated report rows, when no report exists", async () => {
    stubReports(undefined, { reports: [] });
    renderModule("reports");

    expect(await screen.findByText("No reports yet")).toBeInTheDocument();
    for (const fabricated of [
      "Excel issue report",
      "Word handover report",
      "Blocked report",
      "commissioning_handover.docx",
      "Awaiting validation",
    ]) {
      expect(screen.queryByText(fabricated)).toBeNull();
    }
  });

  // An older backend (or a stale cached payload) carries neither new field; the
  // row must still render rather than throwing on .join of undefined.
  it("renders a report that carries neither created_at nor source_run_ids", async () => {
    stubReports(undefined, {
      reports: [
        {
          report_id: "rep-legacy",
          report_type: "issue_report",
          output_format: "xlsx",
          status: "succeeded",
          file_name: "legacy.xlsx",
        },
      ],
    });
    renderModule("reports");

    const row = (await screen.findByLabelText(/Select report legacy\.xlsx/i)).closest("tr")!;
    // Both new cells degrade to the em-dash rather than crashing the table.
    expect(within(row).getAllByRole("cell", { name: "—" })).toHaveLength(2);
    expect(within(row).getByRole("button", { name: "Download" })).toBeEnabled();
  });
});

describe("ModulePage labels and templates", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  function stubBasic() {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/reports")) {
          return jsonResponse({ reports: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  it("renames the discovery run action from Queue to Run", async () => {
    stubBasic();
    renderModule("ip-scanner");
    expect(await screen.findByText("Run IP Discovery")).toBeInTheDocument();
  });

  it("uses Generate (not Queue) for report run actions", async () => {
    stubBasic();
    renderModule("reports");
    expect(await screen.findByText("Generate Excel Report")).toBeInTheDocument();
    expect(screen.getByText("Generate Word Report")).toBeInTheDocument();
  });

  it("drops the duplicate all-templates section but keeps template downloads in Register Import (2026-07-20 walkthrough ITEM-3)", async () => {
    stubBasic();
    renderModule("data-validation");
    // The duplicate "Import Templates for This Page" section is removed.
    expect(await screen.findByText("Register Import")).toBeInTheDocument();
    expect(screen.queryByText("Import Templates for This Page")).not.toBeInTheDocument();
    // Templates remain downloadable from the Default import template card inside
    // Register Import — pick the import profile, then XLSX or CSV.
    expect(screen.getByText("Default import template")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download XLSX" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download CSV" })).toBeInTheDocument();
  });

  // The module hero renders `workspace?.title ?? module.title`, so the
  // operatorData workspace title shadows the moduleData one on every head that
  // has a workspace — i.e. all five. These assert the string an operator
  // actually reads, whichever layer supplies it.
  it("titles the ip-scanner hero 'IP Discovery', matching its menu entry", async () => {
    stubBasic();
    renderModule("ip-scanner");
    expect(await screen.findByRole("heading", { level: 2, name: "IP Discovery" })).toBeInTheDocument();
  });

  it("titles the bacnet-discovery hero 'BACnet Discovery'", async () => {
    stubBasic();
    renderModule("bacnet-discovery");
    expect(
      await screen.findByRole("heading", { level: 2, name: "BACnet Discovery" }),
    ).toBeInTheDocument();
  });

});

describe("ModulePage UDMI workbench live results", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  const udmiAccepted = {
    run_id: "run-udmi-1",
    job_type: "udmi_validation",
    status: "queued",
    message: "UDMI validation accepted.",
  };

  const udmiTerminalRun = {
    run_id: "run-udmi-1",
    job_type: "udmi_validation",
    status: "succeeded",
    stage: "udmi_fixture_validation_complete",
    progress_percent: 100,
    created_at: "2026-07-09T09:00:00Z",
    updated_at: "2026-07-09T09:05:00Z",
    project_id: "demo-project",
    site_id: "demo-site",
    parameters: {},
    result_summary: {
      expected_devices: 1,
      publishing_seen: 1,
      not_publishing: 0,
      issue_count: 1,
      message_count: 3,
      source: "schedule_payload_inputs",
      payload_view_source: "direct_inputs",
      capture_mode: "bounded",
      capture_window_seconds: 120,
      // False = genuinely bounded; true renders the inline-cap wording
      // ("capped at N s (indefinite requested; inline run)") instead.
      indefinite_bounded_inline: false,
      payload_views: [
        {
          asset_id: "EM-1",
          payload_types: [
            {
              payload_type: "pointset",
              expected: {
                timestamp: "<RFC 3339 timestamp>",
                version: "1.5.2",
                points: {
                  energy_sensor: { present_value: "<device-reported value>" },
                  // Expected-only point: the device published a near-identical
                  // (typo'd) name, so this spelling has no observed counterpart —
                  // highlighted amber on the expected side (ISSUE-8).
                  supply_temp_sensor: { present_value: "<device-reported value>" },
                },
              },
              observed: {
                version: "1.4.0",
                points: {
                  energy_sensor: { present_value: 12.5 },
                  // Observed-only point (the typo) — highlighted red on the
                  // observed side. Values (present_value/version) are never marked.
                  suply_temp_sensor: { present_value: 21.4 },
                },
              },
              observed_present: true,
            },
            {
              payload_type: "metadata",
              expected: {
                timestamp: "<RFC 3339 timestamp>",
                version: "1.5.2",
                pointset: { points: { energy_sensor: { units: "kwh" } } },
              },
              observed: { version: "1.5.2", pointset: { points: { energy_sensor: { units: "kilowatt_hours" } } } },
              observed_present: true,
            },
          ],
        },
      ],
    },
    error_message: null,
  };

  const udmiIssuesPayload = {
    run_id: "run-udmi-1",
    issues: [
      {
        issue_id: "UDMI-PS-001",
        asset_id: "EM-1",
        issue_type: "pointset_validation",
        severity: "critical",
        description: "Expected schema version does not match the pointset payload version.",
        point_name: null,
        expected_value: "1.5.2",
        observed_value: "1.4.0",
        suggested_action: "Align the register's Expected schema version with the device's UDMI version.",
        status_detail: null,
        raw_evidence_uri: "runtime://udmi-validation/review-payloads",
      },
    ],
  };

  it("shows no rows until a terminal run, then real per-asset payload rows", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    // Before a run there are no result rows at all — no sample preview, just
    // the honest empty state. Fabricated rows here read as real findings.
    // MDB5-00-044-BLR-2 is unique to the old sample rows (the sample *issues*
    // fallback in the inspector is a separate surface and still stands).
    expect(await screen.findByText("No results yet")).toBeInTheDocument();
    expect(screen.queryByText(/Sample preview/i)).not.toBeInTheDocument();
    expect(screen.queryByText("MDB5-00-044-BLR-2")).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Execute capture" }));

    // After the terminal run the table shows REAL per-asset payload rows —
    // the version-mismatch verdict and the run's asset id, not the sample rows.
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();
    expect(screen.queryByText(/Sample preview/i)).not.toBeInTheDocument();
    // The run monitor shows the capture window the run ACTUALLY used.
    expect(screen.getByText("120 s (bounded)")).toBeInTheDocument();
    // Wait for the issues query to merge so the verdict lands on the row (it can
    // render a beat after the banner, which comes from payload views alone).
    await screen.findAllByText("Non-compliant — 1 issue (1 critical)");
    expect(screen.getAllByText("EM-1").length).toBeGreaterThan(0);
    // The single asset's summary row auto-expands (it is the selected asset), so
    // its per-payload-type rows are visible (ITEM-7 grouping).
    expect(screen.getAllByText("UDMI pointset").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Pass").length).toBeGreaterThan(0);
    // The old illustrative sample asset never appears as a live result.
    expect(screen.queryByText("MDB5-00-043-BLR-1")).not.toBeInTheDocument();

    // Expand the asset in the INSPECTOR drill-down (a separate toggle from the
    // results-table summary row, so scope the query to the inspector aside).
    const inspector = document.querySelector(".inspector") as HTMLElement;
    fireEvent.click(within(inspector).getByRole("button", { name: /EM-1.*issue/i }));
    fireEvent.click(screen.getAllByRole("button", { name: /Show expected vs observed payload/i })[0]);
    expect(screen.getByText("Expected UDMI template")).toBeInTheDocument();
    expect(screen.getByText(/schema-valid sentinel values identify device-supplied fields/i)).toBeInTheDocument();

    // Presence diff (ISSUE-8): the expected-only point spelling shades amber on
    // the expected side, the observed-only (typo'd) spelling shades red on the
    // observed side. jsdom cannot see theme CSS, so assert on the mark classes.
    // The expected-only line names the correct spelling; the observed-only line
    // names the typo — proving each is marked on its own side.
    const expectedOnly = Array.from(document.querySelectorAll(".payload-diff-line.only-expected"));
    const observedOnly = Array.from(document.querySelectorAll(".payload-diff-line.only-observed"));
    expect(expectedOnly.some((el) => el.textContent?.includes("supply_temp_sensor"))).toBe(true);
    expect(observedOnly.some((el) => el.textContent?.includes("suply_temp_sensor"))).toBe(true);
    // A value difference (version 1.5.2 vs 1.4.0) is NEVER highlighted — expected
    // values are template sentinels; the engine's issue cards own value checks.
    expect(expectedOnly.some((el) => el.textContent?.includes("1.5.2"))).toBe(false);
    expect(observedOnly.some((el) => el.textContent?.includes("1.4.0"))).toBe(false);
    // The legend explaining the highlight is present.
    expect(screen.getByText(/Values are not compared here/i)).toBeInTheDocument();
  });

  // Shared stub for the verdict-focused tests below — the same endpoints as the
  // live-results test above, parameterised on the issues payload. An optional
  // issuesResponse factory overrides the whole issues Response (never-settling
  // or failing fetches for the verdict-gating tests).
  function stubUdmiRunFetch(
    issuesPayload: unknown,
    issuesResponse?: () => Response | Promise<Response>,
  ) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return issuesResponse ? issuesResponse() : jsonResponse(issuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  it("shades live UDMI rows amber on non-compliant and green on pass (RAG)", async () => {
    stubUdmiRunFetch(udmiIssuesPayload);
    renderModule("udmi-validation");

    // Nothing has run, so there are no rows to shade — and no sample rows
    // masquerading as results either.
    expect(await screen.findByText("No results yet")).toBeInTheDocument();
    expect(document.querySelector("tr.row-pass, tr.row-fail, tr.row-warn")).toBeNull();

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();

    // Wait for the issues query to merge in (the live banner can render from
    // payload views alone, a beat before the verdict lands on the row).
    await screen.findAllByText("Non-compliant — 1 issue (1 critical)");

    // Under the RAG scheme a PUBLISHING device with a critical issue is amber
    // (row-warn), not red — red is reserved for offline / not-publishing. The
    // clean observed metadata row stays green. The payload-type text also
    // renders in the aside detail, so pick the occurrence inside a table row.
    const pointsetRow = screen
      .getAllByText("UDMI pointset")
      .map((cell) => cell.closest("tr"))
      .find((row) => row !== null);
    expect(pointsetRow).toHaveClass("row-warn");
    const metadataRow = screen
      .getAllByText("UDMI metadata")
      .map((cell) => cell.closest("tr"))
      .find((row) => row !== null);
    expect(metadataRow).toHaveClass("row-pass");
  });

  it("shows the actual issue text in the View detail for a row with 1-2 issues", async () => {
    stubUdmiRunFetch(udmiIssuesPayload);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();
    // Ensure the issues query has merged before opening the detail.
    await screen.findAllByText("Non-compliant — 1 issue (1 critical)");

    // First View = the pointset row, which carries the run's single critical
    // issue; the modal shows its id and full message inline.
    fireEvent.click(screen.getAllByRole("button", { name: "View" })[0]);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("UDMI-PS-001")).toBeInTheDocument();
    expect(
      within(dialog).getByText(/Expected schema version does not match the pointset payload version/i),
    ).toBeInTheDocument();
  });

  it("summarises 3+ issues in the View detail instead of dumping every message", async () => {
    const manyIssues = {
      run_id: "run-udmi-1",
      issues: [1, 2, 3].map((n) => ({
        issue_id: `UDMI-PS-00${n}`,
        asset_id: "EM-1",
        issue_type: "pointset_validation",
        severity: n === 1 ? "critical" : "medium",
        description: `Pointset problem ${n}.`,
        point_name: null,
        expected_value: null,
        observed_value: null,
        suggested_action: null,
        status_detail: null,
        raw_evidence_uri: null,
      })),
    };
    stubUdmiRunFetch(manyIssues);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();
    // Ensure the issues query has merged before opening the detail.
    await screen.findAllByText("Non-compliant — 3 issues (1 critical)");

    fireEvent.click(screen.getAllByRole("button", { name: "View" })[0]);
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText("3 issues — see the issue details below the table."),
    ).toBeInTheDocument();
    // The full message text stays in the issue panel below, not the modal.
    expect(within(dialog).queryByText(/Pointset problem 1\./)).not.toBeInTheDocument();
  });

  it("stamps the shared RAG verdict on the per-asset payload sections", async () => {
    stubUdmiRunFetch(udmiIssuesPayload);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();
    // Ensure the issues query has merged before expanding the asset group.
    await screen.findAllByText("Non-compliant — 1 issue (1 critical)");

    const inspectorStamp = document.querySelector(".inspector") as HTMLElement;
    fireEvent.click(within(inspectorStamp).getByRole("button", { name: /EM-1.*issue/i }));
    // Publishing device with a critical issue → amber "NON-COMPLIANT" section.
    const nonCompliant = await screen.findByText("NON-COMPLIANT — please see details below");
    expect(nonCompliant).toHaveClass("payload-verdict", "warn");
    expect(nonCompliant.closest(".payload-type-group")).toHaveClass("section-warn");
    const pass = screen.getByText("PASS — UDMI Compliant");
    expect(pass).toHaveClass("payload-verdict", "pass");
    expect(pass.closest(".payload-type-group")).toHaveClass("section-pass");
  });

  it("flags a present-but-empty observed value as 'empty' in the issue detail (ISSUE-10)", async () => {
    const emptyValueIssue = {
      run_id: "run-udmi-1",
      issues: [
        {
          issue_id: "UDMI-PS-009",
          asset_id: "EM-1",
          issue_type: "pointset_validation",
          severity: "critical",
          description: "Pointset payload version is blank.",
          point_name: null,
          expected_value: "1.5.2",
          // Present but EMPTY (not null): previously rendered as a bare blank
          // ("observed " with nothing after it); now it must read "empty".
          observed_value: "",
          suggested_action: "Populate the version field.",
          status_detail: null,
          raw_evidence_uri: null,
        },
      ],
    };
    stubUdmiRunFetch(emptyValueIssue);
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();
    await screen.findAllByText("Non-compliant — 1 issue (1 critical)");

    const inspectorEmpty = document.querySelector(".inspector") as HTMLElement;
    fireEvent.click(within(inspectorEmpty).getByRole("button", { name: /EM-1.*issue/i }));
    // Structured issue card (ITEM-9): the expected/observed comparison and the
    // suggested action render as their OWN lines, not one run-on string. The
    // empty observed value is named "empty" rather than dropped or blank.
    expect((await screen.findAllByText("Expected 1.5.2, observed empty")).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("Populate the version field.")).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("Pointset payload version is blank.")).length).toBeGreaterThan(0);
  });

  it("shades silent devices red (offline) on a succeeded run (RAG)", async () => {
    // EM-1 publishes with one major issue → amber. EM-2 was CAPTURED (a real
    // attempt) but stayed silent: pointset + state observed_present false, an
    // engine "not_publishing" issue, and the run summary's not_publishing_devices
    // list. Every EM-2 row must read red "Offline — did not publish", even
    // though the RUN itself SUCCEEDED — the ask as Pete experiences it.
    const silentRun = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        expected_devices: 2,
        publishing_seen: 1,
        not_publishing: 1,
        not_publishing_devices: ["EM-2"],
        payload_views: [
          {
            asset_id: "EM-1",
            payload_types: [
              {
                payload_type: "pointset",
                expected: { version: "1.5.2", points: {} },
                observed: { version: "1.5.2", points: { energy_sensor: { present_value: 12.5 } } },
                observed_present: true,
              },
            ],
          },
          {
            asset_id: "EM-2",
            payload_types: [
              { payload_type: "pointset", expected: { version: "1.5.2", points: {} }, observed: null, observed_present: false },
              { payload_type: "state", expected: { version: "1.5.2" }, observed: null, observed_present: false },
            ],
          },
        ],
      },
    };
    const silentIssues = {
      run_id: "run-udmi-1",
      issues: [
        {
          issue_id: "UDMI-PS-010",
          asset_id: "EM-1",
          issue_type: "pointset_validation",
          severity: "medium",
          description: "Units mismatch on the pointset payload.",
          point_name: null,
          expected_value: null,
          observed_value: null,
          suggested_action: null,
          status_detail: null,
          raw_evidence_uri: null,
        },
        {
          issue_id: "UDMI-NP-001",
          asset_id: "EM-2",
          issue_type: "not_publishing",
          severity: "high",
          description: "No UDMI messages received from this device during the capture window.",
          point_name: null,
          expected_value: null,
          observed_value: null,
          suggested_action: null,
          status_detail: null,
          raw_evidence_uri: null,
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") return jsonResponse(udmiAccepted);
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) return jsonResponse(silentIssues);
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) return jsonResponse(silentRun);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();

    // EM-1 is the first (selected) asset, so it auto-expands; EM-2 is collapsed
    // under ITEM-7 grouping. Expand EM-2's summary row (scope to the table so the
    // query does not also match the inspector's EM-2 drill-down toggle).
    const resultsTable = screen.getByRole("table");
    fireEvent.click(within(resultsTable).getByRole("button", { name: /EM-2/ }));

    // Wait for the offline verdict to land (issues merged) on the now-visible
    // EM-2 rows.
    await screen.findAllByText("Offline — did not publish");

    // Every EM-2 data row (the rows carrying the offline verdict) is red offline.
    const em2Rows = screen
      .getAllByText("Offline — did not publish")
      .map((cell) => cell.closest("tr"))
      .filter((row): row is HTMLTableRowElement => row !== null);
    expect(em2Rows.length).toBeGreaterThan(0);
    for (const row of em2Rows) {
      expect(row).toHaveClass("row-fail");
    }
    // EM-1 is amber (publishing but non-compliant): a summary/data row reads warn.
    const em1Rows = screen
      .getAllByText("EM-1")
      .map((cell) => cell.closest("tr"))
      .filter((row): row is HTMLTableRowElement => row !== null);
    expect(em1Rows.some((row) => row.classList.contains("row-warn"))).toBe(true);

    // The EM-2 section line reads OFFLINE once its INSPECTOR asset group expands.
    const inspectorEm2 = document.querySelector(".inspector") as HTMLElement;
    fireEvent.click(within(inspectorEm2).getByRole("button", { name: /EM-2.*issue/i }));
    expect(
      (await screen.findAllByText("OFFLINE — device did not publish during the capture window")).length,
    ).toBeGreaterThan(0);
  });

  // Shared two-asset stub for the grouping/facet tests: EM-1 published, AHU-9
  // published, EM-2 (facet test) silent — overridable per test.
  function stubTwoAssetUdmi(run: unknown, issues: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) return jsonResponse({ runs: [] });
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST")
          return jsonResponse(udmiAccepted);
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) return jsonResponse(issues);
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) return jsonResponse(run);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  it("groups results by asset: collapsed summary rows expand to per-payload rows (ITEM-7)", async () => {
    const cleanPayload = (asset: string, types: string[]) => ({
      asset_id: asset,
      payload_types: types.map((type) => ({
        payload_type: type,
        expected: { version: "1.5.2", points: {} },
        observed: { version: "1.5.2", points: {} },
        observed_present: true,
      })),
    });
    const twoAssetRun = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        payload_views: [cleanPayload("EM-1", ["pointset", "state"]), cleanPayload("AHU-9", ["pointset"])],
      },
    };
    stubTwoAssetUdmi(twoAssetRun, { run_id: "run-udmi-1", issues: [] });
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();

    const table = screen.getByRole("table");
    // One collapsible summary row per asset.
    expect(within(table).getByRole("button", { name: /EM-1/ })).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: /AHU-9/ })).toBeInTheDocument();
    expect(screen.getByText(/across 2 assets/i)).toBeInTheDocument();

    // EM-1 is the first (selected) asset, so it auto-expands (2 child rows).
    // AHU-9 is collapsed until clicked.
    expect(screen.getAllByRole("button", { name: "View" })).toHaveLength(2);
    fireEvent.click(within(table).getByRole("button", { name: /AHU-9/ }));
    expect(screen.getAllByRole("button", { name: "View" })).toHaveLength(3);

    // Clicking a child row's View selects it (opens the detail modal for AHU-9).
    fireEvent.click(screen.getAllByRole("button", { name: "View" })[2]);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("AHU-9")).toBeInTheDocument();
  });

  it("filters the results and inspector by ONLINE/OFFLINE state (ITEM-10)", async () => {
    const run = {
      ...udmiTerminalRun,
      result_summary: {
        ...udmiTerminalRun.result_summary,
        expected_devices: 2,
        publishing_seen: 1,
        not_publishing_devices: ["EM-2"],
        payload_views: [
          {
            asset_id: "EM-1",
            payload_types: [
              {
                payload_type: "pointset",
                expected: { version: "1.5.2", points: {} },
                observed: { version: "1.5.2", points: {} },
                observed_present: true,
              },
            ],
          },
          {
            asset_id: "EM-2",
            payload_types: [
              { payload_type: "pointset", expected: { version: "1.5.2", points: {} }, observed: null, observed_present: false },
            ],
          },
        ],
      },
    };
    stubTwoAssetUdmi(run, { run_id: "run-udmi-1", issues: [] });
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/across 2 assets/i)).toBeInTheDocument();

    const table = screen.getByRole("table");
    expect(within(table).getByRole("button", { name: /EM-1/ })).toBeInTheDocument();

    // State = Offline hides the observed EM-1 asset and keeps only the silent EM-2.
    fireEvent.change(screen.getByLabelText("State"), { target: { value: "offline" } });
    expect(within(table).queryByRole("button", { name: /EM-1/ })).not.toBeInTheDocument();
    expect(within(table).getByRole("button", { name: /EM-2/ })).toBeInTheDocument();
    expect(screen.getByText(/across 1 asset\b/i)).toBeInTheDocument();
  });

  it("re-attaches a still-running run on arrival and offers Stop run, without locking Execute (ITEM-4)", async () => {
    const runningRun = {
      run_id: "run-udmi-1",
      job_type: "udmi_validation",
      status: "running",
      stage: "capturing",
      progress_percent: 15,
      created_at: "2026-06-11T09:00:00Z",
      updated_at: "2026-06-11T09:00:30Z",
      project_id: "demo-project",
      site_id: "demo-site",
      parameters: { capture_seconds: 0 },
      result_summary: {},
      error_message: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // The rehydration query finds a still-running run of this head's job type.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: [
              {
                run_id: "run-udmi-1",
                job_type: "udmi_validation",
                status: "running",
                stage: "capturing",
                progress_percent: 15,
                created_at: "2026-06-11T09:00:00Z",
                updated_at: "2026-06-11T09:00:30Z",
                edge_id: null,
              },
            ],
          });
        }
        if (url.endsWith("/api/v1/me")) return jsonResponse(mePayload);
        if (url.endsWith("/api/v1/imports/profiles")) return jsonResponse(profilesPayload);
        if (url.endsWith("/api/v1/udmi/schemas")) return jsonResponse([]);
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues"))
          return jsonResponse({ run_id: "run-udmi-1", issues: [] });
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) return jsonResponse(runningRun);
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
    renderModule("udmi-validation");

    // The live run re-attaches its monitor with a Stop run control and the
    // data-kept note. A REHYDRATED run must NOT lock Execute: a fossilized
    // running/queued run (e.g. a hosted worker that died with its dispatch
    // markers, which the startup sweep leaves alone) would otherwise disable
    // Execute forever with no UI escape. Only a run started THIS session blocks.
    expect(await screen.findByRole("button", { name: "Stop run" })).toBeInTheDocument();
    expect(screen.getByText(/Stop run keeps the data collected so far/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Execute capture" })).toBeEnabled(),
    );

    // Progress + elapsed (ITEM-6): the monitor shows an Elapsed entry, and an
    // indefinite-window run (capture_seconds 0) shows the indeterminate sweep.
    expect(screen.getByText("Elapsed")).toBeInTheDocument();
    expect(document.querySelector(".progress-track.indeterminate")).not.toBeNull();
  });

  it("keeps verdicts neutral (Verdict pending, no green Pass) while the issues query is loading", async () => {
    // The issues fetch never settles: the payload views land first (they ride
    // the run record), and an empty issues array must NOT read as a green
    // "Pass" — every verdict surface stays neutral until issues arrive.
    stubUdmiRunFetch(null, () => new Promise<Response>(() => {}));
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();

    // Rows render from the payload views with a neutral pending verdict...
    expect((await screen.findAllByText("Verdict pending")).length).toBeGreaterThan(0);
    // ...and no pass/fail/warn shading or PASS text anywhere: a summary-derived
    // offline signal must not paint amber/red before the issues query settles.
    expect(document.querySelector("tr.row-pass, tr.row-fail, tr.row-warn")).toBeNull();
    // The verdict "Pass" never appears in a results-table row cell (the ISSUE-4
    // filter bar carries a "Pass" tone option, which is a control, not a verdict).
    expect(document.querySelector(".data-table")?.textContent).not.toContain("Pass");
    expect(screen.queryByText("PASS — UDMI Compliant")).not.toBeInTheDocument();
  });

  it("surfaces a visible error and keeps verdicts neutral when the issues fetch fails", async () => {
    // A failed issues fetch previously left the empty issues array in place —
    // PERMANENTLY rendering green Pass verdicts. It must instead surface the
    // failure near the results and keep every verdict surface neutral.
    stubUdmiRunFetch(null, () =>
      ({
        ok: false,
        status: 500,
        statusText: "Internal Server Error",
        json: async () => ({ detail: "issues backend unavailable" }),
      }) as unknown as Response,
    );
    renderModule("udmi-validation");

    const runButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();

    expect(
      await screen.findByText(/Could not load validation issues.*issues backend unavailable/i),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Verdict pending").length).toBeGreaterThan(0);
    expect(document.querySelector("tr.row-pass, tr.row-fail, tr.row-warn")).toBeNull();
    // The verdict "Pass" never appears in a results-table row cell (the ISSUE-4
    // filter bar carries a "Pass" tone option, which is a control, not a verdict).
    expect(document.querySelector(".data-table")?.textContent).not.toContain("Pass");
    expect(screen.queryByText("PASS — UDMI Compliant")).not.toBeInTheDocument();
  });

  it("register-driven mode sends no pasted schedule or payloads so the backend uses the imported register", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          postedBody = JSON.parse(String(init.body)) as { parameters: Record<string, unknown> };
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(await screen.findByLabelText(/Validate against the imported MQTT register/i));
    fireEvent.click(await screen.findByRole("button", { name: "Execute capture" }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> }).parameters;
    // No pasted expectation/payloads: the backend fans out one asset per
    // imported mqtt_register row (topic, points, units, schema version).
    expect(parameters).not.toHaveProperty("expected_schedule");
    expect(parameters).not.toHaveProperty("state_payload");
    expect(parameters).not.toHaveProperty("metadata_payload");
    expect(parameters).not.toHaveProperty("pointset_payload");
    expect(parameters).not.toHaveProperty("state_topic");
    // Blank run time (the default) => 0, the backend's indefinite sentinel:
    // run until every expected topic reports a payload or the run is stopped.
    expect(parameters.capture_seconds).toBe(0);
  });

  it("an accepted MQTT register enables register validation and live capture while keeping both reversible", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse([
            {
              import_type: "mqtt_register",
              description: "Expected MQTT assets.",
              required_columns: ["asset_id", "topic"],
              duplicate_key_fields: ["asset_id"],
            },
          ]);
        }
        if (url.endsWith("/api/v1/imports") && init?.method === "POST") {
          return jsonResponse({
            import_id: "import-mqtt-1",
            import_type: "mqtt_register",
            file_name: "mqtt_register.csv",
            file_type: "csv",
            project_id: "demo-project",
            site_id: "demo-site",
            total_rows: 1,
            accepted_rows: 1,
            rejected_rows: 0,
            status: "accepted",
            missing_columns: [],
            stored_file_name: "import-mqtt-1.csv",
            created_at: "2026-07-10T09:00:00Z",
          });
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          postedBody = JSON.parse(String(init.body)) as { parameters: Record<string, unknown> };
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    const registerMode = await screen.findByLabelText(/Validate against the imported MQTT register/i);
    const liveCapture = screen.getByLabelText(/Capture latest state, metadata, and pointset payloads/i);
    expect(registerMode).not.toBeChecked();
    expect(liveCapture).not.toBeChecked();

    fireEvent.change(screen.getByLabelText(/CSV or XLSX file/i), {
      target: { files: [new File(["asset_id,topic\nEM-1,site/device"], "mqtt_register.csv")] },
    });
    const upload = screen.getByRole("button", { name: "Upload and validate" });
    await waitFor(() => expect(upload).toBeEnabled());
    fireEvent.click(upload);

    await waitFor(() => expect(registerMode).toBeChecked());
    expect(liveCapture).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "Execute capture" }));
    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> }).parameters;
    expect(parameters.use_register).toBe(true);
    expect(parameters.use_live_broker).toBe(true);
    expect(parameters.capture_seconds).toBe(0);
    expect(parameters).not.toHaveProperty("expected_schedule");
    expect(parameters).not.toHaveProperty("state_payload");
    expect(parameters).not.toHaveProperty("metadata_payload");
    expect(parameters).not.toHaveProperty("pointset_payload");

    fireEvent.click(registerMode);
    fireEvent.click(liveCapture);
    expect(registerMode).not.toBeChecked();
    expect(liveCapture).not.toBeChecked();
  });

  it("explains blank run time and every capture safety limit", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i));

    expect(
      screen.getByText(/Blank runs until every expected asset\/topic has reported or you press Stop run/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /48-hour safety limit.*500 distinct\s*concrete topics.*Closing the app ends the run/i,
      ),
    ).toBeInTheDocument();
  });

  it("a positive run time bounds the capture window sent with the run", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          postedBody = JSON.parse(String(init.body)) as { parameters: Record<string, unknown> };
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    // The run-time input renders once live broker capture is ticked.
    fireEvent.click(await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i));
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), { target: { value: "45" } });
    fireEvent.click(await screen.findByRole("button", { name: "Execute capture" }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> }).parameters;
    expect(parameters.capture_seconds).toBe(45);
  });

  it("a non-numeric run time blocks the submit with a validation error and posts nothing", async () => {
    let posted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          posted = true;
          return jsonResponse(udmiAccepted);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i));
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), { target: { value: "45s" } });
    fireEvent.click(await screen.findByRole("button", { name: "Execute capture" }));

    // "45s" must not silently coerce to the 0 = indefinite sentinel: the run is
    // rejected client-side with a visible error and no parameters are posted.
    expect(await screen.findByText(/Run time must be a positive number of seconds/i)).toBeInTheDocument();
    expect(posted).toBe(false);
  });

  it("converts an hours run time to seconds on the wire", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          postedBody = JSON.parse(String(init.body)) as { parameters: Record<string, unknown> };
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i));
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), { target: { value: "2" } });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), { target: { value: "hours" } });
    fireEvent.click(await screen.findByRole("button", { name: "Execute capture" }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    const parameters = (postedBody as unknown as { parameters: Record<string, unknown> }).parameters;
    expect(parameters.capture_seconds).toBe(7200);
  });

  it("refuses a run time over the 48-hour worker cap without posting", async () => {
    let posted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          posted = true;
          return jsonResponse(udmiAccepted);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i));
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), { target: { value: "49" } });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), { target: { value: "hours" } });

    expect(await screen.findByText(/exceeds the 48-hour capture limit/i)).toBeInTheDocument();
    // Execute capture is now the ONLY trigger of the UDMI run action — the Run
    // Controls card is hidden — and it refuses the over-cap window. Hiding the
    // card must not lose the cap gate, so assert both the absence and the guard.
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
    const executeButton = screen.getByRole("button", { name: "Execute capture" });
    expect(executeButton).toBeDisabled();
    fireEvent.click(executeButton);
    expect(posted).toBe(false);
  });

  it("clears a stale over-cap capture window when navigating to another module", async () => {
    stubUdmiRunFetch(udmiIssuesPayload);
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    setApiKey("engineer-key");
    const tree = (route: string) => (
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <MemoryRouter>
            <ModulePage moduleRoute={route} />
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>
    );
    const view = render(tree("udmi-validation"));

    // Type an hours-scale window over the 48h cap on the UDMI workbench.
    fireEvent.click(await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i));
    fireEvent.change(await screen.findByLabelText(/Run time \(blank/i), { target: { value: "49" } });
    fireEvent.change(await screen.findByLabelText(/Run time unit/i), { target: { value: "hours" } });
    expect(await screen.findByText(/exceeds the 48-hour capture limit/i)).toBeInTheDocument();

    // Navigate to data-validation: the run-time control does not render there,
    // so a leaked over-cap window would disable its UDMI run action with no
    // visible input or error. The module-change reset must clear it.
    view.rerender(tree("data-validation"));
    const runButtons = await screen.findAllByRole("button", { name: "Run" });
    expect(runButtons).toHaveLength(3);
    for (const button of runButtons) {
      await waitFor(() => expect(button).toBeEnabled());
    }
  });

  it("generates a report from the run in the chosen format (PDF default)", async () => {
    let reportBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        if (url.endsWith("/api/v1/reports") && init?.method === "POST") {
          reportBody = JSON.parse(String(init.body)) as Record<string, unknown>;
          return jsonResponse({
            file_name: "udmi_validation_rep-9.pdf",
            output_format: "pdf",
            report_id: "rep-9",
            report_type: "udmi_validation",
            status: "succeeded",
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i));
    fireEvent.click(await screen.findByRole("button", { name: "Execute capture" }));

    // Terminal run -> the report affordance appears with the PDF default, once
    // in the run monitor and once at the end of Results. Drive the Results one:
    // that is the copy the operator actually lands on when a run finishes.
    const generateButtons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    expect(generateButtons).toHaveLength(2);
    fireEvent.click(generateButtons[1]);
    await waitFor(() => expect(reportBody).not.toBeNull());
    expect((reportBody as unknown as Record<string, unknown>).output_format).toBe("pdf");
  });

  // THE BREAKAGE-CATCHER. The Run Controls card for this action is hidden, which
  // makes its moduleData entry look unused — but Execute capture resolves it by
  // ARRAY INDEX. Delete the entry and mutationFn throws "Unknown run action."
  // before any fetch, so no POST lands and this test fails.
  it("Execute capture still resolves the hidden UDMI run action by index (do not delete the moduleData entry)", async () => {
    let postedUrl: string | null = null;
    let postedBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/validation/udmi/runs") && init?.method === "POST") {
          postedUrl = url;
          postedBody = JSON.parse(String(init.body)) as Record<string, unknown>;
          return jsonResponse(udmiAccepted);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1/issues")) {
          return jsonResponse(udmiIssuesPayload);
        }
        if (url.endsWith("/api/v1/validation/runs/run-udmi-1")) {
          return jsonResponse(udmiTerminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");
    // Execute capture is engineer-gated, so it stays disabled until /me resolves.
    const executeButton = await screen.findByRole("button", { name: "Execute capture" });
    await waitFor(() => expect(executeButton).toBeEnabled());
    fireEvent.click(executeButton);

    await waitFor(() => expect(postedBody).not.toBeNull());
    // The URL pins runKind and job_type pins jobType, so dispatching the WRONG
    // action (index drift) fails here rather than silently running something else.
    expect(postedUrl as unknown as string).toContain("/api/v1/validation/udmi/runs");
    expect((postedBody as unknown as Record<string, unknown>).job_type).toBe("udmi_validation");
    // onSuccess resolved the same action by index and attached the run.
    expect(await screen.findByText(/Validation run monitor/i)).toBeInTheDocument();
  });

  it("hides the Run UDMI Validation card from Run Controls and signposts Execute capture", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    // Pete 2026-07-15: the run control belongs at the bottom, after the options.
    // The card is genuinely absent from the DOM — a render assertion, not a CSS
    // one, so the jsdom step-gating caveat does not apply here.
    expect(await screen.findByRole("button", { name: "Execute capture" })).toBeInTheDocument();
    expect(screen.queryByText("Run UDMI Validation")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();

    // An empty Execution card would be its own confusion: point the operator at
    // the real trigger instead.
    expect(screen.getByText(/Run controls are at the bottom of Setup/i)).toBeInTheDocument();
    // The all-hidden branch must stay distinct from the no-actions branch — this
    // head DOES need a worker, so the synchronous copy would be a lie.
    expect(screen.queryByText("Saved synchronously")).not.toBeInTheDocument();
  });

  // Index-integrity pin. The fix maps the FULL runActions array and skips hidden
  // entries in place; a refactor to `visibleRunActions.map(...)` would renumber
  // cards and silently dispatch the wrong action the moment any earlier entry on
  // a multi-action head gets flagged. Card N must run runActions[N].
  it("dispatches the run action matching each card's own index on a multi-action head", async () => {
    let postedUrl: string | null = null;
    let postedBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.includes("/api/v1/validation/") && url.endsWith("/runs") && init?.method === "POST") {
          postedUrl = url;
          postedBody = JSON.parse(String(init.body)) as Record<string, unknown>;
          return jsonResponse({
            run_id: "run-bacnet-1",
            job_type: "bacnet_validation",
            status: "queued",
            message: "BACnet validation accepted.",
          });
        }
        if (url.endsWith("/api/v1/validation/runs/run-bacnet-1")) {
          return jsonResponse({ ...udmiTerminalRun, run_id: "run-bacnet-1", job_type: "bacnet_validation" });
        }
        if (url.endsWith("/api/v1/validation/runs/run-bacnet-1/issues")) {
          return jsonResponse({ run_id: "run-bacnet-1", issues: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("data-validation");

    // data-validation renders all three cards (none hidden); the SECOND is the
    // BACnet Point Check at runActions[1].
    const runButtons = await screen.findAllByRole("button", { name: "Run" });
    expect(runButtons).toHaveLength(3);
    await waitFor(() => expect(runButtons[1]).toBeEnabled());
    fireEvent.click(runButtons[1]);

    await waitFor(() => expect(postedBody).not.toBeNull());
    expect(postedUrl as unknown as string).toContain("/api/v1/validation/bacnet/runs");
    expect((postedBody as unknown as Record<string, unknown>).job_type).toBe("bacnet_validation");
  });
});

describe("ModulePage UDMI schema set uploads", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  const existingSet = {
    version_label: "nonpub.1",
    filenames: ["state.json", "pointset.json"],
    uploaded_at: "2026-07-14T09:00:00Z",
  };

  // 204 No Content (DELETE success) carries no JSON body; request() must not
  // try to parse one.
  function noContentResponse(): Response {
    return {
      ok: true,
      status: 204,
      statusText: "No Content",
      json: async () => {
        throw new Error("204 has no body");
      },
    } as unknown as Response;
  }

  it("lists uploaded sets and uploads a new one as multipart FormData", async () => {
    const posted: FormData[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas") && init?.method === "POST") {
          posted.push(init.body as FormData);
          return jsonResponse({
            version_label: "nonpub.2",
            filenames: ["state.json"],
            uploaded_at: "2026-07-14T10:00:00Z",
          });
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([existingSet]);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    // The card renders on the UDMI route with the GET-backed list of sets.
    expect(await screen.findByText("Non-Published UDMI Schema Sets")).toBeInTheDocument();
    expect(await screen.findByText("nonpub.1")).toBeInTheDocument();
    expect(screen.getByText("state.json, pointset.json")).toBeInTheDocument();

    // Upload needs both a version label and at least one .json file.
    const uploadButton = screen.getByRole("button", { name: "Upload schema set" });
    expect(uploadButton).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Version label/i), { target: { value: "nonpub.2" } });
    fireEvent.change(screen.getByLabelText(/Schema JSON files/i), {
      target: {
        files: [new File(['{"title":"state"}'], "state.json", { type: "application/json" })],
      },
    });
    await waitFor(() => expect(uploadButton).toBeEnabled());
    fireEvent.click(uploadButton);

    // The POST is multipart FormData carrying the label plus the file.
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0].get("version_label")).toBe("nonpub.2");
    expect((posted[0].get("files") as File).name).toBe("state.json");
    expect(await screen.findByText("ACCEPTED")).toBeInTheDocument();
  });

  it("deletes an uploaded set via DELETE on its version label", async () => {
    let deletedUrl: string | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Run rehydration asks for this head's last succeeded run on arrival.
        // The "?" keeps this off /discovery/runs/... and the SSE events path.
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas/nonpub.1") && init?.method === "DELETE") {
          deletedUrl = url;
          return noContentResponse();
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([existingSet]);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    const deleteButton = await screen.findByRole("button", { name: "Delete" });
    await waitFor(() => expect(deleteButton).toBeEnabled());
    fireEvent.click(deleteButton);
    await waitFor(() => expect(deletedUrl).toMatch(/\/api\/v1\/udmi\/schemas\/nonpub\.1$/));
  });

  it("downloads the schema-set template zip from the public template endpoint", async () => {
    // jsdom implements no object-URL APIs; triggerBlobDownload uses them.
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:mock"),
      revokeObjectURL: vi.fn(),
    });
    let templateUrl: string | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/udmi/schemas/template")) {
          templateUrl = url;
          // downloadFile() reads .blob() and the Content-Disposition header.
          return {
            ok: true,
            status: 200,
            statusText: "OK",
            blob: async () => new Blob(["zip"]),
            headers: {
              get: (name: string) =>
                name.toLowerCase() === "content-disposition"
                  ? 'attachment; filename="udmi-schema-template-1.5.2.zip"'
                  : null,
            },
          } as unknown as Response;
        }
        if (url.endsWith("/api/v1/udmi/schemas")) {
          return jsonResponse([]);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    const downloadButton = await screen.findByRole("button", {
      name: "Download schema template (1.5.2)",
    });
    fireEvent.click(downloadButton);

    await waitFor(() => expect(templateUrl).toMatch(/\/udmi\/schemas\/template$/));
    // The blob actually reached the browser's download path.
    expect(URL.createObjectURL).toHaveBeenCalled();
  });
});

// Step visibility is CSS-driven (.module-steps > [data-stepgroup] { display:none }
// in the theme), and jsdom does not load that stylesheet. So these tests assert
// on the data-step / data-stepgroup attributes that drive it, never on
// toBeVisible() — which would pass vacuously here.
function stepOf() {
  return document.querySelector(".module-steps")?.getAttribute("data-step");
}

describe("ModulePage run retention", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  // What GET /runs?job_type=ip_discovery&status=succeeded&limit=1 returns: a
  // JobSummary, not the full RunRecord.
  const ipRunSummary = {
    run_id: "run-ip-1",
    job_type: "ip_discovery",
    status: "succeeded",
    stage: "register_comparison",
    progress_percent: 100,
    created_at: "2026-06-11T09:00:00Z",
    updated_at: "2026-06-11T09:05:00Z",
    edge_id: null,
  };

  // Serves the last-succeeded-run lookup per job type, so an ip-scanner render
  // rehydrates run-ip-1 while every other head finds nothing of its own.
  function stubWithLastRun() {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: url.includes("job_type=ip_discovery") ? [ipRunSummary] : [],
          });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(resultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  it("re-attaches the last succeeded run on arrival without the operator running anything", async () => {
    stubWithLastRun();
    renderModule("ip-scanner");

    // No Run click anywhere in this test: the monitor comes back on its own.
    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();
    expect((await screen.findAllByText("run-ip-1")).length).toBeGreaterThan(0);
  });

  it("leaves a restored run on the Setup step instead of hijacking it to Results", async () => {
    stubWithLastRun();
    renderModule("ip-scanner");

    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();
    // The operator came here to set something up. A run they did not just start
    // must not yank them to Results, even though it is terminal-succeeded.
    expect(stepOf()).toBe("setup");
    // Give the terminal-success effect a chance to fire before trusting that.
    await waitFor(() => expect(screen.getAllByText("run-ip-1").length).toBeGreaterThan(0));
    expect(stepOf()).toBe("setup");
  });

  it("shows the restored run's live results once the operator clicks through to Results", async () => {
    stubWithLastRun();
    renderModule("ip-scanner");
    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();

    // Results is one click away and holds the real rows from the restored run.
    fireEvent.click(screen.getByRole("button", { name: /Results/i }));
    await waitFor(() => expect(stepOf()).toBe("results"));
    expect((await screen.findAllByText("plant-controller")).length).toBeGreaterThan(0);
  });

  it("offers Generate report from this run for a restored terminal run", async () => {
    stubWithLastRun();
    renderModule("ip-scanner");

    // A restored run satisfies the engineer + terminal gates just like a fresh
    // one, so the report affordance survives navigating away and back — both
    // the run-monitor copy and the end-of-Results copy.
    expect(
      await screen.findAllByRole("button", { name: /Generate report from this run/i }),
    ).toHaveLength(2);
  });

  it("never bleeds one head's restored run into another head", async () => {
    stubWithLastRun();
    setApiKey("engineer-key");
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    const tree = (route: string) => (
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <MemoryRouter>
            <ModulePage moduleRoute={route} />
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>
    );
    const view = render(tree("ip-scanner"));
    expect((await screen.findAllByText("run-ip-1")).length).toBeGreaterThan(0);

    // Sibling ModulePage routes share one component instance (no key prop), so
    // this rerender — not a remount — is the real cross-head bleed vector.
    // MQTT has no succeeded run of its own, so nothing may be re-attached.
    view.rerender(tree("mqtt-discovery"));
    await waitFor(() => expect(screen.queryByText("run-ip-1")).not.toBeInTheDocument());
    expect(screen.queryByText(/Discovery run monitor/i)).not.toBeInTheDocument();
    expect(stepOf()).toBe("setup");
  });

  it("still advances an operator-started run to the Run step", async () => {
    // This run stays non-terminal, isolating the queued -> Run advance from the
    // succeeded -> Results one the discovery wiring suite covers.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse({ ...terminalRun, status: "running", progress_percent: 40 });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner");
    expect(stepOf()).toBe("setup");

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(stepOf()).toBe("run"));
  });

  it("shows no results and no sample rows on a head that has never run", async () => {
    stubWithLastRun();
    renderModule("mqtt-discovery");

    // "Boiler 1 Controller" is an old fixture row; nothing fabricated may stand
    // in for a run that never happened.
    expect(await screen.findByText("No results yet")).toBeInTheDocument();
    expect(screen.queryByText(/Sample preview/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Boiler 1 Controller")).not.toBeInTheDocument();
  });
});

describe("ModulePage reports visibility", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  const reportsPayload = {
    reports: [
      {
        report_id: "rep-1",
        report_type: "issue_report",
        output_format: "xlsx",
        status: "succeeded",
        file_name: "issue_report.xlsx",
        created_at: "2026-07-15T10:00:00Z",
        source_run_ids: ["run-1"],
      },
    ],
  };

  it("shows the Generated Reports table on arrival, before any step click", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/reports")) {
          return jsonResponse(reportsPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("reports");
    expect(await screen.findByLabelText(/Select report issue_report\.xlsx/i)).toBeInTheDocument();

    // The page lands on Setup and stays there, so the reports table has to be
    // in the Setup step group or the CSS hides it — which is the bug this fixes.
    expect(stepOf()).toBe("setup");
    const section = screen.getByRole("heading", { name: "Generated Reports" }).closest("section");
    expect(section?.getAttribute("data-stepgroup")).toContain("setup");
  });

  it("says Loading reports while the list is in flight, not No reports yet", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/reports")) {
          // Never resolves: the list is still loading.
          return new Promise<Response>(() => {});
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("reports");

    // Scoped to the hero metric: the results table already handled the loading
    // case, so an unscoped query would match that instead and pass regardless.
    // "No reports yet" while we are still asking is a claim we cannot make.
    await waitFor(() =>
      expect(document.querySelector(".module-metrics-empty")).toHaveTextContent(
        "Loading reports...",
      ),
    );
    expect(document.querySelector(".module-metrics-empty")).not.toHaveTextContent(
      "No reports yet",
    );
  });

  it("invalidates the reports list after generating a report from a run", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(resultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        if (url.endsWith("/api/v1/reports") && init?.method === "POST") {
          return jsonResponse({
            file_name: "ip_discovery_rep-7.pdf",
            output_format: "pdf",
            report_id: "rep-7",
            report_type: "ip_discovery",
            status: "succeeded",
          });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    setApiKey("engineer-key");
    // Seed the cache the reports page would populate, so invalidation has
    // something to mark stale.
    queryClient.setQueryData(["reports-list"], { reports: [] });
    render(
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <MemoryRouter>
            <ModulePage moduleRoute="ip-scanner" />
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByLabelText(/I am authorized to scan this network/i));
    const runButton = await screen.findByRole("button", { name: "Run" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    const generateButtons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    fireEvent.click(generateButtons[1]);

    // The toast tells the operator to look in the Reports tab, so the cached
    // list behind that tab must not still be the pre-report one.
    await waitFor(() =>
      expect(queryClient.getQueryState(["reports-list"])?.isInvalidated).toBe(true),
    );
  });
});

describe("ModulePage report controls placement", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  const ipRunSummary = {
    run_id: "run-ip-1",
    job_type: "ip_discovery",
    status: "succeeded",
    stage: "register_comparison",
    progress_percent: 100,
    created_at: "2026-06-11T09:00:00Z",
    updated_at: "2026-06-11T09:05:00Z",
    edge_id: null,
  };

  // Run rehydration hands us a terminal run with no clicks at all, which is the
  // only way to put a *viewer* in front of one — viewers cannot start runs.
  function stubTerminalRun(options: { role?: string; lastRun?: boolean } = {}) {
    const { role = "engineer", lastRun = true } = options;
    const captured: { reportBody: Record<string, unknown> | null } = { reportBody: null };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: lastRun && url.includes("job_type=ip_discovery") ? [ipRunSummary] : [],
          });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse({ ...mePayload, role });
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(resultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        if (url.endsWith("/api/v1/reports") && init?.method === "POST") {
          captured.reportBody = JSON.parse(String(init.body)) as Record<string, unknown>;
          return jsonResponse({
            file_name: "ip_discovery_rep-11.pdf",
            output_format: "pdf",
            report_id: "rep-11",
            report_type: "ip_discovery",
            status: "succeeded",
          });
        }
        // The reports route lists them on arrival.
        if (url.endsWith("/api/v1/reports")) {
          return jsonResponse({ reports: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
    return captured;
  }

  // Pete's walkthrough bug: a finished run auto-advances to Results, and the
  // report controls — which live in the "setup run" group — go with it.
  it("renders the report controls in both the run-monitor and the results step group", async () => {
    stubTerminalRun();
    renderModule("ip-scanner");

    const buttons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    expect(buttons).toHaveLength(2);

    // jsdom does not apply the theme CSS, so both copies are always in the DOM
    // and visibility assertions would be meaningless. What is assertable — and
    // what the CSS gate actually keys on — is the step group each one sits in.
    expect(buttons[0].closest("[data-stepgroup]")).toHaveAttribute("data-stepgroup", "setup run");
    expect(buttons[1].closest("[data-stepgroup]")).toHaveAttribute("data-stepgroup", "results");
  });

  // The gate is `.module-steps > [data-stepgroup]` — a DIRECT child selector. A
  // results section nested one level deeper still looks right in jsdom and in
  // the test above, but would render on every step in a real browser.
  it("hangs the results-step report section directly off .module-steps so the CSS gate applies", async () => {
    stubTerminalRun();
    renderModule("ip-scanner");

    const buttons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    const resultsSection = buttons[1].closest("[data-stepgroup]");
    expect(resultsSection?.parentElement).toHaveClass("module-steps");
  });

  it("shares one format selection between both report control instances", async () => {
    const captured = stubTerminalRun();
    renderModule("ip-scanner");

    const pickers = (await screen.findAllByLabelText("Report format")) as HTMLSelectElement[];
    expect(pickers).toHaveLength(2);

    // Change the results-step picker; the run-monitor one must follow it. Give
    // the extracted component its own useState and the two drift apart — you
    // get a picker that lies about the format it is going to generate.
    fireEvent.change(pickers[1], { target: { value: "docx" } });
    expect(pickers[0].value).toBe("docx");

    // ...and the POST reads the shared state, whichever button you press.
    const buttons = await screen.findAllByRole("button", {
      name: /Generate report from this run/i,
    });
    fireEvent.click(buttons[0]);
    await waitFor(() => expect(captured.reportBody).not.toBeNull());
    expect(captured.reportBody?.output_format).toBe("docx");
  });

  it("renders no report controls until a run exists", async () => {
    stubTerminalRun({ lastRun: false });
    renderModule("ip-scanner");

    // Wait for the page to settle before trusting an absence assertion.
    expect(await screen.findByRole("button", { name: "Run" })).toBeInTheDocument();
    expect(
      screen.queryAllByRole("button", { name: /Generate report from this run/i }),
    ).toHaveLength(0);
  });

  it("renders no report controls for a viewer, even with a terminal run attached", async () => {
    stubTerminalRun({ role: "viewer" });
    renderModule("ip-scanner");

    // The monitor proves the terminal run really is attached — so the absence
    // below is the engineer gate doing its job in the new section, not a page
    // that simply has no run.
    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();
    expect(
      screen.queryAllByRole("button", { name: /Generate report from this run/i }),
    ).toHaveLength(0);
  });

  // Pins the reports route's shape, NOT the `route !== "reports"` clause in the
  // new section's guard: report actions never set activeRun, so that clause is
  // unreachable and this test passes with or without it. Deleting it here would
  // be a silent no-op — it earns its place upstream as defence, not as a fix.
  it("leaves the reports route with a single set of report controls", async () => {
    stubTerminalRun({ lastRun: false });
    renderModule("reports");

    expect(await screen.findByText(/Generated Reports/i)).toBeInTheDocument();
    expect(
      screen.queryAllByRole("button", { name: /Generate report from this run/i }),
    ).toHaveLength(0);
  });
});

describe("ModulePage snap-to-top when results open", () => {
  afterEach(() => {
    // Restores the setup.ts no-op that the spy wrapped.
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearApiKey();
  });

  // jsdom has no layout, so scrollIntoView only exists because src/test/setup.ts
  // installs a no-op — vi.spyOn would throw on an undefined property. The spy
  // calls through to that no-op, so nothing here depends on real scrolling.
  function spyOnScroll() {
    return vi.spyOn(window.HTMLElement.prototype, "scrollIntoView");
  }

  // The element the page scrolled to, recorded by the spy as its `this`.
  function scrollTarget(spy: ReturnType<typeof spyOnScroll>, call = 0) {
    return spy.mock.contexts[call] as HTMLElement;
  }

  const ipRunSummary = {
    run_id: "run-ip-1",
    job_type: "ip_discovery",
    status: "succeeded",
    stage: "register_comparison",
    progress_percent: 100,
    created_at: "2026-06-11T09:00:00Z",
    updated_at: "2026-06-11T09:05:00Z",
    edge_id: null,
  };

  // `lastRun` decides whether this head has a previous succeeded run to
  // rehydrate — the difference between a run the operator just started and one
  // restored on arrival, which must NOT snap.
  function stubIpScanner(options: { lastRun?: boolean } = {}) {
    const { lastRun = false } = options;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({
            runs: lastRun && url.includes("job_type=ip_discovery") ? [ipRunSummary] : [],
          });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/discovery/ip/runs") && init?.method === "POST") {
          return jsonResponse(acceptedRun);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1/results")) {
          return jsonResponse(resultsPayload);
        }
        if (url.endsWith("/api/v1/discovery/runs/run-ip-1")) {
          return jsonResponse(terminalRun);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  }

  // Pete's walkthrough ask: after a run finishes, the page stays where the
  // operator left it mid-Run, so the headline results land off-screen.
  it("snaps to the hero when a succeeded run advances to Results", async () => {
    const scrollSpy = spyOnScroll();
    stubIpScanner();
    renderModule("ip-scanner");

    const queueButton = await screen.findByRole("button", { name: "Run" });
    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    await waitFor(() => expect(queueButton).toBeEnabled());

    // Nothing has opened Results yet, so setting up must not move the page.
    expect(scrollSpy).not.toHaveBeenCalled();

    fireEvent.click(queueButton);

    // jsdom does not apply the step-gating CSS, so `data-step` is the assertable
    // signal that Results actually opened.
    await waitFor(() =>
      expect(document.querySelector(".module-steps")).toHaveAttribute("data-step", "results"),
    );
    // The snap lives in a passive effect and the step update arrives from a
    // react-query poll outside act(), so the waitFor above can observe the
    // commit BEFORE React flushes the effect — seen flaking on the slower
    // windows-2022 runner. Poll for the spy; the assertion itself is unchanged.
    await waitFor(() =>
      expect(scrollSpy).toHaveBeenCalledWith({ behavior: "auto", block: "start" }),
    );
    // ...and it scrolled the hero, not some arbitrary element.
    expect(scrollTarget(scrollSpy)).toHaveClass("module-hero");
  });

  // The report branch of runMutation sets the step directly rather than going
  // through the terminal-run effect — a second, easily-missed route into Results.
  it("snaps to the hero when generating a report opens Results", async () => {
    const scrollSpy = spyOnScroll();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/runs?")) {
          return jsonResponse({ runs: [] });
        }
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        if (url.endsWith("/api/v1/reports") && init?.method === "POST") {
          return jsonResponse({
            file_name: "issue_report.xlsx",
            output_format: "xlsx",
            report_id: "rep-1",
            report_type: "issue_report",
            status: "succeeded",
          });
        }
        if (url.endsWith("/api/v1/reports")) {
          return jsonResponse({ reports: [] });
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("reports");

    // The reports route has one card per format and every button just says
    // "Generate", so scope the query to the Excel card by its label.
    const card = (await screen.findByText("Generate Excel Report")).closest(
      ".run-card",
    ) as HTMLElement;
    const generate = within(card).getByRole("button", { name: "Generate" });
    await waitFor(() => expect(generate).toBeEnabled());
    expect(scrollSpy).not.toHaveBeenCalled();

    fireEvent.click(generate);

    await waitFor(() =>
      expect(document.querySelector(".module-steps")).toHaveAttribute("data-step", "results"),
    );
    // Same passive-effect race as the run-advance test above: the step update
    // comes from the mutation's onSuccess, outside act().
    await waitFor(() =>
      expect(scrollSpy).toHaveBeenCalledWith({ behavior: "auto", block: "start" }),
    );
    expect(scrollTarget(scrollSpy)).toHaveClass("module-hero");
  });

  // A run restored on arrival never advances the step, so it must never snap
  // either — the operator came here to set something up, and yanking the page to
  // the top for a run they did not just start would be exactly the hijack the
  // step-retention work took care to avoid.
  it("does not snap for a run rehydrated on arrival", async () => {
    const scrollSpy = spyOnScroll();
    stubIpScanner({ lastRun: true });
    renderModule("ip-scanner");

    // The monitor proves the restored run really did attach, so the absence
    // below is the restored guard holding, not a page with nothing on it.
    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("plant-controller").length).toBeGreaterThan(0));

    expect(document.querySelector(".module-steps")).toHaveAttribute("data-step", "setup");
    expect(scrollSpy).not.toHaveBeenCalled();
  });

  // The snap follows the step, not the run: a manual jump to Results must move
  // the page too, or clicking "3 Results" from a scrolled-down Run step leaves
  // the operator staring at the middle of the results they asked to see.
  it("snaps to the hero on a manual step click to Results", async () => {
    const scrollSpy = spyOnScroll();
    stubIpScanner({ lastRun: true });
    renderModule("ip-scanner");

    const resultsStep = await screen.findByRole("button", { name: /Results/i });
    await waitFor(() => expect(resultsStep).toBeEnabled());
    expect(scrollSpy).not.toHaveBeenCalled();

    fireEvent.click(resultsStep);

    await waitFor(() =>
      expect(document.querySelector(".module-steps")).toHaveAttribute("data-step", "results"),
    );
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: "auto", block: "start" });
    expect(scrollTarget(scrollSpy)).toHaveClass("module-hero");
  });
});

// A rejected import used to report only "N accepted / M rejected" — the reasons
// were produced and persisted by the backend but never fetched. These cover the
// reasons panel plus the same-filename re-pick fix on the file input.
describe("ModulePage import rejection reasons", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  function rejectedSummary(overrides: Record<string, unknown> = {}) {
    return {
      import_id: "import-ip-2",
      import_type: "ip_register",
      file_name: "ip_register.csv",
      file_type: "csv",
      project_id: "demo-project",
      site_id: "demo-site",
      total_rows: 4,
      accepted_rows: 0,
      rejected_rows: 4,
      status: "rejected",
      missing_columns: [],
      warnings: [],
      stored_file_name: "import-ip-2.csv",
      created_at: "2026-07-15T09:00:00Z",
      ...overrides,
    };
  }

  // Stubs /me + /imports/profiles + POST /imports, and routes the errors GET to
  // `errors`. `onErrors` (when given) replaces the default success response.
  function stubImport(options: {
    summary?: Record<string, unknown>;
    errors?: unknown;
    onErrorsUrl?: (url: string) => void;
    errorsFails?: boolean;
  }) {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/runs?")) {
        return jsonResponse({ runs: [] });
      }
      if (url.endsWith("/api/v1/me")) {
        return jsonResponse(mePayload);
      }
      if (url.endsWith("/api/v1/imports/profiles")) {
        return jsonResponse(profilesPayload);
      }
      if (url.endsWith("/api/v1/imports") && init?.method === "POST") {
        return jsonResponse(options.summary ?? rejectedSummary());
      }
      if (url.includes("/errors")) {
        options.onErrorsUrl?.(url);
        if (options.errorsFails) {
          return {
            ok: false,
            status: 404,
            statusText: "Not Found",
            json: async () => ({ detail: "Import errors for 'import-ip-2' were not found." }),
          } as unknown as Response;
        }
        return jsonResponse(options.errors ?? { import_id: "import-ip-2", errors: [] });
      }
      throw new Error(`Unexpected fetch in test: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  async function uploadFile(name = "ip_register.csv") {
    fireEvent.change(await screen.findByLabelText(/CSV or XLSX file/i), {
      target: { files: [new File(["reg"], name)] },
    });
    const upload = screen.getByRole("button", { name: "Upload and validate" });
    await waitFor(() => expect(upload).toBeEnabled());
    fireEvent.click(upload);
  }

  it("fetches and renders per-row rejection reasons in a red panel", async () => {
    const errorUrls: string[] = [];
    stubImport({
      onErrorsUrl: (url) => errorUrls.push(url),
      errors: {
        import_id: "import-ip-2",
        errors: [
          {
            row_number: 3,
            field: "Expected topic",
            code: "invalid_topic",
            message: "Topic must not contain wildcards.",
          },
          {
            row_number: 5,
            field: null,
            code: "duplicate_row",
            message: "Duplicate record detected for asset_id AHU-01.",
          },
        ],
      },
    });

    renderModule("ip-scanner");
    await uploadFile();

    // Row + field + message + code, with the reason the operator has to act on.
    expect(await screen.findByText(/Row 3 — Expected topic: Topic must not contain wildcards\./))
      .toBeInTheDocument();
    expect(screen.getByText(/\(invalid_topic\)/)).toBeInTheDocument();

    // field is null on duplicate_row records, so no stray ": " prefix.
    const duplicate = screen.getByText(/Duplicate record detected for asset_id AHU-01\./);
    expect(duplicate.textContent).toContain("Row 5 — Duplicate record detected");
    expect(duplicate.textContent).not.toContain("null");

    // Red rejection styling, never the amber warning panel (whose rows are kept).
    const panel = duplicate.closest(".state-panel");
    expect(panel).toHaveClass("error");
    expect(panel).toHaveClass("import-errors");
    expect(panel).not.toHaveClass("warning");

    // The reasons came from the real endpoint, keyed by this import's id.
    await waitFor(() => expect(errorUrls).toHaveLength(1));
    expect(errorUrls[0]).toMatch(/\/api\/v1\/imports\/import-ip-2\/errors$/);
  });

  it("names the missing columns without repeating them as bullets", async () => {
    stubImport({
      summary: rejectedSummary({
        total_rows: 0,
        rejected_rows: 0,
        missing_columns: ["Asset ID", "Expected topic"],
      }),
      errors: {
        import_id: "import-ip-2",
        errors: [
          {
            row_number: null,
            field: "Asset ID",
            code: "missing_required_column",
            message: "Required column 'Asset ID' is missing.",
          },
          {
            row_number: null,
            field: "Expected topic",
            code: "missing_required_column",
            message: "Required column 'Expected topic' is missing.",
          },
        ],
      },
    });

    renderModule("ip-scanner");
    await uploadFile();

    // The summary already carries the columns, so this line needs no fetch...
    expect(
      await screen.findByText("Missing required columns: Asset ID, Expected topic"),
    ).toBeInTheDocument();
    // ...and the per-column records that would repeat it verbatim are filtered out.
    expect(screen.queryByText(/Required column 'Asset ID' is missing\./)).not.toBeInTheDocument();

    // rejected_rows is 0 for a missing-columns file (_status() still says
    // "rejected"), so the panel must not be gated on rejected_rows > 0.
    expect(screen.getByText("Import rejected — reasons below")).toBeInTheDocument();
  });

  it("does not fetch reasons for an accepted import", async () => {
    const errorUrls: string[] = [];
    stubImport({
      summary: rejectedSummary({
        import_id: "import-ip-3",
        total_rows: 2,
        accepted_rows: 2,
        rejected_rows: 0,
        status: "accepted",
      }),
      onErrorsUrl: (url) => errorUrls.push(url),
    });

    renderModule("ip-scanner");
    await uploadFile();

    expect(await screen.findByText("ACCEPTED")).toBeInTheDocument();
    expect(screen.queryByText(/reasons below/)).not.toBeInTheDocument();
    expect(errorUrls).toEqual([]);
  });

  it("says so honestly when the reasons cannot be loaded", async () => {
    stubImport({ errorsFails: true });

    renderModule("ip-scanner");
    await uploadFile();

    // An empty list must never masquerade as "no reasons" when the fetch failed.
    expect(await screen.findByText(/Could not load rejection reasons:/)).toBeInTheDocument();
  });

  it("caps the rendered rows and states the honest remainder", async () => {
    stubImport({
      // A partial import: 5 rows landed, 60 did not. Exercises the partial
      // headline as well as the cap.
      summary: rejectedSummary({
        total_rows: 65,
        accepted_rows: 5,
        rejected_rows: 60,
        status: "partial",
      }),
      errors: {
        import_id: "import-ip-2",
        errors: Array.from({ length: 60 }, (_, index) => ({
          row_number: index + 2,
          field: "Expected topic",
          code: "invalid_topic",
          message: `Row ${index + 2} topic is malformed.`,
        })),
      },
    });

    renderModule("ip-scanner");
    await uploadFile();

    const panel = (await screen.findByText("60 of 65 rows rejected — reasons below")).closest(
      ".state-panel",
    ) as HTMLElement;
    await waitFor(() => expect(within(panel).getAllByRole("listitem")).toHaveLength(50));
    expect(within(panel).getByText(/and 10 more rejected rows not shown/)).toBeInTheDocument();
  });

  it("clears the file input's value on selection so a re-picked same-name file is re-read", async () => {
    stubImport({ summary: rejectedSummary({ status: "accepted", accepted_rows: 4 }) });
    renderModule("ip-scanner");

    const input = (await screen.findByLabelText(/CSV or XLSX file/i)) as HTMLInputElement;

    // jsdom cannot reproduce Chromium's real behaviour here (no change event
    // when the same path is re-picked), and neither `input.value` nor
    // `input.files` can witness the clear: fireEvent installs `files` as an own
    // property that shadows jsdom's getter and never fills jsdom's internal
    // selected-file list, so `value` reads "" before the handler even runs and
    // `files` keeps its array afterwards. Both would-be assertions are
    // therefore vacuous. Spy on the assignment itself instead — that IS the fix.
    const descriptor =
      Object.getOwnPropertyDescriptor(input, "value") ??
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!;
    const valueAssignments: string[] = [];
    Object.defineProperty(input, "value", {
      configurable: true,
      get: () => descriptor.get!.call(input),
      set: (next: string) => {
        valueAssignments.push(next);
        descriptor.set!.call(input, next);
      },
    });

    fireEvent.change(input, { target: { files: [new File(["reg"], "ip_register.csv")] } });

    expect(valueAssignments).toContain("");

    // The File lives in state, so clearing the DOM input costs nothing: the
    // staged name is still shown and the upload still goes through.
    expect(await screen.findByText("Selected: ip_register.csv")).toBeInTheDocument();
    const upload = screen.getByRole("button", { name: "Upload and validate" });
    await waitFor(() => expect(upload).toBeEnabled());
    fireEvent.click(upload);
    expect(await screen.findByText("ACCEPTED")).toBeInTheDocument();
  });
});
