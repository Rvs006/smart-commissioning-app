# Field Quick-Start (print this)

One page for an engineer standing at a panel. Portable-exe path. No install, no
API key, no admin rights. Full setup lives in the app under **Learning →
Installation & Setup**; this is the short version.

## Before you leave the office

- [ ] Windows laptop with an **Intel/AMD (x64)** CPU — not a Snapdragon/ARM one.
- [ ] `Smart_Commissioning_App_Windows_Portable.zip` downloaded from the repo's
      GitHub **Releases** page. Check the asset date is current.
- [ ] A wired connection to the target network: built-in Ethernet, or a
      **USB-C-to-Ethernet adapter** if the laptop has no RJ45 port.
- [ ] Locked-down laptop (ThreatLocker or similar)? Get IT to approve the exe by
      SHA-256 hash BEFORE you leave — the hash is pinned in the release notes,
      or compute it yourself: `Get-FileHash .\SmartCommissioningApp.exe`
      in PowerShell prints it. Every release is a new file with a new hash, so
      re-approval is per release.

## Get it running (about 2 minutes)

1. **Extract the zip** — right-click → *Extract All*. Do not run it from inside
   the zip preview.
2. Double-click **`SmartCommissioningApp.exe`**. A console window opens (leave it
   open — it is the app) and your browser opens the tool.
3. If Windows SmartScreen warns "unknown publisher": *More info* → *Run anyway*.
4. The header shows **"Signed in as local admin"** — that is correct. There is no
   API key to set on this build.

## Put yourself on the network (Windows owns the IP)

Set your IP the way you always do — in **Windows → Network settings**, static or
DHCP, on the VLAN you need. The app never changes adapter settings; it only reads
them and chooses which adapter to scan from.

1. Plug into the switch. Confirm Windows shows the connection up.
2. In the app: **Configuration → Source Interface**.
   - Pick the adapter you are scanning from (wired defaults first; Wi-Fi is
     tagged "not recommended"; virtual adapters sit at the bottom — on a
     Hyper-V host the "vEthernet" entry can be the machine's real network
     adapter, so pick it if it carries the site IP).
   - The read-only panel below shows that adapter's current IP / subnet /
     gateway / DNS — check it matches what you set in Windows. If it does, the
     tool is reading the right NIC.
3. Save the configuration.

## Run a scan (dry-run first, always)

1. Open the module you need — **IP Scanner**, **BACnet**, or **MQTT**.
2. Run a **dry run** first: it previews the plan and touches nothing on the
   network. Confirm the target range/interface looks right.
3. Tick **scan authorization**, then run the real scan. (Real scans refuse to run
   unauthorized — that is deliberate.)
4. **First real scan:** if Windows Firewall pops up, click **Allow**.
5. Review results, add comments, **export CSV/Excel**, send it back.

## Look back at what you ran

**Operate → Run History** lists every run this laptop has done, with the date and
time each started/finished, its type, status, and how long it took. Sort or
filter it, and hit **Export CSV** for the whole list — no digging through files.

## Pete MQTT/UDMI field check (v0.1.1 or later)

1. Download the zip from the repository's [latest GitHub release](https://github.com/Rvs006/smart-commissioning-app/releases/latest), extract it, and start `SmartCommissioningApp.exe`.
2. Import the three-asset MQTT register with the scoped wildcard filters (for example, `MNVRHS/EM-1002001/#`). In **Configuration**, set the broker, port, TLS, and credentials; save.
3. Run **MQTT Discovery** and confirm it displays the broker's concrete topics and payloads.
4. Run **UDMI Workbench** against that same broker with **Run time = 120 seconds**. It should wait for state, metadata, and pointset evidence for each imported asset, then show expected-versus-observed results.
5. If it fails, copy/export the run's `subscribed_topics`, `captured_topics`, and `broker_status_detail`, plus the matching broker-log interval. Do not include broker credentials in the export or message.

This is a field test: a successful portable smoke test does not prove the MSI broker or devices until this run completes on site.

## Upgrading to a new release (settings carry over)

Your settings, certificates, and run history live in
`%LOCALAPPDATA%\SmartCommissioning` — **not** in the release folder — so
extracting a new release and running its exe keeps everything: broker
credentials, uploaded certs, the chosen Source Interface, run history. The
first launch of a new version also migrates state forward automatically if it
finds an older release's `runtime\` folder next to the exe. Moving to a
**different laptop** (or a different Windows user on the same machine) needs a
copy: close the app, copy the whole `%LOCALAPPDATA%\SmartCommissioning` folder
to the same path on the new machine, then start the app there — the folder
carries the settings, certificates, and their encryption key, so treat the
copy as sensitive.

## If something looks wrong

- **Scan finds nothing and no firewall popup appeared** → Windows Firewall is the
  first suspect, not the app. Allow it and retry.
- **Source Interface dropdown shows only "Auto"** → you are on a discontinued
  container (Docker-era) build. Use the portable exe from the latest release — it
  reads the laptop's real NICs.
- **A device is unreachable / broker won't connect** → the tool records a real
  failure status. It never fakes a pass — a red result is real, chase the network.
- **Anything else** → close the console window to stop the app, then send the
  console text and the files under `%LOCALAPPDATA%\SmartCommissioning\logs\`.

## Trust the numbers (once per site)

Cross-check one scan against an independent tool — **Yabe** for BACnet, **MQTT
Explorer** for MQTT. Same devices from two tools = the app is honest.
