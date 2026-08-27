import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AdvancedScannerPanel } from "./AdvancedScannerPanel";

describe("AdvancedScannerPanel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("opens a panel session and probes health, then embeds the proxied app with a trailing-slash src", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
    render(<AdvancedScannerPanel proto="ip" projectId="p" siteId="s" />);
    // Trailing slash matters: the app's relative asset/api URLs resolve under it.
    expect(await screen.findByTitle("IP advanced scanner")).toHaveAttribute(
      "src",
      "/api/v1/scanners/ip/raw/",
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/scanners/ip/raw/session",
      expect.objectContaining({ method: "POST" }),
    );
    // The sidecar is probed before the iframe is shown, so a 503 never reaches it.
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/scanners/ip/raw/api/health", expect.anything());
  });

  it("surfaces an error with Retry when the session request fails (never a ready-looking iframe)", async () => {
    // BF-FAIL-2: a failed session must not render the tool as if it were ready.
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(<AdvancedScannerPanel proto="mqtt" projectId="p" siteId="s" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not start/i);
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByTitle("MQTT advanced scanner")).toBeNull();
  });

  it("surfaces an error when the scanner sidecar is unavailable", async () => {
    // Session opens, but the sidecar health probe returns 503 - show an error
    // instead of an iframe that would load the 503 page with no recovery.
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true }) // session
      .mockResolvedValueOnce({ ok: false, status: 503 }); // health
    vi.stubGlobal("fetch", fetchMock);
    render(<AdvancedScannerPanel proto="ip" projectId="p" siteId="s" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
    expect(screen.queryByTitle("IP advanced scanner")).toBeNull();
  });

  it("recovers to the embedded tool when Retry succeeds", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline")) // attempt 1: session fails
      .mockResolvedValue({ ok: true }); // retry: session + health ok
    vi.stubGlobal("fetch", fetchMock);
    render(<AdvancedScannerPanel proto="ip" projectId="p" siteId="s" />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByTitle("IP advanced scanner")).toBeInTheDocument();
  });
});
