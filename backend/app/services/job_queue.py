import logging
from dataclasses import dataclass

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from app.core.config import get_settings
from app.core.observability import _redis_host
from app.schemas.jobs import RunRecord

_logger = logging.getLogger(__name__)

# Generic, credential-free message surfaced to the API/frontend when the queue
# cannot be reached. The redis_url may embed credentials (redis://:password@host)
# so it is NEVER included here; the host (without credentials) is logged instead.
_QUEUE_UNAVAILABLE_MESSAGE = "Job queue (Redis) is unavailable."


class JobQueueUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class JobDispatch:
    actor_name: str
    queue_name: str


@dataclass(frozen=True)
class RunEnqueuer:
    """A run-enqueue callable that also exposes its worker destination.

    ``dispatch_run`` reads ``queue_name`` / ``actor_name`` BEFORE calling this so
    it can stamp the run's worker-dispatch markers while the queued message is
    still invisible to any worker. queue_name/actor_name are the fixed enqueue
    destination, so they are known up front and do not depend on the enqueue
    having run. Calling the instance performs the enqueue and returns the
    resulting :class:`JobDispatch`.
    """

    service: "JobQueueService"
    actor_name: str
    queue_name: str

    def __call__(self, run: RunRecord) -> JobDispatch:
        return self.service._enqueue(
            actor_name=self.actor_name,
            queue_name=self.queue_name,
            args=(run.run_id, dict(run.parameters)),
        )


class JobQueueService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def enqueue_for(self, actor_name: str, queue_name: str) -> RunEnqueuer:
        """Return an enqueue callable dispatching a run to ``actor_name`` on ``queue_name``.

        The returned :class:`RunEnqueuer` also exposes ``actor_name`` /
        ``queue_name`` so the dispatcher can record the worker-dispatch markers
        before the run is handed to the queue (see :class:`RunEnqueuer`).
        """
        return RunEnqueuer(self, actor_name, queue_name)

    def _enqueue(self, *, actor_name: str, queue_name: str, args: tuple[object, ...]) -> JobDispatch:
        broker = RedisBroker(url=self.settings.redis_url)
        message = dramatiq.Message(
            queue_name=queue_name,
            actor_name=actor_name,
            args=args,
            kwargs={},
            options={},
        )

        try:
            broker.enqueue(message)
        except Exception as error:
            _logger.exception(
                "Could not enqueue %s on Redis broker host %s",
                actor_name,
                _redis_host(self.settings.redis_url),
            )
            raise JobQueueUnavailable(_QUEUE_UNAVAILABLE_MESSAGE) from error
        finally:
            broker.close()

        return JobDispatch(actor_name=actor_name, queue_name=queue_name)
