# Re-importing an upstream scanner drop

How to fold a new scanner drop from upstream into this repo with minimal friction.
Target: an engineer follows this end to end in under half a day.

The upstream tree is **authoritative**. We wholesale-replace, we never hand-merge.
SCT integrates through a thin adapter and a per-app contract test; if the
contract stays green the drop is done, if it goes red the failure names the
seam that moved.

Repo root: `C:\Smart Commissioning App\smart-commissioning-app`
Vendor trees: `scanners/vendor/<app>/`. Three apps, `<app>` = one of:

| `<app>`               | sidecar   | port  | contract test suite                          |
|-----------------------|-----------|-------|----------------------------------------------|
| `network-ip-scanner`  | server.js | :3777 | `tests/test_ip_scanner_contract.py`          |
| `bacnet-scanner`      | server.js | :3778 | `tests/test_bacnet_scanner_contract.py`      |
| `mqtt-discovery`      | server.js | :3799 | `tests/test_mqtt_scanner_contract.py`        |

Run every step below once per app you are re-importing.

---

## Checklist

### 1. Receive and diff
- Drop the new tree somewhere outside the repo first (a scratch dir).
- Diff it against the current `scanners/vendor/<app>/` so you know what moved:
  ```
  git diff --no-index scanners/vendor/<app>/ <scratch>/<app>/
  ```
- Read the diff for structural changes (renamed files, new endpoints, changed
  output shape). Note anything that looks like a seam the adapter depends on
  (see the NEVER-COUPLE list) so you can predict which contract test moves.

### 2. SCRUB (do this BEFORE the tree touches the public repo)
This repo is **public**. No site names, real addresses, broker hosts, device
ids, MAC addresses, or personnel may land in any file, comment, or sample.

Grep the scratch tree (not the repo) first:
```
grep -rniE "site-name|street|road|avenue|[0-9]{1,3}(\.[0-9]{1,3}){3}|mqtt\.|broker|[0-9a-f]{2}(:[0-9a-f]{2}){5}" <scratch>/<app>/
```
Then eyeball, by hand, for anything the regex misses: real hostnames, staff
names, customer identifiers, licence keys.

Neutralize every hit to a generic placeholder, especially sample data in
template CSVs and READMEs:
- site names  -> `Example Site`
- addresses   -> `123 Example Road`
- device names -> generic (`AHU-01`, `Controller-01`, `Sensor-01`)
- IPs/subnets -> documentation ranges (`192.0.2.0/24`, `198.51.100.x`)
- broker hosts -> `broker.example.local`
- MACs        -> `00:00:5E:00:53:xx`

Scrub already applied to the vendored trees (match this bar on the next drop):
- `network-ip-scanner` — real site name -> `Example Site`, device names
  genericized in the template CSV.
- `bacnet-scanner` — register CSV + `README.txt` neutralized to `Example Site` /
  generic device names (`AHU-01`, `Controller-01`, `Sensor-01`); `selftest.js`
  real IP -> `192.0.2.37`.
- `mqtt-discovery` — register CSV + `README.txt` neutralized to `Example Site`
  and topic prefix `udmi/site/example` / generic device names; in
  `public/index.html` the broker placeholder `electracom-broker` ->
  `example-broker` and host -> `192.0.2.20`.

Only a scrubbed tree proceeds to step 3.

### 3. VENDOR wholesale replace
Never hand-merge the upstream code. Replace the whole tree:
```
rm -rf scanners/vendor/<app>
cp -r <scratch>/<app> scanners/vendor/<app>
```
Our integration lives entirely outside `vendor/` (in the adapter and the
contract test), so blowing the tree away is safe and is the point. If you find
yourself editing a file under `vendor/` to make the import work, stop: the fix
belongs in the adapter or the contract test, not in the upstream code.

`mqtt-discovery` vendors **source only**: its `node_modules/` and `dist/` are
gitignored (Node runs `server.js` directly; the portable build regenerates the
bundle). Copy the source tree, not a built drop with deps committed.

### 4. Contract test = the gate
Each app has its own contract suite (see the table above); together the three
suites are the re-import gate. Run the one for the app you dropped (CI uses
`unittest`, so that is the source of truth):
```
cd backend && python -m unittest tests.<suite>   # <suite> from the table, e.g. test_ip_scanner_contract
```
- **Green** = done. The adapter still talks to the sidecar and every persisted
  record is json-safe.
- **Red** = the test names the seam that moved. Fix the **adapter** to match the
  new tree, never the test to match a broken adapter, and never the vendor tree.

The contract test is the honesty and safety boundary. It asserts:
- The adapter records a real `failed` / `unauthorized` status when the sidecar
  is unreachable or authorization is missing. It never fabricates results.
- Every value that reaches a persisted record is json-safe (`json.dumps` round-
  trips). This is the v0.1.16 raw-value lesson: a raw bacpypes3 / non-JSON value
  in a persisted record fails the persist check and the run fails. Coerce at the
  adapter seam, not downstream.

### 5. Node smoke
Confirm the sidecar itself still starts and answers:
```
cd scanners/vendor/<app> && node server.js   # all three start via server.js; zero runtime deps, no npm install needed
```
Hit `/api/health` on the app's port (see table) and confirm it responds and reports a version (see standing
agreements). Ctrl-C when green.

### 6. Commit
```
chore(scanners): import <app> vX.Y.Z drop
```
One drop, one commit. If a seam moved and you touched the adapter, that adapter
change is a separate commit with its own one-line seam note (see agreements).

---

## Standing agreements with upstream

1. **Bump `/api/health` version every drop.** The health endpoint reports the
   sidecar version. It changes on every drop so the adapter and contract test
   can assert the running sidecar matches the imported tree.
2. **One-line note on any seam change.** If upstream moves anything the adapter
   reads (an endpoint, an output field, a status name), the drop carries a
   one-line note saying what moved. No silent reshaping of the contract.

---

## SCT must NEVER couple to

These belong to the upstream tree and change without notice. The adapter and SCT read
around them, never depend on their contents:

- **Port-list contents** — which ports a scan probes.
- **RAG thresholds** — the green/amber/red rules.
- **OUI / vendor tables** — MAC-to-vendor lookups.
- **UI wire formats** — the shapes the sidecar's own front-end consumes.
- **Fixed ports** — the sidecar's listen port or any hard-coded service port.
- **Version literal** — read `/api/health` at runtime; never hard-code the
  version string anywhere in SCT.

If the adapter or a test starts asserting on any of the above, that is the bug.
Couple only to the documented contract: the discovery endpoint, the record
shape the contract test pins, and the health/version signal.
