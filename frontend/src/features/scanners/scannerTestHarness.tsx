import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { createSessionBoundApiClient } from "../../api/client";
import { SessionContext, type SessionContextValue } from "../../app/sessionContext";
import { DEFAULT_WORKSPACE, createSessionScopeId } from "../../app/sessionScope";
import type { SessionScopeId, WorkspaceRef } from "../../app/sessionScope";

// Test-only wrapper for the scanner pages. It supplies the session through
// SessionContext directly rather than SessionProvider, so a test never has to
// stub /me just to get an engineer role, and keeps the query client isolated per
// render. Fetch stubbing stays in each test file.
export function createScannerQueryClient() {
  return new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
}

export function scannerProviders(
  ui: ReactNode,
  options: {
    canEngineer?: boolean;
    authorizationEnforced?: boolean;
    initialEntry?: string;
    queryClient?: QueryClient;
    // A different project/site, for the tests that prove workspace-scoped state
    // is withdrawn on a switch.
    workspace?: WorkspaceRef;
    sessionScopeId?: SessionScopeId;
    // /me still in flight. authorizationEnforced defaults TRUE in that window
    // (session.tsx fails closed), so a page that reads scanAuthorized before the
    // principal lands sees "not authorized" and must wait rather than decide.
    sessionLoading?: boolean;
  } = {},
) {
  const sessionScopeId = options.sessionScopeId ?? createSessionScopeId();
  const workspace = options.workspace ?? DEFAULT_WORKSPACE;
  const loading = options.sessionLoading ?? false;
  const value: SessionContextValue = {
    apiClient: createSessionBoundApiClient(sessionScopeId, workspace, "engineer-key"),
    authorizationEnforced: loading ? true : (options.authorizationEnforced ?? false),
    canAdmin: false,
    canEngineer: loading ? false : (options.canEngineer ?? true),
    error: null,
    hasApiKey: true,
    isLoading: loading,
    me: loading
      ? null
      : {
          effective_scopes: [],
          global_scope: true,
          role: "engineer",
          source: "user_key",
          username: "engineer-1",
        },
    role: loading ? null : "engineer",
    sessionScopeId,
    signIn: () => {},
    signOut: () => {},
    workspace,
  };
  const queryClient = options.queryClient ?? createScannerQueryClient();
  return (
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={value}>
        <MemoryRouter initialEntries={[options.initialEntry ?? "/"]}>{ui}</MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>
  );
}
