import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { clearApiKey } from "../../api/client";
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
      <MemoryRouter>
        <RunHistoryPage />
      </MemoryRouter>
    </QueryClientProvider>,
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
});
