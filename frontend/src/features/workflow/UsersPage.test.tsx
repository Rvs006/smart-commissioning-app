import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import {
  createSessionBoundApiClient,
  type MeResponse,
  type ScopeGrantRecord,
  type UserRecord,
} from "../../api/client";
import { SessionContext, type SessionContextValue } from "../../app/sessionContext";
import { createSessionScopeId, DEFAULT_WORKSPACE } from "../../app/sessionScope";
import { UsersPage } from "./UsersPage";

const user: UserRecord = {
  created_at: "2026-07-24T09:00:00Z",
  id: "user-1",
  is_active: true,
  last_used_at: null,
  role: "admin",
  username: "site-admin",
};

const grant: ScopeGrantRecord = {
  active: true,
  grant_id: "grant-1",
  granted_at: "2026-08-10T09:15:00Z",
  granted_by: "local-admin",
  project_id: "project-a",
  reason: "Commissioning team assignment",
  revoke_reason: null,
  revoked_at: null,
  revoked_by: null,
  site_id: "site-1",
  user_id: user.id,
};

function jsonResponse(payload: unknown): Response {
  return {
    json: async () => payload,
    ok: true,
    status: 200,
    statusText: "OK",
  } as unknown as Response;
}

function stubUsersApi(
  deactivateResponse?: Promise<Response>,
  options: {
    grants?: ScopeGrantRecord[];
    preflight?: {
      active_named_admin_count: number;
      ready: boolean;
      unscoped_active_non_admin_users: Array<{
        id: string;
        role: UserRecord["role"];
        username: string;
      }>;
    };
  } = {},
) {
  const grants = options.grants ?? [grant];
  const preflight = options.preflight ?? {
    active_named_admin_count: 1,
    ready: true,
    unscoped_active_non_admin_users: [],
  };
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = String(input);
      const method = init?.method ?? "GET";

      if (url.endsWith("/api/v1/users") && method === "GET") {
        return jsonResponse([user]);
      }
      if (url.endsWith("/api/v1/users/scope-activation-preflight") && method === "GET") {
        return jsonResponse(preflight);
      }
      if (
        url.endsWith("/api/v1/users/user-1/scope-grants?include_revoked=true") &&
        method === "GET"
      ) {
        return jsonResponse(grants);
      }
      if (url.endsWith("/api/v1/users/user-1/scope-grants") && method === "POST") {
        const payload = JSON.parse(String(init?.body)) as {
          project_id: string;
          reason: string;
          site_id: string;
        };
        return jsonResponse({
          ...grant,
          grant_id: "grant-created",
          project_id: payload.project_id,
          reason: payload.reason,
          site_id: payload.site_id,
        });
      }
      if (url.endsWith("/api/v1/users/user-1/scope-grants/grant-1/revoke") && method === "POST") {
        return jsonResponse({
          ...grant,
          active: false,
          revoke_reason: "Device moved to another contractor",
          revoked_at: "2026-08-10T11:30:00Z",
          revoked_by: "local-admin",
        });
      }
      if (url.endsWith("/api/v1/users/user-1/key") && method === "POST") {
        return jsonResponse({ api_key: "new-secret-key", user });
      }
      if (url.endsWith("/api/v1/users/user-1/deactivate") && method === "POST") {
        return deactivateResponse ?? jsonResponse({ ...user, is_active: false });
      }

      throw new Error(`Unexpected fetch in test: ${method} ${url}`);
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderUsers(meOverrides: Partial<MeResponse> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  const sessionScopeId = createSessionScopeId();
  const session: SessionContextValue = {
    apiClient: createSessionBoundApiClient(sessionScopeId, DEFAULT_WORKSPACE, null),
    authorizationEnforced: true,
    canAdmin: true,
    canEngineer: true,
    error: null,
    hasApiKey: false,
    isLoading: false,
    me: {
      effective_scopes: [],
      global_scope: true,
      role: "admin",
      source: "local",
      username: "local-admin",
      ...meOverrides,
    },
    role: "admin",
    sessionScopeId,
    signIn: vi.fn(),
    signOut: vi.fn(),
    workspace: DEFAULT_WORKSPACE,
  };

  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={session}>
        <UsersPage />
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("UsersPage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  it("does not re-issue or deactivate a user when confirmation is cancelled", async () => {
    const fetchMock = stubUsersApi();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderUsers();

    expect(await screen.findByRole("cell", { name: "site-admin" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Re-issue key" }));
    fireEvent.click(screen.getByRole("button", { name: "Deactivate" }));

    expect(confirm).toHaveBeenNthCalledWith(
      1,
      "Re-issue the API key for site-admin? Their current key will stop working immediately.",
    );
    expect(confirm).toHaveBeenNthCalledWith(
      2,
      "Deactivate site-admin? They will lose access immediately and cannot sign in with their current key.",
    );
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
  });

  it("deactivates once after confirmation and blocks duplicate clicks while pending", async () => {
    const pendingDeactivate = new Promise<Response>(() => undefined);
    const fetchMock = stubUsersApi(pendingDeactivate);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderUsers();

    expect(await screen.findByRole("cell", { name: "site-admin" })).toBeInTheDocument();
    const deactivateButton = screen.getByRole("button", { name: "Deactivate" });
    fireEvent.click(deactivateButton);

    expect(confirm).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(deactivateButton).toBeDisabled());
    fireEvent.click(deactivateButton);

    const deactivateCalls = fetchMock.mock.calls.filter(
      ([input, init]) =>
        String(input).endsWith("/api/v1/users/user-1/deactivate") && init?.method === "POST",
    );
    expect(deactivateCalls).toHaveLength(1);
  });

  it("re-issues a key after confirmation and reports clipboard success or failure", async () => {
    const fetchMock = stubUsersApi();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const writeText = vi.fn<(value: string) => Promise<void>>().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    renderUsers();

    expect(await screen.findByRole("cell", { name: "site-admin" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Re-issue key" }));

    const copyButton = await screen.findByRole("button", { name: "Copy API key" });
    expect(screen.getByText("new-secret-key")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/users/user-1/key",
      expect.objectContaining({ method: "POST" }),
    );

    fireEvent.click(copyButton);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("new-secret-key"));
    expect(screen.getByText("API key for site-admin copied to the clipboard.")).toBeInTheDocument();

    writeText.mockRejectedValueOnce(new Error("Clipboard blocked"));
    fireEvent.click(copyButton);
    expect(
      await screen.findByText(
        "Clipboard access was blocked. Select and copy the API key manually.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the current principal's effective scope and activation readiness", async () => {
    stubUsersApi();
    renderUsers();

    expect(await screen.findByText("Global project and site access")).toBeInTheDocument();
    expect(await screen.findByText("Ready for scope enforcement")).toBeInTheDocument();
    expect(
      screen.getByText(/1 active named admin can recover access\./),
    ).toBeInTheDocument();
  });

  it("shows scoped access from /me and withholds global controls", () => {
    const fetchMock = stubUsersApi();
    renderUsers({
      effective_scopes: [
        { project_id: "project-b", site_id: "north-plant" },
        { project_id: "project-b", site_id: "south-plant" },
      ],
      global_scope: false,
      role: "engineer",
      source: "user_key",
      username: "field-engineer",
    });

    expect(screen.getByText("project-b / north-plant")).toBeInTheDocument();
    expect(screen.getByText("project-b / south-plant")).toBeInTheDocument();
    expect(screen.getByText("Global admin access required")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create user" })).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("creates a project and site grant with the entered audit reason", async () => {
    const fetchMock = stubUsersApi(undefined, { grants: [] });
    renderUsers();

    expect(await screen.findByRole("option", { name: "site-admin" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Project ID"), { target: { value: "project-z" } });
    fireEvent.change(screen.getByLabelText("Site ID"), { target: { value: "plant-room-4" } });
    fireEvent.change(screen.getByLabelText("Grant reason"), {
      target: { value: "Assigned for the Tuesday commissioning visit" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Grant access" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/users/user-1/scope-grants",
        expect.objectContaining({
          body: JSON.stringify({
            project_id: "project-z",
            reason: "Assigned for the Tuesday commissioning visit",
            site_id: "plant-room-4",
          }),
          method: "POST",
        }),
      ),
    );
  });

  it("requires and sends an audit reason when revoking a grant", async () => {
    const fetchMock = stubUsersApi();
    renderUsers();

    expect(await screen.findByText("project-a / site-1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Revoke project-a / site-1" }));

    const confirmButton = screen.getByRole("button", { name: "Confirm revocation" });
    expect(confirmButton).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Revocation reason"), {
      target: { value: "Device moved to another contractor" },
    });
    fireEvent.click(confirmButton);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/users/user-1/scope-grants/grant-1/revoke",
        expect.objectContaining({
          body: JSON.stringify({ reason: "Device moved to another contractor" }),
          method: "POST",
        }),
      ),
    );
  });

  it("names every unscoped user blocking scope activation", async () => {
    stubUsersApi(undefined, {
      preflight: {
        active_named_admin_count: 1,
        ready: false,
        unscoped_active_non_admin_users: [
          { id: "user-2", role: "engineer", username: "north-engineer" },
          { id: "user-3", role: "viewer", username: "audit-viewer" },
        ],
      },
    });
    renderUsers();

    expect(await screen.findByText("Scope enforcement is blocked")).toBeInTheDocument();
    expect(screen.getByText("north-engineer (engineer)")).toBeInTheDocument();
    expect(screen.getByText("audit-viewer (viewer)")).toBeInTheDocument();
  });
});
