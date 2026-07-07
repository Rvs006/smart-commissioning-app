import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createUser,
  deactivateUser,
  listUsers,
  reissueUserKey,
  ROLE_ORDER,
  updateUserRole,
  type Role,
  type UserRecord,
} from "../../api/client";
import { useSession } from "../../app/sessionContext";
import { formatRelativeTime } from "./runFormat";

// Admin-only user management. The route is only reachable when /me reports an
// admin role (the nav entry is hidden otherwise), and every backend call is
// admin-gated server-side, so a non-admin who deep-links here just sees the
// access notice below — no mutation buttons are ever wired for them.
export function UsersPage() {
  const { canAdmin } = useSession();
  const queryClient = useQueryClient();
  const [newUsername, setNewUsername] = useState("");
  const [newRole, setNewRole] = useState<Role>("viewer");
  const [issuedKey, setIssuedKey] = useState<{ username: string; apiKey: string } | null>(null);

  const usersQuery = useQuery({
    enabled: canAdmin,
    queryFn: listUsers,
    queryKey: ["users"],
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["users"] });
  };

  const createMutation = useMutation({
    mutationFn: () => createUser({ role: newRole, username: newUsername.trim() }),
    onSuccess: (result) => {
      setIssuedKey({ apiKey: result.api_key, username: result.user.username });
      setNewUsername("");
      setNewRole("viewer");
      refresh();
    },
  });

  const deactivateMutation = useMutation({
    mutationFn: (userId: string) => deactivateUser(userId),
    onSuccess: refresh,
  });

  // Lost-key recovery: keys are displayed once and can never be retrieved, so
  // the only way back is a fresh key. Re-issuing invalidates the old key
  // immediately; the new plaintext lands in the same issued-key panel as create.
  const reissueMutation = useMutation({
    mutationFn: (userId: string) => reissueUserKey(userId),
    onSuccess: (result) => {
      setIssuedKey({ apiKey: result.api_key, username: result.user.username });
      refresh();
    },
  });

  const roleMutation = useMutation({
    mutationFn: (input: { userId: string; role: Role }) =>
      updateUserRole(input.userId, input.role),
    onSuccess: refresh,
  });

  if (!canAdmin) {
    return (
      <div className="app-page">
        <div className="state-panel error">
          <strong>Admin access required</strong>
          <span>User management is restricted to the admin role.</span>
        </div>
      </div>
    );
  }

  const users = usersQuery.data ?? [];

  return (
    <div className="app-page users-page">
      <section className="surface">
        <div className="surface-heading">
          <div>
            <span className="eyebrow">Provision</span>
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
            <select
              onChange={(event) => setNewRole(event.target.value as Role)}
              value={newRole}
            >
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
          <div className="state-panel error">
            <strong>Could not create user</strong>
            <span>{createMutation.error.message}</span>
          </div>
        )}

        {issuedKey && (
          <div className="state-panel success">
            <strong>API key for {issuedKey.username}</strong>
            <span>
              Copy it now — it is displayed only this once and cannot be retrieved
              later. The key itself does not expire: it keeps working until this
              user is deactivated or an admin re-issues their key.
            </span>
            <code className="issued-key">{issuedKey.apiKey}</code>
            <button
              className="secondary-button compact"
              onClick={() => setIssuedKey(null)}
              type="button"
            >
              Dismiss
            </button>
          </div>
        )}
      </section>

      <section className="surface">
        <div className="surface-heading">
          <div>
            <span className="eyebrow">Directory</span>
            <h3>Users</h3>
          </div>
        </div>

        {reissueMutation.isError && (
          <div className="state-panel error">
            <strong>Could not re-issue key</strong>
            <span>{reissueMutation.error.message}</span>
          </div>
        )}

        <div className="data-table-wrap">
          {usersQuery.isError ? (
            <div className="state-panel error">
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
                    onDeactivate={() => deactivateMutation.mutate(user.id)}
                    onReissueKey={() => reissueMutation.mutate(user.id)}
                    onRoleChange={(role) => roleMutation.mutate({ role, userId: user.id })}
                    reissuing={
                      reissueMutation.isPending && reissueMutation.variables === user.id
                    }
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
          <span className="muted">—</span>
        )}
      </td>
    </tr>
  );
}
