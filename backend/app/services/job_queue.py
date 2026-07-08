import logging
from collections.abc import Callable
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


class JobQueueService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def enqueue_for(self, actor_name: str, queue_name: str) -> Callable[[RunRecord], JobDispatch]:
        """Return an enqueue callable dispatching a run to ``actor_name`` on ``queue_name``."""

        def enqueue(run: RunRecord) -> JobDispatch:
            return self._enqueue(
                actor_name=actor_name,
                queue_name=queue_name,
                args=(run.run_id, dict(run.parameters)),
            )

        return enqueue

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
