import { createHashRouter } from "react-router-dom";
import { App } from "./App";
import { ConfigurationPage } from "../features/workflow/ConfigurationPage";
import { DashboardPage } from "../features/workflow/DashboardPage";
import { HubPage } from "../features/workflow/HubPage";
import { ModulePage } from "../features/workflow/ModulePage";
import { UsersPage } from "../features/workflow/UsersPage";
import { BriefPage } from "../features/brief/BriefPage";
import { LearningPage } from "../features/learning/LearningPage";

export const router = createHashRouter([
  // Standalone product surfaces (own demo-shell header), mirroring the
  // Electracom reference's Product Brief + Course. "Launch the App" enters the
  // console layout below.
  { path: "brief", element: <BriefPage /> },
  { path: "learning", element: <LearningPage /> },
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: "configuration", element: <ConfigurationPage /> },
      { path: "ip-scanner", element: <ModulePage moduleRoute="ip-scanner" /> },
      { path: "bacnet-discovery", element: <ModulePage moduleRoute="bacnet-discovery" /> },
      { path: "mqtt-discovery", element: <ModulePage moduleRoute="mqtt-discovery" /> },
      { path: "udmi-validation", element: <ModulePage moduleRoute="udmi-validation" /> },
      { path: "data-validation", element: <ModulePage moduleRoute="data-validation" /> },
      { path: "reports", element: <ModulePage moduleRoute="reports" /> },
      { path: "hub", element: <HubPage /> },
      { path: "users", element: <UsersPage /> }
    ]
  }
]);
