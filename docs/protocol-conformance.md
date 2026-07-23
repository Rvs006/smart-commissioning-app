# Protocol Conformance

What the Smart Commissioning App validates and supports across UDMI, MQTT, and
BACnet — honestly separated into **tested** (covered by in-process unit tests),
**simulated** (offline fixture/fake), and **live-untested** (requires on-site
validation against real brokers/hardware). Accurate to
`core/smart_commissioning_core/udmi_validation.py`, `mqtt_transport.py`,
`mqtt_settings.py`, and `engines/bacnet_discovery.py`.

## 1. UDMI

UDMI validation lives in `udmi_validation.py` (with the per-version structural
rules in `udmi_schema.py`). It validates expected MQTT assets against UDMI-style
`state`, `metadata`, and `pointset` contracts and emits the shared normalized
issue record. The register's **Expected schema version** is compared against
each payload's declared top-level `version` (mismatch → immediate critical
issue), and on a match the payload structure is checked offline against the
complete recursive canonical schema closure for that payload type (currently
the official `faucetsdn/udmi` `1.5.2` Draft 7 state, metadata, and
events/pointset schemas). An unknown declared version reports "structural
checks skipped" rather than silently passing.

**Non-published versions (2026-07-14):** projects that deliberately do not
conform to any published UDMI version declare a version label starting with
`nonpub` (e.g. `nonpub.1`) in both the register's Expected schema version and
the payloads. The validator then checks payloads against an operator-uploaded
schema set with that label (same `state.json` / `metadata.json` /
`events_pointset.json` layout as the vendored spec, uploaded on the UDMI page
via `POST /api/v1/udmi/schemas`; labels match case-insensitively). Only
canonical Draft 7 validation runs for nonpub payloads — the focused checks
encode published-1.5.2 assumptions. A declared nonpub version with no uploaded
set is a high-severity issue telling the operator exactly what to upload,
never a silent pass.

Validated features (each exercised by unit tests with inline/fixture payloads):

| UDMI feature | What is checked | Status |
| --- | --- | --- |
| Schema version match | register `Expected schema version` vs each payload's top-level `version`; a missing payload version is flagged, while a supported register version still supplies enough authority to check the remaining required fields without changing the raw payload; a declared mismatch is critical and blocks structural checks against the wrong schema. | Tested |
| Canonical schema checks (per declared version, 1.5.2 pinned) | complete offline validation through the official recursive state, metadata, and events/pointset schema closure: nested required fields, types, RFC 3339 formats, patterns, enums, numeric limits, and additional-property rules. Focused checks retain clearer point-name/shape messages where useful. | Tested |
| `state` payload | manufacturer (`system.hardware.make`) and model (`system.hardware.model`) match the asset register; offline/error states. | Tested (inline payloads) |
| `metadata` payload | asset GUID (`system.physical_tag.asset.guid`) matches the register; point units sourced from `metadata.pointset.points`; expected points must be defined in the metadata pointset (missing/extra flagged). | Tested |
| `pointset` payload | expected points present; unexpected points flagged; `present_value` is numeric for numeric units. | Tested |
| Timestamp notation | every schema-declared date-time remains subject to RFC 3339 validation; `Z`, `+00:00`, and `+01:00` are accepted; valid date-times inside one payload are also compared for lexical consistency in separator case, fractional precision, and timezone notation family. Seasonal signed-offset values are not required to be equal. | Tested |
| Units | the register's expected unit must be present in metadata and **match** it after normalisation (case, `-`/`_`, and explicit shorthand aliases such as `ppb` to `parts_per_billion`); import and validation share the vendored Google Digital Buildings unit vocabulary pinned under `core/smart_commissioning_core/schemas/dbo`; `parts_per_billion` and `parts_per_million` remain different units; numeric units require numeric values. | Tested |
| Schedule / expected-asset input | a supplied expected schedule (`expected_schedule` with `asset_id`, `manufacturer`, `model`, `guid`, `udmi_version`, `units`) drives the per-point checks; the backend fills it from the imported `mqtt_register` (including `Expected schema version` and wildcard `Expected topic` expansion, plus the legacy `…/event/pointset` capture alias). | Tested |
| Silent / not-publishing device | devices in `DevicesNotPublishing`, and (live) an empty **or partial** capture window, raise `not_publishing` — a partial capture names each expected topic that never reported. A completed capture window with silent or partially-reporting devices is terminal `succeeded` (stage `udmi_validation_complete_with_silent_devices`) so the operator lands on Results with each silent device red as not publishing; only transport/configuration failures (`broker_unreachable`/`tls_error`/`authentication_error`/`broker_timeout`/`live_capture_unavailable`/`missing_capture_topics`) are terminal `failed` — an unreached broker can never claim a validation. Operator cancellation remains terminal `cancelled` and keeps partial evidence. | Tested (fixture/inline); live capture is live-untested |
| Unexpected publisher measurement | register-driven live validation derives a non-global parent topic scope from expected publisher roots when that can be done safely; a bounded run observes the full configured window and counts publishers in that scope which match no expected asset. The count stays separate from expected, observed, compliance, and validation-issue totals. If no safe scope exists, the result states that measurement was unavailable instead of subscribing to bare `#`. | Tested with fake capture; live capture is live-untested |
| Missing vs unexpected points | expected-but-absent points and received-but-unexpected points are classified separately. | Tested |
| Full-report fixture mode | normalizes a `full_report.json` (DeviceList, PayloadErrors, PointsetErrors, StateErrors, ...) into issues. | Tested (packaged fixture) |

