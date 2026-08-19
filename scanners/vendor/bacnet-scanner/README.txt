==========================================================
  BACnet SCANNER  v1.0
  BACnet/IP discovery + Device Register (RAG) verification
==========================================================

WHAT IT DOES
------------
A standalone BACnet/IP scanner for Windows with a built-in RAG comparison
against an expected "BACnet Device Register".

  - Broadcasts a Who-Is on UDP 47808 and collects every I-Am reply, so it
    finds every BACnet/IP device that will answer on the selected network.
  - Reads each device's identity: object name, vendor, model, firmware
    revision, application-software version, location, description, protocol
    revision, system status, and total object count.
  - Discovers BACnet routers and the remote network numbers behind them
    (Who-Is-Router-To-Network), so multi-network / BBMD sites are visible.
  - Lets you drill into any device and browse its full OBJECT LIST — every
    object's type, instance, name, and (for inputs/outputs/values) its
    present value and engineering units.
  - Compares discovered devices to an imported register and colours each:
        GREEN  = full match  (device present; object count + name + model
                              agree with the register)
        AMBER  = partial     (present, but object count differs, or the
                              device name / model do not match the register)
        RED    = missing     (expected device did not answer)
                 or rogue    (a device answered that is NOT in the register)

It is a single self-contained .exe. No installation, no dependencies, no
Npcap/WinPcap, no admin rights. It listens only on 127.0.0.1 for its own UI
and speaks BACnet/IP out of the network adapter you choose.


HOW TO RUN
----------
1. Double-click  BacnetScanner.exe
2. If Windows SmartScreen shows a blue box:
      "More info"  ->  "Run anyway"
   (Unsigned internal tool. It only serves its UI at 127.0.0.1 and needs no
    admin rights.)
3. A console window opens and your default browser opens the UI at
      http://127.0.0.1:3778
   Keep the console window open while scanning. Close it to stop.

   To use a different UI port:  set PORT=8080 before launching, e.g. in cmd:
      set PORT=8080 && BacnetScanner.exe


USING IT
--------
1. NETWORK ADAPTER — pick the NIC connected to the BACnet network. The Who-Is
   is broadcast to that adapter's subnet broadcast address.
2. DEVICE INSTANCE RANGE (optional) — leave blank to discover ALL devices
   (a global Who-Is). Fill in a low/high device-instance to target a range.
3. DISCOVERY WINDOW — how long (ms) to listen for I-Am replies. On a large or
   slow network, increase it (e.g. 6000-8000 ms) so late repliers are caught.
4. BACNET DEVICE REGISTER (optional — scan with or without one)
      - "Template" downloads the CSV template.
      - Fill it in (one row per expected device) and "Import CSV" to load it.
      - "Clear" removes it and runs pure discovery (no RAG).
5. Click START SCAN. Devices stream in live as they answer Who-Is; identity
   fields fill in as each device is read; the RAG columns settle once the
   scan completes. Click any device row for full detail and to load its
   object list. Use the filter chips / search to focus. "Export CSV" saves
   the results.

EXPORT ASSETS  (objects + current values → ZIP of JSON + XLSX)
   Click "Export Assets" to enter selection mode: a checkbox appears on every
   device row plus a "select all" checkbox in the header. Tick the devices you
   want (or select all), then click "Export Selected". The scanner reads each
   selected device's FULL object list — every object's name and current
   present value — and packages the result as a single ZIP:
       bacnet-export-<timestamp>.zip
         EM-01_2001/
           EM-01_2001.json    (asset details + all points & values)
           EM-01_2001.xlsx    (the same as an Excel sheet)
         AHU-01_1001/
           AHU-01_1001.json
           AHU-01_1001.xlsx
         ...
   One folder per asset, named "<device name>_<BACnet device instance>".
   Reading objects is done one at a time for reliability, so exporting many
   large devices can take a while — a progress bar shows which asset is being
   read. Devices with very large object lists are capped at 2000 objects
   (noted as "truncated" in the files).

SAVE AS REGISTER  (build a real, validated baseline)
   After a scan, click "Save as Register" to download a register CSV built
   from the scan itself — every device's instance, reported name, vendor,
   model and ACTUAL object count become the expected baseline. Import that
   file and future scans validate against the real site: a device that still
   matches shows green; anything that changed (a controller that dropped off,
   a different device now answering at that instance, an object count that
   moved) shows amber/red. This is the recommended way to create a register:
   capture a known-good site once, then re-scan to verify it stays that way.


BACNET DEVICE REGISTER CSV FORMAT
---------------------------------
Header row (column order is flexible; these names are recognised):

  Device Instance, Device Name, Network, IP Address, Vendor, Model,
  Location, Expected Objects, Description

  - "Device Instance" is the match key (the BACnet device object instance,
    globally unique across the internetwork).
  - "Expected Objects" (optional) is the expected total object count; if the
    discovered count differs the device is flagged amber.
  - "Device Name" (optional) is checked against the device's reported
    object-name (match / mismatch).
  - The rest is metadata shown in the device detail panel.

Example row:
  1001,AHU-01,0,10.0.10.15,Trend,IQ4E,Plant Room,42,Example AHU controller


NOTES
-----
- BACnet/IP only (UDP 47808). MS/TP and other data links are not scanned,
  but MS/TP devices behind a BACnet/IP router ARE discovered (their network
  number and MAC are shown, and their objects can be browsed through the
  router).
- Object browsing reads properties one at a time for maximum compatibility
  with small controllers; very large devices are capped (first 200 objects)
  to keep it responsive — the panel notes when a list was truncated.
- The vendor-ID name table is a curated subset; a device's own reported
  vendor-name always takes precedence over it.
- It can run ALONGSIDE other BACnet software (e.g. YABE) that also uses UDP
  47808: discovery shares the port to hear I-Am broadcasts, while all property
  reads use a private port, so responses are never intercepted by the other
  app. No need to close YABE to use this scanner.
==========================================================
