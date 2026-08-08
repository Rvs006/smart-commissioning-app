import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EvoObservatoryPage } from "./EvoObservatoryPage";

const overview = {
  workspace_status: "ready",
  protected_release: "v0.1.40",
  protected_commit: "b3b2f764b78449dae2f5232cd7aab1f2d47c30eb",
  target: "core/smart_commissioning_core/udmi_validation.py",
  metric: "Correct findings first",
  baseline: {
    id: "exp_0000", parent_id: null, status: "committed", score: 0.91,
    correctness: 1, duration_seconds: 4.2, peak_memory_mb: 88,
    change: "Protected baseline", finding: "All gates passed.",
  },
  experiments: [{
    id: "exp_0001", parent_id: "exp_0000", status: "committed", score: 0.94,
    correctness: 1, duration_seconds: 3.6, peak_memory_mb: 80,
    change: "Cache compiled schemas", finding: "Runtime fell without changed findings.",
  }],
  selected_experiment_id: "exp_0001",
  updated_at: "2026-08-08T12:00:00Z",
};

describe("EvoObservatoryPage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(overview), { status: 200, headers: { "Content-Type": "application/json" } })));
  });

  it("shows release protection and lets the operator compare experiments", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><EvoObservatoryPage /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Evo Observatory" })).toBeInTheDocument();
    expect(screen.getByText("v0.1.40")).toBeInTheDocument();
    expect(screen.getAllByText("Cache compiled schemas")).toHaveLength(2);
    expect(screen.getByText(/Runtime fell without changed findings/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /exp_0000/i }));
    expect(screen.getByText("4.20s")).toBeInTheDocument();
  });
});
