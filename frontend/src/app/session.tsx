import { useCallback, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  clearApiKey,
  getApiKey,
  getMe,
  isAuthRejection,
  roleAtLeast,
  setApiKey,
} from "../api/client";
import type { MeResponse } from "../api/client";
import { SessionContext, type SessionContextValue } from "./sessionContext";

// While /me fails for a TRANSIENT reason (server restarting, Wi-Fi blip on a
// multi-homed field laptop), re-ask on this interval so the session heals by
// itself once the backend is reachable again. Auth rejections never re-poll.
const TRANSIENT_ME_RETRY_MS = 15_000;

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
    // looping. When a key IS set, any failure still throws, and the shell reads
    // the error's KIND off meQuery.error: only a real 401/403 means the key was
    // rejected; a network failure / 5xx means the server is unreachable and the
    // key may be fine (see isAuthRejection + SessionBadge).
    queryFn: async () => {
      try {
        return await getMe();
      } catch (error) {
        if (!hasApiKey && isAuthRejection(error)) {
          return null;
        }
        throw error;
      }
    },
    // Key the query on the api key so changing it refetches the principal.
    queryKey: ["me", apiKey],
    // While the failure is transient (NOT an auth rejection), quietly re-ask so
    // the session heals once the server is back; a rejected key never re-polls.
    refetchInterval: (query) =>
      query.state.status === "error" && !isAuthRejection(query.state.error)
        ? TRANSIENT_ME_RETRY_MS
        : false,
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
    // unauthorized caller resolves to null here -> actions disabled. An auth
    // REJECTION (401/403) drops the principal even if a previous /me success is
    // still cached: a key the server just refused must not keep granting UI
    // access. A transient error keeps the cached principal (if any) so a brief
    // server restart or network blip never degrades a working session.
    const me = isAuthRejection(meQuery.error) ? null : (meQuery.data ?? null);
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
