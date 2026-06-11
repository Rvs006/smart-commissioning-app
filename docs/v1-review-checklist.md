# Smart Commissioning V1 Review Checklist

This checklist maps the V1 review notes from `Smart Commissioning Tool version 1 notes.docx` to the production React/FastAPI/worker scaffold. Status values describe the implementation in this repository, not legacy static prototypes.

| Review note | Status | Evidence | Verification |
| --- | --- | --- | --- |
| Rename the starting workspace to Homepage. | Implemented and browser/API verified | Frontend navigation/page title uses `Homepage`, and `/` now serves the React production scaffold. | `npm run build`; browser check |
| Keep module order as configuration, discovery, validation, reporting. | Implemented and browser/API verified | Navigation flows Homepage, Configuration, IP Scanner, BACnet, MQTT Discovery, UDMI, Validation, Reports. | `npm run build`; browser check |
| Remove redundant information icons. | Implemented and browser/API verified | Production React pages do not render inline `i` help icons; section descriptions carry context. | Browser check |
| Rename MQTT / UDMI to MQTT Settings. | Implemented and browser/API verified | Configuration section label is `MQTT Settings`; discovery navigation uses `MQTT Discovery` for module clarity. | `npm run build`; browser check |
| Add subnet mask and DNS servers. | Implemented | Backend defaults and validation include `Subnet Mask` and `DNS Servers`; UI renders typed fields. | `python -m unittest discover -s backend/tests -p "test_*.py"` |
| Add BBMD UDP port, foreign device enabled/disabled, and TTL. | Implemented | Backend defaults/validation and UI select controls cover these BACnet fields. | Backend unit tests |
| Add MQTT broker FQDN or IP address, client ID, keep-alive, username, and password. | Implemented | Backend migrates old MQTT field names and validates broker/client/keep-alive; UI masks password. | Backend unit tests, frontend build |
| Allow CA certificate, client certificate, and private key paste or file selection. | Implemented | `POST /configuration/secrets` stores content server-side and returns masked metadata; UI supports paste and file read. | Backend unit tests, frontend build |
| Avoid duplicate certificate validity, NTP status, and logging status. | Implemented | Certificate validity is section status only; NTP/logging status remain section status fields. | Backend unit tests |
| Add Backup & Restore fields. | Implemented | Defaults and validation include backup schedule, retention, encrypted enabled/disabled, location, last status, and restore action. | Backend unit tests |
| Add last-seen timestamps. | Implemented | Discovery/UDMI result schemas and UI tables include last-seen fields. | Backend unit tests, frontend build |
| Add detailed connection/fault status. | Implemented | Discovery observations and issue records include `status_detail`; UI tables show detailed status. | Backend unit tests, frontend build |
| Add copy payload button. | Implemented and browser/API verified | MQTT and UDMI table rows render a `Copy payload` action for raw payload evidence; copy failures render as warnings, not success. | Frontend build; browser clipboard check |
| Add message count. | Implemented | UDMI summary and MQTT/UDMI UI rows include message counts. | Backend unit tests, frontend build |
| IP scanner should distinguish UDP/TCP and scan common ports if none specified. | Implemented | `parse_port_specification` returns common 47808/udp, 80/tcp, and 443/tcp fallback; IP Discovery exposes editable port/protocol rows and sends the selected `port_specification` to the API. | Backend unit tests, frontend build |
| Add MAC address and MAC-first matching. | Implemented | Discovery observation helper normalizes MAC and uses MAC match before IP/hostname. | Backend unit tests |
| Validate UDMI pointset, metadata, and state payloads against schedules. | Implemented | UDMI adapter treats supplied schedule/payload JSON as first-class validation input, can capture live state/metadata/pointset topics from the configured MQTT broker, and emits specific state, metadata, pointset, unit, GUID, and type issues. | Backend unit tests, frontend build |
| Add config payload publishing and next pointset verification. | Implemented and browser/API verified | `mqtt_config_publish` can publish through the configured MQTT broker and subscribe for the next pointset message, while retaining the safety-gated local pointset verification path for offline review. | Backend unit tests, frontend build, browser check |
| Rename BACnet point issue register to BACnet point issue report and align report names. | Implemented | Reports workspace and module language use `issue report` labels. | Frontend build |

## Remaining Manual Acceptance Checks

- Start API and frontend together, then click through Homepage, Configuration, IP Scanner, BACnet, MQTT Discovery, UDMI, Data Validation, and Reports on desktop and mobile widths.
- In Configuration, paste a sample PEM, select a sample file, store it, and confirm the saved field becomes a masked `secret://` reference.
- In MQTT/UDMI tables, use `Copy payload` and confirm the clipboard receives the raw JSON in a browser context.
- In UDMI validation, confirm pasted schedule/payload JSON validates, the live broker capture toggle requests the configured topics, and the publish checkbox blocks accidental publish attempts until selected.
- Re-enable kluster.ai protection and rerun `kluster_code_review_auto`; the current session could not receive Kluster findings because the trial has ended.
