import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { DashboardPage } from "../features/workflow/DashboardPage";
import { App } from "./App";

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

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => payload,
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
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("App shell", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/health")) {
          return jsonResponse(healthPayload);
        }
        if (url.endsWith("/api/v1/imports/profiles")) {
          return jsonResponse(profilesPayload);
        }
        throw new Error(`Unexpected fetch in test: ${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the brand, module navigation, and page title", async () => {
    renderApp();

    expect(screen.getByText("Smart Commissioning Tool")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Commissioning modules" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Configuration/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /UDMI Workbench/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Homepage" })).toBeInTheDocument();

    // Wait for the mocked health/profiles queries so nothing resolves after teardown.
    expect(await screen.findByText(/API ok/)).toBeInTheDocument();
  });

  it("renders the dashboard with API-backed readiness details", async () => {
    renderApp();

    expect(
      screen.getByRole("heading", { name: "Commissioning evidence workspace for site teams" }),
    ).toBeInTheDocument();

    expect(await screen.findByText(/API ok · 2 import profiles/)).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith("/api/v1/health", undefined);
    expect(fetch).toHaveBeenCalledWith("/api/v1/imports/profiles", undefined);
  });
});
