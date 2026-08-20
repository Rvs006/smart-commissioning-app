import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { clearApiKey } from "../../api/client";
import { SessionProvider } from "../../app/session";
import { RunHistoryPage } from "./RunHistoryPage";

// Run History renders the full /runs list as a sortable, filterable table with
// ABSOLUTE Started/Finished timestamps and a derived Duration, and exports the
// visible rows to CSV. Mirrors the HubPage.test harness (its sibling view) plus
// the ConfigurationPage object-URL stub for the CSV export assertion.

const runsPayload = {
  runs: [
    {
      run_id: "run-succeeded-1",
      job_type: "ip_discovery",
      status: "succeeded",
      stage: "register_comparison",
      progress_percent: 100,
      created_at: "2026-06-11T09:00:00Z",
      updated_at: "2026-06-11T09:05:00Z",
      edge_id: null,
    },
    {
      run_id: "run-running-1",
      job_type: "mqtt_discovery",
      status: "running",
      stage: "subscribing",
      progress_percent: 40,
      created_at: "2026-06-11T09:10:00Z",
      updated_at: "2026-06-11T09:11:00Z",
      edge_id: "edge-west-2",
    },
    {
      run_id: "run-failed-udmi",
      job_type: "udmi_validation",
      status: "failed",
      stage: "validation_failed",
      progress_percent: 72,
      created_at: "2026-06-11T09:20:00Z",
      updated_at: "2026-06-11T09:24:00Z",
      edge_id: null,
    },
    {
      run_id: "run-cancelled-udmi",
      job_type: "udmi_validation",
      status: "cancelled",
      stage: "cancelled",
      progress_percent: 45,
      created_at: "2026-06-11T09:30:00Z",
      updated_at: "2026-06-11T09:32:00Z",
      edge_id: null,
    },
  ],
};

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => payload,
  } as unknown as Response;
}

function stubRuns(payload: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/runs")) {
        return jsonResponse(payload);
      }
      if (url.endsWith("/api/v1/me")) {
        return jsonResponse({ role: "admin", source: "local", username: "local-admin" });
      }
      throw new Error(`Unexpected fetch in test: ${url}`);
    }),
  );
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionProvider>
        <MemoryRouter>
          <LocationProbe />
          <RunHistoryPage />
        </MemoryRouter>
      </SessionProvider>
    </QueryClientProvider>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="history-location">
      {location.pathname}
      {location.search}
    </output>
  );
}

