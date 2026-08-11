"""Project/site authorization and concealment-safe scoped object loaders.

Roles answer *what* a named user may do. Active ``user_scope_grants`` rows
answer *where* non-admin named users may do it. Named admins remain explicit
global administrators. The synthetic local/shared bootstrap principals receive
global scope only on a standalone deployment, never on an edge or hub.

Foreign and absent resource identifiers deliberately follow the same 404 path.
Routes can therefore load a run/import/report through this module without first
revealing whether an inaccessible identifier exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import Depends, HTTPException
from smart_commissioning_core.db.engine import session_factory
from smart_commissioning_core.db.models import (
    ImportRecord,
    Run,
    Site,
    User,
    UserScopeGrant,
)
from smart_commissioning_core.rbac import Role
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.core.auth import AuthPrincipal, get_principal
from app.core.config import get_settings
from app.core.db import get_engine


class ScopeGrantConflictError(ValueError):
    """A grant lifecycle request conflicts with current state."""


class ScopeGrantTargetError(LookupError):
    """The requested user, project/site, or grant does not exist."""


@dataclass(frozen=True)
class ScopedResource:
    """The ownership fields proven by a concealment-safe scoped load."""

    resource_id: str
    project_id: str
    site_id: str


@dataclass(frozen=True)
class AuthorizedImportResource:
    """An import whose scoped or global-only ownership has been authorized."""

    resource_id: str
    project_id: str | None
    site_id: str | None


def _actor(principal: AuthPrincipal) -> str:
    """Return a server-derived stable audit actor, never a client claim."""
    return principal.user_id or principal.source


def has_global_scope(principal: AuthPrincipal) -> bool:
    """Whether this principal may access every project/site.

    A named ``admin`` is always a real global administrator. Synthetic
    bootstrap administrators retain their historical trust boundary only in a
    standalone deployment.
    """
    if principal.role is not Role.ADMIN:
        return False
    if principal.user_id is not None:
        return True
    return get_settings().deployment_role == "standalone"


def require_global_admin(
    principal: AuthPrincipal = Depends(get_principal),
) -> AuthPrincipal:
    """FastAPI dependency for authority that truly spans the runtime."""
    if principal.user_id is not None:
        allowed = (
            principal.role is Role.ADMIN
            and ScopeGrantRepository().is_active_admin(principal.user_id)
        )
    else:
        allowed = has_global_scope(principal)
    if not allowed:
        raise HTTPException(status_code=403, detail="This action requires a global admin.")
    return principal


def _grant_to_dict(grant: UserScopeGrant) -> dict[str, object]:
    return {
        "grant_id": grant.grant_id,
        "user_id": grant.user_id,
        "project_id": grant.project_id,
        "site_id": grant.site_id,
        "active": grant.active_marker is True,
        "granted_by": grant.granted_by,
        "reason": grant.reason,
        "granted_at": grant.granted_at,
        "revoked_by": grant.revoked_by,
        "revoke_reason": grant.revoke_reason,
        "revoked_at": grant.revoked_at,
    }


class ScopeGrantRepository:
    """Transactional current-grant management with retained revoke history."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._session_factory = session_factory(engine or get_engine())

    def create(
        self,
        *,
        user_id: str,
        project_id: str,
        site_id: str,
        reason: str,
        principal: AuthPrincipal,
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            user = session.get(User, user_id)
            if user is None:
                raise ScopeGrantTargetError("User not found.")
            if not user.is_active:
                raise ScopeGrantConflictError("Cannot grant scope to a deactivated user.")
            site = session.scalar(
                select(Site).where(Site.id == site_id, Site.project_id == project_id)
            )
            if site is None:
                raise ScopeGrantTargetError("Project/site not found.")
            grant = UserScopeGrant(
                grant_id=str(uuid4()),
                user_id=user_id,
                project_id=project_id,
                site_id=site_id,
                active_marker=True,
                granted_by=_actor(principal),
                reason=reason,
                granted_at=now,
            )
            session.add(grant)
            try:
                session.flush()
            except IntegrityError as error:
                raise ScopeGrantConflictError(
                    "That user already has an active grant for this project/site."
                ) from error
            return _grant_to_dict(grant)

    def list_for_user(
        self,
        user_id: str,
        *,
        include_revoked: bool = False,
    ) -> list[dict[str, object]]:
        with self._session_factory() as session:
            if session.get(User, user_id) is None:
                raise ScopeGrantTargetError("User not found.")
            statement = select(UserScopeGrant).where(UserScopeGrant.user_id == user_id)
            if not include_revoked:
                statement = statement.where(UserScopeGrant.active_marker.is_(True))
            statement = statement.order_by(
                UserScopeGrant.project_id,
                UserScopeGrant.site_id,
                UserScopeGrant.granted_at.desc(),
                UserScopeGrant.grant_id,
            )
            return [_grant_to_dict(item) for item in session.scalars(statement).all()]

    def revoke(
        self,
        *,
        user_id: str,
        grant_id: str,
        reason: str,
        principal: AuthPrincipal,
    ) -> dict[str, object]:
        with self._session_factory.begin() as session:
            grant = session.scalar(
                select(UserScopeGrant).where(
                    UserScopeGrant.grant_id == grant_id,
                    UserScopeGrant.user_id == user_id,
                )
            )
            if grant is None:
                raise ScopeGrantTargetError("Scope grant not found.")
            if grant.active_marker is not True:
                raise ScopeGrantConflictError("Scope grant is already revoked.")
            grant.active_marker = None
            grant.revoked_by = _actor(principal)
            grant.revoke_reason = reason
            grant.revoked_at = datetime.now(UTC)
            session.flush()
            return _grant_to_dict(grant)

    def effective_scopes(self, user_id: str) -> list[dict[str, str]]:
        """Return active grants only when the owning named user is active."""
        statement = (
            select(UserScopeGrant.project_id, UserScopeGrant.site_id)
            .join(User, User.id == UserScopeGrant.user_id)
            .where(
                UserScopeGrant.user_id == user_id,
                UserScopeGrant.active_marker.is_(True),
                User.is_active.is_(True),
            )
            .order_by(UserScopeGrant.project_id, UserScopeGrant.site_id)
        )
        with self._session_factory() as session:
            return [
                {"project_id": project_id, "site_id": site_id}
                for project_id, site_id in session.execute(statement).all()
            ]

    def permits(self, user_id: str, project_id: str, site_id: str) -> bool:
        """Recheck both grant and named-user activation in one query."""
        statement = (
            select(UserScopeGrant.grant_id)
            .join(User, User.id == UserScopeGrant.user_id)
            .where(
                UserScopeGrant.user_id == user_id,
                UserScopeGrant.project_id == project_id,
                UserScopeGrant.site_id == site_id,
                UserScopeGrant.active_marker.is_(True),
                User.is_active.is_(True),
            )
            .limit(1)
        )
        with self._session_factory() as session:
            return session.scalar(statement) is not None

    def is_active_admin(self, user_id: str) -> bool:
        """Recheck a named global admin against mutable user state."""
        statement = (
            select(User.id)
            .where(
                User.id == user_id,
                User.is_active.is_(True),
                User.role == Role.ADMIN.value,
            )
            .limit(1)
        )
        with self._session_factory() as session:
            return session.scalar(statement) is not None

    def activation_preflight(self) -> dict[str, object]:
        """Report whether named-user scope enforcement is safe to activate.

        Readiness requires an active named global admin and an active grant for
        every active named non-admin. Inactive users do not block rollout, and
        revoked grants never satisfy it. This method records no activation
        state; callers must treat ``ready=False`` as a hard stop.
        """
        active_grant_exists = (
            select(UserScopeGrant.grant_id)
            .where(
                UserScopeGrant.user_id == User.id,
                UserScopeGrant.active_marker.is_(True),
            )
            .exists()
        )
        active_users_statement = (
            select(
                User.id,
                User.username,
                User.role,
                active_grant_exists.label("has_active_grant"),
            )
            .where(User.is_active.is_(True))
            .order_by(User.username, User.id)
        )
        with self._session_factory() as session:
            active_users = session.execute(active_users_statement).all()
        active_named_admin_count = sum(
            role == Role.ADMIN.value for _user_id, _username, role, _has_grant in active_users
        )
        unscoped_users = [
            {"id": user_id, "username": username, "role": role}
            for user_id, username, role, has_active_grant in active_users
            if role != Role.ADMIN.value and not has_active_grant
        ]
        return {
            "ready": active_named_admin_count > 0 and not unscoped_users,
            "active_named_admin_count": active_named_admin_count,
            "unscoped_active_non_admin_users": unscoped_users,
        }


def effective_scopes(
    principal: AuthPrincipal,
    *,
    engine: Engine | None = None,
) -> list[dict[str, str]]:
    """Return the named user's active project/site pairs, or an empty global set."""
    if has_global_scope(principal) or principal.user_id is None:
        return []
    return ScopeGrantRepository(engine).effective_scopes(principal.user_id)


def allowed_scope_pairs(
    principal: AuthPrincipal,
    *,
    engine: Engine | None = None,
) -> set[tuple[str, str]] | None:
    """Return ``None`` for global access or the principal's exact active scopes."""
    repository = ScopeGrantRepository(engine)
    if principal.user_id is not None and principal.role is Role.ADMIN:
        return None if repository.is_active_admin(principal.user_id) else set()
    if principal.user_id is None and has_global_scope(principal):
        return None
    return {
        (item["project_id"], item["site_id"])
        for item in effective_scopes(principal, engine=engine)
    }


def require_project_site_access(
    principal: AuthPrincipal,
    project_id: str,
    site_id: str,
    *,
    engine: Engine | None = None,
) -> ScopedResource:
    """Authorize one scope or raise the same 404 used for a missing scope."""
    repository = ScopeGrantRepository(engine)
    if principal.user_id is None:
        if has_global_scope(principal):
            return ScopedResource("project_site", project_id, site_id)
    else:
        if principal.role is Role.ADMIN and repository.is_active_admin(principal.user_id):
            return ScopedResource("project_site", project_id, site_id)
        if repository.permits(principal.user_id, project_id, site_id):
            return ScopedResource("project_site", project_id, site_id)
    raise HTTPException(status_code=404, detail="Project/site not found.")


def require_project_site(
    project_id: str,
    site_id: str,
    principal: AuthPrincipal = Depends(get_principal),
) -> ScopedResource:
    """FastAPI dependency form for routes with project/site path or query keys."""
    return require_project_site_access(principal, project_id, site_id)


def _authorize_resource(
    *,
    resource_id: str,
    project_id: str | None,
    site_id: str | None,
    kind: str,
    principal: AuthPrincipal,
    engine: Engine,
) -> ScopedResource:
    if not project_id or not site_id:
        raise HTTPException(status_code=404, detail=f"{kind} not found.")
    try:
        require_project_site_access(
            principal,
            project_id,
            site_id,
            engine=engine,
        )
    except HTTPException as error:
        if error.status_code == 404:
            raise HTTPException(status_code=404, detail=f"{kind} not found.") from error
        raise
    return ScopedResource(resource_id, project_id, site_id)


def load_scoped_run(
    run_id: str,
    principal: AuthPrincipal,
    *,
    engine: Engine | None = None,
) -> ScopedResource:
    """Load a run's owner and authorize it without exposing foreign IDs."""
    resolved_engine = engine or get_engine()
    with session_factory(resolved_engine)() as session:
        row = session.execute(
            select(Run.project_id, Run.site_id).where(Run.id == run_id)
        ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return _authorize_resource(
        resource_id=run_id,
        project_id=row.project_id,
        site_id=row.site_id,
        kind="Run",
        principal=principal,
        engine=resolved_engine,
    )


def load_scoped_import(
    import_id: str,
    principal: AuthPrincipal,
    *,
    engine: Engine | None = None,
) -> AuthorizedImportResource:
    """Load an import's owner and authorize it without exposing foreign IDs."""
    resolved_engine = engine or get_engine()
    with session_factory(resolved_engine)() as session:
        row = session.execute(
            select(ImportRecord.project_id, ImportRecord.site_id).where(
                ImportRecord.import_id == import_id
            )
        ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Import not found.")
    if row.project_id is None and row.site_id is None:
        if allowed_scope_pairs(principal, engine=resolved_engine) is None:
            return AuthorizedImportResource(import_id, None, None)
        raise HTTPException(status_code=404, detail="Import not found.")
    scoped = _authorize_resource(
        resource_id=import_id,
        project_id=row.project_id,
        site_id=row.site_id,
        kind="Import",
        principal=principal,
        engine=resolved_engine,
    )
    return AuthorizedImportResource(
        scoped.resource_id,
        scoped.project_id,
        scoped.site_id,
    )


def load_scoped_report(
    report_id: str,
    principal: AuthPrincipal,
    *,
    engine: Engine | None = None,
) -> ScopedResource:
    """Integrity-load a report before authorizing its sealed scope."""
    resolved_engine = engine or get_engine()
    # Local import avoids coupling the scope module's general run loaders to the
    # report service. Serving performs the same verification again after this
    # authorization check, closing the mutation window between the two reads.
    from app.services.run_service import RunService

    try:
        report = RunService(resolved_engine).get_report_for_serving(report_id)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        # An integrity failure occurs before trusted ownership is available.
        # Conceal it exactly like an absent or foreign report id.
        raise HTTPException(status_code=404, detail="Report not found.") from error
    return _authorize_resource(
        resource_id=report_id,
        project_id=report.project_id,
        site_id=report.site_id,
        kind="Report",
        principal=principal,
        engine=resolved_engine,
    )
