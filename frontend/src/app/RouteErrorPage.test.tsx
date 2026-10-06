import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { RouteErrorPage } from "./RouteErrorPage";
import { isStaleBuildError, reloadOnceForStaleBuild } from "./staleBuild";

const STALE_CHUNK_MESSAGE =
  "Failed to fetch dynamically imported module: http://127.0.0.1:8000/assets/IpScannerPage-old.js";

function renderFailingRoute(error: Error) {
  const router = createMemoryRouter(
    [
      {
        path: "/",
        errorElement: <RouteErrorPage />,
        lazy: async () => {
          throw error;
        },
      },
    ],
    { initialEntries: ["/"] },
  );
  render(<RouterProvider router={router} />);
}

describe("RouteErrorPage", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("recognises stale-build chunk failures across browsers", () => {
    expect(isStaleBuildError(new TypeError(STALE_CHUNK_MESSAGE))).toBe(true);
    expect(isStaleBuildError(new TypeError("Importing a module script failed."))).toBe(true);
    expect(isStaleBuildError(new Error("error loading dynamically imported module"))).toBe(true);
    expect(isStaleBuildError(new Error("Cannot read properties of undefined"))).toBe(false);
    expect(isStaleBuildError(undefined)).toBe(false);
  });

  it("reloads once, then refuses inside the window so it cannot loop", () => {
    const reload = vi.fn();
    expect(reloadOnceForStaleBuild(reload)).toBe(true);
    expect(reloadOnceForStaleBuild(reload)).toBe(false);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("shows a reload panel when a stale chunk still fails after the one reload", async () => {
    // A reload already happened moments ago, so the page must not reload again.
    window.sessionStorage.setItem("sct-stale-build-reload-at", String(Date.now()));
    renderFailingRoute(new TypeError(STALE_CHUNK_MESSAGE));

    expect(await screen.findByRole("alert")).toHaveTextContent("This page did not load");
    expect(screen.getByRole("alert")).toHaveTextContent("Close any older Smart Commissioning window");
    expect(screen.getByRole("button", { name: "Reload" })).toBeVisible();
  });

  it("shows the real message for errors that are not stale chunks", async () => {
    renderFailingRoute(new Error("Boom in loader"));

    expect(await screen.findByRole("alert")).toHaveTextContent("Boom in loader");
    expect(window.sessionStorage.getItem("sct-stale-build-reload-at")).toBeNull();
  });
});
