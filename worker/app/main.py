import logging

from app.logging import configure_logging
from app.tasks import broker


def main() -> None:
    # Importing app.tasks already installs structured logging at import time;
    # call it again here so running ``python -m app.main`` directly is covered.
    configure_logging()
    logger = logging.getLogger("smart_commissioning.worker")
    actors = sorted(str(actor) for actor in broker.get_declared_actors())
    logger.info("Registered worker actors", extra={"actors": actors, "actor_count": len(actors)})


if __name__ == "__main__":
    main()
