# v0.1.55 - scanner panels become one Scan surface with results

v0.1.55 finishes the scanner "marriage": the three embedded standalone scanners
(IP, BACnet, MQTT) are reskinned to the SCT theme and wired so a scan done inside
the embedded tool lands as a real SCT run, with the saved configuration pushed
into the tool on open. It builds on the Advanced-panel reverse proxy from v0.1.53
and v0.1.54; that proxy, its role checks, and its evidence rows are unchanged. No
database migration (Alembic head a6b7c8d9e0f1, Sync v2 head a7b8c9d0e1f2).

## What changed

- Reskin: the vendored IP, BACnet, and MQTT scanners now render in the SCT
  Electracom theme (warm cream and teal) through an injected stylesheet. The
  tools' own `app.js` is untouched, so every scanner function is preserved.
- Config-in: when a Source Interface is configured, the panel pre-selects the
  matching NIC in the embedded IP and BACnet tools, and prefills the MQTT connect
  modal with the saved broker (secrets left blank). The IP scan range is prefilled
  from the interface subnet.
- Results-out: a completed panel scan is captured and persisted as a real
  `ip_scanner` / `bacnet_scanner` / `mqtt_scanner` run, so the Results tab, run
  history, and reports fill in. IP and BACnet re-compare (re-RAG against a loaded
  register) is captured too. A completed BACnet export is captured so its
  discovered points are persisted rather than dropped.
- One Scan surface: the IP, BACnet, and MQTT sidecar modules present the embedded
  scanner as the whole module. The old Setup / Run / Results wizard is retired for
  these modules; the built-in discovery modules are unchanged.

## Fixes in this release

- A panel session is now bound to the protocol it was opened for. A session
  opened for one scanner can no longer authorize or attribute a write for another;
  a cross-protocol write is refused.
- The panel checks the session response and probes the sidecar before showing the
  embedded tool. A failed session or an unavailable sidecar now shows an error
  with a Retry control instead of a ready-looking panel that does not work.
- A completed BACnet export no longer loses its points: the export archive is
  decoded and persisted with the run.

Device writes from a panel are still gated by an in-app confirmation and a
single-use, request-bound token, and every action is still recorded as an SCT run
row (`scanner_raw_action` for reads, `scanner_raw_write` for writes). This release
adds no BACnet write capability.

## Compatibility and scope

The scanner modules and the Advanced panel run on the local inline executor and
authenticate via the local principal, so they are available in the portable and
local deployments; a keyed hosted deployment still needs the deferred
panel-session credential. Built-in TCP connect remains the default; Nmap stays
optional, locally installed, and unbundled.

## Validation boundary

CI builds and boot-smokes the portable bundle on a Windows Server 2022 runner.
Field acceptance for this release is recorded privately.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [migration rollback guide](migration-rollback-v0.1.55.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.55.md), and
[release validation record](release-validation-v0.1.55.md).
