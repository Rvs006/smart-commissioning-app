"""Durable queue-or-inline dispatch shared by discovery and validation routes.

Inline execution happens only when the deployment explicitly selects inline
mode or the route has no worker actor. Worker execution publishes the outbox row
created atomically with the run. A failed Redis publish leaves that row pending
for automatic retry and never starts a second inline executor.

A caller supplies an optional queue enqueuer and an inline processor accepting
the owned run store plus its resolved frozen parameters. The response always
carries the created run id; synchronous inline runs can also report their final
status.
"""

import logging
import threading
from collections.abc import Callable
from typing import Any

from smart_commissioning_core.execution_context import (
    resolve_context_parameters,
    verify_stored_context,
)
from smart_commissioning_core.owned_run_heartbeat import (
    DEFAULT_OWNED_RUN_HEARTBEAT_POLICY,
    OWNED_CONTEXT_FAILURE_MESSAGE,
    OWNED_CONTEXT_FAILURE_STAGE,
    OWNED_EXECUTOR_FAILURE_MESSAGE,
    OWNED_EXECUTOR_FAILURE_STAGE,
    OWNED_EXECUTOR_MISSING_TERMINAL_MESSAGE,
    OWNED_EXECUTOR_MISSING_TERMINAL_STAGE,
    OWNED_HEARTBEAT_START_FAILURE_STAGE,
    OwnedRunHeartbeat,
)
from smart_commissioning_core.owned_run_store import (
    OwnedRunStore,
    OwnershipLostError,
)

from app.core.config import get_settings
from app.schemas.jobs import JobAcceptedResponse, RunRecord
from app.services.configuration_service import read_secret_material
from app.services.job_queue import JobQueueService, JobQueueUnavailable, RunEnqueuer
from app.services.run_service import RunService

logger = logging.getLogger(__name__)

InlineFn = Callable[[OwnedRunStore, dict[str, Any]], object]

# Sanitized, credential-free message for a background inline run that raised
# BEFORE the engine wrapper could write its own terminal status (e.g. a failure
# building the persister). The startup orphan sweep only reclaims runs stuck at
# 'running'; a crash here can strand a run at 'queued', which the sweep does NOT
# touch, so the crash guard must land a terminal 'failed' itself.
_OUTBOX_DESTINATIONS = {
    "ip_discovery": ("discover_ip_range", "discovery"),
    "bacnet_discovery": ("discover_bacnet", "discovery"),
    "mqtt_discovery": ("discover_mqtt", "discovery"),
    "udmi_validation": ("validate_udmi_payloads", "validation"),
    "bacnet_validation": ("validate_bacnet_points", "validation"),
    "mapping_validation": ("compare_bacnet_mqtt", "validation"),
}


def publish_pending_dispatches(service: RunService | None = None) -> list[str]:
    """Retry durable pending outbox rows without creating a second executor."""

    run_service = service or RunService()
    queue = JobQueueService()
    published: list[str] = []
    for dispatch in run_service.list_pending_dispatches():
        run = run_service.get_run(dispatch.run_id)
        destination = _OUTBOX_DESTINATIONS.get(run.job_type)
        if destination is None or run.status != "queued":
            continue
        actor_name, queue_name = destination
        try:
            queue.enqueue_for(actor_name, queue_name)(run, dispatch.dispatch_id)
        except JobQueueUnavailable:
            continue
        try:
            if run_service.mark_dispatch_published(dispatch.dispatch_id):
                published.append(dispatch.dispatch_id)
        except Exception as error:
            # Redis may have accepted the message even when the outbox update is
            # temporarily unavailable. Leave the row pending for the same-ID
            # retry. Never fall back to an inline executor.
            logger.warning(
                "queue publication state remains pending for run_id=%s "
                "dispatch_id=%s exception_type=%s",
                run.run_id,
                dispatch.dispatch_id,
                type(error).__name__,
            )
    return published


