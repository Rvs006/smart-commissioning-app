"""Resolve one validated application identity for runtime-facing surfaces."""

from __future__ import annotations

import os

from smart_commissioning_core import __version__

RUNTIME_VERSION_ENV = "SMART_COMMISSIONING_APP_VERSION"


class RuntimeVersionMismatchError(RuntimeError):
    """A runtime stamp claims a release other than the packaged application."""


def packaged_application_version() -> str:
    """Return the canonical version compiled into the shared application package."""

    return __version__


def validate_packaged_application_version(value: str, *, source: str) -> str:
    """Reject a source fallback that drifts from the shared package version."""

    if value != packaged_application_version():
        raise RuntimeVersionMismatchError(
            f"{source} must match the packaged application version "
            f"{packaged_application_version()!r}; got {value!r}."
        )
    return value


def effective_application_version() -> str:
    """Return a compatible portable stamp or the packaged application version.

    Portable and Docker release builds may use a leading ``v`` so their visible
    build marker matches the tagged release. Any non-empty value must identify
    the same packaged release; otherwise the process must not serve API, report,
    or provenance output with conflicting identities.
    """

    runtime_stamp = os.environ.get(RUNTIME_VERSION_ENV, "").strip()
    if not runtime_stamp:
        return packaged_application_version()
    if runtime_stamp.removeprefix("v") != packaged_application_version():
        raise RuntimeVersionMismatchError(
            f"{RUNTIME_VERSION_ENV} must match the packaged application version "
            f"{packaged_application_version()!r}; got {runtime_stamp!r}."
        )
    return runtime_stamp


def validate_runtime_application_version() -> str:
    """Validate and return the effective identity before the application serves."""

    return effective_application_version()
