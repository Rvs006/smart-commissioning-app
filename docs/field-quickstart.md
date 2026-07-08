# Field Quick-Start (print this)

One page for an engineer standing at a panel. Portable-exe path. No install, no
API key, no admin rights. Full setup lives in the app under **Learning →
Installation & Setup**; this is the short version.

## Before you leave the office

- [ ] Windows laptop with an **Intel/AMD (x64)** CPU — not a Snapdragon/ARM one.
- [ ] `SmartCommissioningApp_Windows_Portable.zip` downloaded from the repo's
      GitHub **Releases** page. Check the asset date is current.
- [ ] A wired connection to the target network: built-in Ethernet, or a
      **USB-C-to-Ethernet adapter** if the laptop has no RJ45 port.

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
     tagged "not recommended").
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

## If something looks wrong

- **Scan finds nothing and no firewall popup appeared** → Windows Firewall is the
  first suspect, not the app. Allow it and retry.
- **Source Interface dropdown shows only "Auto"** → you are on the Docker build,
  not the portable exe (Docker cannot see the laptop's NICs). Use the exe.
- **A device is unreachable / broker won't connect** → the tool records a real
  failure status. It never fakes a pass — a red result is real, chase the network.
- **Anything else** → close the console window to stop the app, then send the
  console text and the files under `runtime\logs\` (next to the exe).

## Trust the numbers (once per site)

Cross-check one scan against an independent tool — **Yabe** for BACnet, **MQTT
Explorer** for MQTT. Same devices from two tools = the app is honest.
