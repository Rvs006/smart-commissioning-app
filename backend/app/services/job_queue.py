from dataclasses import dataclass

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from app.core.config import get_settings
from app.schemas.jobs import RunRecord


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
            raise JobQueueUnavailable(
                f"Could not enqueue {actor_name} on Redis broker {self.settings.redis_url}: {error}"
            ) from error
        finally:
            broker.close()

        return JobDispatch(
            actor_name=actor_name,
            queue_name=queue_name,
            message=f"Queued {actor_name} on {queue_name}.",
        )
