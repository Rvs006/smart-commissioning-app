from fastapi import APIRouter

router = APIRouter()


@router.get("/blueprint")
def get_blueprint() -> dict[str, object]:
    return {
        "services": ["frontend", "api", "worker", "postgres", "redis", "object-storage"],
        "modules": [
            "configuration",
            "ip_scanner",
            "bacnet_discovery",
            "mqtt_discovery",
            "udmi_validation",
            "data_validation",
            "reports",
        ],
        "jobs": [
            "ip_discovery",
            "bacnet_discovery",
            "mqtt_discovery",
            "udmi_validation",
            "mqtt_config_publish",
            "mapping_validation",
            "report_generation",
        ],
    }
