import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { clearApiKey, setApiKey } from "../api/client";
import { SessionProvider } from "./session";
import { useSession } from "./sessionContext";

// `npm run dev` wraps the app in React.StrictMode (main.tsx), which mounts the
// tree, runs every effect cleanup, then remounts it with the SAME memoized
// apiClient. The provider's cleanup aborts that client, so if the client stayed
// aborted every request the remounted tree issued (the /me query included)
// would reject with "signal is aborted without reason" and the whole dev
// session would read as API-offline. The production build never double-mounts,
// so only a StrictMode render catches the regression.

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => payload,
  } as unknown as Response;
}

function RoleProbe() {
  const { error, isLoading, role } = useSession();
  return (
    <output>
      {isLoading
        ? "loading"
        : error instanceof Error
          ? `error: ${error.message}`
          : (role ?? "none")}
    </output>
  );
}

describe("SessionProvider under React.StrictMode", () => {
  afterEach(() => {
    clearApiKey();
    vi.unstubAllGlobals();
  });

  it("still resolves /me after StrictMode's simulated unmount and remount", async () => {
    setApiKey("strict-mode-key");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        // Real fetch rejects outright when handed an already-aborted signal.
        if (init?.signal?.aborted) {
          throw init.signal.reason;
        }
        return jsonResponse({ username: "engineer-1", role: "engineer", source: "user_key" });
      }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <StrictMode>
        <QueryClientProvider client={queryClient}>
          <SessionProvider>
            <RoleProbe />
          </SessionProvider>
        </QueryClientProvider>
      </StrictMode>,
    );

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("engineer"));
  });
});
