import { createHashRouter } from "react-router";
import { App } from "./App";
import { RouteLoadingFallback } from "./RouteLoadingFallback";

async function loadBriefPage() {
  const { BriefPage } = await import("../features/brief/BriefPage");
  return { Component: BriefPage };
}

async function loadLearningPage() {
  const { LearningPage } = await import("../features/learning/LearningPage");
  return { Component: LearningPage };
}

async function loadDashboardPage() {
  const { DashboardPage } = await import("../features/workflow/DashboardPage");
  return { Component: DashboardPage };
}

async function loadConfigurationPage() {
  const { ConfigurationPage } = await import("../features/workflow/ConfigurationPage");
  return { Component: ConfigurationPage };
}

function loadModulePage(moduleRoute: string) {
  return async () => {
    const { ModulePage } = await import("../features/workflow/ModulePage");
    return { element: <ModulePage moduleRoute={moduleRoute} /> };
  };
}

async function loadHubPage() {
  const { HubPage } = await import("../features/workflow/HubPage");
  return { Component: HubPage };
}

async function loadRunHistoryPage() {
  const { RunHistoryPage } = await import("../features/workflow/RunHistoryPage");
  return { Component: RunHistoryPage };
}

async function loadUsersPage() {
  const { UsersPage } = await import("../features/workflow/UsersPage");
  return { Component: UsersPage };
}

export const router = createHashRouter([
  // Standalone product surfaces (own demo-shell header), mirroring the
  // Electracom reference's Product Brief + Course. "Launch the App" enters the
  // console layout below.
  {
    path: "brief",
    lazy: loadBriefPage,
    hydrateFallbackElement: <RouteLoadingFallback />,
  },
  {
    path: "learning",
    lazy: loadLearningPage,
    hydrateFallbackElement: <RouteLoadingFallback />,
  },
  {
    path: "/",
    element: <App />,
    hydrateFallbackElement: <RouteLoadingFallback />,
    children: [
      { index: true, lazy: loadDashboardPage },
      { path: "configuration", lazy: loadConfigurationPage },
      { path: "ip-scanner", lazy: loadModulePage("ip-scanner") },
      // Built-in TCP/Nmap engine, off the primary nav but reachable by direct
      // URL. The sidecar module owns /ip-scanner (see moduleData.ts).
      { path: "ip-scanner-sct", lazy: loadModulePage("ip-scanner-sct") },
      { path: "bacnet-discovery", lazy: loadModulePage("bacnet-discovery") },
      { path: "mqtt-discovery", lazy: loadModulePage("mqtt-discovery") },
      { path: "udmi-validation", lazy: loadModulePage("udmi-validation") },
      { path: "data-validation", lazy: loadModulePage("data-validation") },
      { path: "reports", lazy: loadModulePage("reports") },
      { path: "hub", lazy: loadHubPage },
      { path: "run-history", lazy: loadRunHistoryPage },
      { path: "users", lazy: loadUsersPage },
    ],
  },
]);
