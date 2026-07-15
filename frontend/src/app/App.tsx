import { useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { isAuthRejection } from "../api/client";
import { ReviewFeedback } from "../features/workflow/ReviewFeedback";
import { useSession } from "./sessionContext";
import { getTheme, toggleTheme, type ThemeMode } from "./theme";

// Navigation grouped by the commissioning workflow stage (Configure → Discover
// → Validate → Report → Operate) instead of a flat list of equal tabs, so the
// nav reflects the order of the job. Home stays a standalone entry.
type NavGroup = { stage: string | null; items: { label: string; to: string }[] };

const NAV_GROUPS: NavGroup[] = [
  { stage: null, items: [{ label: "Home", to: "/" }] },
  { stage: "Configure", items: [{ label: "Configuration", to: "/configuration" }] },
  {
    stage: "Discover",
    items: [
      { label: "IP Discovery", to: "/ip-scanner" },
      { label: "BACnet Discovery", to: "/bacnet-discovery" },
      { label: "MQTT Discovery", to: "/mqtt-discovery" },
    ],
  },
  {
    stage: "Validate",
    items: [
      { label: "UDMI Workbench", to: "/udmi-validation" },
      { label: "BACnet to MQTT Validation", to: "/data-validation" },
    ],
  },
  { stage: "Report", items: [{ label: "Reports", to: "/reports" }] },
  {
    stage: "Operate",
    items: [
      { label: "Hub", to: "/hub" },
      { label: "Run History", to: "/run-history" },
    ],
  },
];

const pageTitles: Record<string, string> = {
  "/": "Homepage",
  "/bacnet-discovery": "BACnet Discovery",
  "/configuration": "Configuration",
  "/data-validation": "BACnet to MQTT Validation",
  "/hub": "Multi-Project Hub",
  "/ip-scanner": "IP Discovery",
  "/mqtt-discovery": "MQTT Discovery",
  "/reports": "Reports",
  "/run-history": "Run History",
  "/udmi-validation": "UDMI Payload Workbench",
  "/users": "User Management",
};

const pageSubtitles: Record<string, string> = {
  "/": "Start here, review site readiness, then choose the next commissioning action.",
  "/bacnet-discovery": "Review discovered devices, objects and live properties.",
  "/configuration": "Keep connection settings focused and safe to edit.",
  "/data-validation": "Compare point quality and protocol alignment.",
  "/hub": "Track runs across every project, site, and edge from one operator view.",
  "/ip-scanner": "Find reachable, missing and unexpected hosts.",
  "/mqtt-discovery": "Inspect broker topics, payloads and extracted points.",
  "/reports": "Generate evidence packs and issue reports.",
  "/run-history": "Browse, sort, filter, and export every recorded run.",
  "/udmi-validation": "Inspect state, metadata, pointset, and controlled publish evidence in detail.",
  "/users": "Create, list, and manage operator API keys and roles.",
};

export function App() {
  const location = useLocation();
  const { canAdmin } = useSession();
  const pageTitle = pageTitles[location.pathname] ?? "Workspace";
  const pageSubtitle = pageSubtitles[location.pathname] ?? "Commissioning workflow.";

  // The Users entry is admin-only; everyone else never sees it (the route itself
  // stays admin-gated server-side, so this is a UX nicety, not security). It
  // lives in the Operate group alongside the Hub.
  const navGroups: NavGroup[] = canAdmin
    ? NAV_GROUPS.map((group) =>
        group.stage === "Operate"
          ? { ...group, items: [...group.items, { label: "Users", to: "/users" }] }
          : group,
      )
    : NAV_GROUPS;

  return (
    <div className="console-shell">
      <header className="app-header">
        <div className="app-brand-bar">
          <NavLink className="app-brand" title="Go to the Homepage" to="/">
            <img className="brand-logo" src="/electracom-logo.png" alt="Electracom" />
            <span className="app-brand-divider" />
            <span className="app-brand-text">
              <span className="app-brand-title">Smart Commissioning Tool</span>
              <span className="app-brand-kind">Commissioning workspace</span>
            </span>
          </NavLink>

          <div className="app-header-meta">
            {/* Build-stamped release version. build.ps1 sets VITE_APP_VERSION before
                `npm run build`, so a release bundle reads "v0.1.11" and an unstamped
                CI bundle reads its git-describe string; dev servers and the test run
                read "dev". Kept inline (not a module const) so it is read per render,
                and `||` not `??` so an empty-string env var falls back rather than
                rendering a blank pill. */}
            <span className="site-pill subtle" title="App version">
              {import.meta.env.VITE_APP_VERSION || "dev"}
            </span>
            <Link
              className="header-pill"
              title="Product brief — what the tool is and how it works"
              to="/brief"
            >
              Brief
            </Link>
            <Link
              className="header-pill"
              title="Role-based learning walkthroughs"
              to="/learning"
            >
              Learning
            </Link>
            <ThemeToggle />
            <span className="site-pill" title="Active commissioning site">
              <span className="site-pill-dot" />
              Block B Plantroom
            </span>
            <span className="site-pill subtle" title="Connected to the backend API workspace">
              API workspace
            </span>
            <SessionBadge />
          </div>
        </div>

        <nav className="app-tabs grouped" aria-label="Commissioning modules">
          {navGroups.map((group) => (
            <div className="nav-group" key={group.stage ?? "home"}>
              {group.stage ? <span className="nav-group-label">{group.stage}</span> : null}
              <div className="nav-group-items">
                {group.items.map((item) => (
                  <NavLink
                    className={({ isActive }) => `app-tab${isActive ? " active" : ""}`}
                    end={item.to === "/"}
                    key={item.to}
                    to={item.to}
                  >
                    <span className="app-tab-label">{item.label}</span>
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>
      </header>

      <section className="workspace-shell">
        <header className="page-titlebar">
          <div>
            <span className="eyebrow">Smart Commissioning</span>
            <h1>{pageTitle}</h1>
            <p>{pageSubtitle}</p>
          </div>
        </header>

        <main className="page-frame">
          <Outlet />
        </main>
      </section>

      {/* Engineer review-comments widget. Faithful to the standalone HTML review
          build, it ships in every build so pilot engineers can capture feedback
          from the packaged/hosted app — not just `npm run dev`. Comments stay in
          the browser (localStorage) until exported, so it carries no backend risk.
          Set VITE_REVIEW_COMMENTS=false at build time to drop it from a GA build. */}
      {import.meta.env.VITE_REVIEW_COMMENTS !== "false" ? <ReviewFeedback /> : null}
    </div>
  );
}

// Light/dark switch. Mirrors the reference app's theme toggle; the warm token
// palette in electracom-theme.css keys off the `data-theme` attribute.
function ThemeToggle() {
  const [mode, setMode] = useState<ThemeMode>(() => getTheme());

  return (
    <button
      aria-label="Toggle colour theme"
      className="header-pill"
      onClick={() => setMode(toggleTheme())}
      title="Toggle light / dark"
      type="button"
    >
      {mode === "dark" ? "☀ Light" : "☾ Dark"}
    </button>
  );
}

// Current-user indicator + key entry. When a key is set and /me resolves it
// shows the username + role and a sign-out that clears the key. When no key is
// set (or the key is invalid) it offers a small key field. No password flow —
// auth is key-based, exactly as the existing localStorage 'sc.apiKey' path.
function SessionBadge() {
  const { me, role, isLoading, error, hasApiKey, signIn, signOut } = useSession();
  const [keyDraft, setKeyDraft] = useState("");
  const [open, setOpen] = useState(false);

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    signIn(keyDraft);
    setKeyDraft("");
    setOpen(false);
  };

  if (hasApiKey && me) {
    return (
      <span className="session-badge" title={`Signed in via ${me.source}`}>
        <span className="session-badge-id">
          <strong>{me.username}</strong>
          <em className={`role-chip role-${role ?? "unknown"}`} title={`Your access role: ${role}`}>
            {role}
          </em>
        </span>
        <button className="link-button" onClick={signOut} type="button">
          Sign out
        </button>
      </span>
    );
  }

  // Keyless local/portable profile: the backend already granted this loopback
  // client a role with no key involved, so offering "Set API key" reads as a
  // bogus requirement (a field user hit exactly this). State the signed-in
  // fact instead — there is no key to set, clear, or sign out of in this mode.
  if (!hasApiKey && me?.source === "local") {
    return (
      <span
        className="session-badge"
        title="Local mode: the app trusts this laptop's own browser. No API key exists or is needed."
      >
        <span className="session-badge-id">
          <strong>Signed in as local {role}</strong>
        </span>
      </span>
    );
  }

  if (hasApiKey && isLoading) {
    return <span className="site-pill subtle">Identifying...</span>;
  }

  // A key is set but /me failed. Only a real auth REJECTION (401/403) means
  // the key itself was refused — offer to clear it and re-enter rather than
  // leaving the operator stuck. Every other failure (server restarting, Wi-Fi
  // blip on a multi-homed laptop, 5xx) says nothing about the key: keys are
  // displayed once and cannot be retrieved, so never offer to destroy one on a
  // transport error. The session provider re-checks /me on an interval, so the
  // transient state clears itself once the server is reachable again.
  if (hasApiKey && error) {
    if (isAuthRejection(error)) {
      return (
        <span className="session-badge">
          <span className="session-badge-id error-text">Key not recognised</span>
          <button className="link-button" onClick={signOut} type="button">
            Clear key
          </button>
        </span>
      );
    }
    return (
      <span
        className="session-badge"
        title="The server could not be reached to verify your API key. The key is kept and will be re-checked automatically."
      >
        <span className="session-badge-id">Server unreachable — retrying</span>
      </span>
    );
  }

  if (!open) {
    return (
      <button className="site-pill subtle" onClick={() => setOpen(true)} type="button">
        Set API key
      </button>
    );
  }

  return (
    <form className="session-key-form" onSubmit={handleSubmit}>
      <input
        aria-label="API key"
        autoFocus
        onChange={(event) => setKeyDraft(event.target.value)}
        placeholder="Paste API key"
        type="password"
        value={keyDraft}
      />
      <button className="secondary-button compact" type="submit">
        Save
      </button>
    </form>
  );
}
