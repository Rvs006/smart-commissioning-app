"""Worker-side MQTT configuration values provider.

Phase 2 carry-forward: the worker process needs broker connection defaults so
the MQTT discovery / live UDMI capture / config-publish engines can connect when
run via the worker (not just the API). It registers a configuration-values
provider with ``smart_commissioning_core.mqtt_settings`` that reads the CURRENT
configuration snapshot from the SAME database the backend writes to (via the
shared ``ConfigurationRepository``).

WHAT THIS WIRES (and its honest limits):

* MQTT broker host / port / client id / keep-alive / username / password are
  read from the stored ``mqtt`` section. So an MQTT discovery run dispatched to
  the worker WITHOUT explicit broker params in its run parameters still resolves
  a broker host from stored configuration. Run parameters (``broker_host`` etc.)
  always take precedence (see ``build_mqtt_connection_settings``), so a run can
  also fully self-describe its broker without any stored configuration.

* CERTIFICATE material is NOT resolved here. The backend stores certs as
  encrypted files under its secrets root, keyed by a per-install key file the
  worker may not share. Decrypting ``secret://`` references requires the
  backend's secret store and is intentionally out of scope for this provider —
  TLS client-cert auth on the worker path therefore remains UNWIRED and is
  listed in the task's ``live_untested`` output. Username/password and a CA via
  run parameters still work; full mutual-TLS on the worker needs the secrets
  root to be shared and a cert resolver wired in a later phase.

The provider is best-effort: any database error yields empty defaults so a
worker without a reachable/migrated database still imports and runs jobs that
fully self-describe their broker via run parameters.
"""

from smart_commissioning_core.mqtt_settings import set_configuration_values_provider

# These mirror backend ConfigurationService.DEFAULT_PROJECT_ID / DEFAULT_SITE_ID.
# The single-tenant edge deployment uses one project/site; if a deployment uses
# others, the broker should instead be supplied via run parameters.
_DEFAULT_PROJECT_ID = "demo-project"
_DEFAULT_SITE_ID = "demo-site"


def _configuration_values() -> tuple[dict[str, object], dict[str, object]]:
    """Return (mqtt_values, certificate_values) from the stored configuration.

    Certificate values are returned EMPTY on purpose (see the module docstring):
    the worker cannot resolve the backend's encrypted secret:// references.
    """
    try:
        from smart_commissioning_core.db.repositories import ConfigurationRepository

        from app.db import get_engine

        payload = ConfigurationRepository(get_engine()).get_current(
            _DEFAULT_PROJECT_ID, _DEFAULT_SITE_ID
        )
    except Exception:
        # Best-effort: a worker without a reachable/migrated DB still runs jobs
        # whose run parameters carry the broker settings.
        return {}, {}

    if not isinstance(payload, dict):
        return {}, {}
    mqtt_section = payload.get("mqtt")
    mqtt_values = mqtt_section.get("values") if isinstance(mqtt_section, dict) else None
    return (mqtt_values if isinstance(mqtt_values, dict) else {}), {}


def register_worker_mqtt_configuration_provider() -> None:
    """Register the worker's configuration-values provider (idempotent)."""
    set_configuration_values_provider(_configuration_values)
