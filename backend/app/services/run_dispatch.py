"""Queue-or-inline dispatch shared by discovery + validation routes.

The UDMI route established the pattern: respect ``job_execution_mode``
(``inline`` -> run in-process; ``queue`` -> enqueue and fail if Redis is down;
``auto`` -> try the queue, fall back to inline when Redis is unavailable and
``allow_inline_worker_fallback`` is set). This module factors that pattern out
so every engine route uses identical semantics.

A caller supplies:

* ``enqueue`` — a JobQueueService method that enqueues the run, OR ``None`` to
  force the inline path (used by engines whose worker actor is not wired for a
  given deployment, e.g. mqtt-config-publish historically).
* ``run_inline`` — a zero-arg callable that runs the engine processor in-process
  with the RunService as the run store and returns the terminal RunRecord.

It returns a :class:`JobAcceptedResponse`. Inline runs report their real
terminal status; queued runs report ``queued`` with a worker-required summary.
"""

from collections.abc import Callable

from app.core.config import get_settings
from app.schemas.jobs import JobAcceptedResponse, RunRecord
from app.services.job_queue import JobDispatch, JobQueueUnavailable
from app.services.run_service import RunService

EnqueueFn = Callable[[RunRecord], JobDispatch]
InlineFn = Callable[[], RunRecord]


def dispatch_run(
    run: RunRecord,
    *,
    service: RunService,
    enqueue: EnqueueFn | None,
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
        processed = run_inline()
        return JobAcceptedResponse(
            run_id=processed.run_id,
            job_type=processed.job_type,
            status=processed.status,
            message=inline_message,
        )

    try:
        dispatch = enqueue(run)
        service.update_result_summary(
            run.run_id,
            {
                "queued": True,
                "worker_required": True,
                "execution_mode": "dramatiq_redis",
                "queue_name": dispatch.queue_name,
                "actor_name": dispatch.actor_name,
            },
        )
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

        processed = run_inline()
        return JobAcceptedResponse(
            run_id=processed.run_id,
            job_type=processed.job_type,
            status=processed.status,
            message=fallback_message,
        )
