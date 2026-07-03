# Proposal: Source-NIC / network-interface selection for active scans

Status: Draft (design only — no code changed by this document)
Author: (fill in)
Date: 2026-07-03
Scope: `smart-commissioning-app` — core engines, backend API, worker, frontend Configuration page

## 1. Problem and goal

A commissioning engineer on a multi-NIC laptop (e.g. corporate Wi-Fi + a USB-C
Ethernet dongle on the OT VLAN, plus a VPN tap) has no way to tell the app which
interface active scans should egress from. Today every engine relies on the OS
default route, so an IP sweep or a BACnet Who-Is can leave via the wrong NIC
(the internet-facing one) and see nothing — or, worse, touch the wrong network.

Goal: let the operator pick a **source interface** (identified by its local IPv4
address) that all active-scan engines bind their sockets to, while keeping the
current OS-default-route behaviour as the default so nothing changes for
single-NIC users.

The three active-scan egress points that need a source binding:

| Engine | File | Current call | Needs |
|---|---|---|---|
| IP discovery | `core/smart_commissioning_core/engines/ip_scan.py:106` | `asyncio.open_connection(host, port)` | `local_addr=(source_ip, 0)` |
| MQTT transport (discovery + config publish) | `core/smart_commissioning_core/mqtt_transport.py:107` | `socket.create_connection((host, port), timeout)` | `source_address=(source_ip, 0)` |
| BACnet discovery | `core/smart_commissioning_core/engines/bacnet_discovery.py:320-374, 511` | `Bacpypes3Backend(local_address=...)` | already plumbed — nothing currently *feeds* `local_address` |

The BACnet path is the tell: `Bacpypes3Backend.__init__` already accepts
`local_address` (`bacnet_discovery.py:306`), `_ensure_app` already binds the
socket to it (`:347-374`), and `_select_backend` already reads
`parameters.get("local_address")` (`:511`). The only thing missing there is a
producer that puts `local_address` into the run parameters. This proposal adds
one producer that feeds **all three** engines a single chosen source IP.

Note a wrinkle bacpypes3 forces on us: BACnet `local_address` is not a bare IP,
it is `ip/prefixlen` (e.g. `192.168.1.10/24`) — see the docstring at
`bacnet_discovery.py:313-318` and the `_ensure_app` error at `:347-351`. The IP
and MQTT paths want a bare IP. So the chosen interface must carry **both** the
bare IPv4 and its prefix length. That is why enumeration (section 2) returns the
prefix, and why the config field stores `ip/prefix` (section 5).

## 2. Design overview

A single new configuration value, **`device."Source Interface"`**, holds the
operator's choice as a string:

- empty / absent / the literal `Auto (OS default route)` → **default behaviour**:
  engines bind nothing, OS picks the route. This is the backward-compatible path.
- an interface IPv4 with prefix, e.g. `192.168.1.10/24` → engines bind to
  `192.168.1.10`; BACnet gets the full `192.168.1.10/24`.

The value is chosen from a dropdown populated by a new read-only endpoint
`GET /api/v1/system/interfaces` that enumerates the host's usable NICs. A
free-text entry is also accepted (for the case where the app runs on a different
host than the browser, or an interface is momentarily down) — see section 3 UX.

At run-dispatch time, a new helper resolves the configured Source Interface into
a `source_ip` (+ `local_address` for BACnet) and injects it into the run
`parameters` **before** the run record is persisted, so the inline path and the
queued worker path both see it (they both read `run.parameters`). Then each
engine's socket-creation site binds to it.

```
Configuration (device."Source Interface" = "192.168.1.10/24")
        │
        ▼
engine_dispatch.resolve_source_interface(parameters, config)   ← NEW
        │  writes parameters["source_ip"]="192.168.1.10"
        │         parameters["local_address"]="192.168.1.10/24"
        ▼
run record persisted with those parameters
        ├── inline path (routes/discovery.py)  ─┐
        └── worker path (worker/app/tasks.py)  ─┤ both read run.parameters
                                                 ▼
   ip_scan._default_connect      → asyncio.open_connection(..., local_addr=(source_ip,0))
   mqtt_transport.MqttClient      → socket.create_connection(..., source_address=(source_ip,0))
   bacnet_discovery._select_backend → Bacpypes3Backend(local_address=parameters["local_address"])  (already wired)
```

