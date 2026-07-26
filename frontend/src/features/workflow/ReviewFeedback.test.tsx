import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { ReviewFeedback } from "./ReviewFeedback";

function renderFeedback(route: string) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <ReviewFeedback />
    </MemoryRouter>,
  );
}

describe("ReviewFeedback", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it.each([
    ["/hub", "Hub"],
    ["/run-history", "Run History"],
    ["/users", "Users"],
  ])("maps %s to the %s review module", (route, expectedModule) => {
    renderFeedback(route);
    fireEvent.click(screen.getByRole("button", { name: /Review Comments/i }));

    expect(screen.getByLabelText("Module")).toHaveValue(expectedModule);
  });

  it("keeps comments in memory and explains when storage reads are blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Storage blocked", "SecurityError");
    });

    renderFeedback("/hub");
    fireEvent.click(screen.getByRole("button", { name: /Review Comments/i }));

    expect(screen.getByRole("status")).toHaveTextContent(
      "Browser storage is unavailable. Review comments will stay in this session only.",
    );
    expect(screen.getByRole("textbox", { name: "Short title" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Comment"), {
      target: { value: "The run filter needs a clearer empty state." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add comment" }));

    expect(screen.getAllByText("The run filter needs a clearer empty state.")).toHaveLength(2);
    expect(
      screen.getByText("Comment kept for this session; browser storage is unavailable."),
    ).toBeInTheDocument();
  });

  it("stays usable and reports when storage writes fail", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Storage full", "QuotaExceededError");
    });

    renderFeedback("/users");
    fireEvent.click(screen.getByRole("button", { name: /Review Comments/i }));

    expect(screen.getByRole("status")).toHaveTextContent(
      "Browser storage is unavailable. Review comments will stay in this session only.",
    );
    expect(screen.getByLabelText("Module")).toHaveValue("Users");
  });
});
