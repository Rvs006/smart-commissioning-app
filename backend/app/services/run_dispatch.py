"""Queue-or-inline dispatch shared by discovery + validation routes.

The UDMI route established the pattern: respect ``job_execution_mode``
(``inline`` -> run in-process; ``queue`` -> enqueue and fail if Redis is down;
``auto`` -> try the queue, fall back to inline when Redis is unavailable and
``allow_inline_worker_fallback`` is set). This module factors that pattern out
so every engine route uses identical semantics.

A caller supplies:

* ``enqueue`` — a :class:`RunEnqueuer` that enqueues the run (and exposes the
  queue/actor it targets), OR ``None`` to force the inline path (used by engines
  whose worker actor is not wired for a given deployment, e.g. mqtt-config-publish
  historically).
* ``run_inline`` — a zero-arg callable that runs the engine processor in-process
  with the RunService as the run store and returns the terminal RunRecord.

It returns a :class:`JobAcceptedResponse`. Inline runs report their real
terminal status; queued runs report ``queued`` with a worker-required summary.
"""

from collections.abc import Callable

from app.core.config import get_settings
from app.schemas.jobs import JobAcceptedResponse, RunRecord
from app.services.job_queue import JobQueueUnavailable, RunEnqueuer
from app.services.run_service import RunService

InlineFn = Callable[[], RunRecord]


def _inline_response(
    run: RunRecord,
    *,
    service: RunService,
    run_inline: InlineFn,
    message: str,
) -> JobAcceptedResponse:
    """Run the engine in-process and report the outcome without 500-ing on the
    store-failure path.

    ``run_inline`` normally returns the terminal RunRecord, but the engine
    framework's last line of defence (engines.base._safe_update_run_status)
    returns ``None`` when even the terminal status write fails — a poisoned
    session, or a disk-full / locked SQLite database. Dereferencing that None as
    ``processed.run_id`` used to raise AttributeError into a 500 while the run was
    left fossilized at 'running'. On None we re-read the run's current stored
    state (the terminal write never landed, so it reads 'running'; the startup
    orphan sweep reclaims it) and, if that read fails too, fall back to the run as
    created — so this path returns a clean accepted response carrying the run id
    instead of a 500.
    """
    processed = run_inline()
    if processed is None:
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
    fallback_message: str,
) -> JobAcceptedResponse:
    """Dispatch ``run`` via queue or inline per the configured execution mode.

    Raises HTTPException-friendly behaviour to the caller only indirectly: a
    hard queue failure in ``queue`` mode (or when inline fallback is disabled)
    raises :class:`JobQueueUnavailable` for the route to map to 503.
    """
    settings = get_settings()

    if enqueue is None or settings.job_execution_mode == "inline":
        return _inline_response(run, service=service, run_inline=run_inline, message=inline_message)

    # Stamp the worker-dispatch markers BEFORE handing the run to the queue.
    # queue_name/actor_name are the fixed enqueue destination (known up front) and
    # are what the startup orphan sweep uses to tell a live worker run from an
    # interrupted inline run (see run_service._was_queued_to_worker). Written
    # after the enqueue, they left a crash window in which a worker had already
    # flipped the run to 'running' while the markers were still absent, so the
    # sweep false-failed the live run; writing them first closes that window.
    service.update_result_summary(
        run.run_id,
        {
            "queued": True,
            "worker_required": True,
            "execution_mode": "dramatiq_redis",
            "queue_name": enqueue.queue_name,
            "actor_name": enqueue.actor_name,
        },
    )
    try:
        enqueue(run)
        return JobAcceptedResponse(
            run_id=run.run_id,
            job_type=run.job_type,
            status=run.status,
            message=queued_message,
        )
    except JobQueueUnavailable as error:
        if settings.job_execution_mode == "queue" or not settings.allow_inline_worker_fallback:
            service.update_run_status(
                run.run_id,
                status="failed",
                stage="queue_unavailable",
                progress_percent=100,
                error_message=str(error),
            )
            raise

        # The message never reached the queue, so no worker will ever run this.
        # Clear the worker-dispatch markers stamped above so the sweep can still
        # reclaim this run if the inline attempt below is interrupted mid-persist
        # — otherwise the stale markers would make an orphaned inline run look
        # like a live worker run and it would spin at 'running' forever.
        service.update_result_summary(
            run.run_id,
            {
                "queued": False,
                "worker_required": False,
                "execution_mode": "inline_local_fallback",
                "queue_name": None,
                "actor_name": None,
            },
        )
        return _inline_response(run, service=service, run_inline=run_inline, message=fallback_message)
