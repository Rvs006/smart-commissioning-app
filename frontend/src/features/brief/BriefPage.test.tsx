import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { BriefPage } from "./BriefPage";

function renderBrief() {
  return render(
    <MemoryRouter>
      <BriefPage />
    </MemoryRouter>,
  );
}

describe("BriefPage", () => {
  it("exposes the active section and role through pressed-button state", () => {
    renderBrief();

    const basics = screen.getByRole("button", { name: /Basics/i });
    const guidedTour = screen.getByRole("button", { name: /Guided Tour/i });
    expect(basics).toHaveAttribute("aria-pressed", "true");
    expect(guidedTour).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(guidedTour);

    expect(basics).toHaveAttribute("aria-pressed", "false");
    expect(guidedTour).toHaveAttribute("aria-pressed", "true");

    const engineer = screen.getByRole("button", { name: /Commissioning Engineer/i });
    const designer = screen.getByRole("button", { name: /BMS Designer/i });
    expect(engineer).toHaveAttribute("aria-pressed", "true");
    expect(designer).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(designer);

    expect(engineer).toHaveAttribute("aria-pressed", "false");
    expect(designer).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("For a BMS Designer")).toBeInTheDocument();
  });

  it("explains the fixed-profile Nmap flow in IP Discovery", () => {
    renderBrief();

    fireEvent.click(screen.getByRole("button", { name: /Key Features/i }));

    expect(screen.getByText(/approved detected local Nmap/)).toBeInTheDocument();
    expect(screen.getByText(/without filling in Nmap setup fields/)).toBeInTheDocument();
  });

  it("explains the protected preview, authorization, live-run, and retry flow", () => {
    renderBrief();

    expect(
      screen.getByRole("heading", { name: "How a protected discovery run works" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/creates a scan plan without network I\/O/i)).toBeInTheDocument();
    expect(screen.getByText(/completed and sealed preview/i)).toBeInTheDocument();
    expect(screen.getByText(/matching sealed preview authorization/i)).toBeInTheDocument();
    expect(screen.getByText(/dropdown stays unavailable while the preview is sealing/i)).toBeInTheDocument();
    expect(
      screen.getByText(/report generation is available for the selected run as soon as/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/may fail to refresh without removing the report controls/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/same scan can be retried safely/i)).toBeInTheDocument();
    expect(screen.getByText(/list is already limited to the displayed preview/i)).toBeInTheDocument();
    expect(screen.getByText(/authorization ID supplied in the approval record/i)).toBeInTheDocument();
    expect(screen.getByText(/uses consumed and maximum uses as used\/max/i)).toBeInTheDocument();
    expect(screen.getByText(/page creates a fresh request key for every button press/i)).toBeInTheDocument();
    expect(screen.queryByText(/ticket, purpose, expiry/i)).not.toBeInTheDocument();
  });

  it("keeps operational language user-facing", () => {
    renderBrief();

    expect(screen.queryByText(/epoch/i)).not.toBeInTheDocument();
  });
});
