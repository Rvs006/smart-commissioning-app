import { formatApiDetail } from "./client";

describe("formatApiDetail", () => {
  it("returns string details unchanged", () => {
    expect(formatApiDetail("Broker unreachable.")).toBe("Broker unreachable.");
  });

  it("stringifies primitive details", () => {
    expect(formatApiDetail(404)).toBe("404");
    expect(formatApiDetail(false)).toBe("false");
  });

  it("formats FastAPI validation errors as location-prefixed messages", () => {
    const detail = {
      loc: ["body", "mqtt", "Broker Port"],
      msg: "value is not a valid integer",
      type: "type_error.integer",
    };
    expect(formatApiDetail(detail)).toBe("mqtt.Broker Port: value is not a valid integer");
  });

  it("falls back to a generic message for null or undefined details", () => {
    expect(formatApiDetail(null)).toBe("Unknown API error.");
    expect(formatApiDetail(undefined)).toBe("Unknown API error.");
  });
});
