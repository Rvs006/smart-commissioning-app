import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
