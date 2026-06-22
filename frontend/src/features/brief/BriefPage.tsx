import { useState } from "react";
import { Link } from "react-router-dom";
import { getTheme, toggleTheme } from "../../app/theme";

// Standalone Product Brief surface. Mirrors the Electracom reference brief 1:1 in
// format (4 tabs: Basics / Key Features / Section Reference / Guided Tour) but the
// copy is grounded in the real Smart Commissioning Tool modules. Carries its own
// demo-shell header; "Launch the App" enters the console layout at "/".

type TabId = "basics" | "features" | "reference" | "tour";

type RoleId = "engineer" | "designer" | "pm" | "integration";

const tabs: { id: TabId; num: string; label: string }[] = [
  { id: "basics", num: "01", label: "Basics" },
  { id: "features", num: "02", label: "Key Features" },
  { id: "reference", num: "03", label: "Section Reference" },
  { id: "tour", num: "04", label: "Guided Tour" },
];

const roles: { id: RoleId; icon: string; pill: string; title: string; lead: string }[] = [
  {
    id: "engineer",
    icon: "🛠",
    pill: "Commissioning Engineer",
    title: "For a Commissioning Engineer",
    lead: "You are on site, you need every point alive and proven. Walk a job from a cold network to a signed evidence pack.",
  },
  {
    id: "designer",
    icon: "📐",
    pill: "BMS Designer",
    title: "For a BMS Designer",
    lead: "You own the points list and the ontology. Confirm the field reality matches the design intent before handover.",
  },
  {
    id: "pm",
    icon: "📋",
    pill: "Project Manager",
    title: "For a Project Manager",
    lead: "You track progress across sites and need defensible status, not optimism. Read the run history and export the evidence.",
  },
  {
    id: "integration",
    icon: "🔌",
    pill: "Integration Engineer",
    title: "For an Integration Engineer",
    lead: "You wire BACnet to the broker and to UDMI. Verify the bridge end to end and catch payload drift before the cloud does.",
  },
];

