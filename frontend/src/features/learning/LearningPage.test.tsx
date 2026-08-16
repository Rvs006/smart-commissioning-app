import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
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
    expect(screen.getByText("Smart_Commissioning_App_Windows_Portable.zip")).toBeInTheDocument();
    expect(screen.queryByText("SmartCommissioningApp_Windows_Portable.zip")).not.toBeInTheDocument();
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
    expect(screen.getByText("Use Nmap without configuring it")).toBeInTheDocument();
    expect(screen.getByText(/no fields for Nmap paths, flags, scripts, or commands/)).toBeInTheDocument();
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

describe("LearningPage — operator guides", () => {
  it("documents the complete IP preview and live-scan procedure", () => {
    renderLearning();

    expect(screen.getByRole("heading", { name: "Run an IP discovery" })).toBeInTheDocument();
    expect(screen.getByText(/Configure the targets and TCP ports/i)).toBeInTheDocument();
    expect(screen.getByText(/Wait until the preview succeeds and finishes sealing/i)).toBeInTheDocument();
    expect(
      screen.getByText(/An administrator must create and approve the scan authorization/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/If you are not an administrator, ask one to approve it/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/select the approved authorization already listed/i)).toBeInTheDocument();
    expect(screen.getByText(/authorization ID supplied in the approval record/i)).toBeInTheDocument();
    expect(screen.getByText(/used\/max counter shows uses consumed and maximum uses/i)).toBeInTheDocument();
    expect(screen.getByText(/limited the choices to the displayed sealed preview/i)).toBeInTheDocument();
    expect(screen.getByText(/Wait for confirmed terminal completion/i)).toBeInTheDocument();
    expect(screen.getByText(/Open Results, then generate the required report/i)).toBeInTheDocument();
  });

  it("explains equivalent retries and genuine idempotency conflicts", () => {
    renderLearning();

    expect(screen.getByRole("heading", { name: "Retry an IP discovery safely" })).toBeInTheDocument();
    expect(screen.getByText(/check Run History before pressing Preview or Run again/i)).toBeInTheDocument();
    expect(screen.getByText(/Same-key replay is an API or automatic transport behaviour/i)).toBeInTheDocument();
    expect(screen.getByText(/reordered ports describe the same scan/i)).toBeInTheDocument();
    expect(screen.getByText(/omitted default values describe the same scan/i)).toBeInTheDocument();
    expect(screen.getByText(/page creates a fresh request key for each button press/i)).toBeInTheDocument();
    expect(screen.getByText(/genuinely different request still conflicts/i)).toBeInTheDocument();
    expect(screen.getByText(/page supplies a fresh key when you press Run/i)).toBeInTheDocument();
    expect(screen.getByText(/API integrations must supply their own new key/i)).toBeInTheDocument();
    expect(screen.queryByText(/Keep the same request key/i)).not.toBeInTheDocument();
  });

  it("distinguishes workflow phases from persisted run statuses", () => {
    renderLearning();

    expect(screen.getByRole("heading", { name: "Workflow phases" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Persisted run statuses" })).toBeInTheDocument();
    expect(screen.getByText(/separate from the stored run status shown in Run History/i)).toBeInTheDocument();

    for (const phase of [
      "Preview",
      "Sealing",
      "Authorization pending",
      "Accepted by API",
      "Running",
      "Engine complete",
      "Final evidence is synchronising",
      "Results ready",
      "Failed or rejected",
      "Final evidence unavailable",
    ]) {
      expect(screen.getAllByText(phase, { exact: true }).length).toBeGreaterThan(0);
    }

    for (const status of ["Queued", "Running", "Succeeded", "Failed", "Cancelled"]) {
      expect(screen.getAllByText(status, { exact: true }).length).toBeGreaterThan(0);
    }
  });

  it("documents BACnet, MQTT, UDMI, and Nmap boundaries", () => {
    const { container } = renderLearning();

    expect(screen.getByText(/does not add BACnet write capability/i)).toBeInTheDocument();
    expect(screen.getByText(/blank Payload type is valid/i)).toBeInTheDocument();
    expect(screen.getByText(/belongs in a topic or topic-filter field/i)).toBeInTheDocument();
    expect(container).toHaveTextContent("# belongs in a topic or topic-filter field");
    expect(container).not.toHaveTextContent("#belongs");
    expect(screen.getByText(/does not bundle, install, or download Nmap or Npcap/i)).toBeInTheDocument();
    expect(screen.getByText(/retry and preview\/live fixes are unrelated to Nmap/i)).toBeInTheDocument();
  });

  it("documents report timing, exports, stale-run protection, and troubleshooting", () => {
    renderLearning();

    expect(screen.getByText(/Validation JSON/i)).toBeInTheDocument();
    expect(screen.getByText(/MQTT XLSX/i)).toBeInTheDocument();
    expect(screen.getByText(/stale export cannot finish against another scan/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Troubleshooting" })).toBeInTheDocument();

    for (const problem of [
      "Empty authorization dropdown",
      "Preview still sealing",
      "Live Run button disabled",
      "Results not ready",
      "Terminal discovery results not ready",
      "Nmap unavailable",
      "Authorization mismatch",
      "Idempotency conflict",
      "Scan accepted but still running",
      "Evidence synchronization still pending",
      "Failed scan or rejected authorization",
    ]) {
      expect(screen.getByText(problem, { exact: true })).toBeInTheDocument();
    }
    expect(screen.getByText(/If Final evidence unavailable appears, read the stated cause and rerun only/i)).toBeInTheDocument();
  });
});
