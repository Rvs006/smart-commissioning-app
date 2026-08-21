import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AdvancedScannerPanel } from "./AdvancedScannerPanel";

describe("AdvancedScannerPanel", () => {
  it("embeds the proxied standalone app with a trailing-slash src", () => {
    render(<AdvancedScannerPanel proto="ip" />);
    // Trailing slash matters: the app's relative asset/api URLs resolve under it.
    expect(screen.getByTitle("IP advanced scanner")).toHaveAttribute("src", "/api/v1/scanners/ip/raw/");
  });

  it("labels bacnet and mqtt panels", () => {
    const { rerender } = render(<AdvancedScannerPanel proto="bacnet" />);
    expect(screen.getByTitle("BACnet advanced scanner")).toHaveAttribute(
      "src",
      "/api/v1/scanners/bacnet/raw/",
    );
    rerender(<AdvancedScannerPanel proto="mqtt" />);
    expect(screen.getByTitle("MQTT advanced scanner")).toHaveAttribute("src", "/api/v1/scanners/mqtt/raw/");
  });
});
