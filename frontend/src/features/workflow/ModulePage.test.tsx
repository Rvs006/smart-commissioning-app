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
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
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
    // Live banner is shown, not a fabricated "Result" verdict column.
    expect(screen.getByText(/Live discovery observations/i)).toBeInTheDocument();

    // Headline metric now reflects the real run (hosts_responsive: 1), never the
    // old hardcoded "118" sample.
    expect(await screen.findByText("responsive hosts")).toBeInTheDocument();
    expect(screen.queryByText("118")).not.toBeInTheDocument();
  });

  it("renders import warnings as a non-blocking amber panel distinct from errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
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

  it("sends a CIDR target override as parameters.cidr with no addresses key and no fabricated authorization principal", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
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

  it("renders the MAC column and opens a per-host detail dialog from the row View button", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
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
});

describe("ModulePage reports wiring", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  it("disables Export until a report has been queued", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
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
    const reportsPayload = {
      reports: [
        {
          report_id: "rep-1",
          report_type: "issue_report",
          output_format: "xlsx",
          status: "succeeded",
          file_name: "issue_report.xlsx",
        },
        {
          report_id: "rep-2",
          report_type: "evidence_pack",
          output_format: "docx",
          status: "queued",
          file_name: "evidence_pack.docx",
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
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

  it("exposes XLSX and CSV template downloads for every import type on a page", async () => {
    stubBasic();
    renderModule("data-validation");
    // The all-templates panel lists each import type the validation page accepts.
    expect(await screen.findByText("Import Templates for This Page")).toBeInTheDocument();
    expect(screen.getByText("Asset Validation")).toBeInTheDocument();
    expect(screen.getByText("Bacnet Points")).toBeInTheDocument();
    expect(screen.getByText("Mqtt Points")).toBeInTheDocument();
    expect(screen.getByText("Mapping")).toBeInTheDocument();
    expect(screen.getByText("Tolerances")).toBeInTheDocument();
    // Each card offers both XLSX and CSV (5 import types -> 5 of each).
    expect(screen.getAllByRole("button", { name: "XLSX" })).toHaveLength(5);
    expect(screen.getAllByRole("button", { name: "CSV" })).toHaveLength(5);
  });

  it("shows IP Address and Network Number columns for BACnet discovery", async () => {
    stubBasic();
    renderModule("bacnet-discovery");
    // Sample BACnet table is shown until a run; it now carries the new columns.
    expect(await screen.findByRole("columnheader", { name: "IP Address" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Network Number" })).toBeInTheDocument();
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
                points: { energy_sensor: { present_value: "<device-reported value>" } },
              },
              observed: { version: "1.4.0", points: { energy_sensor: { present_value: 12.5 } } },
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

  it("replaces the sample rows with real per-asset payload rows after a terminal run", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
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

    // Before a run the labelled sample rows are shown.
    expect(await screen.findByText(/Sample preview/i)).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Run" }));

    // After the terminal run the table shows REAL per-asset payload rows —
    // the version-mismatch verdict and the run's asset id, not the sample rows.
    expect(await screen.findByText(/Live validation results/i)).toBeInTheDocument();
    expect(screen.queryByText(/Sample preview/i)).not.toBeInTheDocument();
    // The run monitor shows the capture window the run ACTUALLY used.
    expect(screen.getByText("120 s (bounded)")).toBeInTheDocument();
    expect(screen.getAllByText("EM-1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("UDMI pointset").length).toBeGreaterThan(0);
    // Row cells also render in the selected-result detail aside, so match >=1.
    expect(screen.getAllByText("Fail — 1 issue (1 critical)").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Pass").length).toBeGreaterThan(0);
    // The old illustrative sample asset never appears as a live result.
    expect(screen.queryByText("MDB5-00-043-BLR-1")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /EM-1.*issue/i }));
    fireEvent.click(screen.getAllByRole("button", { name: /Show expected vs observed payload/i })[0]);
    expect(screen.getByText("Expected UDMI template")).toBeInTheDocument();
    expect(screen.getByText(/schema-valid sentinel values identify device-supplied fields/i)).toBeInTheDocument();
  });

  it("register-driven mode sends no pasted schedule or payloads so the backend uses the imported register", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
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
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));

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
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
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

    fireEvent.click(screen.getByRole("button", { name: "Run" }));
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
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("udmi-validation");

    fireEvent.click(await screen.findByLabelText(/Capture latest state, metadata, and pointset payloads/i));

    expect(screen.getByText(/Blank runs until all required topics report or you press Cancel run/i)).toBeInTheDocument();
    expect(
      screen.getByText(
        /Worker captures are capped at 1 hour.*inline\/portable captures at 240 seconds.*500 distinct concrete topics/i,
      ),
    ).toBeInTheDocument();
  });

  it("a positive run time bounds the capture window sent with the run", async () => {
    let postedBody: { parameters: Record<string, unknown> } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
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
    fireEvent.change(await screen.findByLabelText(/Run time \(seconds/i), { target: { value: "45" } });
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));

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
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse(mePayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
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
    fireEvent.change(await screen.findByLabelText(/Run time \(seconds/i), { target: { value: "45s" } });
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));

    // "45s" must not silently coerce to the 0 = indefinite sentinel: the run is
    // rejected client-side with a visible error and no parameters are posted.
    expect(await screen.findByText(/Run time must be a positive number of seconds/i)).toBeInTheDocument();
    expect(posted).toBe(false);
  });
});
