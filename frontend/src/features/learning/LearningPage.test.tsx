import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { LearningPage } from "./LearningPage";

// The Learning page is fully static (no fetch): a MemoryRouter wrapper is all
// the <Link>s need.
function renderLearning() {
  return render(
    <MemoryRouter>
      <LearningPage />
    </MemoryRouter>,
  );
}

describe("LearningPage — Installation & Setup", () => {
  it("shows the setup section with the portable steps and no Docker path", () => {
    renderLearning();

    expect(screen.getByText("Installation & Setup")).toBeInTheDocument();

    // Portable steps are visible: the exe name renders as a code chip.
    expect(screen.getAllByText("SmartCommissioningApp.exe").length).toBeGreaterThan(0);
    expect(screen.getByText("You are already signed in")).toBeInTheDocument();

    // The single-entry install picker is hidden, and Docker is gone entirely.
    expect(screen.queryByRole("button", { name: /Docker/i })).not.toBeInTheDocument();
  });

  it("purges the Docker path and names the SHA-256 allow-listing flow instead", () => {
    renderLearning();

    // No Docker container instructions survive anywhere on the page.
    expect(screen.queryByText(/docker compose/i)).not.toBeInTheDocument();
    expect(screen.queryByText("./scripts/bootstrap-env.ps1")).not.toBeInTheDocument();

    // The locked-down-laptop note now describes the IT hash-approval flow.
    expect(screen.getByText(/Get-FileHash/)).toBeInTheDocument();
    expect(screen.getByText(/SHA-256/)).toBeInTheDocument();
  });

  it("always shows the shared first-run steps", () => {
    renderLearning();

    expect(screen.getByText("Source Interface")).toBeInTheDocument();
    expect(screen.getByText(/no packets are sent and no authorization is needed/)).toBeInTheDocument();
  });
});

describe("LearningPage — role walkthroughs", () => {
  it("renders the Commissioning Engineer path by default and swaps on role change", () => {
    renderLearning();

    expect(
      screen.getByRole("button", { name: /Commissioning Engineer/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("Capture the proof, not screenshots")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /BMS Designer/ }));

    expect(screen.getByText("Inspect the UDMI metadata and pointset")).toBeInTheDocument();
    expect(screen.queryByText("Capture the proof, not screenshots")).not.toBeInTheDocument();
  });
});
