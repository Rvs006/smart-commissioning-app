import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createSessionBoundApiClient, type UserRecord } from "../../api/client";
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

function jsonResponse(payload: unknown): Response {
  return {
    json: async () => payload,
    ok: true,
    status: 200,
    statusText: "OK",
  } as unknown as Response;
}

function stubUsersApi(deactivateResponse?: Promise<Response>) {
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = String(input);
      const method = init?.method ?? "GET";

      if (url.endsWith("/api/v1/users") && method === "GET") {
        return jsonResponse([user]);
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

function renderUsers() {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  const sessionScopeId = createSessionScopeId();
  const session: SessionContextValue = {
    apiClient: createSessionBoundApiClient(sessionScopeId, DEFAULT_WORKSPACE, null),
    canAdmin: true,
    canEngineer: true,
    error: null,
    hasApiKey: false,
    isLoading: false,
    me: { role: "admin", source: "local", username: "local-admin" },
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
    expect(
      screen.getByText("API key for site-admin copied to the clipboard."),
    ).toBeInTheDocument();

    writeText.mockRejectedValueOnce(new Error("Clipboard blocked"));
    fireEvent.click(copyButton);
    expect(
      await screen.findByText(
        "Clipboard access was blocked. Select and copy the API key manually.",
      ),
    ).toBeInTheDocument();
  });
});
