import { NavLink, Outlet, useLocation } from "react-router-dom";
import { ReviewFeedback } from "../features/workflow/ReviewFeedback";

const navItems = [
  { label: "Homepage", number: "01", to: "/" },
  { label: "Configuration", number: "02", to: "/configuration" },
  { label: "IP Discovery", number: "03", to: "/ip-scanner" },
  { label: "BACnet", number: "04", to: "/bacnet-discovery" },
  { label: "MQTT Discovery", number: "05", to: "/mqtt-discovery" },
  { label: "UDMI Workbench", number: "06", to: "/udmi-validation" },
  { label: "Validation", number: "07", to: "/data-validation" },
  { label: "Reports", number: "08", to: "/reports" },
];

const pageTitles: Record<string, string> = {
  "/": "Homepage",
  "/bacnet-discovery": "BACnet Discovery",
  "/configuration": "Configuration",
  "/data-validation": "Data Validation",
  "/ip-scanner": "IP Scanner",
  "/mqtt-discovery": "MQTT Discovery",
  "/reports": "Reports",
  "/udmi-validation": "UDMI Payload Workbench",
};

const pageSubtitles: Record<string, string> = {
  "/": "Start here, review site readiness, then choose the next commissioning action.",
  "/bacnet-discovery": "Review discovered devices, objects and live properties.",
  "/configuration": "Keep connection settings focused and safe to edit.",
  "/data-validation": "Compare point quality and protocol alignment.",
  "/ip-scanner": "Find reachable, missing and unexpected hosts.",
  "/mqtt-discovery": "Inspect broker topics, payloads and extracted points.",
  "/reports": "Generate evidence packs and issue reports.",
  "/udmi-validation": "Inspect state, metadata, pointset, and controlled publish evidence in detail.",
};

export function App() {
  const location = useLocation();
  const pageTitle = pageTitles[location.pathname] ?? "Workspace";
  const pageSubtitle = pageSubtitles[location.pathname] ?? "Commissioning workflow.";

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
          </div>
        </div>

        <nav className="app-tabs" aria-label="Commissioning modules">
          {navItems.map((item) => (
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

      <ReviewFeedback />
    </div>
  );
}
