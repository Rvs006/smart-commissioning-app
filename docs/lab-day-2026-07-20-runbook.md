# Lab day runbook — Monday 2026-07-20, BACnet discovery (v0.1.17)

For the engineer running BACnet discovery in the lab. Follow it top to bottom.
Every quoted message in this document was copied out of the shipped code (the
quotes originate in v0.1.12 and are unchanged through v0.1.17) — if you see
wording that is close but not identical, tell us, because that means something
changed under us.

**This document changed on 2026-07-17.** The original assumed the lab sits
behind a BBMD and made BBMD registration the main path. A live field session on
2026-07-17 established two things: a BBMD is **optional** — flat single-subnet
scanning with Foreign Device left alone is a first-class, and now
**field-proven**, path — and the previous build's live-scan save crash (HTTP
500 + runs frozen at "running") is fixed in v0.1.16. Read
[Section 9 — what is proven and what is not](#9-what-is-proven-and-what-is-not)
before you start.

---

## 0. First question: which network shape is the lab?

Answer this before touching anything, because it decides which pre-flight you
run:

- **Path A — flat network.** The BACnet devices are on the **same subnet** as
  your laptop. No router hops, no BBMD needed, and Foreign Device stays
  Disabled. This is the path proven on real hardware on 2026-07-17.
- **Path B — routed network behind a BBMD.** Some or all devices are on other
  subnets, reachable only if the app registers with a BBMD as a foreign
  device. This path has still never run against a real BBMD.

**If you are not sure:** run Path A first. It is always safe, it needs no
configuration beyond the Source Interface, and its results answer the
question — devices you expected but cannot hear from your subnet are exactly
the ones that live behind a router (see Section 8). Then, and only then,
consider Path B.

---

## The one thing (both paths)

**Source Interface + Save.** In **Network Basics** (not the BACnet section),
Source Interface must be set to **your wired lab NIC**, not
`Auto (OS default route)` and not blank — and then you must press **Save
Configuration**. The form remembers nothing you did not Save, and a value that
*looks* selected in the UI is not saved until you press Save. On Auto/blank
the run fails with:

> No Source Interface selected for a live BACnet scan. Open the Configuration page, set
> Source Interface to your wired network adapter, and Save, then run the scan again — a
> real BACnet Who-Is must bind to a specific local network interface.

Everything else is per-path:

- **Path A:** leave the whole BACnet section alone. Foreign Device stays
  Disabled, the BBMD fields stay untouched (the seeded demo address
  `10.10.25.20` is fictional and harmless while Foreign Device is Disabled —
  nothing reads it).
- **Path B:** the seeded defaults are a trap — the entire foreign-device path
  is inert until you set the Section 1B fields by hand and press Save. Saved
  settings are **not** updated when a new build changes defaults; anything you
  already saved stays exactly as you saved it.

---

## 1A. Pre-flight, Path A — flat network (do this first)

1. **Network Basics → Source Interface** = your wired lab NIC. **Save
   Configuration.**
2. **Do not touch** Foreign Device, BBMD Address, BBMD UDP Port, TTL, or the
   informational **BBMD** toggle. They play no part on a flat network.

### How to confirm it took

Tick **Dry run** on the BACnet Discovery step and press **Run BACnet
Discovery**, then fetch the run record (Section 4). In
`result_summary.dry_run_plan.notes` you must see:

> `using local broadcast only`

**On this path that sentence is the CORRECT, healthy state** — it is not a
failed save and not a missing setting. (The previous version of the operator
instructions treated it as an error; that guidance was written for Path B and
misdirected a flat-network session on 2026-07-17. The requirement that
local-only scanning is first-class is now recorded in
`docs/protocol-conformance.md` §3.)

Also check `result_summary.dry_run_plan.transport`: `mode` is `broadcast`,
there is **one** lane (`broadcast`, `udp_port: 47808`), and its `bind_ip` is
your wired NIC's IP.

And `dry_run_plan.unicast_target_count` — how many devices from your imported
`bacnet_register` the app will probe directly. **If this is 0 or missing and
you imported a register, the import did not reach the engine.** Cheapest catch
of the day.

---

## 1B. Pre-flight, Path B — behind a BBMD

Open the app, go to the **Configuration** page, find the **BACnet Discovery**
section. Set these fields:

| Field | Set it to | Why |
|---|---|---|
| **Foreign Device** | **Enabled** | This is the **only** switch that turns on BBMD registration. Nothing else does. |
| **BBMD Address** | The lab BBMD's **real IP**, e.g. `10.20.30.4` | Seeded value `10.10.25.20` is demo data and does not exist. **Bare IP only** — no `:port`. |
| **BBMD UDP Port** | The BBMD's port (normally `47808`) | If it is blank or junk, the app quietly uses 47808. |
| **TTL** | `300` unless the site says otherwise | Registration lifetime in seconds. Junk here quietly becomes 300. |

One field that will mislead you if you let it: the **BBMD** toggle (seeded
`Disabled`) — **discovery never reads it.** It is informational. Its help text
says so: *"Discovery does not read this toggle — enable Foreign Device to
actually register with a BBMD."* Leave it alone.

Then:

1. Press **Validate Snapshot**. It checks the values without saving. **BBMD
   Address is only checked when Foreign Device is Enabled** — the error is
   labelled `BACnet BBMD Address`.
2. Press **Save Configuration**. Nothing takes effect until you do.

### How to confirm it took

Dry run (as in 1A), then in `result_summary.dry_run_plan.notes` you must see:

> `using foreign-device registration via BBMD 10.20.30.4:47808`

(with *your* BBMD address and port). If it instead says `using local broadcast
only`, the config did not take: Foreign Device is not Enabled, or the snapshot
was not saved.

**If you get an HTTP 400 when you start the run**, the message names the
problem:

> Foreign Device is Enabled but BBMD Address is empty. Set your BBMD's IP address on the
> Configuration page (BACnet -> BBMD Address) and Save, then run discovery again.

or

> Foreign Device is Enabled but BBMD Address '<what you typed>' is not a valid IP address.
> Fix it on the Configuration page (BACnet -> BBMD Address) and Save, then run discovery
> again.

This is deliberate: no run is created, so you never get a half-configured scan
reporting a clean empty result.

---

## 2. Firewall and admin pre-flight

A silently dropped inbound packet looks **exactly** like an empty network.
There is no message for it. This section is the only defence.

**The app binds local UDP ports on your Source Interface IP:**

| Port | Lane | Used when |
|---|---|---|
| **47808** | Local broadcast | Always (both paths) |
| **47809** | Foreign-device registration via the BBMD | **Path B only** (Foreign Device = Enabled) |

**47809 is not the port anyone expects.** It exists because a foreign-device
app suppresses its own local broadcast, so we run two separate BACnet stacks
and each needs its own port. It also keeps us out of the way of a BACnet
browser sitting on 47808.

Do this before the lab:

- Create a **Windows Firewall inbound allow rule** for the app's executable
  covering **UDP 47808** (and **UDP 47809** if Path B is possible). Or run one
  scan and accept the Defender prompt when it appears — but do that *before*
  anyone is watching, not at 09:00.
- Path B: ask whoever owns the network whether anything between your subnet
  and the BBMD's subnet filters UDP. The BBMD will send its registration
  acknowledgement and forwarded broadcasts **back to port 47809**. A firewall
  that only permits 47808 will let your registration out and drop every reply.
- Confirm you have **local admin** — you need it for the firewall rule and for
  `pktmon`.

---

## 3. Hygiene (10 minutes, before Gate 1)

1. **Close every other BACnet tool** — browsers, Yabe, vendor tools. All of
   them.
2. Confirm nothing holds 47808 or 47809:

   ```powershell
   netstat -ano -p UDP | findstr "47808 47809"
   ```

   Any line here is a problem. Note the PID in the last column and use
   `Get-Process -Id <PID>` to find out what it is.
3. Write down: your laptop's IP and mask; on Path B also the BBMD's IP and
   port, and whether you are on the BBMD's subnet or routed to it.
4. Start a packet capture (built into Windows 11, no install needed — run the
   prompt as Administrator):

   ```powershell
   pktmon filter remove
   pktmon filter add bacnet -p 47808
   pktmon filter add bacnetfd -p 47809
   pktmon start --capture --pkt-size 0
   ```

   At the end of the day: `pktmon stop`, then
   `pktmon etl2pcap PktMon.etl -o labday.pcap`. If you do not have admin, skip
   this — the run record diagnostics in Section 7 are designed to work without
   it.

---

## 4. How to read a run

Everything you need to diagnose a scan is in one place. Get the run ID from
the run monitor, then:

```powershell
# Read the App URL from the app's console window; the port is usually 8000.
$app = "http://127.0.0.1:8000"
$runId = "<paste the run id>"
Invoke-RestMethod -Uri "$app/api/v1/discovery/runs/$runId" | ConvertTo-Json -Depth 12
```

Save every one you care about:

```powershell
New-Item -ItemType Directory -Force "$home\Desktop\labday" | Out-Null
Invoke-RestMethod -Uri "$app/api/v1/discovery/runs/$runId" | ConvertTo-Json -Depth 12 |
  Out-File -Encoding utf8 "$home\Desktop\labday\run-$runId.json"
```

That record holds `parameters`, `result_summary` (including
`bacnet_diagnostics`, `lanes`, `expected_not_responding`), `issues`,
`error_message`, and `status`. No login needed — the portable app trusts
loopback.

**Reading the transport off the results page.** The badge under the results
heading names the transport the run actually used:

- `Live bacpypes3 scan — local broadcast only (no foreign-device registration configured)`
  — this subnet only. **Path A: this is the correct badge.** Path B: your
  settings did not reach the run — go back to §1B.
- `Live bacpypes3 scan — foreign-device registration via BBMD 10.0.0.5` —
  registered and scanned through that BBMD. **Path B's correct badge.**
- `Live bacpypes3 scan.` with nothing after it — a run recorded before the
  transport badge existed. Not evidence of anything; re-run it.

The authoritative answer is always `result_summary.bacnet_diagnostics.mode` in
the run record. The badge is a mirror of it, and the badge is the thing you
can read at a glance.

---

## 5. The verification sequence

**Start narrowest. Do not open with a full-lab scan.** Each gate is go/no-go:
if a gate fails, stop and work the fallback tree in Section 6. Do not "just
try the big one".

### Gate 1 — Control test with the third-party browser (5 min)

Open your own BACnet browser and confirm **one known device** answers it
(Path A: any device on your subnet; Path B: the known device behind the BBMD).

- **Success:** the device answers. The device, the network (and on Path B the
  BBMD) are healthy *today*. Any later failure is ours, and now you can prove
  it.
- **Failure:** stop. The lab itself is not ready. Nothing below will work and
  it will not be this app's fault.
- **Then close the browser** and re-run the `netstat` from Section 3. Confirm
  47808 is free.

### Gate 2 — Dry run (no packets)

Tick **Dry run**, press **Run BACnet Discovery**. Nothing goes on the wire.

- **Success:** `result_summary.dry_run_plan.notes` carries the transport
  sentence for your path (§1A: `using local broadcast only`; §1B:
  `using foreign-device registration via BBMD <your-bbmd>:47808`), and the
  note begins:

  > `Dry run: no Who-Is broadcast emitted. Would scan the device-instance range [0, 4194303] via the 'simulated' backend`

  (`'simulated'` is correct and expected here — a dry run builds no real
  stack.)

  Also check `result_summary.dry_run_plan.transport`:
  - **Path A:** `mode` = `broadcast`, one `broadcast` lane
    (`udp_port: 47808`), `bind_ip` = your wired NIC's IP.
  - **Path B:** `mode` = `foreign_device`, **two** lanes: one `broadcast`
    (`udp_port: 47808`) and one `foreign_device` (`udp_port: 47809`, plus
    `fd_bbmd_address` and `fd_ttl`), each lane's `bind_ip` = your wired NIC's
    IP.

  And `dry_run_plan.unicast_target_count` — see §1A; **0 with a register
  imported = the import never reached the engine.**

- **Failure:** a lane carries an `error` string instead of a plan, or the
  transport sentence is wrong for your path. Go back to §1A/§1B.
- **In the UI** a dry run shows "Dry run complete — preview only" with the
  detail "No packets were sent and no live results are expected. Run a real
  scan to populate results." That is normal. The plan itself is only in the
  run record.

**Gate: do not send a single live packet until this note says what your path
expects.**

### Gate 3 — One known device, narrow window

The **"Run BACnet Discovery"** button always scans the full instance range
`0–4194303`; there is no instance-range control in the UI. To narrow it to
your one known device you must post the run directly:

```powershell
$app = "http://127.0.0.1:8000"
$known = 1001   # <-- your one known device's BACnet instance
$body = @{
  project_id = "demo-project"
  site_id    = "demo-site"
  job_type   = "bacnet_discovery"
  parameters = @{
    authorized           = $true
    device_instance_low  = $known
    device_instance_high = $known
  }
} | ConvertTo-Json -Depth 5
$run = Invoke-RestMethod -Uri "$app/api/v1/discovery/bacnet/runs" -Method Post `
         -Body $body -ContentType "application/json"
$run.run_id
```

The run appears in the run monitor like any other. Everything from your saved
configuration — Source Interface, and on Path B the BBMD address, port, TTL —
is still applied; you are only narrowing the Who-Is window.

**Success looks like:** status `succeeded`, 1 device, with points, and in the
run record:

- **Both paths:** `result_summary.bacnet_diagnostics.bind` =
  `{"attempted": true, "ok": true, "ip": "<your NIC>", "port": 47808}`,
  `transport_verified` = `true`,
  `bacpypes3_version` = `"0.0.106"`.
- **Path A:** `bacnet_diagnostics.mode` = `"broadcast"` and
  `fd_registration` = `null` — both **correct** here.
- **Path B:** `mode` = `"foreign_device"`,
  `fd_registration.outcome` = `"registered"`,
  `result_summary.lanes.foreign_device.ran` = `true`.

**If it comes back empty (`succeeded`, `device_count: 0`):**

1. **Run it again.** Once. Both scans, then conclude.
2. Read `bacnet_diagnostics` and go to the table in Section 7.
3. Path B: the results page will carry the engine's own sentence under
   **"Discovery complete — no BACnet devices responded"**, e.g.:

   > Registered with the BBMD at 10.20.30.4:47808, but no devices answered the Who-Is
   > (instances 1001–1001) within 5s. Check the device-instance range, and ask the BBMD
   > administrator whether its broadcast distribution table covers the subnets these
   > devices are on.

**Capture before moving on:** the run record JSON (Section 4), and the pktmon
capture if you have it.

**Gate: one device, with points. Do not widen until you have it.**

### Gate 4 — Full instance range

Press **Run BACnet Discovery** in the UI (full range `0–4194303`), authorized,
not a dry run.

- **Success:** `device_count` is close to your register's row count for the
  subnets your path can reach. Compare by device instance, not by count alone.
- Capture the run record. Note which register rows are missing — Gate 5 is
  about them.

### Gate 5 — The directed sweep (the register-driven second path)

This lane runs automatically as part of Gate 4: after the broadcast lane(s),
the app sends a single directed Who-Is to each register address that **stayed
silent** — never to devices it already heard. It needs no BBMD cooperation at
all.

Read it in the run record:

- `result_summary.lanes.directed` → `probe_count`, `device_count`,
  `i_am_count`
- `result_summary.expected_not_responding` → one row per expected-but-silent
  device, each with `asset_id`, `asset_name`, `device_instance`, `address`,
  and `directed_probe_sent`

**That list is your honest punch list.** Read Section 8 before you treat any
of it as "device offline" — it is not that.

### Gate 6 — The deep scan (the whole lab)

This is the committed deliverable: **the device inventory**. Point reads are
best-effort — large devices can abort an object-list read, and every read is
throttled.

**Say this out loud before you start it:** *a full device inventory plus
partial point data is a win.* Set that expectation with the room now, not at
16:00.

### Gate 7 — Wrap up

Generate the discovery report. Record every run ID. `pktmon stop` and
`pktmon etl2pcap PktMon.etl -o labday.pcap`. Save the run-record JSON files.
Also save **Configuration → Download log bundle** — since v0.1.16 it contains
the run breadcrumbs and any error tracebacks, so an evening post-mortem can
happen from the artifacts alone, without you on a call.

---

## 6. When it goes wrong

### 6a. (Path B) The BBMD refuses or ignores the registration

The run **fails loudly and names the BBMD**. It does not fall back to a local
broadcast — that would report a clean scan of the wrong network.

**Refused** — the BBMD said no:

> The BBMD at 10.20.30.4:47808 refused foreign-device registration (BVLL result code 3).
> Ask the BBMD administrator to permit foreign-device registrations from this machine's IP
> address, and to check the BBMD's foreign-device table has a free entry.

**No answer** — the BBMD said nothing for 10 seconds:

> No response from the BBMD at 10.20.30.4:47808 — it did not acknowledge foreign-device
> registration within 10.002s. Check the BBMD address and UDP port on the Configuration
> page, and that UDP traffic is routed and permitted between this machine and the BBMD.

(The number is the real measured wait, so expect something just over 10 —
`10.002s`, `10.13s`. It is not a typo.)

**What to do, in order:**

1. **Re-run once.** Then stop re-running.
2. **Refused** → this is a decision the BBMD made. Go to the BBMD
   administrator and ask for two things: (a) permit foreign-device
   registration from your laptop's IP, and (b) confirm the foreign-device
   table has a free slot. **A locked or absent foreign-device table is
   common** on JACE/Niagara, Delta, ALC and similar gear, and it is a site
   policy decision. **This app cannot fix it and neither can you from the
   laptop.** Nothing you type in Configuration changes a BBMD's mind.
3. **No answer** → check, in this order: the BBMD IP and port are right (a
   typo looks identical to a firewall); UDP is routed between the subnets;
   **inbound UDP 47809 is not being dropped at your laptop or in between**
   (Section 2 — this is the most likely cause if the address is right).
4. **Either way, the lab is not blocked.** Set **Foreign Device = Disabled**,
   Save, and re-run — you have simply dropped back to Path A. The directed
   sweep against your imported register reaches every IP-routable device with
   no BBMD involved at all. You lose routed MS/TP devices behind BACnet
   routers — those only exist to us through the BBMD — and you lose nothing
   else. Record the FD problem as a site follow-up and carry on.

### 6b. UDP 47808 is contended (a BACnet browser is open)

The run fails with:

> UDP port 47808 on 10.20.30.50 is already in use by another program — usually another
> BACnet tool (for example a BACnet browser) still running on this machine. Close it and
> run the scan again.

**What to do:** `netstat -ano -p UDP | findstr 47808`, find the PID, close
that program, re-run. That is the whole fix.

Two notes:

- If you see the same message for **47809**, it is the foreign-device lane's
  port, not the browser's. Check `bacnet_diagnostics.fd_bind` — it is recorded
  separately from `bacnet_diagnostics.bind` for exactly this reason.
- If you get this message and you are **certain** nothing else is running:
  tell us. In builds before v0.1.12 the app leaked its own socket and the
  second scan of a session conflicted with itself. That is fixed, and if it
  has come back we need to know immediately.

A related failure to recognise: if the **interface** is wrong rather than the
port:

> Cannot bind UDP port 47808 on 10.20.30.50 for a live BACnet scan (error 10049). Check
> that Source Interface on the Configuration page matches a network adapter that is up on
> this machine.

(The error number is whatever Windows reported; the wording around it is
fixed.)

### 6c. A run fails, or the Run button reports an error

Since v0.1.16 a live run can no longer die with a bare "500 Internal Server
Error" while its run-monitor entry sits at "Running" forever — that failure
class (results failing to save after a scan that worked on the wire) is fixed,
and every run now ends with a real status and a plain-English
`error_message`. If a run does fail:

1. Read `error_message` in the run record (Section 4) — the engine's own
   sentence is the diagnosis.
2. **Configuration → Download log bundle** — since v0.1.16 it actually
   contains the traceback and the run's breadcrumbs. Send us that zip.
3. If you ever DO see a run stuck at "Running" with the app still up, that is
   a new bug — log bundle + run ID, immediately.

Also expected: on the **first start after upgrading**, any runs a previous
build left stuck at "Running" are marked failed with a note saying they were
interrupted by an application restart. That is cleanup, not new breakage.

### 6d. Run any scan TWICE before concluding failure

For all of the above. One dropped UDP frame is not a diagnosis, and "we ran it
twice" is the first question anyone will ask you.

---

## 7. Diagnostics triage table

Every live run stamps `result_summary.bacnet_diagnostics`, on success **and**
on every self-diagnosed failure. This is the bar we built to: **a failed scan
must be diagnosable from the run record alone.**

| Key | Values | What it tells you |
|---|---|---|
| `interface` | your Source Interface, e.g. `"10.20.30.50/24"` | What we bound, verbatim as configured. Wrong NIC = everything fails identically. |
| `udp_port` | `47808` | The broadcast lane's local port. |
| `bind.attempted` / `bind.ok` | `true`/`false` | `ok: false` = we never got the socket. The scan stopped there; the network was never touched. |
| `bind.reason` | `"udp_port_in_use"` | Another program has the port → Section 6b. Present only on failure. |
| `bind.reason` | `"interface_bind_failed"` | The IP is not on this host or the adapter is down → check Source Interface. |
| `mode` | `"broadcast"` | **Path A: correct.** Path B: Foreign Device was not Enabled — cross-subnet devices were never reachable → §1B. |
| `mode` | `"foreign_device"` | The Path B config took. |
| `fd_registration` | `null` | No foreign-device lane ran. With `mode: "broadcast"` that is expected (and on Path A, correct). |
| `fd_registration.outcome` | `"registered"` | **The BBMD accepted us.** An empty scan now is about the BBMD's broadcast distribution or the instance range — not about registration. |
| `fd_registration.outcome` | `"refused"` | The BBMD said no. `status` holds the BVLL result code it sent. **BBMD-side policy** → Section 6a. |
| `fd_registration.outcome` | `"timeout"` | Nothing came back within `waited_s`. Address / routing / **inbound UDP 47809** → Section 6a. |
| `fd_registration.outcome` | `"unknown"` | We could not read the registration state out of the BACnet library at all. Almost certainly the wrong `bacpypes3` version — check `bacpypes3_version` below. Tell us. |
| `fd_registration.fd_bbmd_address` | `"10.20.30.4:47808"` | The address we actually registered against, port resolved. Compare it to what you typed. |
| `fd_registration.waited_s` | seconds | How long we waited. Around 10 = a timeout. |
| `fd_bind` | same shape as `bind`, port `47809` | The foreign-device lane's own bind. A conflict here is **not** the browser on 47808. |
| `who_is.broadcast_sent` | count | How many broadcast Who-Is went out (1 local, +1 through the BBMD on Path B). |
| `who_is.unicast_targets` | count | Register rows in scope for the directed lane. **0 with a register imported = the import never reached the run.** |
| `who_is.unicast_sent` | count | Directed Who-Is actually sent (silent targets only). |
| `who_is.i_am_count` | count | Raw I-Am replies across all lanes, before dedupe. **`i_am_count: 0` with `bind.ok: true` (and on Path B `fd_registration.outcome: "registered"`) = we sent and nothing came back.** That is a network/firewall/BBMD-distribution question, not an app question. |
| `who_is.instance_low` / `instance_high` | numbers | The window we actually scanned. A device outside it cannot answer, by definition. |
| `who_is.timeout_s` | seconds | How long each Who-Is listened. |
| `bacpypes3_version` | `"0.0.106"` | **Anything else and this is not the build we tested.** First thing to check when CI was green and the lab is red. |
| `transport_verified` | `true` | The transport did what we said it would. **An empty scan is only a clean empty when this is `true`.** |

Alongside it: `result_summary.lanes` (per-lane `ran` / `device_count` /
`i_am_count`, and a `reason` when a lane did **not** run — e.g.
`"not_configured"`), and `result_summary.expected_not_responding`.

**Packet-level triage** (only if you have the pktmon capture):

| What the capture shows | Verdict |
|---|---|
| (Path B) No **Register-Foreign-Device** frame ever sent | **Our bug.** Send us the run record. |
| (Path B) Register-Foreign-Device sent, no **Result** back | Network, routing, or the BBMD is not listening. |
| (Path B) Result returned with a **NAK** code | **BBMD-side policy.** Section 6a. |
| (Path A) Who-Is broadcast sent, I-Am replies visible, app shows fewer devices | Send us the run record + capture — since v0.1.16 the save path is tested for exactly this, and a recurrence is a bug we want the same day. |

---

## 8. Expected, and NOT a bug

Please do not report these as failures, and please stop anyone in the room who
calls them failures:

- **On Path A, `local broadcast only` / `mode: "broadcast"` is the healthy
  state**, not a missing setting. A BBMD is optional per site.
- **An empty scan is a `succeeded` run, not a failure.** Finding nothing is a
  real result. The run will carry a sentence explaining what it did and why
  nothing answered — that is the engine telling you the truth, not the app
  breaking.
- **Amber "expected but did not answer" rows are inconclusive, not "device
  offline."** The issue text says so directly:

  > ... was expected from the register import but did not answer this scan — no answer to a
  > directed Who-Is sent to 10.20.31.7. This is INCONCLUSIVE, not proof the device is
  > offline: BACnet permits a device to answer a directed Who-Is with a broadcast this host
  > cannot hear from another subnet, and devices behind a BACnet router are only reachable
  > through a BBMD.

  Devices that are genuinely powered off land here too. So do healthy devices
  we simply cannot hear from where we are standing. **The list is a list of
  questions, not a list of faults.**
- **Routed MS/TP devices are invisible to the directed lane by design.** A
  unicast Who-Is to an IP cannot reach a device that lives on an MS/TP trunk
  behind a router. Only the BBMD lane sees those. If your register's "IP
  address" column holds supervisor/router addresses rather than each device's
  own address, expect amber rows there and expect the BBMD lane to be the one
  that finds them.
- **A device answering at a register address with a different instance**
  raises a mismatch issue and is recorded under the instance it **announced**,
  never the one we hoped for. You will also get an "expected but did not
  answer" row for the instance the register expected. **Both rows are
  intentional** — they are different facts, and you need both to work out
  whether the register or the controller is wrong.
- **Old runs flipping to "failed — interrupted by an application restart" on
  first start after upgrade** is the v0.1.16 cleanup sweep, not new breakage.
- **A point recorded with a read error** (e.g. an object type with no
  present-value, like a network-port) is one point's honest read failure, not
  a failed scan. The 2026-07-17 field session produced exactly this and the
  scan carried on by design.
- **Partial point data with a full device inventory is a WIN.** Object-list
  reads can abort on large devices. The inventory is the deliverable.

---

## 9. What is proven and what is not

Straight, because you are the one who will find out.

**Proven on real hardware (field session, 2026-07-17, flat network):** the
live bacpypes3 broadcast lane end to end — binding the configured NIC,
broadcasting Who-Is over the full instance range, collecting I-Am from
multiple real devices (two vendors), reading each device's object-list, and
walking per-object present-value reads, including the per-point read-error
path (an object with no present-value recorded as a read failure while the
scan continued). A Wireshark capture confirms every step on the wire.

**Fixed since that session but not yet re-proven on hardware:** saving those
results. The 2026-07-17 scans crashed while persisting (raw BACnet library
values in the saved-results path) — fixed in v0.1.16 with tests that replay
the exact failure, but Monday is the first live scan through the fixed save
path. If a scan completes on the wire and the app shows fewer devices than the
capture, that is the first thing to report (Section 7's Path A packet-triage
row).

**Still never executed against real gear:**

1. **Everything foreign-device/BBMD (Path B).** Whether the lab BBMD accepts
   foreign-device registrations at all is the single biggest unknown and it is
   not in our code. If the BBMD's foreign-device table is locked or absent —
   common, and a site policy decision — the foreign-device lane is dead on
   arrival and the directed register sweep is your only route to cross-subnet
   devices. We made the refusal loud and named; we cannot make it not happen.
2. **Whether the BBMD replies to our port 47809.** Using a non-default source
   port is legal under the BACnet spec, and the BBMD is supposed to record the
   port we registered from and forward there. Non-conformant gear that
   hard-codes replies to 47808 exists. If registration succeeds but nothing
   ever arrives, this is a live suspect — the fallback is to free 47808
   completely and tell us, because the port is currently fixed in code.
3. **The directed (unicast) sweep.** The 2026-07-17 session ran with no
   register imported, so the directed lane has never sent a live packet.
4. **Whether your register's IP addresses are each device's own BACnet/IP
   address**, or supervisor/router addresses fronting MS/TP trunks. If the
   latter, those devices read as silent on the directed lane even though they
   are perfectly alive.
5. **Whether cross-subnet devices answer a directed Who-Is with a unicast
   reply to us.** The spec permits them to answer with a broadcast on their
   own subnet instead — which we cannot hear. That is why directed silence is
   reported as inconclusive.
6. **The Windows error-code handling for the port-conflict message.** The
   mapping was written from documentation, never executed on Windows. If a
   BACnet browser is open and you get the generic "Cannot bind UDP port ..."
   message instead of the "already in use by another program" one, that is
   this — tell us, and treat it as a port conflict anyway.

The lanes exist precisely so that no single one of these blanks your day. If
the BBMD refuses you, you drop to Path A and the directed sweep still runs. If
the register is wrong, the broadcast lanes still run. If you get a device
inventory out of Monday, that is the deliverable, and how you got it is a
footnote.

---

## Quick reference

| | Path A — flat (no BBMD) | Path B — behind a BBMD |
|---|---|---|
| **Config gate** | Network Basics → **Source Interface** = wired lab NIC → **Save** | Same, PLUS BACnet Discovery → **Foreign Device = Enabled** + real **BBMD Address** → **Save** |
| **The trap** | A selected-looking Source Interface is not saved until you press Save | Saved settings never auto-update; seeded BBMD Address `10.10.25.20` is fictional |
| **Ignore** | The whole BACnet section, incl. the **BBMD** toggle | The **BBMD** toggle (discovery never reads it) |
| **Dry-run must say** | `using local broadcast only` (**correct here**) | `using foreign-device registration via BBMD <ip>:<port>` |
| **Diagnostics `mode`** | `broadcast` (correct) | `foreign_device` |
| **Firewall** | Inbound **UDP 47808** | Inbound **UDP 47808 and 47809** |
| **Order** | Browser control test → dry run → one device narrow → full range → sweep → whole lab | Same |
| **Always** | Run any scan **twice** before concluding failure | Same |
| **Triage** | `GET /api/v1/discovery/runs/{run_id}` → `result_summary.bacnet_diagnostics` | Same |
| **Empty scan** | `succeeded`. Not a failure. | Same |
| **Amber rows** | Inconclusive. Not "offline". | Same |
| **If a run fails** | Read `error_message`, then **Download log bundle** (it has the traceback since v0.1.16) | Same |
