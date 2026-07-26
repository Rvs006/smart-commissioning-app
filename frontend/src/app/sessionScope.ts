declare const sessionScopeBrand: unique symbol;

/** Opaque identity for one authenticated browser session lifetime. */
export type SessionScopeId = string & { readonly [sessionScopeBrand]: true };

export type WorkspaceRef = Readonly<{
  projectId: string;
  siteId: string;
}>;

export type RunRef = Readonly<{
  sessionScopeId: SessionScopeId;
  workspace: WorkspaceRef;
  module: string;
  runId: string;
  family: "discovery" | "validation";
  jobType: string;
  origin: "submitted" | "restored" | "dashboard";
}>;

export const DEFAULT_WORKSPACE: WorkspaceRef = Object.freeze({
  projectId: "demo-project",
  siteId: "demo-site",
});

let nextSessionSequence = 0;

/**
 * Creates an opaque cache/request boundary. It deliberately contains no API-key
 * material, username, project name, or other value that could leak through
 * query-devtools or logs.
 */
export function createSessionScopeId(): SessionScopeId {
  nextSessionSequence += 1;
  const randomPart = globalThis.crypto?.randomUUID?.() ?? String(nextSessionSequence);
  return `session-${nextSessionSequence}-${randomPart}` as SessionScopeId;
}
