import { useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { ReviewFeedback } from "../features/workflow/ReviewFeedback";
import { useSession } from "./sessionContext";

const navItems = [
  { label: "Homepage", number: "01", to: "/" },
  { label: "Configuration", number: "02", to: "/configuration" },
  { label: "IP Discovery", number: "03", to: "/ip-scanner" },
  { label: "BACnet", number: "04", to: "/bacnet-discovery" },
  { label: "MQTT Discovery", number: "05", to: "/mqtt-discovery" },
  { label: "UDMI Workbench", number: "06", to: "/udmi-validation" },
  { label: "BACnet to MQTT Validation", number: "07", to: "/data-validation" },
  { label: "Reports", number: "08", to: "/reports" },
  { label: "Hub", number: "09", to: "/hub" },
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

  // The Users tab is admin-only; everyone else never sees the entry (the route
  // itself stays admin-gated server-side, so this is a UX nicety, not security).
  const tabs = canAdmin
    ? [...navItems, { label: "Users", number: "10", to: "/users" }]
    : navItems;

  return (
    <div className="console-shell">
      <header className="app-header">
        <div className="app-brand-bar">
          <NavLink className="app-brand" to="/">
            <span className="app-brand-title">Smart Commissioning Tool</span>
            <span className="app-brand-kind">Commissioning workspace</span>
          </NavLink>

          <div className="app-header-meta">
            <span className="site-pill">
              <span className="site-pill-dot" />
              Block B Plantroom
            </span>
            <span className="site-pill subtle">API workspace</span>
            <SessionBadge />
          </div>
        </div>

        <nav className="app-tabs" aria-label="Commissioning modules">
          {tabs.map((item) => (
            <NavLink
              className={({ isActive }) => `app-tab${isActive ? " active" : ""}`}
              end={item.to === "/"}
              key={item.to}
              to={item.to}
            >
              <span className="app-tab-num">{item.number}</span>
              <span className="app-tab-label">{item.label}</span>
            </NavLink>
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

      {/* The dry-run review widget is a development-only tool; it must not ship
          to end users in a production build. */}
      {import.meta.env.DEV ? <ReviewFeedback /> : null}
    </div>
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
