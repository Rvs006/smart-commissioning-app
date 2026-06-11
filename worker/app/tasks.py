import dramatiq
from dramatiq.brokers.redis import RedisBroker
from smart_commissioning_core.mqtt_config_publish_processor import process_mqtt_config_publish_run
from smart_commissioning_core.udmi_run_processor import process_udmi_validation_run

from app.config import get_settings
from app.services.run_store import FileRunStore

settings = get_settings()
broker = RedisBroker(url=settings.redis_url)
dramatiq.set_broker(broker)


def _log_run(run_id: str, job_name: str) -> None:
    print(f"[worker] queued placeholder execution for {job_name} ({run_id})")


@dramatiq.actor(queue_name="discovery")
def discover_ip_range(run_id: str, parameters: dict) -> None:
    _log_run(run_id, "discover_ip_range")


@dramatiq.actor(queue_name="discovery")
def discover_bacnet(run_id: str, parameters: dict) -> None:
    _log_run(run_id, "discover_bacnet")


@dramatiq.actor(queue_name="discovery")
def discover_mqtt(run_id: str, parameters: dict) -> None:
    _log_run(run_id, "discover_mqtt")


@dramatiq.actor(queue_name="validation")
def validate_udmi_payloads(run_id: str, parameters: dict) -> None:
    print(f"[worker] starting UDMI validation ({run_id})")
    # live_capture=None: the worker has no broker configuration access yet, so
    # live capture stays an API-side capability for now.
    process_udmi_validation_run(
        run_id,
        parameters,
        run_store=FileRunStore(),
        execution_mode="dramatiq_worker",
        live_capture=None,
    )
    print(f"[worker] finished UDMI validation ({run_id})")


@dramatiq.actor(queue_name="validation")
def publish_mqtt_config(run_id: str, parameters: dict) -> None:
    print(f"[worker] starting MQTT config publish ({run_id})")
    # broker_publisher=None: validate-only path; the worker never publishes to
    # a live broker until it gains broker configuration access.
    process_mqtt_config_publish_run(
        run_id,
        parameters,
        run_store=FileRunStore(),
        execution_mode="dramatiq_worker",
        broker_publisher=None,
    )
    print(f"[worker] finished MQTT config publish ({run_id})")


@dramatiq.actor(queue_name="validation")
def validate_bacnet_points(run_id: str, parameters: dict) -> None:
    _log_run(run_id, "validate_bacnet_points")


@dramatiq.actor(queue_name="validation")
def compare_bacnet_mqtt(run_id: str, parameters: dict) -> None:
    _log_run(run_id, "compare_bacnet_mqtt")


@dramatiq.actor(queue_name="reports")
def generate_report(run_id: str, parameters: dict) -> None:
    _log_run(run_id, "generate_report")