## 3. UX — Configuration page NIC selector

### 3.1 Placement

Add a **`Source Interface`** field to the **Network Basics** (`device`) section.
That section is expanded by default (`ConfigurationPage.tsx:37-45`,
`defaultExpandedSections.device = true`) and is where IP/gateway identity already
lives, so the operator sees it before starting a run. It sits naturally beside
`IP Assignment`.

### 3.2 Control type — dropdown from enumeration with free-text fallback

Recommendation: a **hybrid** — a dropdown seeded from
`GET /api/v1/system/interfaces`, but one whose `<select>` renders any current
value even if it is not in the enumerated list. The existing `FieldControl`
`select` branch already does exactly this:

```tsx
// ConfigurationPage.tsx:816-823
<select ...>
  {!options.includes(value) && value !== "" && <option value={value}>{value}</option>}
  {options.map(...)}
</select>
```

That `!options.includes(value)` line means a stored value like `192.168.1.10/24`
still displays when the host that renders the page can't enumerate it (browser on
a different machine than the API, or the NIC is down). That preserves the value
instead of silently dropping it — the exact fallback we want.

Two options considered:

- **Pure dropdown (enumeration only)** — clean, no typos, but breaks when the API
  host's NIC list doesn't match what the operator expects (or the endpoint 500s).
  A blank selector then blocks the operator.
- **Free-text IP only** — always works, but invites typos and gives no
  discoverability of what NICs exist.

The hybrid gets discoverability *and* a fallback. Because the current
`FieldDefinition` type only models `kind`/`options` (`ConfigurationPage.tsx:19-22`)
and options are a static `string[]`, a plain `select` cannot express "editable
combobox". Two ways to ship the hybrid:

1. **Phase-1 minimal (recommended first slice):** render `Source Interface` as a
   `select` whose `options` are fetched at runtime and prepended with the literal
   `Auto (OS default route)`. Because of the `!options.includes(value)` escape
   hatch, a value typed via Import JSON or set on another host still shows. This
   needs a small change to let one field's options come from a query rather than
   the static `fieldDefinitions` map (see 3.4).
2. **Phase-2 nicety:** a true editable combobox (`<input list=...>` + `<datalist>`)
   so the operator can free-type while still getting suggestions. This is a new
   `FieldKind` (`"combobox"`) in `FieldControl`. Deferred — not needed for the
   first useful slice.

### 3.3 Default and label

- Default value: the field is **absent** from `DEFAULT_CONFIGURATION` (section 5),
  which the resolver treats as `Auto`. The dropdown shows `Auto (OS default route)`
  as the first option and as the effective selection when empty.
- Label copy / tooltip (add to `FIELD_TOOLTIPS`, `ConfigurationPage.tsx:661-712`):
  > `Source Interface`: "Which local network interface active scans send from.
  > Leave on Auto to use the OS default route; pick a NIC on a multi-homed laptop
  > to force IP/BACnet/MQTT scans out the right adapter."

### 3.4 Frontend wiring (files / anchors)

- `frontend/src/features/workflow/ConfigurationPage.tsx`
  - Add a `useQuery` for interfaces (queryKey `["system-interfaces"]`) calling a
    new `getSystemInterfaces()` in `api/client.ts`.
  - Build the option list: `["Auto (OS default route)", ...data.map(i => i.cidr)]`
    where `cidr` is `"192.168.1.10/24"`.
  - Because `fieldDefinitions` (`:141-171`) is a static const, add a small
    override at render so the `device."Source Interface"` field's `options` come
    from the query result. Concretely, where `FieldControl` reads
    `options={fieldDefinitions[section]?.[field]?.options}` (`:618`), special-case
    this one field to pass the fetched list and `kind="select"`.
  - Handle the query loading/error state by falling back to just
    `["Auto (OS default route)"]` so the page never blocks on enumeration.
