==========================================================
  SCA · MQTT DISCOVERY  v2.0
  Broker subscribe · asset & point discovery · UDMI validation
==========================================================

WHAT IT DOES
------------
Connects to an MQTT broker, subscribes to a root topic filter, and inspects
the JSON payloads flowing on each topic. It is built for UDMI-style BMS/IoT
estates: it discovers ASSETS from topics, extracts POINTS from the payloads,
and validates the live estate against an imported MQTT Register.

  - Connects over plain (mqtt://) or TLS (mqtts://) with anonymous,
    username/password, or client-certificate authentication.
  - Subscribes to a Root Topic Filter (e.g. "udmi/#") at a chosen QoS and
    streams live.
  - Discovers every unique topic, its message RATE (msg/s), and maps each
    topic to an ASSET (e.g. udmi/site/example/ahu/01 -> AHU-01).
  - Detects the payload SCHEMA (udmi-v2 / json / raw).
  - Extracts POINTS from UDMI payloads: a leaf like
        "supply_air_temp": { "value": 14.2, "units": "degC" }
    becomes a live point  supply_air_temp = 14.2 degC.
  - Compares each asset's live points against the MQTT Register and reports
        MATCHED  - expected point seen live
        MISSING  - expected point not seen
        EXTRA    - live point not in the register
  - Flags SCHEMA / PAYLOAD ISSUES (non-JSON payloads, missing UDMI envelope
    fields, points with no value or missing units).
  - Lets you PUBLISH a message to any topic (raw or JSON, QoS 0/1, retain).

The tool is a passive subscriber by default - it only publishes when you use
the Publish button.


HOW TO RUN
----------
1. Double-click  MqttDiscovery.exe
2. If Windows SmartScreen shows a blue box:  "More info" -> "Run anyway".
   (Unsigned internal tool. It only listens on your own machine at
    127.0.0.1 and needs no admin rights.)
3. A console window opens and your browser opens the UI at
      http://127.0.0.1:3799
   Keep the console window open while it runs. Close it to stop.

   To use a different port:  set PORT=8080 && MqttDiscovery.exe


USING IT
--------
1. Click CONNECT... (top-left card). Fill in:
      - Protocol:  mqtt:// (plain) or mqtts:// (TLS)
      - Host / Port  (1883 plain, 8883 TLS by default)
      - Username / Password  (leave blank for anonymous)
      - Root Topic Filter  (default "#" = everything; narrow later live)
      - QoS  (0/1/2)
      - TLS certificates (optional): paste CA / client cert / client key PEM,
        OR click "Upload file..." to load a .pem/.crt/.cer/.key from disk.
        Untick "Validate broker TLS certificate" for self-signed brokers.
2. IMPORT REGISTER (optional): download the Template, fill it in, Import.
   This drives Expected Assets and the Matched / Missing / Extra comparison.
3. Browse the TOPIC TREE (left). It fills in live, assets nested within their
   topic paths, exactly like MQTT Explorer:
      - Click a caret to expand/collapse a branch; each branch shows its
        topic + message counts, each device its message rate.
      - Nodes FLASH when a new payload arrives (branches flash when anything
        beneath them updates).
      - Sort children by Rate (top contributors first) or A-Z.
      - Hover a node and click the copy icon to copy that topic path.
      - Click a device to inspect it on the right:
          Overview     - summary, topics (copyable), register comparison
          Live Payload - streaming JSON + last-8 history chips (Pause/Export)
          Points       - live points vs register (matched/missing/extra)
          Config       - WRITE a config payload back to the device (see below)
          Metadata     - identity from the payload + expected register data
4. SEARCH (top of the tree) matches topic names, asset ids AND payload
   contents across ALL topics - type a value that appears inside a message
   (e.g. a meter reading) and it finds the topic carrying it.
5. CHANGE THE SUBSCRIPTION LIVE: edit the Root Topic Filter card and click
   Apply - it re-subscribes without disconnecting. "clear on change" (ticked
   by default) resets the view to the new scope; untick it to accumulate
   across several filters. QoS can be changed the same way.
6. SAVE AS REGISTER: download a register CSV built from the live estate -
   every discovered asset and its points become expected rows. Import it to
   baseline/validate future subscriptions.
7. EXPORT: click Export to download a ZIP of EVERY discovered asset - live
   and historic payloads - in a folder tree mirroring the topic structure,
   with a _manifest.json index. (History keeps the last 8 payloads per topic.)
8. PUBLISH: click Publish, set topic + payload (raw or JSON) + QoS + retain.

WRITE CONFIG TO A DEVICE
   Select a device, open the Config tab. The target "<device>/config" topic is
   shown. Click "Load current" to prefill the device's current retained config,
   edit the JSON, then "Send to /config". A confirmation shows the exact topic
   and payload before anything is written - config messages change how live
   equipment behaves, so review carefully. Sent retained at QoS 1 by default.

TIP: drag the divider between the tree and the asset detail to give more room
to either side (double-click it to reset). The split is remembered.


MQTT REGISTER CSV FORMAT
------------------------
One row per expected POINT (rows are grouped into assets). Column order is
flexible; these header names are recognised:

  Asset, Topic, Type, Point, Unit, Data Type, Schema, Site, Location, Description

  - Asset : the asset/equipment id (e.g. AHU-01). If blank, it is derived
            from Topic.
  - Topic : the MQTT topic that carries this asset (maps topic -> asset so the
            live view uses your names). Also added to the subscription.
  - Point : the telemetry point name (e.g. supply_air_temp). Matched case- and
            separator-insensitively (supply_air_temp == "Supply Air Temp").
  - The remaining columns are metadata shown in the asset detail.
  - An asset row with no Point still counts as an expected asset (presence).

Example rows:
  AHU-01,udmi/site/example/ahu/01,AHU,supply_air_temp,degC,analog,udmi-v2,Example Site,Plant Room,AHU-01 supply air temp
  AHU-01,udmi/site/example/ahu/01,AHU,return_air_temp,degC,analog,udmi-v2,Example Site,Plant Room,AHU-01 return air temp


NOTES
-----
- No installation, no runtime dependencies - one self-contained .exe.
- The MQTT client is hand-rolled (pure Node over TCP/TLS), speaking MQTT
  3.1.1. It holds a live session, keeps it alive, and reconnects on drop.
- WebSocket transports (ws:// / wss://) are not yet implemented - use the
  native mqtt:// / mqtts:// ports. This is the next planned add-on.
- Point extraction understands UDMI  { value, units }  leaves (and
  present_value / pointset.points variants). Non-UDMI JSON still shows in the
  Live Payload viewer; scalar telemetry-looking fields are picked up too.

BUILDING FROM SOURCE  (developer note)
--------------------------------------
  node build-bundle.js                            # -> dist/bundle.js
  node --experimental-sea-config sea-config.json  # -> dist/sea-prep.blob
  copy node.exe dist\MqttDiscovery.exe
  node dist\_inject.js                            # postject blob into the exe
Verify without running the (unsigned) exe:
  set NO_OPEN=1 && node dist/bundle.js            # runs the server from the bundle
==========================================================
