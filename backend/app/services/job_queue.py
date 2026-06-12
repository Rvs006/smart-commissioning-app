import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from app.core.config import get_settings
from app.schemas.jobs import RunRecord

_logger = logging.getLogger(__name__)

# Generic, credential-free message surfaced to the API/frontend when the queue
# cannot be reached. The redis_url may embed credentials (redis://:password@host)
# so it is NEVER included here; the host (without credentials) is logged instead.
_QUEUE_UNAVAILABLE_MESSAGE = "Job queue (Redis) is unavailable."


def _redis_host(redis_url: str) -> str:
    """Return the redis host[:port] for logging, stripping any credentials."""
    try:
        parts = urlsplit(redis_url)
    except ValueError:
        return "<unparseable>"
    host = parts.hostname or "<unknown>"
    if parts.port is not None:
        return f"{host}:{parts.port}"
    return host


class JobQueueUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class JobDispatch:
    actor_name: str
    queue_name: str
    message: str


class JobQueueService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def enqueue_udmi_validation(self, run: RunRecord) -> JobDispatch:
        return self._enqueue(
            actor_name="validate_udmi_payloads",
            queue_name="validation",
            args=(run.run_id, dict(run.parameters)),
        )

    def enqueue_mqtt_config_publish(self, run: RunRecord) -> JobDispatch:
        return self._enqueue(
            actor_name="publish_mqtt_config",
            queue_name="validation",
            args=(run.run_id, dict(run.parameters)),
        )

    def enqueue_ip_discovery(self, run: RunRecord) -> JobDispatch:
        return self._enqueue(
            actor_name="discover_ip_range",
            queue_name="discovery",
            args=(run.run_id, dict(run.parameters)),
        )

    def enqueue_bacnet_discovery(self, run: RunRecord) -> JobDispatch:
        return self._enqueue(
            actor_name="discover_bacnet",
            queue_name="discovery",
            args=(run.run_id, dict(run.parameters)),
        )

    def enqueue_mqtt_discovery(self, run: RunRecord) -> JobDispatch:
        return self._enqueue(
            actor_name="discover_mqtt",
            queue_name="discovery",
            args=(run.run_id, dict(run.parameters)),
        )

    def enqueue_bacnet_validation(self, run: RunRecord) -> JobDispatch:
        return self._enqueue(
            actor_name="validate_bacnet_points",
            queue_name="validation",
            args=(run.run_id, dict(run.parameters)),
        )

    def enqueue_mapping_validation(self, run: RunRecord) -> JobDispatch:
        return self._enqueue(
            actor_name="compare_bacnet_mqtt",
            queue_name="validation",
            args=(run.run_id, dict(run.parameters)),
        )

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

        return JobDispatch(
            actor_name=actor_name,
            queue_name=queue_name,
            message=f"Queued {actor_name} on {queue_name}.",
        )
