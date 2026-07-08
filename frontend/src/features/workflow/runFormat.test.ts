import { describe, expect, it } from "vitest";
import { formatAbsoluteTime, formatDuration } from "./runFormat";

// Unit tests for the two Run History formatters. Both are pure and guard against
// missing/unparseable input honestly rather than fabricating a value.

describe("formatAbsoluteTime", () => {
  it("renders an absolute timestamp via the platform Intl API", () => {
    const iso = "2026-06-11T09:00:00Z";
    expect(formatAbsoluteTime(iso)).toBe(new Date(iso).toLocaleString());
  });

  it("returns a dash for missing input", () => {
    expect(formatAbsoluteTime(undefined)).toBe("—");
  });

  it("returns the raw string for unparseable input", () => {
    expect(formatAbsoluteTime("not-a-date")).toBe("not-a-date");
  });
});

describe("formatDuration", () => {
  it("formats a sub-minute delta in seconds", () => {
    expect(formatDuration("2026-06-11T09:00:00Z", "2026-06-11T09:00:45Z")).toBe("45s");
  });

  it("formats whole minutes without a seconds suffix", () => {
    expect(formatDuration("2026-06-11T09:00:00Z", "2026-06-11T09:05:00Z")).toBe("5m");
  });

  it("formats minutes and seconds", () => {
    expect(formatDuration("2026-06-11T09:00:00Z", "2026-06-11T09:01:05Z")).toBe("1m 5s");
  });

  it("formats hours and minutes", () => {
    expect(formatDuration("2026-06-11T09:00:00Z", "2026-06-11T11:30:00Z")).toBe("2h 30m");
  });

  it("returns a dash for missing, unparseable, or negative deltas", () => {
    expect(formatDuration(undefined, "2026-06-11T09:00:00Z")).toBe("—");
    expect(formatDuration("2026-06-11T09:00:00Z", "nope")).toBe("—");
    expect(formatDuration("2026-06-11T09:05:00Z", "2026-06-11T09:00:00Z")).toBe("—");
  });
});
