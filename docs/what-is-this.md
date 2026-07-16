# What is this? — Smart Commissioning App in plain English

A one-page onboarding for anyone new to the project — read this before the README's
quickstart. For the full feature map and code references, see
[review-comments-verification.md](review-comments-verification.md); for architecture, see
[production-architecture.md](production-architecture.md).

---

## The one-liner

**Smart Commissioning App is the tool an engineer uses on a building site to prove that every
smart device was installed and configured correctly — then hand the client a signed report as
evidence.** It replaces the manual grind of laptops + spreadsheets + MQTT Explorer with one
guided workflow.

---

## What it is, for a human

When a new "smart building" is built, hundreds or thousands of devices go in — sensors, meters,
HVAC controllers, valves — all wired into the Building Management System (BMS). Before handover,
someone has to check: *Is each device actually there? Is it talking on the network? Is it sending
the right data, in the right format, matching the design?*

Today that check is done by hand: an engineer with a laptop, a spreadsheet of what *should* be
there, and tools like MQTT Explorer to sniff the network — ticking boxes one by one. It is slow,
error-prone, and the "proof" ends up being a messy spreadsheet.

This app turns that into a repeatable, guided process and produces real evidence at the end. Think
of it as a **commissioning co-pilot**: it knows what the building is *supposed* to look like, goes
and looks at what's *actually* there, tells you what matches and what doesn't, and prints the
certificate.

---

## What it does — the 5 steps

1. **Configure** — tell it about the site: network, BACnet, MQTT broker (with security
   certificates), timezone, backups.
2. **Import** — upload the design's expected list: the devices and data points that *should*
   exist (a CSV/Excel; the app gives you a blank template for each module).
3. **Discover** — it scans the live building network to see what is *actually* there: an IP scan,
   BACnet device discovery, and MQTT topic capture (the MQTT-Explorer part — subscribe to topics,
   watch live payloads, export them).
4. **Validate** — it compares actual vs. expected: Are the payloads in the correct **UDMI** format?
   Do the data types match (e.g. a temperature is a number, not text)? Does the BACnet side agree
   with the MQTT side within tolerance? The result is a clear **Pass / Fail with the reason**.
5. **Report** — export a commissioning evidence pack (Excel / Word / Zip), digitally signed so it
   cannot be tampered with, to hand to the client.

**Bonus:** a controlled **config publish** — correct a device's setpoint and confirm the change
actually took effect, with a rollback if needed.

---

## How to explain it to an engineer

### As a human (the "why")

> "It's the on-site tool for proving the building's devices are installed and reporting correctly,
> instead of doing it by hand. You set up the site once, import the expected device list, run the
> scans, and it shows you Pass/Fail per device with reasons. At the end it produces a signed
> evidence report for the client. The MQTT-Explorer-style live capture is built in, so you don't
> need a separate tool."

### As a technical engineer (the "how")

- **Shape:** a web app. **React/TypeScript** frontend → **FastAPI** backend → a **Redis-queued
  Dramatiq worker** for long scans. The scan/validation logic lives in one shared Python package
  (`smart_commissioning_core`), so a quick in-request run and a heavy queued run behave identically.
- **Data:** **SQLAlchemy + Alembic**, on **SQLite** for a single laptop or **PostgreSQL** for the
  hosted/central setup.
- **Runs three ways** (`DEPLOYMENT_ROLE`): a portable **`.exe`** (one laptop, works offline /
  air-gapped), an **`edge`** instance (does the on-site scanning, then syncs signed run bundles to a
  hub), and a **`hub`** (central, multi-project, with logins and roles).
- **Engines:** IP scan (TCP), BACnet discovery, MQTT discovery (a real MQTT client), UDMI payload
  validation, BACnet ↔ MQTT comparison. **Scan-safety is built in:** every live scan is dry-run by
  default, requires an explicit "authorized" flag, is rate-throttled, and is cancellable — it will
  not blast a live building network by accident.
- **Security:** per-user roles (`viewer < reviewer < engineer < admin`), API-key auth, broker
  passwords/TLS keys encrypted at rest, and evidence/reports hashed + signed (SHA-256 + Ed25519).
- **Honest status:** code-complete and hardened; the *live-network* paths (real BACnet/MQTT
  hardware, a real broker) are pending **on-site validation** — they are explicitly marked, never
  faked. See [protocol-conformance.md](protocol-conformance.md) and
  [phase5-onsite-validation.md](phase5-onsite-validation.md).

### How they actually run it

Follow the **[README](../README.md)** quickstart — three ways to start (the Docker one-liner is
easiest). Then use **[review-comments-verification.md](review-comments-verification.md)** to see
every feature, where it lives in the code, and the exact screen to see it on.

> The repository is **public** — a new engineer can clone it directly, no
> collaborator invitation needed.
