# Production Architecture

## Goal

Build the Smart Commissioning Tool as a real commissioning platform, not a browser-only mockup. The production system must satisfy the workflow and acceptance criteria in `Smart Commissioning Tool Specification.pdf` while incorporating the useful MQTT and UDMI validation logic already present in `device_udmi_payload_validation/`.

## Current File Assessment

### What is already useful

- The original UI prototypes (`preview.html` and `smart_commissiong_tool_ui.jsx`, removed after the baseline commit and available in git history at commit `3471050`) and `Smart Commissioning Tool UI.txt` provide the correct operator workflow, navigation model, detail-inspector pattern, and commissioning language.
- `device_udmi_payload_validation/` contains real MQTT subscription and UDMI payload validation logic.
- `Smart Commissioning Tool Specification.pdf` defines the actual product behaviour, data model, and acceptance criteria.

### What is not production-ready

- The HTML prototypes persisted state in browser storage and used mocked scan and validation flows.
- The UDMI validator is a standalone script with absolute local paths, no app-level API, no shared issue model, and no persistent run history.
- The workspace does not yet contain the backend data model, import framework, background job system, or secure configuration storage required by the specification.

## Recommended Stack

### Frontend

- React
- TypeScript
- Vite
- React Router
- TanStack Query
- TanStack Table
- TanStack Virtual
- React Hook Form
- Zod

### Backend and Jobs

- FastAPI for HTTP APIs
- PostgreSQL for persistent domain data
- Redis for queues, transient job state, and cache
- Dramatiq workers for discovery and validation jobs
- Server-Sent Events for job progress first, WebSockets later if needed

### Protocol and Evidence Services

- BACnet: `bacpypes3` or `BAC0`
- MQTT: `paho-mqtt` or an async MQTT client
- IP scan orchestration: `nmap` subprocesses plus controlled parsing
- Reports: `pandas`, `openpyxl`, `WeasyPrint` or `Playwright`
- Object storage for raw evidence: local disk in MVP, MinIO or S3-compatible storage after that

### Deployment

- Docker Compose for local and pilot deployment
- Linux host or gateway located near the project network

## System Context

```text
React UI
  -> FastAPI API
    -> PostgreSQL
    -> Redis
    -> Object Storage
    -> Worker Queue

Worker Queue
  -> IP Discovery Job
  -> BACnet Discovery Job
  -> MQTT Discovery Job
  -> UDMI Validation Job
  -> BACnet to MQTT Comparison Job
  -> Report Generation Job
```

## Product Modules

### Configuration

Purpose:
- store network, BACnet, MQTT, certificate, time, backup, and logging settings

Primary service:
- configuration service in API

Persistence:
- PostgreSQL settings tables plus secure secret references

### Imports

Purpose:
- import IP registers, BACnet registers, MQTT registers, validation files, mapping files, tolerance files, and expected units files

Primary service:
- import service in API

Persistence:
- imported file metadata in PostgreSQL
- raw source files in object storage
- rejected-row reports in object storage

### IP Scanner

Purpose:
- discover reachable devices, services, rogue hosts, and register alignment

Primary service:
- background job plus result query APIs

### BACnet Discovery

Purpose:
- discover BACnet devices and live objects, then compare against expected registers

Primary service:
- background job plus BACnet detail APIs

### MQTT Discovery

Purpose:
- connect to broker, discover topics and assets, inspect payloads, extract points, compare against expected MQTT register

Primary service:
- streaming MQTT discovery job plus payload extraction service

### UDMI Validation

Purpose:
- validate expected MQTT assets against UDMI-style `state` and `pointset` contracts, including silent-device detection and evidence capture

Primary service:
- background UDMI validation job

Source of logic:
- port the useful behaviour from `device_udmi_payload_validation/run_payload_validation.py` and `device_diagnostic_data.py`

### Data Validation

Purpose:
- validate BACnet point quality
- validate MQTT payload quality
- compare BACnet source values to MQTT translated values
- raise issues with severity and traceability

Primary service:
- validation engine plus shared issue model

### Reports

Purpose:
- generate evidence packs and export discovery and validation outputs

Primary service:
- report generation job plus file delivery endpoints

## Shared Domain Model

The backend should converge on these core entities from the specification:

