==========================================================
  NETWORK IP SCANNER  v1.0
  Discovery + IP Device Register (RAG) verification
==========================================================

WHAT IT DOES
------------
A real network scanner for Windows (in the style of Advanced IP Scanner)
with a built-in RAG comparison against an expected "IP Device Register".

  - Discovers every live device on the selected network, including
    devices that DO NOT reply to ping (caught via the ARP cache).
  - Detects open TCP ports + services, and probes UDP services
    (BACnet/IP Who-Is on 47808, SNMP on 161).
  - Resolves hostnames (reverse DNS, then NetBIOS) and MAC vendor.
  - Lets you pick which network adapter to scan; it auto-discovers all
    Windows adapters and their subnets.
  - Compares results to an imported register and colours each device:
        GREEN  = full match  (device present, all expected ports open)
        AMBER  = partial     (present, but some expected ports missing
                              or unexpected extra ports open)
        RED    = missing     (expected device not found / unreachable)
                 or rogue    (device found that is NOT in the register)


HOW TO RUN
----------
1. Double-click  NetworkIPScanner.exe
2. If Windows SmartScreen shows a blue box:
      "More info"  ->  "Run anyway"
   (The app is an unsigned internal tool. It only listens on your own
    machine at 127.0.0.1 and needs no admin rights.)
3. A console window opens and your default browser opens the UI at
      http://127.0.0.1:3777
   Keep the console window open while scanning. Close it to stop.

   To use a different port:  set PORT=8080 before launching, e.g. in cmd:
      set PORT=8080 && NetworkIPScanner.exe


USING IT
--------
1. NETWORK ADAPTER  - pick the NIC connected to the network you want to
   scan. The target IP range auto-fills to that adapter's subnet; edit
   the start/end if you only want part of it.
2. IP DEVICE REGISTER  (optional - you can scan with or without one)
      - Click "Template" to download the CSV template.
      - Fill it in (one row per expected device).
      - Click "Import CSV" to load it.
      - Click "Clear" to remove the loaded register and scan with no
        RAG comparison (pure discovery).
3. Click START SCAN. Rows stream in live; the RAG columns fill once the
   scan completes. Click any row for full device detail. Use the filter
   chips / search to focus. "Export CSV" saves the results.

SAVE AS REGISTER  (build a real, validated baseline)
   After a scan, click "Save as Register" to download a register CSV built
   from the scan itself - every device's ACTUAL open ports become its
   Expected Ports and its resolved name becomes its Hostname. Import that
   file and future scans validate against the real baseline: a device that
   still matches shows green, and anything that changed (a port that closed,
   a new port that opened, a different device answering at that IP) shows
   amber/red. This is the recommended way to create a register - capture a
   known-good network once, then re-scan to verify it stays that way.

Ports/services are scanned AUTOMATICALLY - there is no port box. Every
scan probes a comprehensive set of ~1200 common TCP service ports, plus
any extra ports named in your register, plus BACnet(47808) and SNMP(161)
over UDP. Results are then matched against the register for RAG.

FOREIGN / UNEXPECTED DEVICES
   Every discovered host - including ones not in the register - gets the
   full port/service scan and a hostname lookup (DNS -> NetBIOS -> mDNS
   -> LLMNR), so you can see what an unexpected device actually is.

HOSTNAME CHECKING
   If a register row has a Hostname, the scan compares it to the name it
   actually resolves on the network. A mismatch is flagged (amber) and
   the device detail panel shows Expected vs Discovered. A device whose
   name can't be resolved is shown as "unverified" rather than failed.


REGISTER CSV FORMAT
-------------------
Header row (column order is flexible; these names are recognised):

  IP Address, Hostname, Type, Vendor, Model, Expected Ports,
  Project, Location, Description

  - "Expected Ports" may be separated by ; , space or |   e.g. 47808;80;443
  - Only IP (or Hostname) + Expected Ports are needed for RAG; the rest is
    metadata shown in the detail panel.
  - If you fill in "Hostname", the scan also verifies it against the name
    the device resolves on the network (match / mismatch / unverified).

Example row:
  10.0.10.15,DEV-VSD-01,VSD,Schneider Electric,ATV600,47808;80;443,Example Site,Plant Room,Supply fan VSD


NOTES
-----
- No installation, no dependencies - it is one self-contained .exe.
- Scans use the Windows networking stack (ICMP, ARP, TCP connect, UDP
  service probes). No raw-socket driver (e.g. WinPcap/Npcap) is required.
- The MAC-vendor list is a curated subset; unknown vendors show blank.
  It can be extended in the source (oui.js) if you rebuild.
==========================================================
