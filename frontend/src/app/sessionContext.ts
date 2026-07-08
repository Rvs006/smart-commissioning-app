import { createContext, useContext } from "react";
import type { MeResponse, Role } from "../api/client";

// Session/role context value. Split out from the provider component so this
// module exports only non-component values (keeps react-refresh happy and lets
// hooks/components import the context + hook without pulling in the provider).
export type SessionContextValue = {
  // The resolved principal, or null when unauthenticated / not yet loaded.
  me: MeResponse | null;
  role: Role | null;
  // Convenience gates derived from the role. False while loading or signed out,
  // so actions stay disabled until the role is positively known (fail-closed).
  canEngineer: boolean;
  canAdmin: boolean;
  // True while the /me query is in flight (a key is set but role not yet known).
  isLoading: boolean;
  // The /me query errored (e.g. an invalid key -> 401). The shell surfaces this.
  error: unknown;
  // Whether an API key is currently configured (localStorage or env).
  hasApiKey: boolean;
  // Persist a new key and refetch /me. An empty/blank value is ignored.
  signIn: (key: string) => void;
  // Clear the stored key and the cached identity.
  signOut: () => void;
};

export const SessionContext = createContext<SessionContextValue | null>(null);

export function useSession(): SessionContextValue {
  const context = useContext(SessionContext);
  if (!context) {
    throw new Error("useSession must be used within a SessionProvider.");
  }
  return context;
}

// Shared tooltip copy for disabled role-gated actions, so a viewer/reviewer sees
// why an action is unavailable instead of triggering a 403 on click.
export const ENGINEER_REQUIRED_TOOLTIP = "Requires the engineer role or higher.";
export const ADMIN_REQUIRED_TOOLTIP = "Requires the admin role.";
