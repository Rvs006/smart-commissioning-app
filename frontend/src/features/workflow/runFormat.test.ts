import { describe, expect, it } from "vitest";
import { formatAbsoluteTime, formatDuration, formatRunProgress, runPollInterval } from "./runFormat";

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

describe("formatRunProgress (mid-run device progress)", () => {
  it("formats devices done/total with an optional points-read count", () => {
    expect(
      formatRunProgress({ progress: { devices_done: 3, devices_total: 41, points_read: 128 } }),
    ).toBe("3 of 41 devices · 128 points read");
    expect(formatRunProgress({ progress: { devices_done: 0, devices_total: 41 } })).toBe(
      "0 of 41 devices",
    );
  });

  it("returns null when the progress object is absent or incomplete, so the monitor shows nothing", () => {
    expect(formatRunProgress(undefined)).toBeNull();
    expect(formatRunProgress({})).toBeNull();
    expect(formatRunProgress({ progress: { devices_done: 3 } })).toBeNull();
  });
});

describe("runPollInterval (SSE-vs-poll handoff)", () => {
  it("keeps polling when an SSE stream is OPEN but has not delivered a frame yet", () => {
    // The regression: a stream that connects but never emits (buffering proxy,
    // half-open socket) must not pause the poll — sseDriving is false until a
    // frame arrives, so the monitor keeps refetching and never freezes on the
    // mount-time snapshot.
    expect(
      runPollInterval({ reachedTerminal: false, recordTerminal: false, sseDriving: false }),
    ).toBe(1500);
  });

  it("keeps a slow poll while SSE drives so result_summary (the X-of-Y device row) stays fresh", () => {
    // The SSE frame carries no result_summary, so pausing the poll entirely froze
    // the device-progress row at the mount-time snapshot for the whole run. A slow
    // record poll under SSE keeps that row advancing.
    expect(
      runPollInterval({ reachedTerminal: false, recordTerminal: false, sseDriving: true }),
    ).toBe(4000);
  });

  it("stops polling once the run is terminal by record or by SSE terminal frame", () => {
    expect(
      runPollInterval({ reachedTerminal: false, recordTerminal: true, sseDriving: false }),
    ).toBe(false);
    expect(
      runPollInterval({ reachedTerminal: true, recordTerminal: false, sseDriving: false }),
    ).toBe(false);
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