Issue taxonomy emitted (`ValidationIssueRecord`, prefixes): `UDMI-NP`
(not_publishing), `UDMI-PL` (payload_error), `UDMI-PS`
(pointset_validation), `UDMI-TS` (pointset_timestamp), `UDMI-ST`
(state_validation), `UDMI-MD` (metadata_validation). `UDMI-UN`
(unexpected_device) is a retired legacy prefix and is not emitted by the current
validation path; unexpected publishers are stored only as separate measurement
evidence.

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
| SUBSCRIBE + SUBACK (incl. failure 0x80) | Tested | All filters are encoded in one SUBSCRIBE before retained payloads can arrive; packet id, grant count, and rejection codes are checked. This fixes the real-broker ordering where a retained PUBLISH was mistaken for the next per-topic SUBACK. |
| PUBLISH send / receive, topic filter match | Tested | `read_publish` / `read_publish_any` with a fake socket; `#` and `prefix/#` filters match concrete publish topics. |
| Username / password auth | Tested (framing) | Credentials placed in the CONNECT payload; real broker auth is live-untested. |
| TLS (CA cert, client cert, private key, SNI) | Live-untested | `_wrap_tls` builds an SSL context and loads cert/key from file paths; not exercised against a real TLS broker here. |
| Config-publish + wait-for-next-pointset | Tested (flow with fake); live-untested | `publish_config_and_wait_for_pointset`; live broker write requires authorization (`docs/security-posture.md`). |
| Retained-message evidence | Tested | Incoming RETAIN is preserved on `MqttMessage` and payload views. A retained pointset older than the register's Expected reporting interval is a validation issue, never current commissioning evidence. |
| Keep-alive PINGREQ | Tested (framing); live-untested | Long/indefinite captures ping at half the configured keep-alive; broker-drop errors preserve partial evidence. |
| QoS > 0 PUBLISH acknowledgement | Not implemented | SUBSCRIBE requests the configured maximum QoS, but outbound config PUBLISH remains QoS0 and the client is not a general-purpose QoS1/2 session implementation. Validate Niagara publishes at QoS0 (as in field engineer's 2026-07-10 broker log) or set subscribe QoS0 until this is implemented. |

Connection settings (`mqtt_settings.py`) parse broker host/port, client id,
keep-alive, username/password, and TLS material; `secret://` references are not
treated as file paths (so masked references never get mis-loaded). The transport
imports cleanly with stdlib only.

## 3. BACnet

BACnet discovery (`engines/bacnet_discovery.py`) is behind a swappable backend
abstraction with two implementations:

- **`SimulatedBacnetBackend` (DRY-RUN/TEST ONLY).** A deterministic in-memory fixture
  (a few fake AHU/VAV/chiller devices with objects and present-values). Performs
  **no network I/O** and produces sample data only for previews and explicitly
  injected tests. A non-dry run requesting simulation is rejected. Results are
  stamped `result_summary["backend"] == "simulated"`. **Tested.**
- **`Bacpypes3Backend` (LIVE DEFAULT, UNVALIDATED).** The real BACnet/IP path using the
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

**Transport modes — a BBMD is OPTIONAL; local broadcast is the default (hard
requirement, field-confirmed 2026-07-17).** Not every job site has a BBMD:
single-subnet ("flat") networks are a normal case, and the app must scan them
with Foreign Device left Disabled — the scan then uses **local broadcast
only**, which on a flat network is the healthy state, not a degraded fallback.
Foreign-device registration through a BBMD engages ONLY when the saved
configuration has Foreign Device = Enabled (with a real BBMD Address), and
exists solely to reach devices behind routers on other subnets. Operator docs
and messages must never present "local broadcast only" as an error, and must
never instruct enabling the BBMD fields except for sites that actually have
one — a 2026-07-17 field session was misdirected by exactly that (see the
correction note in `docs/field-message-v0.1.15.md`).

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
| Simulated AHU / VAV / chiller controllers | Simulated (tested) | Dry-run/test fixture; analog-input/value, binary-output objects. |
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