- `project`
- `site`
- `system`
- `device`
- `asset`
- `ip_scan_run`
- `ip_scan_result`
- `bacnet_discovery_run`
- `bacnet_device`
- `bacnet_object`
- `mqtt_discovery_run`
- `mqtt_topic`
- `mqtt_payload`
- `mqtt_extracted_point`
- `validation_run`
- `validation_result`
- `mapping_result`
- `issue`
- `report`
- `user_note`
- `import_batch`
- `evidence_artifact`

## Normalized Issue Model

All validation paths should emit one shared issue contract:

- `issue_id`
- `run_id`
- `project_id`
- `site_id`
- `asset_id`
- `device_id`
- `source`
- `issue_type`
- `severity`
- `status`
- `point_name`
- `topic`
- `bacnet_object_ref`
- `description`
- `expected_value`
- `observed_value`
- `tolerance`
- `suggested_action`
- `raw_evidence_uri`
- `created_at`
- `resolved_at`

This matters because the current UDMI validator produces separate payload, state, and pointset error buckets. The production app should store them in one consistent structure.

## UDMI Integration Plan

Port these concepts from the current validator into the app:

- topic subscription for `state` and `pointset`
- mapping from topic segment to expected device
- expected device contract from `metadata.json`
- silent-device detection based on timeout
- state-only device handling for gateway-style devices
- point presence and type checks
- per-device `state.json`, `pointset.json`, and `errors.json` evidence artifacts
- summary JSON and XLSX outputs

Restore or extend these rules from the older validator logic:

- percent range validation
- degrees-celsius lower bound checks
- enum upper-bound checks
- clearer missing-point and unexpected-point classification

## API Surface

### Health and Metadata

- `GET /api/v1/health`
- `GET /api/v1/blueprint`

### Configuration

- `GET /api/v1/configuration`
- `PUT /api/v1/configuration`
- `POST /api/v1/configuration/validate`

### Imports

- `POST /api/v1/imports`
- `GET /api/v1/imports/{import_id}`
- `GET /api/v1/imports/{import_id}/errors`

### Discovery

- `POST /api/v1/discovery/ip/runs`
- `POST /api/v1/discovery/bacnet/runs`
- `POST /api/v1/discovery/mqtt/runs`
- `GET /api/v1/discovery/runs/{run_id}`
- `GET /api/v1/discovery/runs/{run_id}/results`

### Validation

- `POST /api/v1/validation/udmi/runs`
- `POST /api/v1/validation/bacnet/runs`
- `POST /api/v1/validation/mapping/runs`
- `GET /api/v1/validation/runs/{run_id}`
- `GET /api/v1/validation/runs/{run_id}/issues`

### Reports

- `POST /api/v1/reports`
- `GET /api/v1/reports/{report_id}`
- `GET /api/v1/reports/{report_id}/download`

## Background Jobs

The spec explicitly expects long-running operations and scalable data handling. These jobs should run asynchronously:

- IP scan
- BACnet discovery
- MQTT discovery
- UDMI validation
- BACnet to MQTT comparison
- report generation

Each job should have:

- `queued`
- `running`
- `succeeded`
- `failed`
- `cancelled`

and publish:

- progress percent
- current stage
- affected project and site
- counts discovered and validated
- latest user-facing message

## Security and Secret Handling

Do not store private keys or passwords in browser storage.

Use:

- secure server-side secret storage references
- masked values in API responses
- TLS for MQTT
- certificate-backed client authentication where required
- audit logs for configuration changes, imports, runs, reports, and exports

## MVP Scope

The first production build should deliver:

- React app shell and routed pages
- FastAPI API shell
- PostgreSQL-backed configuration and import metadata
- CSV and XLSX import framework
- IP scan job lifecycle
- MQTT discovery job lifecycle
- UDMI validation job lifecycle
- BACnet to MQTT comparison result model
- report generation stubs and downloadable files

## Later Scope

Keep architecture ready for:

- Modbus TCP
- SNMP
- OPC UA
- REST API validation
- role-based access
- scheduled validation runs
- historical run comparison
- issue tracker integration

## Scaffold Layout

```text
docs/
  production-architecture.md
frontend/
  src/
backend/
  app/
worker/
  app/
infra/
  docker-compose.yml
```

## Immediate Build Order

1. Stand up the shared domain model and import framework.
2. Replace browser-local configuration with backend-backed settings.
3. Implement the job registry and status APIs.
4. Port MQTT and UDMI validation into worker jobs.
5. Implement report and evidence storage contracts.
6. Replace prototype pages with real API-driven React pages one module at a time.

