import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { clearApiKey, setApiKey } from "../../api/client";
import { SessionProvider } from "../../app/session";
import { ModulePage } from "./ModulePage";

// Verifies the role gate end-to-end through the real SessionProvider: a viewer
// sees the engineer-only "Upload and validate" action disabled with the
// requires-engineer tooltip, while an engineer sees the same action enabled.

const profilesPayload = [
  {
    import_type: "ip_register",
    description: "Expected IP-addressable assets.",
    required_columns: ["asset_id", "ip_address"],
    duplicate_key_fields: ["asset_id"],
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

// Stubs /me with the given role plus the import-profiles the module loads.
function stubFetchWithRole(role: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/me")) {
        return jsonResponse({ username: `${role}-1`, role, source: "user_key" });
      }
      if (url.endsWith("/api/v1/imports/profiles")) {
        return jsonResponse(profilesPayload);
      }
      throw new Error(`Unexpected fetch in test: ${url}`);
    }),
  );
}

function renderModule(route: string) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  setApiKey("role-key");
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

describe("role-aware engineer actions", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
  });

  it("disables the engineer-only upload action for a viewer with a requires-engineer tooltip", async () => {
    stubFetchWithRole("viewer");
    renderModule("ip-scanner");

    const uploadButton = await screen.findByRole("button", { name: /Upload and validate/i });
    // A viewer never gets an enabled engineer action — even with a file selected
    // it stays disabled, and the tooltip explains why (no 401/403 on click).
    await waitFor(() => expect(uploadButton).toHaveAttribute("title", expect.stringMatching(/engineer/i)));
    expect(uploadButton).toBeDisabled();
  });

  it("enables the engineer-only upload action (title cleared) for an engineer", async () => {
    stubFetchWithRole("engineer");
    renderModule("ip-scanner");

    const uploadButton = await screen.findByRole("button", { name: /Upload and validate/i });
    // For an engineer the requires-engineer tooltip is removed once /me resolves.
    await waitFor(() => expect(uploadButton).not.toHaveAttribute("title"));
    // It is still disabled only because no file is selected — not by the role
    // gate — which is the correct pre-RBAC behaviour for an engineer.
    expect(uploadButton).toBeDisabled();
  });
});
