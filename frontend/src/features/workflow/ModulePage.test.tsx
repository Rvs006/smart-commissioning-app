import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ModulePage } from "./ModulePage";

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
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ModulePage moduleRoute={route} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ModulePage discovery wiring", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("blocks a real scan until authorization is confirmed, then queues and renders live results", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
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

    // The real-scan queue button is disabled until the operator confirms.
    const queueButton = await screen.findByRole("button", { name: "Queue" });
    expect(queueButton).toBeDisabled();

    fireEvent.click(screen.getByLabelText(/I am authorized to scan this network/i));
    expect(queueButton).toBeEnabled();

    fireEvent.click(queueButton);

    // Run monitor appears and live discovered hosts render from the results payload.
    expect(await screen.findByText(/Discovery run monitor/i)).toBeInTheDocument();
    // hostname is unique to the live results payload (not present in sample rows).
    expect(await screen.findByText("plant-controller")).toBeInTheDocument();
    // Live banner is shown, not a fabricated "Result" verdict column.
    expect(screen.getByText(/Live discovery observations/i)).toBeInTheDocument();
  });

  it("shows a dry-run preview button that needs no authorization", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("ip-scanner");

    fireEvent.click(screen.getByLabelText(/Dry run/i));
    const previewButton = await screen.findByRole("button", { name: "Preview" });
    expect(previewButton).toBeEnabled();
  });
});

describe("ModulePage reports wiring", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("disables Export until a report has been queued", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );

    renderModule("reports");

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Export" })).toBeDisabled();
    });
  });
});