const walks: Record<RoleId, { num: string; title: string; rows: { lab: string; text: string }[] }[]> = {
  engineer: [
    {
      num: "1",
      title: "Confirm the network is reachable",
      rows: [
        { lab: "Go to", text: "IP Discovery (IP Scanner) and load the expected host list for the site." },
        { lab: "Do", text: "Run a scan across the commissioning subnet to find reachable, missing and unexpected hosts." },
        { lab: "See", text: "A red flag on any missing controller before you waste an afternoon chasing it on BACnet." },
      ],
    },
    {
      num: "2",
      title: "Discover the BACnet estate",
      rows: [
        { lab: "Go to", text: "BACnet Discovery and select the network range you just proved reachable." },
        { lab: "Do", text: "Discover devices, enumerate their objects and read live present-value for the key points." },
        { lab: "See", text: "Object names, units and live values, so you know the device is real and not a stale cache." },
      ],
    },
    {
      num: "3",
      title: "Prove the points reach the broker",
      rows: [
        { lab: "Go to", text: "MQTT Discovery, then BACnet to MQTT Validation (Data Validation)." },
        { lab: "Do", text: "Match each BACnet source point to its published topic and compare value, unit and quality." },
        { lab: "See", text: "A pass / fail per point, with mismatched units and stale timestamps called out explicitly." },
      ],
    },
    {
      num: "4",
      title: "Generate the evidence pack",
      rows: [
        { lab: "Go to", text: "Reports once the validation run is green." },
        { lab: "Do", text: "Generate an evidence pack that bundles the scan, discovery and validation results for this run." },
        { lab: "See", text: "A signed, timestamped report you can hand to the client as proof of commissioning." },
      ],
    },
  ],
  designer: [
    {
      num: "1",
      title: "Load the intended points",
      rows: [
        { lab: "Go to", text: "Configuration and confirm the connection settings the field team scanned against." },
        { lab: "Do", text: "Cross-check the discovered estate against your design points list for this device family." },
        { lab: "See", text: "Which designed points actually exist on the controllers versus which were never wired." },
      ],
    },
    {
      num: "2",
      title: "Inspect object naming and units",
      rows: [
        { lab: "Go to", text: "BACnet Discovery and expand a device to its object list." },
        { lab: "Do", text: "Review object names, types and engineering units against the specification." },
        { lab: "See", text: "Naming drift and unit mismatches that would otherwise surface as cloud-side surprises." },
      ],
    },
    {
      num: "3",
      title: "Check the UDMI shape",
      rows: [
        { lab: "Go to", text: "UDMI Payload Workbench." },
        { lab: "Do", text: "Inspect state, metadata and pointset to confirm the points map to the agreed UDMI model." },
        { lab: "See", text: "Controlled-publish evidence that the device reports what your design said it should." },
      ],
    },
    {
      num: "4",
      title: "Sign off the design intent",
      rows: [
        { lab: "Go to", text: "Reports." },
        { lab: "Do", text: "Export a section report that ties discovered reality back to the design points list." },
        { lab: "See", text: "A handover artifact you can attach to the design package as as-built proof." },
      ],
    },
  ],
  pm: [
    {
      num: "1",
      title: "Open the multi-project view",
      rows: [
        { lab: "Go to", text: "Hub (Multi-Project Hub)." },
        { lab: "Do", text: "Filter runs by project, site and edge to see every commissioning run in one table." },
        { lab: "See", text: "Status, stage and health per run, attributed to the edge it came from." },
      ],
    },
    {
      num: "2",
      title: "Read a run in detail",
      rows: [
        { lab: "Go to", text: "Any run row and open its history." },
        { lab: "Do", text: "Follow the run from queued to succeeded or failed, stage by stage." },
        { lab: "See", text: "Exactly where a failed run stopped, so the conversation is about facts, not blame." },
      ],
    },
    {
      num: "3",
      title: "Check validation coverage",
      rows: [
        { lab: "Go to", text: "BACnet to MQTT Validation (Data Validation) for a flagged site." },
        { lab: "Do", text: "Review how many points passed protocol alignment and quality checks." },
        { lab: "See", text: "A defensible completion percentage instead of a verbal estimate." },
      ],
    },
    {
      num: "4",
      title: "Export status for the client",
      rows: [
        { lab: "Go to", text: "Reports." },
        { lab: "Do", text: "Generate an evidence pack and issue report for the period you are reporting on." },
        { lab: "See", text: "A document the client and contractor both accept as the source of truth." },
      ],
    },
  ],
  integration: [
    {
      num: "1",
      title: "Set the connection parameters",
      rows: [
        { lab: "Go to", text: "Configuration." },
        { lab: "Do", text: "Set BACnet, MQTT broker and UDMI endpoints, ports, credentials and secrets for the edge." },
        { lab: "See", text: "A single place where the bridge configuration lives, masked secrets included." },
      ],
    },
    {
      num: "2",
      title: "Confirm both ends discover",
      rows: [
        { lab: "Go to", text: "BACnet Discovery, then MQTT Discovery." },
        { lab: "Do", text: "Discover BACnet objects and the broker topics and payloads the bridge is publishing." },
        { lab: "See", text: "That each source point is producing a topic, and no orphan topics exist." },
      ],
    },
    {
      num: "3",
      title: "Validate the UDMI publish",
      rows: [
        { lab: "Go to", text: "UDMI Payload Workbench." },
        { lab: "Do", text: "Inspect pointset and metadata and trigger a controlled publish to capture evidence." },
        { lab: "See", text: "Whether the device reports state and pointset the way the cloud platform expects." },
      ],
    },
    {
      num: "4",
      title: "Close the loop end to end",
      rows: [
        { lab: "Go to", text: "BACnet to MQTT Validation (Data Validation), then Reports." },
        { lab: "Do", text: "Run the alignment comparison and export the result for the integration record." },
        { lab: "See", text: "A point-by-point bridge verification you can keep for the change record." },
      ],
    },
  ],
};