def _run_inline_guarded(
    service: RunService,
    run: RunRecord,
    run_inline: InlineFn,
    owned_store: OwnedRunStore,
    heartbeat: OwnedRunHeartbeat,
) -> None:
    """Resolve and execute one claimed inline run under heartbeat protection.

    The engine wrapper (engines.base.run_engine) already writes a terminal status
    on every path it reaches, so the only gap this closes is an exception raised
    BEFORE the wrapper's own try/except runs. In that case nothing wrote a
    terminal status, so mark the run failed with a sanitized message. ``Exception``
    only: KeyboardInterrupt / SystemExit propagate to end the thread (the wrapper
    already recorded 'failed' for those), and the terminal write is itself guarded
    so a store hiccup cannot raise out of the thread.
    """
    try:
        dispatch = service.get_dispatch_for_run(run.run_id)
        service.mark_dispatch_published(dispatch.dispatch_id)
        try:
            stored_context = service.get_execution_context(run.run_id)
            context = verify_stored_context(stored_context, owned_store.lease)
            parameters = resolve_context_parameters(
                context,
                owned_store.lease,
                deployment_id=get_settings().deployment_id,
                channel=run.job_type,
                secret_resolver=lambda reference: read_secret_material(
                    reference
                ).encode("utf-8"),
            )
        except Exception as error:
            if heartbeat.ownership_lost or owned_store.ownership_lost:
                return
            logger.error(
                "inline execution context could not be resolved for run_id=%s "
                "exception_type=%s",
                run.run_id,
                type(error).__name__,
            )
            try:
                owned_store.update_run_status(
                    run.run_id,
                    status="failed",
                    stage=OWNED_CONTEXT_FAILURE_STAGE,
                    progress_percent=100,
                    error_message=OWNED_CONTEXT_FAILURE_MESSAGE,
                )
            except Exception as error:
                logger.error(
                    "failed to seal inline context-resolution failure for run_id=%s "
                    "exception_type=%s",
                    run.run_id,
                    type(error).__name__,
                )
            return
        if heartbeat.ownership_lost or owned_store.ownership_lost:
            return
        run_inline(owned_store, parameters)
        if (
            owned_store.terminal_outcome is None
            and not heartbeat.ownership_lost
            and not owned_store.ownership_lost
        ):
            owned_store.update_run_status(
                run.run_id,
                status="failed",
                stage=OWNED_EXECUTOR_MISSING_TERMINAL_STAGE,
                progress_percent=100,
                error_message=OWNED_EXECUTOR_MISSING_TERMINAL_MESSAGE,
            )
        if owned_store.ownership_lost and not heartbeat.ownership_lost:
            logger.warning(
                "inline processor observed ownership loss for run_id=%s; "
                "terminal evidence was not written",
                run.run_id,
            )
    except OwnershipLostError:
        logger.warning(
            "inline processor observed ownership loss for run_id=%s; "
            "terminal evidence was not written",
            run.run_id,
        )
    except Exception as error:
        logger.error(
            "background inline run %s crashed before it could finish "
            "exception_type=%s",
            run.run_id,
            type(error).__name__,
        )
        if heartbeat.ownership_lost or owned_store.ownership_lost:
            return
        if owned_store.terminal_outcome is not None:
            # The processor committed its one terminal result and then raised
            # during cleanup. Preserve that outcome instead of recording a
            # bogus competing finalization conflict.
            return
        try:
            owned_store.update_run_status(
                run.run_id,
                status="failed",
                stage=OWNED_EXECUTOR_FAILURE_STAGE,
                progress_percent=100,
                error_message=OWNED_EXECUTOR_FAILURE_MESSAGE,
            )
        except Exception as write_error:
            logger.error(
                "failed to mark crashed inline run %s as failed exception_type=%s",
                run.run_id,
                type(write_error).__name__,
            )
    finally:
        heartbeat.stop_and_join()


