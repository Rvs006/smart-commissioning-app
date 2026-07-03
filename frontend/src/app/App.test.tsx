import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { clearApiKey, getApiKey, setApiKey } from "../api/client";
import { DashboardPage } from "../features/workflow/DashboardPage";
import { App } from "./App";
import { SessionProvider } from "./session";

const healthPayload = { status: "ok", timestamp: "2026-06-11T00:00:00Z" };

const profilesPayload = [
  {
    import_type: "ip_register",
    description: "Expected IP-addressable assets.",
    required_columns: ["asset_id", "ip_address"],
    duplicate_key_fields: ["asset_id"],
  },
  {
    import_type: "bacnet_points",
    description: "Expected BACnet points.",
    required_columns: ["asset_id", "point_name"],
    duplicate_key_fields: ["asset_id", "point_name"],
  },
];

const runsPayload = {
  runs: [
    {
      run_id: "run-001",
      job_type: "ip_discovery",
      status: "succeeded",
      stage: "register_comparison",
      progress_percent: 100,
      created_at: "2026-06-11T09:00:00Z",
      updated_at: "2026-06-11T09:05:00Z",
    },
    {
      run_id: "run-002",
      job_type: "mqtt_discovery",
      status: "running",
      stage: "subscribing",
      progress_percent: 40,
      created_at: "2026-06-11T09:10:00Z",
      updated_at: "2026-06-11T09:11:00Z",
    },
  ],
};

const emptyRunsPayload = { runs: [] };
const reportsPayload = { reports: [] };

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => payload,
  } as unknown as Response;
}

// A 401 the client turns into an ApiError; the keyless SessionProvider resolves
// it to a null principal (mirrors hosted api_key mode with no key set).
function unauthorizedResponse(): Response {
  return {
    ok: false,
    status: 401,
    statusText: "Unauthorized",
    json: async () => ({ detail: "Missing or invalid API key." }),
  } as unknown as Response;
}

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: { retry: false },
    },
  });
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: <App />,
        children: [{ index: true, element: <DashboardPage /> }],
      },
    ],
    { initialEntries: ["/"] },
  );

  return render(
    <QueryClientProvider client={queryClient}>
      <SessionProvider>
        <RouterProvider router={router} />
      </SessionProvider>
    </QueryClientProvider>,
  );
}

// Routes the dashboard's on-mount queries. `runsResponse` lets a test choose
// between a populated runs list and the empty-state payload; `meHandler` lets a
// test choose how /me answers (defaults to a 401, mirroring hosted api_key mode
// with no key configured).
function stubDashboardFetch(
  runsResponse: unknown = runsPayload,
  meHandler?: () => Response | Promise<Response>,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/me")) {
        // By default these App-shell tests run with no key configured. The
        // SessionProvider always calls /me; keyless, hosted api_key mode
        // answers 401, which the provider resolves to a null principal (badge
        // shows "Set API key").
        return meHandler ? meHandler() : unauthorizedResponse();
      }
      if (url.endsWith("/api/v1/health")) {
        return jsonResponse(healthPayload);
      }
      if (url.endsWith("/api/v1/imports/profiles")) {
        return jsonResponse(profilesPayload);
      }
      if (url.includes("/api/v1/runs")) {
        return jsonResponse(runsResponse);
      }
      if (url.endsWith("/api/v1/reports")) {
        return jsonResponse(reportsPayload);
      }
      if (url.endsWith("/api/v1/validation/runs")) {
        return jsonResponse(emptyRunsPayload);
      }
      throw new Error(`Unexpected fetch in test: ${url}`);
    }),
  );
}

describe("App shell", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  it("renders the brand, module navigation, and page title", async () => {
    stubDashboardFetch();
    renderApp();

    expect(screen.getByText("Smart Commissioning Tool")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Commissioning modules" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Configuration/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /UDMI Workbench/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Homepage" })).toBeInTheDocument();

    // Wait for the mocked health query so nothing resolves after teardown.
    expect(await screen.findByText("ok")).toBeInTheDocument();
  });

  it("renders recent runs from the live /runs response", async () => {
    stubDashboardFetch();
    renderApp();

    expect(
      screen.getByRole("heading", { name: "Commissioning evidence workspace for site teams" }),
    ).toBeInTheDocument();

    // Rows render from the API payload, not from removed demo constants.
    expect(await screen.findByText("IP discovery")).toBeInTheDocument();
    expect(await screen.findByText("MQTT discovery")).toBeInTheDocument();

    expect(fetch).toHaveBeenCalledWith("/api/v1/health", undefined);
    expect(fetch).toHaveBeenCalledWith("/api/v1/imports/profiles", undefined);
    expect(fetch).toHaveBeenCalledWith("/api/v1/runs?limit=50", undefined);
  });

  it("shows the recent-runs empty state when no runs exist", async () => {
    stubDashboardFetch(emptyRunsPayload);
    renderApp();

    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
  });

  it("offers to clear the key only when the server rejects it (401)", async () => {
    setApiKey("a-key-the-server-rejects");
    stubDashboardFetch(); // /me answers 401 by default
    renderApp();

    expect(await screen.findByText("Key not recognised")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear key" })).toBeInTheDocument();
  });

  it("keeps the key and never offers to clear it when /me fails in transit", async () => {
    // Field regression guard: a backend restart / network blip used to render
    // "Key not recognised" + "Clear key", and clearing destroyed a key that is
    // displayed only once. A transport failure says nothing about the key.
    setApiKey("a-perfectly-valid-key");
    stubDashboardFetch(runsPayload, () =>
      Promise.reject(new TypeError("Failed to fetch")),
    );
    renderApp();

    expect(await screen.findByText(/Server unreachable/)).toBeInTheDocument();
    expect(screen.queryByText("Key not recognised")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear key" })).not.toBeInTheDocument();
    // The stored key survives the outage untouched.
    expect(getApiKey()).toBe("a-perfectly-valid-key");
  });
});
