import { render, screen } from "@testing-library/react";
import { RouteLoadingFallback } from "./RouteLoadingFallback";
import { router } from "./routes";

describe("application routes", () => {
  it("keeps the App shell eager and lazy-loads every page route", () => {
    const shellRoute = router.routes.find((route) => route.path === "/");
    expect(shellRoute).toBeDefined();
    expect(shellRoute?.lazy).toBeUndefined();

    for (const path of ["brief", "learning"]) {
      const route = router.routes.find((candidate) => candidate.path === path);
      expect(route?.lazy).toEqual(expect.any(Function));
    }

    const childRoutes = shellRoute?.children ?? [];
    const indexRoute = childRoutes.find((route) => route.index === true);
    expect(indexRoute?.lazy).toEqual(expect.any(Function));

    for (const path of [
      "configuration",
      "ip-scanner",
      "bacnet-discovery",
      "mqtt-discovery",
      "udmi-validation",
      "data-validation",
      "reports",
      "hub",
      "run-history",
      "users",
    ]) {
      const route = childRoutes.find((candidate) => candidate.path === path);
      expect(route?.lazy).toEqual(expect.any(Function));
    }
  });

  it("renders a visible static status while a cold route loads", () => {
    render(<RouteLoadingFallback />);

    expect(screen.getByRole("status")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Loading Smart Commissioning...");
    expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
  });
});
