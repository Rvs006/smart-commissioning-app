import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router/dom";
import { reloadOnceForStaleBuild } from "./app/staleBuild";
import { RouteLoadingFallback } from "./app/RouteLoadingFallback";
import { router } from "./app/routes";
import { SessionProvider } from "./app/session";
import { initTheme } from "./app/theme";
import "./styles.css";
import "./styles/electracom-theme.css";

initTheme();

// Vite fires this when a lazy chunk or its CSS preload 404s (stale build).
// Reload once for the current index.html; a repeat failure reaches the route
// errorElement instead.
window.addEventListener("vite:preloadError", (event) => {
  if (reloadOnceForStaleBuild()) {
    event.preventDefault();
  }
});

const queryClient = new QueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <SessionProvider>
        <React.Suspense fallback={<RouteLoadingFallback />}>
          <RouterProvider router={router} />
        </React.Suspense>
      </SessionProvider>
    </QueryClientProvider>
  </React.StrictMode>
);