- `frontend/src/api/client.ts`
  - Add `getSystemInterfaces(): Promise<SystemInterface[]>` hitting
    `GET /api/v1/system/interfaces`, plus a `SystemInterface` type
    `{ name: string; ipv4: string; prefix_length: number; cidr: string; is_up: boolean }`.
- Frontend tests: extend the existing ConfigurationPage test to assert the
  Source Interface select renders `Auto (OS default route)` and enumerated
  options, and that a stored non-enumerated value still displays (covers the
  `!options.includes` fallback).

## 4. NIC enumeration on Windows

The app's target host here is Windows 11 (see env / MEMORY), and this is
explicitly a Windows-first feature. Three ways to list interfaces:

### 4.1 Options

**A. stdlib only (`socket`) — no clean Windows API.**
`socket.if_nameindex()` exists but on Windows returns interface *indexes*, not
usable IPv4s or up/down state. `socket.gethostbyname_ex(socket.gethostname())`
returns *some* local IPv4s but drops interface names, prefix lengths, and
up/down status, and misdirects on hosts with odd DNS. There is no stdlib call
that gives `(name, ipv4, prefix, up/down)` on Windows. Verdict: insufficient for
a trustworthy dropdown — we'd be guessing prefixes (bad, because BACnet needs the
real prefix, section 1).

**B. `psutil` — clean, but a NEW dependency.**
`psutil.net_if_addrs()` returns per-interface address lists including
`family == AF_INET`, `address`, and `netmask` (convertible to a prefix length
via `ipaddress.IPv4Network("0.0.0.0/" + netmask).prefixlen`).
`psutil.net_if_stats()` returns per-interface `isup` and `speed`. Together they
give exactly `(name, ipv4, prefix_length, is_up)` cross-platform, including
Windows, with no subprocess and no parsing of localized output. Verdict:
cleanest by far; cost is one new runtime dependency in `core`/`backend`.

**C. WMI / `ipconfig` parsing — stdlib deps only, but brittle.**
Either shell out to `ipconfig /all` and parse, or query WMI
(`Win32_NetworkAdapterConfiguration`) via `pywin32`/`comtypes`. `ipconfig`
parsing is locale-sensitive (field labels are localized on non-English Windows)
and fragile; WMI needs a Windows-only dependency anyway (so it isn't really
"stdlib") and is heavier than psutil. Verdict: avoid unless a no-new-PyPI-dep
policy forces it; if forced, prefer a narrow WMI query over `ipconfig` text
parsing.

### 4.2 Recommendation

**Use `psutil`** (option B), isolated behind one function so the dependency is
swappable. Rationale: it is the only option that returns the real prefix length
(which BACnet requires) and up/down status without locale-fragile parsing, and
it is a well-maintained, widely-vendored library. Implement the enumerator so a
missing `psutil` import degrades gracefully to `Auto`-only rather than 500-ing
the endpoint (import guarded, like `bacpypes3` is at `bacnet_discovery.py:334-345`).

> **OPEN DECISION (dependency tradeoff):** adding `psutil` to `core`/`backend`
> conflicts with the repo convention "stdlib before deps" (`CLAUDE.md` →
> Conventions). It must be a deliberate, `ponytail:`-commented choice. If the
> team rejects a new dependency, fall back to option C behind the *same*
> enumerator interface (a guarded Windows-only WMI query), accepting that on
> non-Windows / non-English hosts enumeration may return an empty list and the UI
> falls back to free-text + `Auto`. Either way the enumerator's return shape and
> the endpoint contract (section 3.4 / section 5) stay identical, so this
> decision is swappable without touching callers, the endpoint schema, or the UI.

### 4.3 Enumerator sketch (design, not final code)

New module `backend/app/services/interface_service.py` (or
`core/.../net_interfaces.py` if BACnet/worker also wants it directly):

