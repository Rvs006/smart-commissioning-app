# Smart Commissioning Worker

This worker owns long-running jobs for:

- IP discovery
- BACnet discovery
- MQTT discovery
- UDMI validation
- BACnet validation
- BACnet to MQTT mapping validation
- report generation

The current actors are placeholders that define the queue boundary and payload shape. Business logic should be ported into these actors from dedicated services and from the existing `device_udmi_payload_validation/` utility where appropriate.

