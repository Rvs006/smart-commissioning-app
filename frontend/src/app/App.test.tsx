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

// Defaults mount the dashboard at "/". `route` lets a test park the shell on
// another path with a stub child element, so App-shell assertions (the page h1)
// need no fetch stubs for the real page component.
function renderApp(
  route?: { path: string; initialEntry: string },
) {
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
        children: route
          ? [{ path: route.path, element: <div /> }]
          : [{ index: true, element: <DashboardPage /> }],
      },
    ],
    { initialEntries: [route ? route.initialEntry : "/"] },
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
    vi.unstubAllEnvs();
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

    // The fictional "Block B Plantroom" site pill was purged from the header.
    expect(screen.queryByText("Block B Plantroom")).not.toBeInTheDocument();

    // Wait for the mocked health query so nothing resolves after teardown.
    expect(await screen.findByText("ok")).toBeInTheDocument();
  });

  it("shows the build-stamped version in the brand bar", async () => {
    // field engineer 2026-07-15: no way to tell which build is on screen. build.ps1 sets
    // VITE_APP_VERSION before `npm run build`; vite bakes it into the bundle.
    vi.stubEnv("VITE_APP_VERSION", "v9.9.9");
    stubDashboardFetch();
    renderApp();

    expect(screen.getByTitle("App version")).toHaveTextContent("v9.9.9");

    expect(await screen.findByText("ok")).toBeInTheDocument();
  });

  it("falls back to 'dev' when no version was baked in", async () => {
    // Dev servers and the test run have no VITE_APP_VERSION (no frontend/.env*
    // file exists). `||` not `??`, so a future CI step exporting an empty
    // VITE_APP_VERSION="" also lands here instead of rendering a blank pill.
    stubDashboardFetch();
    renderApp();

    expect(screen.getByTitle("App version")).toHaveTextContent("dev");

    expect(await screen.findByText("ok")).toBeInTheDocument();
  });

  it("renders an empty-string version as 'dev' rather than a blank pill", async () => {
    vi.stubEnv("VITE_APP_VERSION", "");
    stubDashboardFetch();
    renderApp();

    expect(screen.getByTitle("App version")).toHaveTextContent("dev");

    expect(await screen.findByText("ok")).toBeInTheDocument();
  });

  it("names all three discovery heads '<Protocol> Discovery' in the menu", async () => {
    // field engineer 2026-07-15: "all discovery or none" — the BACnet entry read plain
    // "BACnet" while its neighbours read "IP Discovery" / "MQTT Discovery".
    // Exact names, never regexes: /BACnet/ also matches the separate
    // "BACnet to MQTT Validation" link and would pass even if this regressed.
    stubDashboardFetch();
    renderApp();

    expect(screen.getByRole("link", { name: "IP Discovery" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "BACnet Discovery" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "MQTT Discovery" })).toBeInTheDocument();

    expect(await screen.findByText("ok")).toBeInTheDocument();
  });

  it("titles the /ip-scanner page 'IP Discovery', matching its menu entry", async () => {
    // The pageTitles h1 layer used to say "IP Scanner" while the menu said "IP
    // Discovery" — the same head named two things on one screen.
    stubDashboardFetch();
    renderApp({ path: "ip-scanner", initialEntry: "/ip-scanner" });

    expect(screen.getByRole("heading", { level: 1, name: "IP Discovery" })).toBeInTheDocument();

    // Settle the session query so nothing resolves after teardown.
    expect(await screen.findByRole("button", { name: "Set API key" })).toBeInTheDocument();
  });

  it("renders recent runs from the live /runs response", async () => {
    stubDashboardFetch();
    renderApp();

    expect(
      screen.getByRole("heading", { name: "Commissioning evidence workspace for site teams" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Highest-priority issue" })).toBeInTheDocument();
    expect(screen.queryByText("Blocking Finding")).not.toBeInTheDocument();

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

  it("hides Set API key and shows the local identity on the keyless local profile", async () => {
    // Portable exe profile (AUTH_MODE=local): a keyless loopback client is
    // already admin server-side. Offering "Set API key" here misled a field
    // user into thinking a key was required — the badge must state the
    // signed-in fact instead, with no key affordances at all.
    stubDashboardFetch(runsPayload, () =>
      jsonResponse({ username: "local", role: "admin", source: "local" }),
    );
    renderApp();

    expect(await screen.findByText("Signed in as local admin")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set API key" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("still offers Set API key when a keyless /me is unauthorized (hosted mode)", async () => {
    stubDashboardFetch(); // /me answers 401 by default, mirroring hosted api_key mode
    renderApp();

    expect(await screen.findByRole("button", { name: "Set API key" })).toBeInTheDocument();
    expect(screen.queryByText(/Signed in as local/)).not.toBeInTheDocument();
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