- `list_usable_interfaces() -> list[InterfaceInfo]` where `InterfaceInfo` carries
  `name, ipv4, prefix_length, cidr, is_up`.
- Filter to `AF_INET` addresses; **exclude** loopback (`127.0.0.0/8`) and
  link-local APIPA (`169.254.0.0/16`) so the dropdown only offers real egress
  NICs. (Keep them out of the list but still *accept* a manually-entered value —
  validation, section 7, only blocks obviously-broken input.)
- Sort `is_up` first, then by name, so the likely-correct NIC is near the top.

## 5. Backend endpoint `GET /api/v1/system/interfaces`

### 5.1 New route module

`backend/app/api/routes/system.py`:

```python
router = APIRouter()
require_viewer = require_role(Role.VIEWER)   # pattern from configuration.py:19

@router.get("/interfaces", response_model=list[SystemInterface],
            dependencies=[Depends(require_viewer)])
def list_interfaces() -> list[SystemInterface]:
    return interface_service.list_usable_interfaces()
```

Register it in `backend/app/api/router.py` on `protected_router` (so it inherits
`require_auth`), mirroring the existing includes at `router.py:43-72`:

```python
protected_router.include_router(system.router, prefix="/system", tags=["system"])
```

and add `system` to the `from app.api.routes import (...)` tuple (`router.py:4-17`).

### 5.2 Response schema (new, in `backend/app/schemas/`)

```python
class SystemInterface(BaseModel):
    name: str            # OS adapter name, e.g. "Ethernet 3"
    ipv4: str            # "192.168.1.10"
    prefix_length: int   # 24
    cidr: str            # "192.168.1.10/24"  (what the dropdown stores)
    is_up: bool
```

### 5.3 Auth / RBAC / information-leak notes

- **Auth:** mounted under `protected_router`, so `require_auth` applies (no
  anonymous access). This is not health-probe data.
- **RBAC:** `require_role(Role.VIEWER)`. Rationale: it feeds a Configuration
  field a viewer can already see, and it is read-only. It does **not** need to be
  engineer-gated (choosing the value and saving config already is, via
  `configuration.router` PUT at `configuration.py:31`).
- **Do not leak beyond need:** return **only** `name / ipv4 / prefix_length /
  cidr / is_up`. Deliberately omit MAC addresses, gateway, DNS, adapter
  descriptions/driver strings, and any non-IPv4 addressing — none are needed to
  pick an egress NIC and each widens the host-fingerprint surface exposed over
  the API. Exclude loopback and APIPA from the list (section 4.3).

## 6. Per-engine source binding — exact edits

### 6.1 IP discovery — `local_addr`

`core/smart_commissioning_core/engines/ip_scan.py`

`_default_connect` (`:94-118`) is the only production socket site, but it is a
module-level function with no access to run parameters. Thread `source_ip`
through the injectable probe, which is already the extensibility seam (`ConnectProbe`,
`:91`; injected via `process_ip_discovery_run(connect=...)` at `:331/368`).

Concretely:

- Add an optional `source_ip: str | None` bound into the probe. Cleanest without
  changing the `ConnectProbe` signature: build the default probe with a closure
  in `process_ip_discovery_run` that reads `parameters.get("source_ip")` and
  passes `local_addr`:

  ```python
  # near ip_scan.py:368, where `probe = connect or _default_connect`
  source_ip = (parameters or {}).get("source_ip") or None
  probe = connect or _make_default_connect(source_ip)
  ```

  where `_make_default_connect(source_ip)` returns a coroutine identical to
  today's `_default_connect` (`:94-118`) except the connect line becomes:

  ```python
  local_addr = (source_ip, 0) if source_ip else None
  connect = asyncio.open_connection(host, port, local_addr=local_addr)
  ```

  `asyncio.open_connection` accepts `local_addr=(host, port)`; port `0` = OS
  picks the ephemeral source port. `local_addr=None` is exactly today's
  behaviour, so `Auto` is a no-op.
