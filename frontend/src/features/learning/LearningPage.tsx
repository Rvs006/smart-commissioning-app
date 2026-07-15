import { useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { getTheme, toggleTheme } from "../../app/theme";

// Stand-alone "Learning as a…" course surface for the Smart Commissioning Tool.
// Mirrors the Electracom reference course: pick a role, get a goal + a guided
// walkthrough of the real modules (Configuration, IP Discovery, BACnet/MQTT/UDMI
// discovery, Data Validation, Reports, Hub) grounded in that role's site job.
// An "Installation & Setup" panel sits above the role paths: pick an install
// path (Windows portable / Docker), then the shared first-run steps.

type WalkRow = { lab: string; text: ReactNode };
type Lesson = { num: string; title: string; rows: WalkRow[] };
type Role = {
  id: string;
  label: string;
  icon: string;
  goal: string;
  lessons: Lesson[];
  recap: string;
};

const ROLES: Role[] = [
  {
    id: "commissioning-engineer",
    label: "Commissioning Engineer",
    icon: "🛠",
    goal:
      "Walk onto site, prove what is reachable and live, validate that every point reads true end to end, and leave with an evidence pack that survives review.",
    lessons: [
      {
        num: "1",
        title: "Set the job up so every scan is trustworthy",
        rows: [
          { lab: "Go to", text: "Configuration" },
          {
            lab: "Do",
            text: "Enter the BACnet network number and the gateway IP range, the MQTT broker host, port and credentials, and the UDMI project and registry. Save the connection profile before you scan anything.",
          },
          {
            lab: "See",
            text: "A saved connection profile with a green broker reachability check and the BACnet port (47808) confirmed open.",
          },
          {
            lab: "Why",
            text: "A wrong network number or stale broker secret makes every later discovery look broken. Lock the settings first so any finding is real, not a config mistake.",
          },
        ],
      },
      {
        num: "2",
        title: "Map what is actually on the wire",
        rows: [
          { lab: "Go to", text: "IP Discovery" },
          {
            lab: "Do",
            text: "Run a scan across the site subnet and sort hosts into reachable, missing and unexpected against your device schedule.",
          },
          {
            lab: "See",
            text: "Three buckets: controllers that answered, controllers that should exist but did not, and hosts you did not expect to find.",
          },
          {
            lab: "Why",
            text: "You cannot commission a device you cannot reach. A missing controller here explains a missing BACnet device later, so chase the network gap before the protocol layer.",
          },
        ],
      },
      {
        num: "3",
        title: "Discover BACnet devices, objects and live values",
        rows: [
          { lab: "Go to", text: "BACnet Discovery" },
          {
            lab: "Do",
            text: "Discover devices, expand their object lists, and read present-value on the analog and binary objects that matter for this plant.",
          },
          {
            lab: "See",
            text: "Live property values per object, for example AHU-1 supply-air-temp reading 14.2 C with a present, in-range value.",
          },
          {
            lab: "Why",
            text: "This is the source of truth. If the sensor is wrong at the controller it will be wrong everywhere downstream, so confirm the BACnet value is sane before trusting any published copy.",
          },
        ],
      },
      {
        num: "4",
        title: "Confirm the same points reach the broker",
        rows: [
          { lab: "Go to", text: "MQTT Discovery" },
          {
            lab: "Do",
            text: "Browse broker topics, open recent payloads and check that the points you just read in BACnet are being published with sensible values.",
          },
          {
            lab: "See",
            text: "Topic tree for the device with extracted points and a recent timestamp, not a stale or empty payload.",
          },
          {
            lab: "Why",
            text: "A point that is live in BACnet but silent on MQTT is a gateway or mapping fault. Spotting it here tells you where the break is before formal validation.",
          },
        ],
      },
      {
        num: "5",
        title: "Validate BACnet against MQTT and UDMI",
        rows: [
          { lab: "Go to", text: "BACnet to MQTT Validation (Data Validation)" },
          {
            lab: "Do",
            text: "Run a validation that compares each BACnet source point with its published MQTT/UDMI output, checking type, unit, value tolerance and freshness.",
          },
          {
            lab: "See",
            text: "A pass/fail list with severities, for example fault_status expected STRING but received NUMBER flagged critical.",
          },
          {
            lab: "Why",
            text: "This is the line between it looks connected and it is correct. Every issue here is something you can hand a contractor with a concrete fix.",
          },
        ],
      },
      {
        num: "6",
        title: "Capture the proof, not screenshots",
        rows: [
          { lab: "Go to", text: "Reports" },
          {
            lab: "Do",
            text: "Generate an evidence pack from the discovery and validation runs you just executed and attach the open-issue list.",
          },
          {
            lab: "See",
            text: "A repeatable report tied to real run IDs, with the counts of reachable devices, validated points and outstanding issues.",
          },
          {
            lab: "Why",
            text: "Handover is won on evidence. A report built from stored runs is defensible in a way a folder of phone photos never is.",
          },
        ],
      },
    ],
    recap:
      "You have learned to lock the connection, map the network, read live BACnet values, confirm them on MQTT/UDMI, validate the whole chain, and export a defensible evidence pack.",
  },
  {
    id: "bms-designer",
    label: "BMS Designer",
    icon: "📐",
    goal:
      "Check that the system as built matches the design intent: the right points exist, are named and typed correctly, and carry the units your sequences assume.",
    lessons: [
      {
        num: "1",
        title: "Confirm the network matches the schedule",
        rows: [
          { lab: "Go to", text: "Configuration" },
          {
            lab: "Do",
            text: "Set the BACnet network number and IP range to match your points schedule and controller layout, then save the profile.",
          },
          {
            lab: "See",
            text: "Connection settings that line up with the design documents, ready for a discovery pass.",
          },
          {
            lab: "Why",
            text: "Discovery only proves the design if it is pointed at the network your drawings describe. Align it here so gaps are real, not addressing errors.",
          },
        ],
      },
      {
        num: "2",
        title: "Check every designed device exists",
        rows: [
          { lab: "Go to", text: "IP Discovery" },
          {
            lab: "Do",
            text: "Scan and compare reachable hosts against your device schedule to spot missing controllers and unexpected extras.",
          },
          {
            lab: "See",
            text: "Missing-device entries that map directly to lines on your schedule, plus unexpected hosts that were never designed in.",
          },
          {
            lab: "Why",
            text: "A missing controller means a missing plant item or a wiring change in the field. This is the fastest way to reconcile as-built against as-designed.",
          },
        ],
      },
      {
        num: "3",
        title: "Verify the object list matches the point count",
        rows: [
          { lab: "Go to", text: "BACnet Discovery" },
          {
            lab: "Do",
            text: "Open each device and count its analog/binary inputs and outputs against the points you specified for that plant item.",
          },
          {
            lab: "See",
            text: "The real object list per device, so you can confirm AHU-1 has all its specified sensors and commandable points.",
          },
          {
            lab: "Why",
            text: "Sequences fail silently when a designed point was never built. Catching a short object list now is far cheaper than during witness testing.",
          },
        ],
      },
      {
        num: "4",
        title: "Inspect the UDMI metadata and pointset",
        rows: [
          { lab: "Go to", text: "UDMI Payload Workbench" },
          {
            lab: "Do",
            text: "Open state, metadata and pointset for a device and confirm point names, types and units match your naming and engineering-unit conventions.",
          },
          {
            lab: "See",
            text: "The structured pointset with each point's declared type and unit, alongside the metadata describing the device model.",
          },
          {
            lab: "Why",
            text: "The pointset is your design contract expressed in data. If a temperature point is published unitless or as a string, the model does not match intent.",
          },
        ],
      },
      {
        num: "5",
        title: "Catch type and unit drift across the chain",
        rows: [
          { lab: "Go to", text: "BACnet to MQTT Validation (Data Validation)" },
          {
            lab: "Do",
            text: "Run a validation and filter for type-mismatch and unit-mismatch issues between the BACnet source and the published output.",
          },
          {
            lab: "See",
            text: "A list of points where the published type or unit diverges from the source, flagged with severity.",
          },
          {
            lab: "Why",
            text: "Drift between layers breaks analytics and energy dashboards downstream. Each flag is a mapping the integrator needs to correct before sign-off.",
          },
        ],
      },
      {
        num: "6",
        title: "Record the design-conformance result",
        rows: [
          { lab: "Go to", text: "Reports" },
          {
            lab: "Do",
            text: "Generate a report capturing device coverage, point counts and outstanding type/unit issues against the design.",
          },
          {
            lab: "See",
            text: "An evidence pack you can attach to the design review showing what conforms and what still needs rework.",
          },
          {
            lab: "Why",
            text: "It closes the loop on your drawings: the report is proof the installed system either meets the design or has a tracked list of exceptions.",
          },
        ],
      },
    ],
    recap:
      "You have learned to align discovery with your schedule, confirm every designed device and point exists, read the UDMI pointset as a design contract, catch type/unit drift, and record conformance against intent.",
  },
  {
    id: "project-manager",
    label: "Project Manager",
    icon: "📊",
    goal:
      "See commissioning progress and risk across every site at a glance, know what is blocking handover, and have evidence ready when the client asks.",
    lessons: [
      {
        num: "1",
        title: "Start from the cross-project view",
        rows: [
          { lab: "Go to", text: "Hub (Multi-Project Hub)" },
          {
            lab: "Do",
            text: "Open the Hub to see every project, site and edge gateway with its latest commissioning run status in one operator view.",
          },
          {
            lab: "See",
            text: "A board of runs across sites, each showing whether discovery and validation are queued, running, succeeded or failed.",
          },
          {
            lab: "Why",
            text: "You manage a portfolio, not one panel. The Hub turns scattered site activity into a single progress picture you can report on.",
          },
        ],
      },
      {
        num: "2",
        title: "Read run status without reading protocols",
        rows: [
          { lab: "Go to", text: "Hub (Multi-Project Hub)" },
          {
            lab: "Do",
            text: "Drill into a site to see recent runs and their state, and spot which sites have stalled or failing runs.",
          },
          {
            lab: "See",
            text: "Status tokens such as succeeded, running and failed with timestamps showing how fresh each result is.",
          },
          {
            lab: "Why",
            text: "A failed run on a site is a schedule risk. Seeing it early lets you redeploy the engineer before it becomes a missed milestone.",
          },
        ],
      },
      {
        num: "3",
        title: "Quantify what is blocking handover",
        rows: [
          { lab: "Go to", text: "BACnet to MQTT Validation (Data Validation)" },
          {
            lab: "Do",
            text: "Open the latest validation result for a site and look at the count of open issues by severity.",
          },
          {
            lab: "See",
            text: "How many critical and high issues remain, for example three critical points still failing on a plant room.",
          },
          {
            lab: "Why",
            text: "Critical issues, not percent-complete, decide handover. This gives you a defensible answer to is this site ready.",
          },
        ],
      },
      {
        num: "4",
        title: "Confirm the evidence exists",
        rows: [
          { lab: "Go to", text: "Reports" },
          {
            lab: "Do",
            text: "Check that each site has a generated evidence pack tied to real discovery and validation runs.",
          },
          {
            lab: "See",
            text: "A list of reports per project with their status, so you know the handover paperwork is ready to send.",
          },
          {
            lab: "Why",
            text: "No report means no proof. Verifying packs exist before the client meeting avoids a scramble on the day.",
          },
        ],
      },
      {
        num: "5",
        title: "Track readiness over time",
        rows: [
          { lab: "Go to", text: "Hub (Multi-Project Hub)" },
          {
            lab: "Do",
            text: "Compare run history across the portfolio to see which sites are converging and which keep re-opening issues.",
          },
          {
            lab: "See",
            text: "Trends in run outcomes that show whether a site is genuinely closing out or churning.",
          },
          {
            lab: "Why",
            text: "A site that re-runs validation repeatedly without clearing issues signals a systemic fault worth escalating, not just one bad point.",
          },
        ],
      },
    ],
    recap:
      "You have learned to run the portfolio from the Hub, read run status without touching protocols, quantify handover blockers by severity, confirm evidence packs exist, and track readiness trends across sites.",
  },
  {
    id: "integration-engineer",
    label: "Integration Engineer",
    icon: "🔌",
    goal:
      "Prove the data path holds: the broker, certificates and UDMI publishing are correct, and every BACnet point lands on the cloud with the right shape.",
    lessons: [
      {
        num: "1",
        title: "Wire up broker, certificates and UDMI",
        rows: [
          { lab: "Go to", text: "Configuration" },
          {
            lab: "Do",
            text: "Set the MQTT broker host and port, load the device credentials and TLS material, and enter the UDMI project and registry IDs.",
          },
          {
            lab: "See",
            text: "A successful broker connection and a confirmed UDMI registry binding, with secrets stored rather than echoed back.",
          },
          {
            lab: "Why",
            text: "Most integration faults are connection faults. Getting auth, ports and the registry right here removes the noisiest class of failure first.",
          },
        ],
      },
      {
        num: "2",
        title: "Confirm the gateway is reachable",
        rows: [
          { lab: "Go to", text: "IP Discovery" },
          {
            lab: "Do",
            text: "Scan to confirm the edge gateway and the BACnet side of the network respond at the addresses you configured.",
          },
          {
            lab: "See",
            text: "The gateway listed as reachable and the BACnet controllers it bridges answering on the network.",
          },
          {
            lab: "Why",
            text: "If the gateway is unreachable, nothing it should publish will appear. Confirm the host before you debug the payload layer.",
          },
        ],
      },
      {
        num: "3",
        title: "Inspect topics and extracted points",
        rows: [
          { lab: "Go to", text: "MQTT Discovery" },
          {
            lab: "Do",
            text: "Browse the broker topic tree, open raw payloads, and check how the gateway has extracted points from each message.",
          },
          {
            lab: "See",
            text: "The topic hierarchy per device with recent payloads and the points the tool parsed out of them.",
          },
          {
            lab: "Why",
            text: "This shows the integration as it really publishes. A malformed topic or missing field here is a gateway mapping bug you own.",
          },
        ],
      },
      {
        num: "4",
        title: "Validate state, metadata and pointset against UDMI",
        rows: [
          { lab: "Go to", text: "UDMI Payload Workbench" },
          {
            lab: "Do",
            text: "Open state, metadata, pointset and the controlled-publish evidence, and check the payloads conform to the UDMI schema.",
          },
          {
            lab: "See",
            text: "Structured state and pointset blocks, plus evidence that a controlled publish was received and acknowledged.",
          },
          {
            lab: "Why",
            text: "Cloud ingestion rejects off-schema payloads. Confirming UDMI conformance here is what makes the data usable upstream.",
          },
        ],
      },
      {
        num: "5",
        title: "Prove BACnet to MQTT/UDMI alignment",
        rows: [
          { lab: "Go to", text: "BACnet to MQTT Validation (Data Validation)" },
          {
            lab: "Do",
            text: "Run a validation that compares each BACnet source point with the published output for protocol alignment, type, unit and freshness.",
          },
          {
            lab: "See",
            text: "Per-point pass/fail with the exact mismatch, for example a point published stale by 40 minutes or with a wrong type.",
          },
          {
            lab: "Why",
            text: "This is the acceptance test for your integration: it shows every source point lands correctly, not just that messages are flowing.",
          },
        ],
      },
      {
        num: "6",
        title: "Hand back a clean integration record",
        rows: [
          { lab: "Go to", text: "Reports" },
          {
            lab: "Do",
            text: "Generate an evidence pack from the MQTT/UDMI and validation runs and attach the remaining mapping issues.",
          },
          {
            lab: "See",
            text: "A report tied to real run IDs documenting topic coverage, UDMI conformance and any open alignment issues.",
          },
          {
            lab: "Why",
            text: "It is your proof the data path works and a precise punch list of what is left, so the next pass starts from facts.",
          },
        ],
      },
    ],
    recap:
      "You have learned to wire broker, certs and UDMI correctly, confirm the gateway is reachable, inspect topics and payloads, validate against the UDMI schema, prove BACnet to MQTT/UDMI alignment, and hand back a clean integration record.",
  },
];

// Installation & Setup: the two supported ways to get the tool running, in the
// same declarative Lesson shape as the role paths above.
type SetupPath = {
  id: string;
  label: string;
  icon: string;
  blurb: string;
  note?: { kind: "ok" | "warn"; title: string; text: string };
  lessons: Lesson[];
};

const SETUP_PATHS: SetupPath[] = [
  {
    id: "windows-portable",
    label: "Windows portable app",
    icon: "🪟",
    blurb: "One zip, one double-click. Nothing to install — recommended for field laptops.",
    note: {
      kind: "warn",
      title: "Locked-down company laptop?",
      text: "If your laptop uses application allow-listing (for example ThreatLocker), the unsigned exe may be blocked from running at all. Ask IT to approve it, or use the Docker path instead.",
    },
    lessons: [
      {
        num: "1",
        title: "Download and unzip",
        rows: [
          {
            lab: "Get",
            text: (
              <>
                <code>SmartCommissioningApp_Windows_Portable.zip</code> from the latest GitHub
                release — ask your project lead if you don&apos;t have the link.
              </>
            ),
          },
          {
            lab: "Do",
            text: "Right-click the zip and choose Extract All, into a normal folder — Desktop is fine.",
          },
          {
            lab: "See",
            text: (
              <>
                A folder containing <code>SmartCommissioningApp.exe</code>.
              </>
            ),
          },
          {
            lab: "Why",
            text: "The zip is the whole app — no Python, Node, Docker or admin install needed.",
          },
        ],
      },
      {
        num: "2",
        title: "Launch it",
        rows: [
          {
            lab: "Do",
            text: (
              <>
                Double-click <code>SmartCommissioningApp.exe</code> and keep the black console
                window open while you work.
              </>
            ),
          },
          {
            lab: "See",
            text: (
              <>
                Your browser opens at the printed URL, usually <code>http://127.0.0.1:8000/</code>.
                If port 8000 is busy the launcher picks the next free port — always use the URL
                from the console.
              </>
            ),
          },
          {
            lab: "Why",
            text: "Everything runs only on your laptop (it binds to 127.0.0.1). Windows SmartScreen may warn because this is an internal unsigned build — choose More info, then Run anyway, only if the zip came from the project owner or the official releases page.",
          },
        ],
      },
      {
        num: "3",
        title: "You are already signed in",
        rows: [
          {
            lab: "See",
            text: "Run / Publish / Export and the certificate and key Replace buttons enabled, with no key asked for.",
          },
          {
            lab: "Why",
            text: "The portable app trusts your own machine (loopback), so no API key or sign-in is needed. When you are done, stop the app with Ctrl+C in the console or close the console window.",
          },
        ],
      },
    ],
  },
  {
    id: "docker",
    label: "Docker Desktop (shared / team)",
    icon: "🐳",
    blurb: "One command brings up the identical full stack for everyone.",
    note: {
      kind: "warn",
      title: "Before you start",
      text: "Docker Desktop must be installed and running; a machine with around 32 GB RAM is recommended for the full stack; and you need the repository cloned — it is private, so ask your project lead for access.",
    },
    lessons: [
      {
        num: "1",
        title: "Generate a key and start the stack",
        rows: [
          {
            lab: "Do",
            text: (
              <>
                From the repository folder run <code>./scripts/bootstrap-env.ps1</code> (Windows,
                PowerShell 7) or <code>./scripts/bootstrap-env.sh</code> (Linux/macOS), then{" "}
                <code>
                  docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --build
                </code>
                .
              </>
            ),
          },
          {
            lab: "See",
            text: "The script writes infra/.env with fresh random secrets and prints your API_KEY — keep a copy: it is your sign-in key. Then the containers build, start, and report healthy.",
          },
          {
            lab: "Why",
            text: "UI, API, worker, database and queue start together — identically on every machine, which is why this path is recommended for shared team servers.",
          },
        ],
      },
      {
        num: "2",
        title: "Open and sign in",
        rows: [
          {
            lab: "Go to",
            text: <code>http://127.0.0.1:8080</code>,
          },
          {
            lab: "Do",
            text: (
              <>
                Click <strong>Set API key</strong> at the top-right of the header, paste the
                API_KEY value you generated, and save.
              </>
            ),
          },
          { lab: "See", text: "The page reloads and shows your role." },
          {
            lab: "Why",
            text: "Hosted deployments require a key for every action — without one, Run / Upload / Export stay disabled by design.",
          },
        ],
      },
    ],
  },
];

const CLOUD_NOTE =
  "Cloud / hosted-server deployment is set up centrally, not by field engineers — ask your project lead for the server address and your personal API key.";

// Shared first-run steps, shown after whichever install path is selected.
const FIRST_RUN: Lesson[] = [
  {
    num: "1",
    title: "Point scans at the right network",
    rows: [
      { lab: "Go to", text: "Configuration" },
      {
        lab: "Do",
        text: (
          <>
            Confirm <strong>Source Interface</strong> — when never chosen, the tool pre-selects
            the first wired adapter (Ethernet or USB-Ethernet dongle), which is usually right on a
            field laptop. If it picked the wrong one, choose the adapter that is plugged into the
            building/BMS network, not the Wi-Fi you use for internet.
          </>
        ),
      },
      {
        lab: "See",
        text: "A list of your machine's real network adapters to choose from, read live from the system.",
      },
      {
        lab: "Why",
        text: "On a two-network laptop, a scan that leaves via Wi-Fi will never find the BACnet controllers on the wired side.",
      },
    ],
  },
  {
    num: "2",
    title: "Run your first scan — safely",
    rows: [
      { lab: "Go to", text: "IP Discovery" },
      {
        lab: "Do",
        text: (
          <>
            Upload the project&apos;s IP register under <strong>Register Import</strong> — scan
            targets come from its Expected IP address column (blank XLSX/CSV templates are
            downloadable in the same panel). Then tick <strong>Dry run</strong> in Run Controls (it
            is off by default) and start the scan.
          </>
        ),
      },
      {
        lab: "See",
        text: "A plan of the scan targets from your register — no packets are sent and no authorization is needed.",
      },
      {
        lab: "Why",
        text: 'A dry-run proves the whole workflow end to end without touching a live BMS network. Real scans stay locked behind the explicit "I am authorized" tick.',
      },
    ],
  },
];

// One guided walkthrough block; used by both the setup paths and the role paths.
function WalkSteps({ lessons }: { lessons: Lesson[] }) {
  return (
    <div className="dc-walk">
      {lessons.map((lesson) => (
        <div className="dc-walk-step" key={lesson.num}>
          <div className="dc-walk-head">
            <div className="dc-walk-num">{lesson.num}</div>
            <div className="dc-walk-title">{lesson.title}</div>
          </div>
          <div className="dc-walk-body">
            {lesson.rows.map((row) => (
              <div className="dc-walk-row" key={row.lab}>
                <span className="dc-walk-lab">{row.lab}</span>
                <span>{row.text}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

export function LearningPage() {
  const [activeRoleId, setActiveRoleId] = useState<string>(ROLES[0].id);
  const [setupPathId, setSetupPathId] = useState<string>(SETUP_PATHS[0].id);
  const [themeMode, setThemeMode] = useState<"light" | "dark">(getTheme());

  const role = ROLES.find((r) => r.id === activeRoleId) ?? ROLES[0];
  const setupPath = SETUP_PATHS.find((p) => p.id === setupPathId) ?? SETUP_PATHS[0];

  return (
    <div className="demo-shell">
      <header className="dc-header">
        <div className="dc-brand-bar">
          <div className="dc-brand">
            <img className="dc-brand-logo" src="/electracom-logo.png" alt="Electracom" />
            <div>
              <div className="dc-brand-title">Smart Commissioning Tool</div>
              <div className="dc-brand-kind">Learning</div>
            </div>
          </div>
          <div className="dc-header-actions">
            <Link className="dc-ghost" to="/brief">
              ← Back to Brief
            </Link>
            <button
              className="dc-ghost"
              type="button"
              onClick={() => setThemeMode(toggleTheme())}
            >
              {themeMode === "dark" ? "☀ Light" : "☾ Dark"}
            </button>
            <Link className="dc-launch" to="/">
              <span className="dc-launch-dot" />
              Launch the App →
            </Link>
          </div>
        </div>
      </header>

      <main className="dc-panels">
        <section className="dc-panel" id="installation-setup">
          <div className="dc-intro">
            <div className="dc-kicker">Installation &amp; Setup</div>
            <h1 className="dc-h1">Get the tool running first.</h1>
            <p className="dc-lead">
              Two ways to run it — pick the one that matches how you work, then do the two
              first-run steps below.
            </p>
          </div>

          <div className="dc-role-picker">
            <div className="dc-role-picker-label">
              Install using…
              <small>Pick the way you will run the tool.</small>
            </div>
            <div className="dc-role-pills">
              {SETUP_PATHS.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  aria-pressed={p.id === setupPathId}
                  className={`dc-role-pill${p.id === setupPathId ? " active" : ""}`}
                  onClick={() => setSetupPathId(p.id)}
                >
                  <span className="dc-role-pill-icon">{p.icon}</span>
                  {p.label}
                </button>
              ))}
            </div>
          </div>

          <div className="dc-hero">
            <div className="dc-hero-kicker">This path</div>
            <div className="dc-hero-text">{setupPath.blurb}</div>
          </div>

          {setupPath.note && (
            <div className={`dc-callout ${setupPath.note.kind}`}>
              <span className="dc-callout-icon">
                {setupPath.note.kind === "ok" ? "✓" : "⚠"}
              </span>
              <div>
                <strong>{setupPath.note.title}</strong>
                <p>{setupPath.note.text}</p>
              </div>
            </div>
          )}

          <WalkSteps lessons={setupPath.lessons} />

          <div className="dc-callout">
            <span className="dc-callout-icon">☁</span>
            <div>
              <strong>Looking for the cloud / hosted server?</strong>
              <p>{CLOUD_NOTE}</p>
            </div>
          </div>

          <div className="dc-body">
            <h2>First run — from blank app to first scan</h2>
          </div>
          <WalkSteps lessons={FIRST_RUN} />

          <div className="dc-callout ok">
            <span className="dc-callout-icon">✓</span>
            <div>
              <strong>You are set up.</strong>
              <p>
                You have installed the tool, pointed it at the right network, and proven a scan
                runs. Now pick your role below and learn the modules you&apos;ll use on site.
              </p>
            </div>
          </div>
        </section>

        <section className="dc-panel">
          <div className="dc-intro">
            <div className="dc-kicker">Learning path</div>
            <h1 className="dc-h1">Learn the tool the way you will use it.</h1>
            <p className="dc-lead">
              Commissioning means something different to an engineer on the wire, a designer
              checking intent, a manager tracking risk, and an integrator proving the data path.
              Pick your role and follow a guided walkthrough of the exact modules you will touch on
              site, in the order you will touch them. Tool not running yet? Start with
              Installation &amp; Setup above.
            </p>
          </div>

          <div className="dc-role-picker">
            <div className="dc-role-picker-label">
              Learning as a…
              <small>Pick one to see that role&apos;s path.</small>
            </div>
            <div className="dc-role-pills">
              {ROLES.map((r) => (
                <button
                  key={r.id}
                  type="button"
                  aria-pressed={r.id === activeRoleId}
                  className={`dc-role-pill${r.id === activeRoleId ? " active" : ""}`}
                  onClick={() => setActiveRoleId(r.id)}
                >
                  <span className="dc-role-pill-icon">{r.icon}</span>
                  {r.label}
                </button>
              ))}
            </div>
          </div>

          <div className="dc-hero">
            <div className="dc-hero-kicker">Your goal</div>
            <div className="dc-hero-text">{role.goal}</div>
          </div>

          <WalkSteps lessons={role.lessons} />

          <div className="dc-callout ok">
            <span className="dc-callout-icon">✓</span>
            <div>
              <strong>You have learned…</strong>
              <p>{role.recap}</p>
            </div>
          </div>

          <div className="dc-launch-zone">
            <h3>Put it into practice</h3>
            <p>
              Open the console and run your first discovery and validation on a real site. Everything
              in this path lives one click away.
            </p>
            <Link className="dc-launch" to="/">
              <span className="dc-launch-dot" />
              Launch the App →
            </Link>
          </div>
        </section>
      </main>

      <footer className="dc-footer">
        <Link className="dc-footer-link" to="/brief">
          ← Back to the Product Brief
        </Link>
        <p>Electracom · Smart Commissioning · Learning</p>
      </footer>
    </div>
  );
}