describe("RunHistoryPage", () => {
  beforeEach(() => {
    // jsdom does not implement object-URL APIs; the CSV export path uses them.
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

  it("renders every run with absolute Started/Finished and a derived Duration", async () => {
    stubRuns(runsPayload);
    renderPage();

    // Both runs render, not just the newest few.
    expect(await screen.findByText("run-succeeded-1")).toBeInTheDocument();
    expect(screen.getByText("run-running-1")).toBeInTheDocument();

    // Job-type humanisation flows through the shared formatter (assert the cell,
    // since the label also appears in the filter <select>).
    expect(screen.getByRole("cell", { name: "IP discovery" })).toBeInTheDocument();

    // Started renders as an ABSOLUTE timestamp via the platform Intl API — assert
    // against the app's own formatting so the check is timezone/locale-agnostic.
    const startedAbsolute = new Date("2026-06-11T09:00:00Z").toLocaleString();
    expect(screen.getByText(startedAbsolute)).toBeInTheDocument();

    // The terminal run has a real Finished + Duration (09:00 -> 09:05 = 5m); the
    // in-flight run honestly shows neither (no fabricated finish).
    const finishedAbsolute = new Date("2026-06-11T09:05:00Z").toLocaleString();
    expect(screen.getByText(finishedAbsolute)).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "5m" })).toBeInTheDocument();
  });

  it("narrows the list when a status filter is applied", async () => {
    stubRuns(runsPayload);
    renderPage();

    expect(await screen.findByText("run-succeeded-1")).toBeInTheDocument();
    expect(screen.getByText("run-running-1")).toBeInTheDocument();

    // Filtering to succeeded drops the running run from the visible rows.
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "succeeded" } });

    expect(screen.getByText("run-succeeded-1")).toBeInTheDocument();
    expect(screen.queryByText("run-running-1")).not.toBeInTheDocument();
  });

  it("exports the visible rows via a client-side CSV download", async () => {
    stubRuns(runsPayload);
    renderPage();

    await screen.findByText("run-succeeded-1");
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));

    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  it("shows an empty state when no runs exist", async () => {
    stubRuns({ runs: [] });
    renderPage();

    expect(await screen.findByText("No runs to show")).toBeInTheDocument();
  });

  it("keeps a compatible sealed pair in the URL and exposes typed differences", async () => {
    const comparisonRuns = {
      runs: [
        ...runsPayload.runs,
        {
          ...runsPayload.runs[0],
          run_id: "run-succeeded-2",
          created_at: "2026-06-11T10:00:00Z",
          updated_at: "2026-06-11T10:06:00Z",
        },
      ],
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/runs")) return jsonResponse(comparisonRuns);
      if (url.includes("/api/v1/discovery/runs/run-succeeded-2/comparison")) {
        return jsonResponse({
          baseline_run_id: "run-succeeded-1",
          candidate_run_id: "run-succeeded-2",
          job_type: "ip_discovery",
          compatible: true,
          reason: null,
          additions: [{ key: "192.0.2.10", value: { ip_address: "192.0.2.10" } }],
          removals: [],
          changes: [],
        });
      }
      throw new Error(`Unexpected fetch in test: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();

    await screen.findByText("run-succeeded-1");
    fireEvent.change(screen.getByLabelText("Comparison baseline"), {
      target: { value: "run-succeeded-1" },
    });
    fireEvent.change(screen.getByLabelText("Comparison candidate"), {
      target: { value: "run-succeeded-2" },
    });
    expect(await screen.findByText("1 additions")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open in module" }));
    // A built-in ip_discovery run must open the built-in engine page
    // (/ip-scanner-sct), NOT the sidecar page that owns /ip-scanner.
    expect(screen.getByTestId("history-location")).toHaveTextContent(
      "/ip-scanner-sct?run=run-succeeded-2&compare=run-succeeded-1",
    );
  });

  it("opens the sidecar page for a sidecar discovery comparison", async () => {
    const comparisonRuns = {
      runs: [
        {
          ...runsPayload.runs[0],
          run_id: "run-scan-1",
          job_type: "bacnet_scanner",
          created_at: "2026-06-11T09:00:00Z",
          updated_at: "2026-06-11T09:05:00Z",
        },
        {
          ...runsPayload.runs[0],
          run_id: "run-scan-2",
          job_type: "bacnet_scanner",
          created_at: "2026-06-11T10:00:00Z",
          updated_at: "2026-06-11T10:06:00Z",
        },
      ],
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/runs")) return jsonResponse(comparisonRuns);
      if (url.includes("/api/v1/discovery/runs/run-scan-2/comparison")) {
        return jsonResponse({
          baseline_run_id: "run-scan-1",
          candidate_run_id: "run-scan-2",
          job_type: "bacnet_scanner",
          compatible: true,
          reason: null,
          additions: [],
          removals: [],
          changes: [],
        });
      }
      throw new Error(`Unexpected fetch in test: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();

    await screen.findByText("run-scan-1");
    fireEvent.change(screen.getByLabelText("Comparison baseline"), {
      target: { value: "run-scan-1" },
    });
    fireEvent.change(screen.getByLabelText("Comparison candidate"), {
      target: { value: "run-scan-2" },
    });
    // Wait for the compatible summary so the "Open in module" button is enabled.
    expect(await screen.findByText("0 additions")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open in module" }));
    expect(screen.getByTestId("history-location")).toHaveTextContent(
      "/bacnet-scanner?run=run-scan-2&compare=run-scan-1",
    );
  });

  it("downloads raw JSON evidence for a terminal UDMI run, including failed runs", async () => {
    const fileBlob = new Blob(['{"schema_version":"1.0"}'], { type: "application/json" });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/runs")) {
        return jsonResponse(runsPayload);
      }
      if (url.includes("/api/v1/validation/runs/run-failed-udmi/export.json")) {
        return {
          ok: true,
          status: 200,
          headers: { get: () => 'attachment; filename="stored-evidence.json"' },
          blob: async () => fileBlob,
        } as unknown as Response;
      }
      throw new Error(`Unexpected fetch in test: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    renderPage();

    await screen.findByText("run-failed-udmi");
    expect(
      screen.getByRole("button", { name: "Download raw JSON for run-cancelled-udmi" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Download raw JSON for run-failed-udmi" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("/api/v1/validation/runs/run-failed-udmi/export.json"),
        ),
      ).toBe(true);
      expect(clickSpy).toHaveBeenCalled();
    });
  });
});
