# Protocol Conformance

What the Smart Commissioning App validates and supports across UDMI, MQTT, and
BACnet — honestly separated into **tested** (covered by in-process unit tests),
**simulated** (offline fixture/fake), and **live-untested** (requires on-site
validation against real brokers/hardware). Accurate to
`core/smart_commissioning_core/udmi_validation.py`, `mqtt_transport.py`,
`mqtt_settings.py`, and `engines/bacnet_discovery.py`.

## 1. UDMI

UDMI validation lives in `udmi_validation.py`. It validates expected MQTT assets
against UDMI-style `state`, `metadata`, and `pointset` contracts and emits the
shared normalized issue record. There is no single pinned UDMI schema-version
import here; validation is **feature-level** against the UDMI message shapes the
tool consumes, rather than a claim of full conformance to a specific UDMI
release.

Validated features (each exercised by unit tests with inline/fixture payloads):

| UDMI feature | What is checked | Status |
| --- | --- | --- |
| `state` payload | manufacturer (`system.hardware.make`) and model (`system.hardware.model`) match the asset register; offline/error states. | Tested (inline payloads) |
| `metadata` payload | asset GUID (`system.physical_tag.asset.guid`) matches the register; point units sourced from `metadata.pointset.points`. | Tested |
| `pointset` payload | expected points present; unexpected points flagged; `present_value` is numeric for numeric units. | Tested |
| Units | unit is a known UDMI unit (`degrees_celsius`, `parts_per_million`, `percent`, `volts`, `amperes`, `hertz`, `kilowatts`, `kilowatt_hours`, `kilovolt_amperes`, `kilovolt_amperes_reactive`, plus `no_units`/`boolean`/`enum`); numeric units require numeric values. | Tested |
| Schedule / expected-asset input | a supplied expected schedule (`expected_schedule` with `asset_id`, `manufacturer`, `model`, `guid`, `units`) drives the per-point checks. | Tested |
| Silent / not-publishing device | devices in `DevicesNotPublishing`, and (live) an empty capture window, raise `not_publishing`. | Tested (fixture/inline); live capture is live-untested |
| Missing vs unexpected points | expected-but-absent points and received-but-unexpected points are classified separately. | Tested |
| Full-report fixture mode | normalizes a `full_report.json` (DeviceList, PayloadErrors, PointsetErrors, StateErrors, ...) into issues. | Tested (packaged fixture) |

Issue taxonomy emitted (`ValidationIssueRecord`, prefixes): `UDMI-NP`
(not_publishing), `UDMI-UN` (unexpected_device), `UDMI-PL` (payload_error),
`UDMI-PS` (pointset_validation), `UDMI-ST` (state_validation), `UDMI-MD`
(metadata_validation).

**Live capture** (subscribing to a device's `…/state`, `…/metadata`,
`…/pointset` topics, or a broader `#` / `prefix/#` filter on a live broker) is
**live-untested**: with no broker egress the engine honestly records
`live_capture_unavailable`,
`missing_capture_topics`, `broker_unreachable`/`tls_error`/
`authentication_error`/`broker_timeout`, or `live_capture_timeout` rather than
fabricating payloads. The mapping from topic suffix to payload bucket and the
status-labelling logic are tested with a fake; MQTT wildcard filter matching is
covered by a raw transport regression test. The actual subscribe-and-capture
against a real broker requires on-site validation.

## 2. MQTT

MQTT support is a **hand-rolled MQTT 3.1.1 client** (`mqtt_transport.py`) over a
raw socket — no `paho-mqtt` dependency. It implements exactly the subset the
tool needs: CONNECT/CONNACK (with the documented CONNACK return-code mapping),
SUBSCRIBE/SUBACK, PUBLISH (send + receive), and DISCONNECT, with MQTT
remaining-length varint encoding/decoding.

| MQTT capability | Status | Notes |
| --- | --- | --- |
| Protocol level 3.1.1 (`MQTT`, level `0x04`) | Tested | CONNECT variable header is hard-coded to 3.1.1. |
| Packet framing (remaining-length varint, UTF-8 string fields) | Tested | Encode/decode exercised with a fake socket. |
| CONNECT + CONNACK return codes | Tested | Codes 1–5 mapped to clear errors (e.g. "bad username or password", "not authorised"). |
| SUBSCRIBE + SUBACK (incl. failure 0x80) | Tested | Rejected subscription raises. |
| PUBLISH send / receive, topic filter match | Tested | `read_publish` / `read_publish_any` with a fake socket; `#` and `prefix/#` filters match concrete publish topics. |
| Username / password auth | Tested (framing) | Credentials placed in the CONNECT payload; real broker auth is live-untested. |
| TLS (CA cert, client cert, private key, SNI) | Live-untested | `_wrap_tls` builds an SSL context and loads cert/key from file paths; not exercised against a real TLS broker here. |
| Config-publish + wait-for-next-pointset | Tested (flow with fake); live-untested | `publish_config_and_wait_for_pointset`; live broker write requires authorization (`docs/security-posture.md`). |
| QoS > 0, retained-message semantics, keep-alive PINGREQ | Not implemented | The client uses QoS 0 framing and does not maintain keep-alive pings; sufficient for capture/publish, not a general-purpose client. |

