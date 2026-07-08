"""Role-based access control primitives shared by the backend (and CLIs).

A single, totally-ordered set of roles so a minimum-role check is just an
integer comparison. The string ``value`` of each role is what gets persisted on
``User.role`` and returned over the API, so the wire/storage contract is the
lowercase role name.

Permission tiers (intended authority; enforcement lands route-by-route):

    viewer   — read-only. May GET project data (runs, issues, discovery,
               configuration, reports). No mutations.
    reviewer — everything viewer can do, plus (future) annotate / comment on
               runs and issues. Still no create/run/publish.
    engineer — operational authority: create + run discovery and validation
               jobs, publish configuration, manage configuration and imports.
               Cannot manage users.
    admin    — full authority, including user management (create users, list,
               deactivate, change roles) on top of everything engineer can do.

The order below defines the total order; do not reorder without a migration
plan, since comparisons rely on the declaration order.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Role", "ROLE_ORDER"]


class Role(StrEnum):
    """The RBAC roles, ordered viewer < reviewer < engineer < admin.

    A ``StrEnum`` so a Role serializes to its lowercase name in JSON and compares
    equal to that string, while the rank methods below provide the total order
    used by ``require_role``.
    """

    VIEWER = "viewer"
    REVIEWER = "reviewer"
    ENGINEER = "engineer"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        """0-based position in the total order (viewer=0 ... admin=3)."""
        return ROLE_ORDER.index(self)

    def at_least(self, minimum: Role) -> bool:
        """True when this role is >= ``minimum`` in the total order."""
        return self.rank >= minimum.rank

    @classmethod
    def from_value(cls, value: str) -> Role:
        """Parse a stored/role-string into a Role.

        Raises ValueError on an unknown role so a corrupt/forged role value fails
        loudly rather than silently being treated as a low (or high) privilege.
        """
        try:
            return cls(value)
        except ValueError as error:
            valid = ", ".join(role.value for role in cls)
            raise ValueError(f"Unknown role {value!r}; expected one of: {valid}.") from error


# Declaration order IS the privilege order (ascending). Used by Role.rank.
ROLE_ORDER: tuple[Role, ...] = (Role.VIEWER, Role.REVIEWER, Role.ENGINEER, Role.ADMIN)
