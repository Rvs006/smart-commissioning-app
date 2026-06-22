import { useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
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
      { label: "BACnet", to: "/bacnet-discovery" },
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
  { stage: "Operate", items: [{ label: "Hub", to: "/hub" }] },
];

const pageTitles: Record<string, string> = {
  "/": "Homepage",
  "/bacnet-discovery": "BACnet Discovery",
  "/configuration": "Configuration",
  "/data-validation": "BACnet to MQTT Validation",
  "/hub": "Multi-Project Hub",
  "/ip-scanner": "IP Scanner",
  "/mqtt-discovery": "MQTT Discovery",
  "/reports": "Reports",
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
          <NavLink className="app-brand" to="/">
            <img className="brand-logo" src="/electracom-logo.png" alt="Electracom" />
            <span className="app-brand-divider" />
            <span className="app-brand-text">
              <span className="app-brand-title">Smart Commissioning Tool</span>
              <span className="app-brand-kind">Commissioning workspace</span>
            </span>
          </NavLink>

          <div className="app-header-meta">
            <Link className="header-pill" to="/brief">
              Brief
            </Link>
            <Link className="header-pill" to="/learning">
              Learning
            </Link>
            <ThemeToggle />
            <span className="site-pill">
              <span className="site-pill-dot" />
              Block B Plantroom
            </span>
            <span className="site-pill subtle">API workspace</span>
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
          <em className={`role-chip role-${role ?? "unknown"}`}>{role}</em>
        </span>
        <button className="link-button" onClick={signOut} type="button">
          Sign out
        </button>
      </span>
    );
  }

  if (hasApiKey && isLoading) {
    return <span className="site-pill subtle">Identifying...</span>;
  }

  // A key is set but /me failed (e.g. invalid/inactive key -> 401). Offer to
  // clear it and re-enter, rather than leaving the operator stuck.
  if (hasApiKey && error) {
    return (
      <span className="session-badge">
        <span className="session-badge-id error-text">Key not recognised</span>
        <button className="link-button" onClick={signOut} type="button">
          Clear key
        </button>
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
