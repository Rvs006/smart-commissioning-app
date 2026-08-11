import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createUser,
  createUserScopeGrant,
  deactivateUser,
  getScopeActivationPreflight,
  listUsers,
  listUserScopeGrants,
  reissueUserKey,
  revokeUserScopeGrant,
  ROLE_ORDER,
  updateUserRole,
  type MeResponse,
  type Role,
  type ScopeGrantRecord,
  type UserRecord,
} from "../../api/client";
import { useSession } from "../../app/sessionContext";
import { mutationKeys, queryKeys } from "../../api/queryKeys";
import { formatRelativeTime } from "./runFormat";

// Global-admin user management. Role and /me scope are both checked here, and
// every backend mutation is independently global-admin gated. A scoped caller
// who deep-links here can inspect their effective scopes but gets no controls.
export function UsersPage() {
  const { apiClient, canAdmin, me, sessionScopeId, workspace } = useSession();
  const queryClient = useQueryClient();
  const [newUsername, setNewUsername] = useState("");
  const [newRole, setNewRole] = useState<Role>("viewer");
  const [issuedKey, setIssuedKey] = useState<{ username: string; apiKey: string } | null>(null);
  const [issuedKeyStatus, setIssuedKeyStatus] = useState("");
  const [selectedUserId, setSelectedUserId] = useState("");
  const [grantProjectId, setGrantProjectId] = useState("");
  const [grantSiteId, setGrantSiteId] = useState("");
  const [grantReason, setGrantReason] = useState("");
  const [revokeTarget, setRevokeTarget] = useState<ScopeGrantRecord | null>(null);
  const [revokeReason, setRevokeReason] = useState("");
  const hasGlobalAdmin = canAdmin && me?.global_scope === true;

  const usersQuery = useQuery({
    enabled: hasGlobalAdmin,
    queryFn: ({ signal }) => listUsers({ client: apiClient, signal }),
    queryKey: queryKeys.users(sessionScopeId, workspace),
  });

  const users = usersQuery.data ?? [];
  const selectedActiveUser = users.find(
    (user) => user.id === selectedUserId && user.is_active,
  );
  const effectiveSelectedUserId =
    selectedActiveUser?.id || users.find((user) => user.is_active)?.id || "";

  const preflightQuery = useQuery({
    enabled: hasGlobalAdmin,
    queryFn: ({ signal }) => getScopeActivationPreflight({ client: apiClient, signal }),
    queryKey: queryKeys.scopeActivationPreflight(sessionScopeId, workspace),
  });

  const grantsQuery = useQuery({
    enabled: hasGlobalAdmin && Boolean(effectiveSelectedUserId),
    queryFn: ({ signal }) =>
      listUserScopeGrants(effectiveSelectedUserId, {
        context: { client: apiClient, signal },
        includeRevoked: true,
      }),
    queryKey: queryKeys.userScopeGrants(
      sessionScopeId,
      workspace,
      effectiveSelectedUserId || "none",
    ),
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.users(sessionScopeId, workspace) });
  };

  const refreshScopeData = (userId: string) => {
    void queryClient.invalidateQueries({
      queryKey: queryKeys.userScopeGrants(sessionScopeId, workspace, userId),
    });
    void queryClient.invalidateQueries({
      queryKey: queryKeys.scopeActivationPreflight(sessionScopeId, workspace),
    });
  };

  const createMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "users.create"),
    mutationFn: () =>
      createUser({
        context: { client: apiClient },
        role: newRole,
        username: newUsername.trim(),
      }),
    onSuccess: (result) => {
      setIssuedKey({ apiKey: result.api_key, username: result.user.username });
      setIssuedKeyStatus("");
      setNewUsername("");
      setNewRole("viewer");
      refresh();
    },
  });

  const deactivateMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "users.deactivate"),
    mutationFn: (userId: string) => deactivateUser(userId, { client: apiClient }),
    onSuccess: refresh,
  });

  // Lost-key recovery: keys are displayed once and can never be retrieved, so
  // the only way back is a fresh key. Re-issuing invalidates the old key
  // immediately; the new plaintext lands in the same issued-key panel as create.
  const reissueMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "users.reissue"),
    mutationFn: (userId: string) => reissueUserKey(userId, { client: apiClient }),
    onSuccess: (result) => {
      setIssuedKey({ apiKey: result.api_key, username: result.user.username });
      setIssuedKeyStatus("");
      refresh();
    },
  });

  const roleMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "users.role"),
    mutationFn: (input: { userId: string; role: Role }) =>
      updateUserRole(input.userId, input.role, { client: apiClient }),
    onSuccess: refresh,
  });

  const grantMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "users.scope-grant.create"),
    mutationFn: () =>
      createUserScopeGrant({
        context: { client: apiClient },
        projectId: grantProjectId.trim(),
        reason: grantReason.trim(),
        siteId: grantSiteId.trim(),
        userId: effectiveSelectedUserId,
      }),
    onSuccess: () => {
      setGrantProjectId("");
      setGrantSiteId("");
      setGrantReason("");
      refreshScopeData(effectiveSelectedUserId);
    },
  });

  const revokeMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "users.scope-grant.revoke"),
    mutationFn: (input: { grant: ScopeGrantRecord; reason: string }) =>
      revokeUserScopeGrant({
        context: { client: apiClient },
        grantId: input.grant.grant_id,
        reason: input.reason,
        userId: input.grant.user_id,
      }),
    onSuccess: (_, input) => {
      setRevokeTarget(null);
      setRevokeReason("");
      refreshScopeData(input.grant.user_id);
    },
  });

  const copyIssuedKey = async () => {
    if (!issuedKey) {
      return;
    }
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error("Clipboard API unavailable");
      }
      await navigator.clipboard.writeText(issuedKey.apiKey);
      setIssuedKeyStatus(`API key for ${issuedKey.username} copied to the clipboard.`);
    } catch {
      setIssuedKeyStatus("Clipboard access was blocked. Select and copy the API key manually.");
    }
  };

  const confirmReissue = (user: UserRecord) => {
    if (
      window.confirm(
        `Re-issue the API key for ${user.username}? Their current key will stop working immediately.`,
      )
    ) {
      reissueMutation.mutate(user.id);
    }
  };

  const confirmDeactivate = (user: UserRecord) => {
    if (
      window.confirm(
        `Deactivate ${user.username}? They will lose access immediately and cannot sign in with their current key.`,
      )
    ) {
      deactivateMutation.mutate(user.id);
    }
  };

  if (!hasGlobalAdmin) {
    return (
      <div className="app-page users-page">
        <CurrentScopeSummary me={me} />
        <div className="state-panel error" role="alert">
          <strong>Global admin access required</strong>
          <span>
            User and scope management requires an active named global admin, or the local standalone
            administrator.
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="app-page users-page">
      <CurrentScopeSummary me={me} />

      <section className="surface" aria-labelledby="scope-readiness-heading">
        <div className="surface-heading">
          <div>
            <h3 id="scope-readiness-heading">Scope enforcement readiness</h3>
            <p className="muted">
              Check this before enabling project and site isolation on an edge or hub deployment.
            </p>
          </div>
        </div>

        {preflightQuery.isError ? (
          <div className="state-panel error" role="alert">
            <strong>Could not check scope enforcement</strong>
            <span>
              {preflightQuery.error instanceof Error
                ? preflightQuery.error.message
                : "Request failed."}
            </span>
          </div>
        ) : preflightQuery.isLoading ? (
          <div className="state-panel" role="status">
            <strong>Checking scope enforcement...</strong>
            <span>Comparing active named users with their current grants.</span>
          </div>
        ) : preflightQuery.data?.ready ? (
          <div className="state-panel success" role="status">
            <strong>Ready for scope enforcement</strong>
            <span>
              {namedAdminRecoveryText(preflightQuery.data.active_named_admin_count)} All active
              named non-admin users have at least one project and site grant.
            </span>
          </div>
        ) : (
          <div className="state-panel warning" role="status">
            <strong>Scope enforcement is blocked</strong>
            <span>
              Grant at least one project and site to each named user below before activation.
            </span>
            <ul>
              {(preflightQuery.data?.unscoped_active_non_admin_users ?? []).map((user) => (
                <li key={user.id}>
                  {user.username} ({user.role})
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <section className="surface">
        <div className="surface-heading">
          <div>
            <h3>Create a user</h3>
          </div>
        </div>

        <form
          className="hub-filter-grid"
          onSubmit={(event) => {
            event.preventDefault();
            if (newUsername.trim()) {
              createMutation.mutate();
            }
          }}
        >
          <label>
            Username
            <input
              onChange={(event) => setNewUsername(event.target.value)}
              placeholder="site-engineer"
              value={newUsername}
            />
          </label>
          <label>
            Role
            <select onChange={(event) => setNewRole(event.target.value as Role)} value={newRole}>
              {ROLE_ORDER.map((role) => (
                <option key={role} value={role}>
                  {role}
                </option>
              ))}
            </select>
          </label>
          <div className="hub-filter-action">
            <button
              className="primary-button"
              disabled={!newUsername.trim() || createMutation.isPending}
              type="submit"
            >
              {createMutation.isPending ? "Creating..." : "Create user"}
            </button>
          </div>
        </form>

        {createMutation.isError && (
          <div className="state-panel error" role="alert">
            <strong>Could not create user</strong>
            <span>{createMutation.error.message}</span>
          </div>
        )}

        {issuedKey && (
          <div className="state-panel success">
            <strong>API key for {issuedKey.username}</strong>
            <span>
              Copy it now. It is displayed only this once and cannot be retrieved later. The key
              itself does not expire: it keeps working until this user is deactivated or an admin
              re-issues their key.
            </span>
            <code className="issued-key">{issuedKey.apiKey}</code>
            <button
              className="secondary-button compact"
              onClick={() => void copyIssuedKey()}
              type="button"
            >
              Copy API key
            </button>
            <span aria-live="polite" role="status">
              {issuedKeyStatus}
            </span>
            <button
              className="secondary-button compact"
              onClick={() => {
                setIssuedKey(null);
                setIssuedKeyStatus("");
              }}
              type="button"
            >
              Dismiss
            </button>
          </div>
        )}
      </section>

      <section className="surface" aria-labelledby="scope-grants-heading">
        <div className="surface-heading">
          <div>
            <h3 id="scope-grants-heading">Project and site access</h3>
            <p className="muted">
              Grants take effect immediately. Revoked grants stay in the audit history.
            </p>
          </div>
        </div>

        <form
          className="hub-filter-grid"
          onSubmit={(event) => {
            event.preventDefault();
            if (
              effectiveSelectedUserId &&
              grantProjectId.trim() &&
              grantSiteId.trim() &&
              grantReason.trim()
            ) {
              grantMutation.mutate();
            }
          }}
        >
          <label>
            User
            <select
              disabled={users.length === 0}
              onChange={(event) => {
                setSelectedUserId(event.target.value);
                setRevokeTarget(null);
                setRevokeReason("");
              }}
              value={effectiveSelectedUserId}
            >
              {users.length === 0 && <option value="">No users available</option>}
              {users.map((user) => (
                <option disabled={!user.is_active} key={user.id} value={user.id}>
                  {user.username}
                  {!user.is_active ? " (disabled)" : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            Project ID
            <input
              autoComplete="off"
              onChange={(event) => setGrantProjectId(event.target.value)}
              placeholder="project-a"
              value={grantProjectId}
            />
          </label>
          <label>
            Site ID
            <input
              autoComplete="off"
              onChange={(event) => setGrantSiteId(event.target.value)}
              placeholder="north-plant"
              value={grantSiteId}
            />
          </label>
          <label>
            Grant reason
            <input
              maxLength={2000}
              onChange={(event) => setGrantReason(event.target.value)}
              placeholder="Assigned for the commissioning visit"
              value={grantReason}
            />
          </label>
          <div className="hub-filter-action">
            <button
              className="primary-button"
              disabled={
                !effectiveSelectedUserId ||
                !grantProjectId.trim() ||
                !grantSiteId.trim() ||
                !grantReason.trim() ||
                grantMutation.isPending
              }
              type="submit"
            >
              {grantMutation.isPending ? "Granting access..." : "Grant access"}
            </button>
          </div>
        </form>

        {grantMutation.isError && (
          <div className="state-panel error" role="alert">
            <strong>Could not grant access</strong>
            <span>{grantMutation.error.message}</span>
          </div>
        )}
        {revokeMutation.isError && (
          <div className="state-panel error" role="alert">
            <strong>Could not revoke access</strong>
            <span>{revokeMutation.error.message}</span>
          </div>
        )}

        {revokeTarget && (
          <form
            className="state-panel warning"
            onSubmit={(event) => {
              event.preventDefault();
              if (revokeReason.trim()) {
                revokeMutation.mutate({ grant: revokeTarget, reason: revokeReason.trim() });
              }
            }}
          >
            <strong>
              Revoke {revokeTarget.project_id} / {revokeTarget.site_id}
            </strong>
            <span>
              The user loses this access immediately. The grant and your reason remain in history.
            </span>
            <label>
              Revocation reason
              <input
                autoFocus
                maxLength={2000}
                onChange={(event) => setRevokeReason(event.target.value)}
                value={revokeReason}
              />
            </label>
            <div>
              <button
                className="primary-button compact"
                disabled={!revokeReason.trim() || revokeMutation.isPending}
                type="submit"
              >
                {revokeMutation.isPending ? "Revoking..." : "Confirm revocation"}
              </button>{" "}
              <button
                className="link-button"
                disabled={revokeMutation.isPending}
                onClick={() => {
                  setRevokeTarget(null);
                  setRevokeReason("");
                }}
                type="button"
              >
                Keep access
              </button>
            </div>
          </form>
        )}

        <div className="data-table-wrap">
          {grantsQuery.isError ? (
            <div className="state-panel error" role="alert">
              <strong>Could not load scope grants</strong>
              <span>
                {grantsQuery.error instanceof Error ? grantsQuery.error.message : "Request failed."}
              </span>
            </div>
          ) : grantsQuery.isLoading && effectiveSelectedUserId ? (
            <div className="empty-workspace">
              <strong>Loading project and site access...</strong>
              <span>Fetching current grants and audit history.</span>
            </div>
          ) : (grantsQuery.data?.length ?? 0) > 0 ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Scope</th>
                  <th>Status</th>
                  <th>Granted</th>
                  <th>Audit reason</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {(grantsQuery.data ?? []).map((grant) => (
                  <tr key={grant.grant_id}>
                    <td>
                      {grant.project_id} / {grant.site_id}
                    </td>
                    <td>{grant.active ? "Current" : "Revoked"}</td>
                    <td>
                      {formatRelativeTime(grant.granted_at)}
                      <span>by {grant.granted_by}</span>
                    </td>
                    <td>
                      {grant.reason}
                      {!grant.active && grant.revoke_reason && (
                        <span>
                          Revoked: {grant.revoke_reason}
                          {grant.revoked_by ? ` by ${grant.revoked_by}` : ""}
                        </span>
                      )}
                    </td>
                    <td>
                      {grant.active ? (
                        <button
                          aria-label={`Revoke ${grant.project_id} / ${grant.site_id}`}
                          className="secondary-button compact"
                          disabled={revokeMutation.isPending}
                          onClick={() => {
                            setRevokeTarget(grant);
                            setRevokeReason("");
                          }}
                          type="button"
                        >
                          Revoke
                        </button>
                      ) : (
                        <span className="muted">Recorded</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty-workspace">
              <strong>No scope grants for this user</strong>
              <span>Choose a valid project and site, then record why access is needed.</span>
            </div>
          )}
        </div>
      </section>

      <section className="surface">
        <div className="surface-heading">
          <div>
            <h3>Users</h3>
          </div>
        </div>

        {reissueMutation.isError && (
          <div className="state-panel error" role="alert">
            <strong>Could not re-issue key</strong>
            <span>{reissueMutation.error.message}</span>
          </div>
        )}

        <div className="data-table-wrap">
          {usersQuery.isError ? (
            <div className="state-panel error" role="alert">
              <strong>Could not load users</strong>
              <span>
                {usersQuery.error instanceof Error ? usersQuery.error.message : "Request failed."}
              </span>
            </div>
          ) : usersQuery.isLoading ? (
            <div className="empty-workspace">
              <strong>Loading users...</strong>
              <span>Fetching the operator directory.</span>
            </div>
          ) : users.length > 0 ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Username</th>
                  <th>Role</th>
                  <th>Active</th>
                  <th>Last used</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <UserRow
                    key={user.id}
                    deactivating={
                      deactivateMutation.isPending && deactivateMutation.variables === user.id
                    }
                    onDeactivate={() => confirmDeactivate(user)}
                    onReissueKey={() => confirmReissue(user)}
                    onRoleChange={(role) => roleMutation.mutate({ role, userId: user.id })}
                    reissuing={reissueMutation.isPending && reissueMutation.variables === user.id}
                    user={user}
                  />
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty-workspace">
              <strong>No users yet</strong>
              <span>Create the first named operator above.</span>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

function namedAdminRecoveryText(count: number): string {
  return count === 1
    ? "1 active named admin can recover access."
    : `${count} active named admins can recover access.`;
}

function CurrentScopeSummary({ me }: { me: MeResponse | null }) {
  const scopes = me?.effective_scopes ?? [];
  return (
    <section className="surface" aria-labelledby="current-scope-heading">
      <div className="surface-heading">
        <div>
          <h3 id="current-scope-heading">Your project and site access</h3>
        </div>
      </div>
      {!me ? (
        <div className="state-panel error">
          <strong>Identity unavailable</strong>
          <span>Sign in again so the application can resolve your current access.</span>
        </div>
      ) : me.global_scope ? (
        <div className="state-panel success">
          <strong>Global project and site access</strong>
          <span>{me.username} can administer every current and future project and site.</span>
        </div>
      ) : scopes.length > 0 ? (
        <div className="state-panel">
          <strong>{me.username} has access to:</strong>
          <ul>
            {scopes.map((scope) => (
              <li key={`${scope.project_id}:${scope.site_id}`}>
                {scope.project_id} / {scope.site_id}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="state-panel warning">
          <strong>No project or site access</strong>
          <span>Ask a global administrator to add at least one project and site grant.</span>
        </div>
      )}
    </section>
  );
}

function UserRow({
  user,
  onRoleChange,
  onDeactivate,
  onReissueKey,
  deactivating,
  reissuing,
}: {
  user: UserRecord;
  onRoleChange: (role: Role) => void;
  onDeactivate: () => void;
  onReissueKey: () => void;
  deactivating: boolean;
  reissuing: boolean;
}) {
  return (
    <tr>
      <td>{user.username}</td>
      <td>
        <select
          aria-label={`Role for ${user.username}`}
          disabled={!user.is_active}
          onChange={(event) => onRoleChange(event.target.value as Role)}
          value={user.role}
        >
          {ROLE_ORDER.map((role) => (
            <option key={role} value={role}>
              {role}
            </option>
          ))}
        </select>
      </td>
      <td>{user.is_active ? "Active" : "Disabled"}</td>
      <td>{user.last_used_at ? formatRelativeTime(user.last_used_at) : "Never"}</td>
      <td>
        {user.is_active ? (
          <>
            <button
              className="secondary-button compact"
              disabled={reissuing}
              onClick={onReissueKey}
              title={`Replace ${user.username}'s lost key: the current key stops working immediately and the new one is displayed once.`}
              type="button"
            >
              {reissuing ? "Re-issuing..." : "Re-issue key"}
            </button>{" "}
            <button
              className="secondary-button compact"
              disabled={deactivating}
              onClick={onDeactivate}
              type="button"
            >
              {deactivating ? "Deactivating..." : "Deactivate"}
            </button>
          </>
        ) : (
          <span className="muted">Unavailable</span>
        )}
      </td>
    </tr>
  );
}
