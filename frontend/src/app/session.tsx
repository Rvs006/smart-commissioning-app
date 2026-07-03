import { useCallback, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, clearApiKey, getApiKey, getMe, roleAtLeast, setApiKey } from "../api/client";
import type { MeResponse } from "../api/client";
import { SessionContext, type SessionContextValue } from "./sessionContext";

// The hook (useSession), context, and shared tooltip constants live in
// sessionContext.ts (a non-component module). Import them from there. This file
// exports only the provider component so react-refresh stays happy.

// On load the provider always calls GET /me to learn the principal's role,
// which the UI uses to gate engineer/admin actions. This matters on the
// local/portable profile, where a keyless loopback client is already granted
// admin server-side — so /me must be asked even with no key set. In hosted
// api_key mode an unauthenticated /me returns 401/403; the query resolves that
// to a null principal (actions stay disabled) instead of throwing. The key
// itself still lives in localStorage via the client helpers; this surfaces the
// current identity and a setter so the shell can show a sign-in field and a
// sign-out that clears the key + cached identity.
export function SessionProvider({ children }: { children: ReactNode }) {
  // Mirror the configured key into state so a sign-in/out re-renders consumers
  // and re-keys the /me query (localStorage changes alone would not).
  const [apiKey, setApiKeyState] = useState<string | null>(() => getApiKey());
  const hasApiKey = Boolean(apiKey);

  const meQuery = useQuery<MeResponse | null>({
    // Always ask /me: on the local profile a keyless loopback client resolves to
    // admin, so gating this on hasApiKey would leave engineer actions disabled.
    // With NO key configured, an unauthorized /me (hosted api_key mode, or a
    // non-loopback caller) is expected — resolve it to null (no principal) so the
    // query settles, actions stay disabled, and retry:false stops it from
    // looping. When a key IS set, a rejection is a bad/inactive key: let it throw
    // so the shell can surface "Key not recognised" exactly as before.
    queryFn: async () => {
      try {
        return await getMe();
      } catch (error) {
        if (
          !hasApiKey &&
          error instanceof ApiError &&
          (error.status === 401 || error.status === 403)
        ) {
          return null;
        }
        throw error;
      }
    },
    // Key the query on the api key so changing it refetches the principal.
    queryKey: ["me", apiKey],
    retry: false,
    staleTime: 60_000,
  });

  const signIn = useCallback((key: string) => {
    const trimmed = key.trim();
    if (!trimmed) {
      return;
    }
    setApiKey(trimmed);
    setApiKeyState(trimmed);
  }, []);

  const signOut = useCallback(() => {
    clearApiKey();
    setApiKeyState(null);
  }, []);

  const value = useMemo<SessionContextValue>(() => {
    // /me now drives the principal in both modes: a keyless loopback admin
    // (local profile) and a keyed/shared-key admin (hosted) alike. A keyless
    // unauthorized caller resolves to null here -> actions disabled; a rejected
    // key surfaces via meQuery.error (data stays undefined -> me null).
    const me = meQuery.data ?? null;
    const role = me?.role ?? null;
    return {
      canAdmin: roleAtLeast(role ?? undefined, "admin"),
      canEngineer: roleAtLeast(role ?? undefined, "engineer"),
      canReview: roleAtLeast(role ?? undefined, "reviewer"),
      error: meQuery.error,
      hasApiKey,
      isLoading: meQuery.isLoading,
      me,
      role,
      signIn,
      signOut,
    };
  }, [hasApiKey, meQuery.data, meQuery.error, meQuery.isLoading, signIn, signOut]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
