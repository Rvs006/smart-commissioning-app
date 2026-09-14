import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { createSessionBoundApiClient } from "../../api/client";
import { SessionContext, type SessionContextValue } from "../../app/sessionContext";
import { DEFAULT_WORKSPACE, createSessionScopeId } from "../../app/sessionScope";

// Test-only wrapper for the scanner pages. It supplies the session through
// SessionContext directly rather than SessionProvider, so a test never has to
// stub /me just to get an engineer role, and keeps the query client isolated per
// render. Fetch stubbing stays in each test file.
export function scannerProviders(
  ui: ReactNode,
  options: { canEngineer?: boolean; authorizationEnforced?: boolean; initialEntry?: string } = {},
) {
  const sessionScopeId = createSessionScopeId();
  const workspace = DEFAULT_WORKSPACE;
  const value: SessionContextValue = {
    apiClient: createSessionBoundApiClient(sessionScopeId, workspace, "engineer-key"),
    authorizationEnforced: options.authorizationEnforced ?? false,
    canAdmin: false,
    canEngineer: options.canEngineer ?? true,
    error: null,
    hasApiKey: true,
    isLoading: false,
    me: {
      effective_scopes: [],
      global_scope: true,
      role: "engineer",
      source: "user_key",
      username: "engineer-1",
    },
    role: "engineer",
    sessionScopeId,
    signIn: () => {},
    signOut: () => {},
    workspace,
  };
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={value}>
        <MemoryRouter initialEntries={[options.initialEntry ?? "/"]}>{ui}</MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>
  );
}
