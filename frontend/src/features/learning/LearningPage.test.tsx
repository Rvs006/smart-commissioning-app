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
  it("shows the setup section with the Windows portable path selected by default", () => {
    renderLearning();

    expect(screen.getByText("Installation & Setup")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Windows portable app/ }),
    ).toBeInTheDocument();

    // Portable steps are visible: the exe name renders as a code chip.
    expect(screen.getAllByText("SmartCommissioningApp.exe").length).toBeGreaterThan(0);
    expect(screen.getByText("You are already signed in")).toBeInTheDocument();
  });

  it("switches to the Docker path: bootstrap script, compose command and Set API key appear, exe steps go", () => {
    renderLearning();

    fireEvent.click(screen.getByRole("button", { name: /Docker Desktop/ }));

    expect(screen.getByText("./scripts/bootstrap-env.ps1")).toBeInTheDocument();
    expect(
      screen.getByText(/docker compose -f infra\/docker-compose\.yml --env-file infra\/\.env up/),
    ).toBeInTheDocument();
    expect(screen.getByText("Set API key")).toBeInTheDocument();
    expect(screen.queryByText("SmartCommissioningApp.exe")).not.toBeInTheDocument();
  });

  it("always shows the shared first-run steps", () => {
    renderLearning();

    expect(screen.getByText("Source Interface")).toBeInTheDocument();
    expect(screen.getByText(/no packets are sent and no authorization is needed/)).toBeInTheDocument();

    // Still there after switching install path.
    fireEvent.click(screen.getByRole("button", { name: /Docker Desktop/ }));
    expect(screen.getByText("Source Interface")).toBeInTheDocument();
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