- Error handling: an invalid/down `source_ip` makes `open_connection` raise
  `OSError` (e.g. `EADDRNOTAVAIL`). Today's `except (OSError, TimeoutError,
  ValueError)` at `:109` would swallow that as "port closed" for **every** host —
  a silent all-negative sweep, the worst failure mode. Fix: **pre-validate** the
  bind once, before the sweep, in `_run_ip_discovery` (`:379-386`, right after
  `require_scan_authorization`): attempt a throwaway `socket.socket()` +
  `bind((source_ip, 0))`; on `OSError`, raise `ValueError("source interface
  <ip> is not available on this host")` so `run_engine` records an honest
  terminal failure (matching the module's "engines never fake success" honesty
  rule, `CLAUDE.md`) instead of a bogus empty result.

### 6.2 MQTT transport — `source_address`

`core/smart_commissioning_core/mqtt_transport.py`

- Add `source_address: tuple[str, int] | None = None` to `MqttConnectionSettings`
  (`:65-77`) — a frozen dataclass, so add a field with a default (backward
  compatible; every existing constructor call keeps working).
- In `MqttClient.__enter__` (`:106-116`), the socket is created at `:107`:

  ```python
  raw_socket = self.socket_factory((self.settings.host, self.settings.port), self.settings.timeout_seconds)
  ```

  `self.socket_factory` defaults to `socket.create_connection` (`:100`), which
  accepts `source_address=(host, port)`. But the factory signature is only
  `(address, timeout)` (`:97`), so pass `source_address` via a bound default
  rather than a 3rd positional arg. Cleanest: when `settings.source_address` is
  set and no custom factory was injected, wrap `create_connection`:

  ```python
  if socket_factory is None and settings.source_address is not None:
      src = settings.source_address
      self.socket_factory = lambda addr, timeout: socket.create_connection(addr, timeout, source_address=src)
  else:
      self.socket_factory = socket_factory or socket.create_connection
  ```

  Injected `socket_factory`s (the tests at `test_mqtt_transport.py:41/53`) are
  untouched.
- Producer: `build_mqtt_connection_settings` (`mqtt_settings.py:40-61`) is where
  settings are assembled from run parameters + config. Add:

  ```python
  source_ip = _string(parameters.get("source_ip"))
  source_address = (source_ip, 0) if source_ip else None
  ```

  and pass `source_address=source_address` into the `MqttConnectionSettings(...)`
  call (`:49-61`).
- Error handling: `create_connection` with a bad `source_address` raises `OSError`
  on connect. The MQTT engine already maps connect failures to an honest
  `broker_unreachable`-family status (`_broker_error_status`,
  `mqtt_settings.py:80-89`), so a bad bind won't fake success — but its label
  would be misleading. Optional nicety: detect `EADDRNOTAVAIL` and surface a
  `source_interface_unavailable` status. Not required for the first slice.

### 6.3 BACnet discovery — feed the existing `local_address`

`core/smart_commissioning_core/engines/bacnet_discovery.py`

Nothing changes in this file — the plumbing already exists:

- `_select_backend` reads `parameters.get("local_address")` (`:511`) and passes
  it to `Bacpypes3Backend(local_address=...)`.
- `Bacpypes3Backend.__init__` stores it (`:320`); `_ensure_app` binds the
  network-port to it (`:362-374`) and raises a clear `RuntimeError` when it's
  missing while the real backend is selected (`:347-351`).

The only requirement is that the dispatch layer (section 7) puts the
`ip/prefix` form into `parameters["local_address"]`. The simulated backend
(default) ignores it, so `Auto` + simulated is unaffected.

- Error handling: if the operator selects the real `bacpypes3` backend with
  `Auto` (no `local_address`), `_ensure_app` already raises the actionable
  `RuntimeError` (`:347-351`). We should surface that earlier as a validation
  error at run creation (section 7): "BACnet real backend requires a Source
  Interface (Auto is not supported for BACnet/IP binding)."

## 7. Dispatch wiring (routes + worker)

The single injection point so inline and queued runs behave identically.

### 7.1 New resolver in `engine_dispatch.py`

`backend/app/services/engine_dispatch.py` (currently ends at line 198). Add:

```python
def resolve_source_interface(parameters: dict[str, Any], source_interface: str | None) -> None:
    """Inject source_ip (+ local_address for BACnet) into run parameters, in place.

    `source_interface` is the configured device."Source Interface" value:
      - falsy / "Auto (OS default route)"  -> no-op (OS default route).
      - "192.168.1.10/24" or "192.168.1.10" -> parameters["source_ip"]="192.168.1.10"
                                               parameters["local_address"]="192.168.1.10/24"
    An operator-supplied parameters["source_ip"]/["local_address"] wins (setdefault).
    Raises ValueError on a malformed value so the route returns a clean 400.
    """
```

Behaviour:
- Treat empty / `"Auto (OS default route)"` (case-insensitively) as no-op.
- Parse `ip[/prefix]` with `ipaddress`: `ipaddress.ip_interface("192.168.1.10/24")`
  → `.ip` (bare) and `.with_prefixlen`. A bare IP defaults BACnet to `/32`
  (bacpypes3 needs *a* prefix; note this in the field help — a bare IP is fine
  for IP/MQTT but the operator should give the real subnet for BACnet).
- `parameters.setdefault("source_ip", str(interface.ip))` and
  `parameters.setdefault("local_address", interface.with_prefixlen)` so an
  explicit run-level override is never clobbered.
- Raise `ValueError` on unparseable input (route maps to `HTTPException(400)`,
  matching `configuration.py:60-61`).

### 7.2 Route inline path

`backend/app/api/routes/discovery.py` — each `create_*_run` builds `parameters`
before persisting the run:

- IP (`:187-217`): after `_resolve_expected_ports(...)` (`:197`) and before
  `_create_run(...)` (`:198`), call
  `resolve_source_interface(parameters, _configured_source_interface(request.project_id, request.site_id))`.
- BACnet (`:220-244`): `parameters = dict(run.parameters)` is built at `:223`;
  inject right after. (Note BACnet builds `run` first then copies params — either
  inject into `parameters` before the `run_inline` closure reads it, or restructure
  to resolve params before `_create_run` like the IP route, so the **persisted**
  `run.parameters` also carries it for the worker path. Prefer the latter for
  consistency with IP/MQTT.)
- MQTT (`:247-280`): inject into `parameters` after the subscribe-defaults block
  (`:253-256`), before `_create_run` (`:257`).

Add a small helper next to the other config-reading helpers (`discovery.py`
already imports `config_service`, used at `:253`):

```python
def _configured_source_interface(project_id: str, site_id: str) -> str | None:
    values = config_service.load(project_id, site_id).device.values
    return str(values.get("Source Interface") or "").strip() or None
```

Because injection happens **before** `_create_run`, the resolved `source_ip` /
`local_address` are persisted into `run.parameters`, so the worker (which reads
`run.parameters`, per the comment at `discovery.py:190-192`) gets them for free.

### 7.3 Worker path

`worker/app/tasks.py` actors (`discover_ip_range` `:134`, `discover_bacnet`
`:150`, `discover_mqtt` `:171`, and the MQTT config-publish `:207`) receive
`parameters: dict` already containing `source_ip` / `local_address` from the
persisted run record — **no per-actor change needed**, provided section 7.2
injects before persist. This is the key to "inline and queued behave
identically": one resolver, run once at creation, persisted once.

> Belt-and-braces option: also call `resolve_source_interface` inside the worker
> if the worker has config access, so a run enqueued by an older API build still
> gets bound. Only worthwhile if API/worker version skew is a real concern;
> otherwise it duplicates logic. Flagged as a minor open decision.

### 7.4 Config schema / defaults

- `backend/app/schemas/configuration.py`: no structural change needed —
  `ConfigurationSection.values` is a free `dict[str, str]` (`:5`), so
  `"Source Interface"` is just a new key. (If we want it to appear even on a
  fresh install, seed it in `DEFAULT_CONFIGURATION`.)
- `backend/app/services/configuration_service.py`: add
  `"Source Interface": "Auto (OS default route)"` to `DEFAULT_CONFIGURATION.device.values`
  (`:28-39`). Because `_merge_with_defaults` (`:399-407`) unions defaults under
  loaded values, existing saved snapshots gain the key on next load without a
  migration. Add a validation rule in `ConfigurationService.validate` (near the
  device-field checks at `:312-315`): accept empty / `Auto...` / a parseable
  `ip[/prefix]`; otherwise append `"Source Interface must be 'Auto (OS default
  route)' or a valid interface IP (optionally with prefix, e.g. 192.168.1.10/24)."`

## 8. Validation, tests, and on-real-hardware verification

### 8.1 Validation (input hardening)

- Config-time: `ConfigurationService.validate` rejects a malformed Source
  Interface (section 7.4) → surfaced by the existing PUT `/configuration`
  400 path (`configuration.py:37-39`) and the Validate Snapshot button.
- Run-time: `resolve_source_interface` raises `ValueError` on a bad value → 400
  at run creation.
- Bind pre-check: `ip_scan` pre-validates the bind before the sweep (section 6.1)
  so a down NIC fails the run honestly rather than returning an empty scan.

### 8.2 Tests (stdlib `unittest`, to match CI — `CLAUDE.md` → Tests)

Core (`core/tests/`, alphabetical order preserved):
- `test_ip_scan.py`: add a case asserting that when `parameters["source_ip"]` is
  set, the default probe passes `local_addr=(source_ip, 0)` to
  `open_connection`. Inject a fake `connect`/monkeypatch `open_connection` to
  capture kwargs (the module already injects `connect` for socket-free tests —
  `ip_scan.py:331/368`). Add a case asserting an unavailable `source_ip`
  produces a terminal failure, not an empty success.
- `test_mqtt_transport.py`: assert `MqttConnectionSettings(source_address=...)`
  causes `MqttClient.__enter__` to create the socket with that `source_address`.
  The tests already inject `socket_factory` (`:41/53`); assert the wrapper passes
  `source_address` when no factory is injected (capture via a fake
  `create_connection`).
- New `test_engine_dispatch_source_interface.py` (backend `tests/`): unit-test
  `resolve_source_interface` — Auto → no-op; `1.2.3.4/24` → both keys set;
  bare IP → `/32` local_address; malformed → `ValueError`; existing
  `source_ip`/`local_address` not clobbered.

Backend (`backend/tests/`):
- `test_system_interfaces_api.py`: mock the enumerator (so tests don't depend on
  the CI host's real NICs) and assert `GET /api/v1/system/interfaces` returns the
  mocked list, requires auth (401 without key), is viewer-allowed, and the
  response contains **only** the five allowed fields (no MAC/gateway/DNS leak —
  guards section 5.3).
- Extend a discovery route test to assert a configured `Source Interface`
  results in `source_ip` / `local_address` landing in the persisted
  `run.parameters` (covers the inline→worker hand-off, section 7.3).

Frontend (`frontend`, `npm test`):
- Extend the ConfigurationPage test (mock `getSystemInterfaces`) to assert the
  Source Interface select shows `Auto (OS default route)` + enumerated options,
  and that a stored non-enumerated value still renders.

### 8.3 What CANNOT be tested here (must be verified on a real multi-NIC box)

Mirror the honesty pattern the engines already use ("REQUIRES ON-SITE
VALIDATION", `ip_scan.py:28-36`, `bacnet_discovery.py:15-27`). None of the
following can be exercised in this environment:

- That binding `local_addr`/`source_address` actually forces egress out the
  chosen physical NIC (needs ≥2 real NICs on different L2 segments).
- That `psutil` enumeration returns the expected adapters/prefixes/up-down on the
  operator's actual Windows laptop (adapter naming, VPN taps, USB dongles).
- BACnet `local_address` binding against a real controller via bacpypes3 (already
  on-site-untested, `bacnet_discovery.py:22-27`).

Manual verification checklist for a real box (document in the PR):
1. `GET /api/v1/system/interfaces` lists the Ethernet/OT NIC with correct
   `cidr`/`is_up`; loopback/APIPA excluded.
2. With `Source Interface = <OT NIC>/24`, run an IP sweep against a host reachable
   **only** via the OT NIC and confirm it is found; then set `Source Interface`
   to the Wi-Fi NIC and confirm the same host is **not** found (proves binding
   changes egress).
3. Confirm `Auto` reproduces today's behaviour on a single-NIC machine (no
   regression).
4. Down the chosen NIC and confirm the run fails with the honest
   "source interface not available" status, not a silent empty result.
5. Verify identical results whether the run goes inline (worker down) or queued
   (worker up).

## 9. Phased rollout

**Phase 0 — plumbing, no UI (smallest useful slice).**
- `resolve_source_interface` in `engine_dispatch.py` + wire into the three
  discovery routes before persist (section 7.2).
- `ip_scan` `local_addr` + bind pre-check (6.1); `mqtt_transport` `source_address`
  + `mqtt_settings` producer (6.2). BACnet needs no code (6.3).
- Accept `source_ip`/`local_address` as run `parameters` (already possible) — an
  operator/API caller can set it manually. Ship with core + dispatch tests.
- Value: an engineer can already force the NIC via a saved config key or a run
  parameter, and inline+worker are consistent — the hard part is done and tested
  without any UI or new dependency.

**Phase 1 — configuration field + validation.**
- `DEFAULT_CONFIGURATION` key + `validate` rule (7.4). Config-driven selection
  works end to end via a free-text field; still no enumeration dependency.

**Phase 2 — enumeration endpoint + dropdown.**
- `interface_service` (psutil, guarded) + `GET /system/interfaces` + schema +
  router registration (sections 4-5). Frontend dropdown with the
  `!options.includes` fallback (section 3). This is where the `psutil` open
  decision is resolved.

**Phase 3 — niceties (optional).**
- True editable combobox (`<datalist>`); `EADDRNOTAVAIL`-specific MQTT status;
  worker-side belt-and-braces re-resolve.

## 10. Effort estimate (rough)

| Phase | Work | Est. |
|---|---|---|
| 0 | Resolver + 3 route hookups + ip_scan/mqtt binding + core/dispatch tests | ~1.0–1.5 days |
| 1 | Config default + validation + tests | ~0.5 day |
| 2 | Enumerator (psutil) + endpoint + schema + router + frontend dropdown + tests | ~1.5–2.0 days |
| 3 | Niceties | ~0.5–1.0 day (optional) |

Total for a shippable feature (Phases 0-2): **~3–4 days**, plus on-site
validation time on a real multi-NIC box (section 8.3), which is gated on
hardware availability rather than engineering effort.

## 11. Open decisions (summary)

1. **`psutil` dependency** vs. WMI/ipconfig fallback vs. stdlib-only (accepting a
   degraded, prefix-guessing enumeration). Recommended: `psutil`, guarded, with a
   `ponytail:` comment naming the exception to "stdlib before deps". The
   enumerator interface is designed to make this swappable. (Section 4.2.)
2. **BACnet + `Auto`**: real bacpypes3 backend cannot bind without a
   `local_address`. Decide whether to hard-require a Source Interface when
   `bacnet_backend=bacpypes3` (recommended: validation error at run creation) vs.
   letting `_ensure_app` raise later. (Section 6.3.)
3. **Bare-IP → `/32` for BACnet**: acceptable default, or require an explicit
   prefix for BACnet? Recommended: default `/32` but document that the real
   subnet prefix should be given for BACnet. (Section 7.1.)
4. **Worker re-resolve (belt-and-braces)**: only if API/worker version skew is a
   real risk. Recommended: skip initially. (Section 7.3.)