def _inline_response(
    run: RunRecord,
    *,
    service: RunService,
    run_inline: InlineFn,
    message: str,
) -> JobAcceptedResponse:
    """Run the engine in-process and report the outcome without 500-ing on the
    store-failure path.

    When ``inline_run_async`` is set (the portable-exe default, ITEM-4) the run is
    started on a daemon background thread and this returns immediately with the
    run's current (non-terminal) status, so the caller learns the run_id at once
    and the run monitor + Stop-run control can render while the run is live. The
    background run does not survive closing the app: a run killed by process exit
    has no worker markers and is reclaimed as failed/interrupted at next startup.

    In synchronous mode (the tests' INLINE_RUN_ASYNC=0 path, byte-for-byte the
    historical behaviour): ``run_inline`` normally returns the terminal RunRecord,
    but the engine framework's last line of defence
    (engines.base._safe_update_run_status) returns ``None`` when even the terminal
    status write fails — a poisoned session, or a disk-full / locked SQLite
    database. Dereferencing that None as ``processed.run_id`` used to raise
    AttributeError into a 500 while the run was left fossilized at 'running'. On
    None we re-read the run's current stored state (the terminal write never
    landed, so it reads 'running'; the startup orphan sweep reclaims it) and, if
    that read fails too, fall back to the run as created — so this path returns a
    clean accepted response carrying the run id instead of a 500.
    """
    settings = get_settings()
    lease_seconds = int(
        getattr(
            settings,
            "run_lease_seconds",
            DEFAULT_OWNED_RUN_HEARTBEAT_POLICY.lease_seconds,
        )
    )
    heartbeat_seconds = float(
        getattr(
            settings,
            "run_heartbeat_seconds",
            DEFAULT_OWNED_RUN_HEARTBEAT_POLICY.interval_seconds,
        )
    )
    owned_store = service.claim_owned_run(
        run.run_id,
        lease_seconds=lease_seconds,
    )
    if owned_store is None:
        current = service.get_run(run.run_id)
        return JobAcceptedResponse(
            run_id=current.run_id,
            job_type=current.job_type,
            status=current.status,
            message=message,
        )

    heartbeat: OwnedRunHeartbeat | None = None
    try:
        heartbeat = OwnedRunHeartbeat(
            owned_store,
            lease_seconds=lease_seconds,
            interval_seconds=heartbeat_seconds,
            executor_label="inline",
            thread_name_prefix="inline-heartbeat",
        )
        heartbeat.start()
    except Exception as error:
        logger.error(
            "inline heartbeat could not start for run_id=%s exception_type=%s",
            run.run_id,
            type(error).__name__,
        )
        try:
            owned_store.update_run_status(
                run.run_id,
                status="failed",
                stage=OWNED_HEARTBEAT_START_FAILURE_STAGE,
                progress_percent=100,
                error_message=OWNED_EXECUTOR_FAILURE_MESSAGE,
            )
        except Exception as write_error:
            logger.error(
                "failed to seal heartbeat-start failure for run_id=%s "
                "exception_type=%s",
                run.run_id,
                type(write_error).__name__,
            )
        finally:
            if heartbeat is not None:
                heartbeat.stop_and_join()
        current = service.get_run(run.run_id)
        return JobAcceptedResponse(
            run_id=current.run_id,
            job_type=current.job_type,
            status=current.status,
            message=message,
        )

    assert heartbeat is not None
    if settings.inline_run_async:
        try:
            executor = threading.Thread(
                target=_run_inline_guarded,
                args=(service, run, run_inline, owned_store, heartbeat),
                name=f"inline-run-{run.run_id}",
                daemon=True,
            )
            executor.start()
        except Exception as error:
            logger.error(
                "inline executor could not start for run_id=%s exception_type=%s",
                run.run_id,
                type(error).__name__,
            )
            try:
                owned_store.update_run_status(
                    run.run_id,
                    status="failed",
                    stage="inline_executor_start_failed",
                    progress_percent=100,
                    error_message=OWNED_EXECUTOR_FAILURE_MESSAGE,
                )
            except Exception as write_error:
                logger.error(
                    "failed to seal executor-start failure for run_id=%s "
                    "exception_type=%s",
                    run.run_id,
                    type(write_error).__name__,
                )
            finally:
                heartbeat.stop_and_join()
            current = service.get_run(run.run_id)
            return JobAcceptedResponse(
                run_id=current.run_id,
                job_type=current.job_type,
                status=current.status,
                message=message,
            )
        return JobAcceptedResponse(
            run_id=run.run_id,
            job_type=run.job_type,
            status=run.status,
            message=message,
        )

    _run_inline_guarded(service, run, run_inline, owned_store, heartbeat)
    try:
        processed = service.get_run(run.run_id)
    except Exception:
        processed = run
    return JobAcceptedResponse(
        run_id=processed.run_id,
        job_type=processed.job_type,
        status=processed.status,
        message=message,
    )


def dispatch_run(
    run: RunRecord,
    *,
    service: RunService,
    enqueue: RunEnqueuer | None,
    run_inline: InlineFn,
    inline_message: str,
    queued_message: str,
) -> JobAcceptedResponse:
    """Dispatch inline or publish the run's durable pending outbox row."""
    settings = get_settings()

    if enqueue is None or settings.job_execution_mode == "inline":
        return _inline_response(run, service=service, run_inline=run_inline, message=inline_message)

    # Run creation committed this pending row with the frozen context. Publishing
    # it can therefore be retried safely without reconstructing current settings.
    dispatch = service.get_dispatch_for_run(run.run_id)
    try:
        enqueue(run, dispatch.dispatch_id)
    except JobQueueUnavailable:
        logger.warning(
            "queue publish deferred for run_id=%s dispatch_id=%s",
            run.run_id,
            dispatch.dispatch_id,
        )
        return JobAcceptedResponse(
            run_id=run.run_id,
            job_type=run.job_type,
            status=run.status,
            message="Job accepted; queue publication is pending automatic retry.",
        )

    try:
        published = service.mark_dispatch_published(dispatch.dispatch_id)
    except Exception as error:
        # The message may already be visible in Redis. Preserve the pending
        # outbox row so maintenance can reconcile it with the same dispatch ID.
        # A second inline executor is never started from this uncertainty path.
        logger.warning(
            "queue publication acknowledgement could not be stored for run_id=%s "
            "dispatch_id=%s exception_type=%s",
            run.run_id,
            dispatch.dispatch_id,
            type(error).__name__,
        )
        published = False
    return JobAcceptedResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        message=(
            queued_message
            if published
            else "Job accepted; queue publication is pending automatic retry."
        ),
    )
