import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { clearApiKey } from "../../api/client";
import { HubPage } from "./HubPage";

// The hub view renders cross-project runs from listRuns, showing honest edge
// attribution (a populated edge id vs "Local edge" for null), and an empty
// state when no runs match.

const runsPayload = {
  runs: [
    {
      run_id: "run-local-1",
      job_type: "ip_discovery",
      status: "succeeded",
      stage: "register_comparison",
      progress_percent: 100,
      created_at: "2026-06-11T09:00:00Z",
      updated_at: "2026-06-11T09:05:00Z",
      edge_id: null,
    },
    {
      run_id: "run-edge-1",
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

function renderHub() {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <HubPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("HubPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  it("renders cross-project runs with honest edge attribution", async () => {
    stubRuns(runsPayload);
    renderHub();

    // Both runs render in the table.
    expect(await screen.findByText("run-local-1")).toBeInTheDocument();
    expect(await screen.findByText("run-edge-1")).toBeInTheDocument();

    // The ingested run shows its originating edge id; the local run is labelled
    // honestly as "Local edge" rather than fabricating an edge.
    expect(screen.getByText("edge-west-2")).toBeInTheDocument();
    expect(screen.getByText("Local edge")).toBeInTheDocument();

    // Job-type humanisation flows through the shared formatter. The label also
    // appears in the filter <select> options, so assert on the table cell.
    expect(screen.getByRole("cell", { name: "IP discovery" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "MQTT discovery" })).toBeInTheDocument();
  });

  it("shows an empty state when no runs match the view", async () => {
    stubRuns({ runs: [] });
    renderHub();

    expect(await screen.findByText("No runs match this view")).toBeInTheDocument();
  });
});
