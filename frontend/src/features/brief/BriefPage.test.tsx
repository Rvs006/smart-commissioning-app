import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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
});
