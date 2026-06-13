import { useCallback, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { clearApiKey, getApiKey, getMe, roleAtLeast, setApiKey } from "../api/client";
import { SessionContext, type SessionContextValue } from "./sessionContext";

// The hook (useSession), context, and shared tooltip constants live in
// sessionContext.ts (a non-component module). Import them from there. This file
// exports only the provider component so react-refresh stays happy.

// On load (when a key is configured) the provider calls GET /me to learn the
// principal's role, which the UI uses to gate engineer/admin actions. The key
// itself still lives in localStorage via the client helpers; this surfaces the
// current identity and a setter so the shell can show a sign-in field and a
// sign-out that clears the key + cached identity.
export function SessionProvider({ children }: { children: ReactNode }) {
  // Mirror the configured key into state so a sign-in/out re-renders consumers
  // and re-keys the /me query (localStorage changes alone would not).
  const [apiKey, setApiKeyState] = useState<string | null>(() => getApiKey());
  const hasApiKey = Boolean(apiKey);

  const meQuery = useQuery({
    enabled: hasApiKey,
    queryFn: getMe,
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
    const me = hasApiKey ? (meQuery.data ?? null) : null;
    const role = me?.role ?? null;
    return {
      canAdmin: roleAtLeast(role ?? undefined, "admin"),
      canEngineer: roleAtLeast(role ?? undefined, "engineer"),
      canReview: roleAtLeast(role ?? undefined, "reviewer"),
      error: meQuery.error,
      hasApiKey,
      isLoading: hasApiKey && meQuery.isLoading,
      me,
      role,
      signIn,
      signOut,
    };
  }, [hasApiKey, meQuery.data, meQuery.error, meQuery.isLoading, signIn, signOut]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
