import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AdvancedScannerPanel } from "./AdvancedScannerPanel";

describe("AdvancedScannerPanel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("opens a panel session, then embeds the proxied app with a trailing-slash src", async () => {
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
  });

  it("still renders the tool when the session request fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(<AdvancedScannerPanel proto="mqtt" projectId="p" siteId="s" />);
    expect(await screen.findByTitle("MQTT advanced scanner")).toHaveAttribute(
      "src",
      "/api/v1/scanners/mqtt/raw/",
    );
  });
});