export function BriefPage() {
  const [tab, setTab] = useState<TabId>("basics");
  const [role, setRole] = useState<RoleId>("engineer");
  const [mode, setMode] = useState<"light" | "dark">(getTheme());

  const onToggleTheme = () => {
    toggleTheme();
    setMode(getTheme());
  };

  const activeRole = roles.find((r) => r.id === role) ?? roles[0];

  return (
    <div className="demo-shell">
      <header className="dc-header">
        <div className="dc-brand-bar">
          <div className="dc-brand">
            <img className="dc-brand-logo" src="/electracom-logo.png" alt="Electracom" />
            <span className="dc-brand-title">Smart Commissioning Tool</span>
            <span className="dc-brand-kind">Product Brief</span>
          </div>
          <div className="dc-header-actions">
            <button className="dc-ghost" onClick={onToggleTheme}>
              {mode === "dark" ? "☀ Light" : "☾ Dark"}
            </button>
            <Link className="dc-launch" to="/">
              <span className="dc-launch-dot" />
              Launch the App →
            </Link>
          </div>
        </div>
        <nav className="dc-tabs">
          {tabs.map((t) => (
            <button
              key={t.id}
              className={t.id === tab ? "dc-tab on" : "dc-tab"}
              onClick={() => setTab(t.id)}
            >
              <span className="dc-tab-num">{t.num}</span>
              <span className="dc-tab-label">{t.label}</span>
            </button>
          ))}
        </nav>
      </header>

      <main className="dc-panels">
        {tab === "basics" && (
          <section className="dc-panel">
            <div className="dc-intro">
              <p className="dc-kicker">The basics</p>
              <h1 className="dc-h1">What this tool is, and the job it does on site.</h1>
              <p className="dc-lead">
                A one-minute orientation for anyone walking onto a building management
                commissioning job. By the end of this page you will know what a point is,
                what a run is, and how the tool turns a cold network into proof.
              </p>
            </div>

            <div className="dc-hero">
              <p className="dc-hero-kicker">In one sentence</p>
              <p className="dc-hero-text">
                The Smart Commissioning Tool is a single operator console for discovering,
                validating and evidencing BMS points across BACnet, MQTT and UDMI on a
                commissioning job.
              </p>
            </div>

            <div className="dc-body">
              <h2>Why this tool exists</h2>
              <p>
                Commissioning a building management system usually means three disconnected
                worlds: a field controller talking BACnet, a broker talking MQTT, and a cloud
                platform expecting UDMI. The hard part is never one protocol on its own. It is
                proving that the same physical point reads the same value, with the same unit
                and the same quality, all the way from the field bus to the cloud. This tool
                puts that whole chain in one place so an engineer can see a break the moment it
                happens, instead of discovering it weeks later in a cloud dashboard.
              </p>

              <h2>The commissioning pipeline in 90 seconds</h2>
              <p>
                Every job follows the same shape. You discover what is on the network, inspect
                what each device and topic actually reports, validate that the source and the
                output agree, and then capture the result as evidence. The tool is organised
                around exactly those four moves, in that order.
              </p>

              <div className="dc-pipeline">
                <div className="dc-pipe-slot">
                  <span className="dc-pipe-label">Discover</span>
                  <span className="dc-pipe-val">IP, BACnet, MQTT</span>
                </div>
                <div className="dc-pipe-slot">
                  <span className="dc-pipe-label">Inspect</span>
                  <span className="dc-pipe-val">objects, topics, UDMI</span>
                </div>
                <div className="dc-pipe-slot">
                  <span className="dc-pipe-label">Validate</span>
                  <span className="dc-pipe-val">value, unit, quality</span>
                </div>
                <div className="dc-pipe-slot">
                  <span className="dc-pipe-label">Evidence</span>
                  <span className="dc-pipe-val">report &amp; sign-off</span>
                </div>
                <span className="dc-pipe-arrow">↓</span>
                <div className="dc-pipe-result">A signed evidence pack</div>
              </div>

              <p className="dc-note">
                You do not have to run the steps in one sitting. Each run is recorded, so you
                can scan today, validate tomorrow, and still produce one coherent evidence
                pack at the end.
              </p>
            </div>

            <div className="dc-cards">
              <div className="dc-card">
                <div className="dc-card-icon">📍</div>
                <div className="dc-card-title">A point</div>
                <p className="dc-card-desc">
                  One physical or logical signal — a zone temperature, a damper position, an
                  alarm. The whole job is making each point readable, correctly labelled and
                  consistent from the BACnet object through to the UDMI pointset.
                </p>
              </div>
              <div className="dc-card">
                <div className="dc-card-icon">🧾</div>
                <div className="dc-card-title">A run &amp; its evidence</div>
                <p className="dc-card-desc">
                  A run is one execution of a step — a scan, a discovery, a validation. Every
                  run is timestamped and attributed to an edge, so the evidence pack you export
                  is a defensible record rather than a screenshot.
                </p>
              </div>
            </div>

            <div className="dc-icon-rows">
              <div className="dc-icon-row">
                <span className="dc-icon-circle">🛠</span>
                <span>Commissioning Engineer — drives the job from cold network to sign-off.</span>
              </div>
              <div className="dc-icon-row">
                <span className="dc-icon-circle">📐</span>
                <span>BMS Designer — checks field reality against the design points list.</span>
              </div>
              <div className="dc-icon-row">
                <span className="dc-icon-circle">📋</span>
                <span>Project Manager — tracks runs across sites and exports status.</span>
              </div>
              <div className="dc-icon-row">
                <span className="dc-icon-circle">🔌</span>
                <span>Integration Engineer — verifies the BACnet to MQTT to UDMI bridge.</span>
              </div>
            </div>

            <div className="dc-stats">
              <div className="dc-stat">
                <span className="dc-stat-val">8</span>
                <span className="dc-stat-lab">operator modules</span>
              </div>
              <div className="dc-stat">
                <span className="dc-stat-val">3</span>
                <span className="dc-stat-lab">protocols: BACnet, MQTT, UDMI</span>
              </div>
              <div className="dc-stat">
                <span className="dc-stat-val">4</span>
                <span className="dc-stat-lab">pipeline stages</span>
              </div>
              <div className="dc-stat">
                <span className="dc-stat-val">3</span>
                <span className="dc-stat-lab">deployment profiles: standalone, edge, hub</span>
              </div>
              <div className="dc-stat">
                <span className="dc-stat-val">4</span>
                <span className="dc-stat-lab">access roles: viewer, reviewer, engineer, admin</span>
              </div>
              <div className="dc-stat">
                <span className="dc-stat-val">2</span>
                <span className="dc-stat-lab">evidence signatures: SHA-256 hash + Ed25519</span>
              </div>
            </div>

            <div className="dc-launch-zone">
              <p>Ready to start?</p>
              <Link className="dc-launch" to="/">
                <span className="dc-launch-dot" />
                Launch the App →
              </Link>
            </div>
          </section>
        )}

        {tab === "features" && (
          <section className="dc-panel">
            <div className="dc-intro">
              <p className="dc-kicker">Key features</p>
              <h1 className="dc-h1">Nine modules, one commissioning workflow.</h1>
              <p className="dc-lead">
                Each module owns one part of the pipeline. Together they take you from a
                connection string to a signed evidence pack without leaving the console.
              </p>
            </div>

            <div className="dc-cards">
              <div className="dc-card">
                <div className="dc-card-icon">⚙️</div>
                <div className="dc-card-title">Configuration</div>
                <p className="dc-card-desc">
                  Connection settings for BACnet, the MQTT broker and UDMI, with credentials
                  and ports kept in one place for the edge.
                </p>
                <ul className="dc-bullets">
                  <li>BACnet, MQTT and UDMI endpoints and ports</li>
                  <li>Credentials and secrets, masked in the UI</li>
                  <li>One source of truth for the bridge config</li>
                </ul>
              </div>

              <div className="dc-card">
                <div className="dc-card-icon">🌐</div>
                <div className="dc-card-title">IP Discovery (IP Scanner)</div>
                <p className="dc-card-desc">
                  Scan the site network to find what is reachable, what is missing and what
                  is unexpected before you touch a protocol.
                </p>
                <ul className="dc-bullets">
                  <li>Reachable, missing and unexpected hosts</li>
                  <li>Compare against the expected host list</li>
                  <li>Catch a dead controller in minutes</li>
                </ul>
              </div>

              <div className="dc-card">
                <div className="dc-card-icon">🔎</div>
                <div className="dc-card-title">BACnet Discovery</div>
                <p className="dc-card-desc">
                  Discover BACnet devices, enumerate their objects and read live property
                  values straight from the field bus.
                </p>
                <ul className="dc-bullets">
                  <li>Device and object enumeration</li>
                  <li>Live present-value, units and types</li>
                  <li>Proof the device is real, not cached</li>
                </ul>
              </div>

              <div className="dc-card">
                <div className="dc-card-icon">📡</div>
                <div className="dc-card-title">MQTT Discovery</div>
                <p className="dc-card-desc">
                  Inspect the broker: which topics exist, what payloads they carry and which
                  points have been extracted from them.
                </p>
                <ul className="dc-bullets">
                  <li>Browse live topics and payloads</li>
                  <li>See extracted points per topic</li>
                  <li>Spot orphan or duplicate topics</li>
                </ul>
              </div>

              <div className="dc-card">
                <div className="dc-card-icon">🧪</div>
                <div className="dc-card-title">UDMI Payload Workbench</div>
                <p className="dc-card-desc">
                  Inspect UDMI state, metadata and pointset, and capture controlled-publish
                  evidence the cloud platform will accept.
                </p>
                <ul className="dc-bullets">
                  <li>State, metadata and pointset views</li>
                  <li>Controlled-publish evidence capture</li>
                  <li>Confirm the agreed UDMI model</li>
                </ul>
              </div>

              <div className="dc-card">
                <div className="dc-card-icon">✅</div>
                <div className="dc-card-title">BACnet to MQTT Validation</div>
                <p className="dc-card-desc">
                  Compare point quality and protocol alignment between the BACnet source and
                  the MQTT/UDMI output, point by point.
                </p>
                <ul className="dc-bullets">
                  <li>Value, unit and quality comparison</li>
                  <li>Pass / fail per point</li>
                  <li>Stale timestamps flagged explicitly</li>
                </ul>
              </div>

              <div className="dc-card">
                <div className="dc-card-icon">📄</div>
                <div className="dc-card-title">Reports</div>
                <p className="dc-card-desc">
                  Generate evidence packs and issue reports that bundle scan, discovery and
                  validation results into a defensible artifact.
                </p>
                <ul className="dc-bullets">
                  <li>Signed, timestamped evidence packs</li>
                  <li>Issue reports for open defects</li>
                  <li>Client-ready handover documents</li>
                </ul>
              </div>

              <div className="dc-card">
                <div className="dc-card-icon">🗂</div>
                <div className="dc-card-title">Hub (Multi-Project Hub)</div>
                <p className="dc-card-desc">
                  Track commissioning runs across every project, site and edge from one
                  operator view, with status and health per run.
                </p>
                <ul className="dc-bullets">
                  <li>Cross-project, cross-site run table</li>
                  <li>Filter by project, site and edge</li>
                  <li>Honest per-run status and stage</li>
                </ul>
              </div>

              <div className="dc-card">
                <div className="dc-card-icon">👥</div>
                <div className="dc-card-title">Users</div>
                <p className="dc-card-desc">
                  Manage operator API keys and roles. Admin only — so access to the network
                  and the evidence stays accountable.
                </p>
                <ul className="dc-bullets">
                  <li>Operator API keys and roles</li>
                  <li>Admin-only access control</li>
                  <li>Accountable, attributable runs</li>
                </ul>
              </div>
            </div>

            <div className="dc-launch-zone">
              <p>See it on a real network.</p>
              <Link className="dc-launch" to="/">
                <span className="dc-launch-dot" />
                Launch the App →
              </Link>
            </div>
          </section>
        )}

        {tab === "reference" && (
          <section className="dc-panel">
            <div className="dc-intro">
              <p className="dc-kicker">Section reference</p>
              <h1 className="dc-h1">Every section, what it is for, and how to use it.</h1>
              <p className="dc-lead">
                A working reference for each module: what it does well, the gotcha to watch
                for, and a concrete thing to try the first time you open it.
              </p>
            </div>

            <div className="dc-ref">
              <div className="dc-ref-card">
                <div className="dc-ref-head">
                  <span className="dc-ref-title">Configuration</span>
                  <span className="dc-ref-role">for everyone</span>
                </div>
                <p className="dc-ref-lead">
                  Where the edge learns how to reach BACnet, the MQTT broker and UDMI.
                </p>
                <p className="dc-sub">What it does well</p>
                <ul className="dc-caps">
                  <li><strong>Centralises endpoints</strong> for all three protocols and their ports.</li>
                  <li><strong>Protects secrets</strong> by masking credentials in the interface.</li>
                  <li><strong>Feeds every other module</strong> so a scan and a validation agree on targets.</li>
                </ul>
                <div className="dc-callout warn">
                  <span className="dc-callout-icon">⚠</span>
                  <span>A wrong broker port here makes MQTT Discovery look empty. Confirm Configuration first when a module returns nothing.</span>
                </div>
                <p className="dc-try">
                  <span className="dc-try-lab">Try it yourself</span>
                  Enter the broker host and port, save, then open MQTT Discovery to confirm topics appear.
                </p>
              </div>

              <div className="dc-ref-card">
                <div className="dc-ref-head">
                  <span className="dc-ref-title">IP Discovery (IP Scanner)</span>
                  <span className="dc-ref-role">for everyone</span>
                </div>
                <p className="dc-ref-lead">
                  Proves the network before you blame a protocol.
                </p>
                <p className="dc-sub">What it does well</p>
                <ul className="dc-caps">
                  <li><strong>Finds reachable hosts</strong> across the commissioning subnet.</li>
                  <li><strong>Flags missing hosts</strong> from the expected list.</li>
                  <li><strong>Surfaces unexpected hosts</strong> that should not be on this segment.</li>
                </ul>
                <div className="dc-callout warn">
                  <span className="dc-callout-icon">⚠</span>
                  <span>A host that pings is not the same as a host talking BACnet. Reachable means routable, not commissioned.</span>
                </div>
                <p className="dc-try">
                  <span className="dc-try-lab">Try it yourself</span>
                  Scan the subnet and check whether every controller on your panel schedule shows as reachable.
                </p>
              </div>

              <div className="dc-ref-card">
                <div className="dc-ref-head">
                  <span className="dc-ref-title">BACnet Discovery</span>
                  <span className="dc-ref-role">for everyone</span>
                </div>
                <p className="dc-ref-lead">
                  Reads the field bus: devices, objects and live values.
                </p>
                <p className="dc-sub">What it does well</p>
                <ul className="dc-caps">
                  <li><strong>Enumerates devices and objects</strong> on the selected range.</li>
                  <li><strong>Reads live present-value</strong> with units and object types.</li>
                  <li><strong>Confirms the device is live</strong>, not a stale directory entry.</li>
                </ul>
                <p className="dc-try">
                  <span className="dc-try-lab">Try it yourself</span>
                  Discover one AHU controller, open its object list, and read a zone temperature live.
                </p>
              </div>

              <div className="dc-ref-card">
                <div className="dc-ref-head">
                  <span className="dc-ref-title">MQTT Discovery</span>
                  <span className="dc-ref-role">for everyone</span>
                </div>
                <p className="dc-ref-lead">
                  Looks at the broker the way the cloud will.
                </p>
                <p className="dc-sub">What it does well</p>
                <ul className="dc-caps">
                  <li><strong>Browses topics</strong> and their live payloads.</li>
                  <li><strong>Shows extracted points</strong> derived from each payload.</li>
                  <li><strong>Reveals orphans</strong> — topics with no source, or sources with no topic.</li>
                </ul>
                <div className="dc-callout warn">
                  <span className="dc-callout-icon">⚠</span>
                  <span>An empty topic list usually means a broker credential or port problem in Configuration, not a missing device.</span>
                </div>
                <p className="dc-try">
                  <span className="dc-try-lab">Try it yourself</span>
                  Subscribe to a known device topic and confirm the payload value matches the BACnet reading.
                </p>
              </div>

              <div className="dc-ref-card">
                <div className="dc-ref-head">
                  <span className="dc-ref-title">UDMI Payload Workbench</span>
                  <span className="dc-ref-role">for everyone</span>
                </div>
                <p className="dc-ref-lead">
                  Inspects the UDMI shape and captures publish evidence.
                </p>
                <p className="dc-sub">What it does well</p>
                <ul className="dc-caps">
                  <li><strong>Views state, metadata and pointset</strong> in one place.</li>
                  <li><strong>Captures controlled-publish evidence</strong> on demand.</li>
                  <li><strong>Confirms the model</strong> matches what the cloud platform expects.</li>
                </ul>
                <p className="dc-try">
                  <span className="dc-try-lab">Try it yourself</span>
                  Open a device pointset, trigger a controlled publish, and save the captured payload as evidence.
                </p>
              </div>

              <div className="dc-ref-card">
                <div className="dc-ref-head">
                  <span className="dc-ref-title">BACnet to MQTT Validation</span>
                  <span className="dc-ref-role">for everyone</span>
                </div>
                <p className="dc-ref-lead">
                  The judgement step: does the source match the output?
                </p>
                <p className="dc-sub">What it does well</p>
                <ul className="dc-caps">
                  <li><strong>Compares value, unit and quality</strong> between BACnet and MQTT/UDMI.</li>
                  <li><strong>Gives a pass or fail</strong> for every point.</li>
                  <li><strong>Flags stale data</strong> with explicit timestamps.</li>
                </ul>
                <div className="dc-callout warn">
                  <span className="dc-callout-icon">⚠</span>
                  <span>A value match with a unit mismatch is still a fail. Degrees C published as degrees F will read plausibly and be wrong.</span>
                </div>
                <p className="dc-try">
                  <span className="dc-try-lab">Try it yourself</span>
                  Run a validation on one device and open the first failed point to see exactly what diverged.
                </p>
              </div>

              <div className="dc-ref-card">
                <div className="dc-ref-head">
                  <span className="dc-ref-title">Reports</span>
                  <span className="dc-ref-role">for everyone</span>
                </div>
                <p className="dc-ref-lead">
                  Turns runs into a defensible handover artifact.
                </p>
                <p className="dc-sub">What it does well</p>
                <ul className="dc-caps">
                  <li><strong>Bundles runs</strong> — scan, discovery and validation — into one pack.</li>
                  <li><strong>Issues defect reports</strong> for anything still failing.</li>
                  <li><strong>Timestamps and signs</strong> so the document stands up later.</li>
                </ul>
                <p className="dc-try">
                  <span className="dc-try-lab">Try it yourself</span>
                  Generate an evidence pack for a green validation run and review what it includes before sending it.
                </p>
              </div>

              <div className="dc-ref-card">
                <div className="dc-ref-head">
                  <span className="dc-ref-title">Hub (Multi-Project Hub)</span>
                  <span className="dc-ref-role">for everyone</span>
                </div>
                <p className="dc-ref-lead">
                  One operator view across every project, site and edge.
                </p>
                <p className="dc-sub">What it does well</p>
                <ul className="dc-caps">
                  <li><strong>Lists every run</strong> in a single cross-project table.</li>
                  <li><strong>Filters by project, site and edge</strong> to narrow the view.</li>
                  <li><strong>Shows honest status and stage</strong>, attributed to its edge.</li>
                </ul>
                <p className="dc-try">
                  <span className="dc-try-lab">Try it yourself</span>
                  Filter the Hub to one site and read the most recent failed run to see where it stopped.
                </p>
              </div>

              <div className="dc-ref-card">
                <div className="dc-ref-head">
                  <span className="dc-ref-title">Users</span>
                  <span className="dc-ref-role">admin only</span>
                </div>
                <p className="dc-ref-lead">
                  Keeps access to the network and the evidence accountable.
                </p>
                <p className="dc-sub">What it does well</p>
                <ul className="dc-caps">
                  <li><strong>Manages operator API keys</strong> for each engineer.</li>
                  <li><strong>Assigns roles</strong> so permissions match responsibility.</li>
                  <li><strong>Attributes runs</strong> to a key, keeping the record honest.</li>
                </ul>
                <div className="dc-callout warn">
                  <span className="dc-callout-icon">⚠</span>
                  <span>Only admins see this section. Rotate a key the moment a contractor leaves the job.</span>
                </div>
                <p className="dc-try">
                  <span className="dc-try-lab">Try it yourself</span>
                  As an admin, issue a scoped key for a visiting integrator and remove it when their work is signed off.
                </p>
              </div>
            </div>

            <div className="dc-launch-zone">
              <p>Open a module and follow along.</p>
              <Link className="dc-launch" to="/">
                <span className="dc-launch-dot" />
                Launch the App →
              </Link>
            </div>
          </section>
        )}

        {tab === "tour" && (
          <section className="dc-panel">
            <div className="dc-intro">
              <p className="dc-kicker">Guided tour</p>
              <h1 className="dc-h1">Pick your role and walk a real job.</h1>
              <p className="dc-lead">
                Choose who you are on site. We will lay out the four moves that matter most
                for your role, grounded in the actual modules.
              </p>
            </div>

            <div className="dc-role-picker">
              <span className="dc-role-picker-label">I am a…</span>
              <div className="dc-role-pills">
                {roles.map((r) => (
                  <button
                    key={r.id}
                    className={r.id === role ? "dc-role-pill active" : "dc-role-pill"}
                    onClick={() => setRole(r.id)}
                  >
                    <span className="dc-role-pill-icon">{r.icon}</span>
                    {r.pill}
                  </button>
                ))}
              </div>
              <Link className="dc-launch" to="/">
                <span className="dc-launch-dot" />
                Launch with this tour →
              </Link>
            </div>

            <div className="dc-tour-card">
              <div className="dc-tour-header">
                <span className="dc-tour-icon">{activeRole.icon}</span>
                <span className="dc-tour-title">{activeRole.title}</span>
                <p className="dc-tour-lead">{activeRole.lead}</p>
              </div>

              <div className="dc-walk">
                {walks[role].map((step) => (
                  <div className="dc-walk-step" key={step.num}>
                    <div className="dc-walk-head">
                      <span className="dc-walk-num">{step.num}</span>
                      <span className="dc-walk-title">{step.title}</span>
                    </div>
                    <div className="dc-walk-body">
                      {step.rows.map((row, i) => (
                        <div className="dc-walk-row" key={i}>
                          <span className="dc-walk-lab">{row.lab}</span>
                          <span>{row.text}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>

              <div className="dc-tour-launch">
                <Link className="dc-launch" to="/">
                  <span className="dc-launch-dot" />
                  Launch the App →
                </Link>
              </div>
            </div>
          </section>
        )}
      </main>

      <footer className="dc-footer">
        <Link className="dc-footer-link" to="/learning">
          Want a deeper walk? Read the full course →
        </Link>
        <p>Electracom · Smart Commissioning · Product Brief</p>
      </footer>
    </div>
  );
}