Connection settings (`mqtt_settings.py`) parse broker host/port, client id,
keep-alive, username/password, and TLS material; `secret://` references are not
treated as file paths (so masked references never get mis-loaded). The transport
imports cleanly with stdlib only.

## 3. BACnet

BACnet discovery (`engines/bacnet_discovery.py`) is behind a swappable backend
abstraction with two implementations:

- **`SimulatedBacnetBackend` (DEFAULT).** A deterministic in-memory fixture
  (a few fake AHU/VAV/chiller devices with objects and present-values). Performs
  **no network I/O**, so the engine runs end-to-end **offline** and produces
  sample data. Results are stamped `result_summary["backend"] == "simulated"` so
  simulated data is never mistaken for a real scan. **Tested.**
- **`Bacpypes3Backend` (REAL, UNVALIDATED).** The real BACnet/IP path using the
  optional `bacpypes3` dependency (Who-Is + ReadProperty). It has **never been
  integration-tested** — there is no BACnet device or building network in this
  environment. The `bacpypes3` import is **lazy and guarded**: importing the
  module never requires `bacpypes3`; selecting the real backend without the
  extra raises a clear `RuntimeError` with an install hint. Uncertain API
  assumptions are flagged inline with `# UNVERIFIED:` comments. **Live-untested —
  REQUIRES on-site validation against real controllers.**

| BACnet capability | Status | Notes |
| --- | --- | --- |
| Who-Is / I-Am over a device-instance range | Simulated (tested) / real (live-untested) | Default range is the full 0–4194303 22-bit space unless narrowed. |
| Per-device object-list read | Simulated (tested) / real (live-untested) | Real path may need APDU chunking on large devices (flagged UNVERIFIED). |
| present-value read per object | Simulated (tested) / real (live-untested) | Per-point read errors are recorded without aborting the device scan. |
| Throttle + cooperative cancellation | Tested | Devices scanned as throttled units; cancellation yields partial results. |
| Dry-run plan (no Who-Is broadcast) | Tested | Returns the planned instance range + actions with no I/O. |
| Authorization gate before a real scan | Tested | A real (non-dry-run) scan requires the authorization contract. |
| Directed (unicast) Who-Is | Not wired in real path | The documented signature did not accept it; flagged UNVERIFIED. |

Install the real path with the optional extra:
`pip install 'smart-commissioning-core[bacnet]'` (not installed by default; see
`docs/SBOM.md`).

## 4. Compatibility matrix

Honest status per environment. **Tested** = covered by in-process unit tests;
**Simulated** = offline fixture/fake, no real device/broker; **Untested** =
requires on-site/live validation, not asserted here.

### MQTT brokers

| Broker | Status | Notes |
| --- | --- | --- |
| Generic MQTT 3.1.1 broker (Mosquitto, etc.), plaintext | Untested (live) | Framing/return-code handling tested with a fake socket; not run against a live broker here. |
| MQTT 3.1.1 broker over TLS | Untested (live) | TLS context + cert/key loading implemented; no live TLS broker in this environment. |
| Authenticated broker (username/password) | Untested (live) | CONNECT auth framing tested; real credential acceptance is live. |
| MQTT 5.0 broker | Not supported | Client speaks 3.1.1 only. |
| In-process fake transport | Tested | Drives all client paths deterministically. |

### BACnet device classes

| Device class | Status | Notes |
| --- | --- | --- |
| Simulated AHU / VAV / chiller controllers | Simulated (tested) | The default fixture; analog-input/value, binary-output objects. |
| Real BACnet/IP controllers (any vendor) | Untested (live) | `Bacpypes3Backend` — UNVALIDATED, requires on-site validation. |
| BACnet MS/TP (serial) | Not supported | Only BACnet/IP is modelled. |
| Devices needing APDU-chunked object-list reads | Untested (live) | Flagged UNVERIFIED in the real backend. |

### Protocols overall

| Protocol | Status |
| --- | --- |
| UDMI state/metadata/pointset validation (inline/fixture) | Tested |
| UDMI live broker capture | Untested (live) |
| MQTT 3.1.1 client framing | Tested (fake socket) |
| MQTT against a live broker (plain/TLS/auth) | Untested (live) |
| BACnet discovery (simulated) | Simulated (tested) |
| BACnet discovery (real, bacpypes3) | Untested (live) |
| Modbus TCP / SNMP / OPC UA / REST validation | Not implemented (architecture "later scope") |

## 5. Honesty summary

Everything marked **Tested** runs in-process against fakes, temporary SQLite, or
packaged fixtures — no live infra. Everything marked **Untested (live)** depends
on a real broker, a real BACnet device, or a real building network that does not
exist in this environment, and is **not** claimed to work; those paths are
written conservatively, import-guarded so they never crash an import when a
dependency is absent, and flagged for on-site validation. See
`docs/security-posture.md` for the authorization controls that gate the live
paths and `docs/runbook.md` for the operational guidance.
