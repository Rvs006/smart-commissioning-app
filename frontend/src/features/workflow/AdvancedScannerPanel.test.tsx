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

  it("drops to the Retry error state when the sidecar dies after the panel is ready", async () => {
    // BF-INTEGRATION-2: preflight succeeds and the tool embeds, then the sidecar
    // goes down. The iframe reloads the proxy's 503; the onLoad health recheck must
    // surface the recoverable error + Retry, not leave a ready-looking panel.
    let outage = false;
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        url.endsWith("/api/health") && outage ? { ok: false, status: 503 } : { ok: true },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<AdvancedScannerPanel proto="ip" projectId="p" siteId="s" />);
    const frame = await screen.findByTitle("IP advanced scanner");
    outage = true; // sidecar dies while the panel is up
    fireEvent.load(frame); // iframe (re)loads the 503 -> onLoad rechecks health
    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByTitle("IP advanced scanner")).toBeNull();
  });

  it("clears a pending write confirmation when the scanner context changes", async () => {
    // Switching IP -> BACnet must not leave the old IP write dialog up: it would
    // otherwise be confirmable against the now-active BACnet sidecar.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true }));
    const { rerender } = render(<AdvancedScannerPanel proto="ip" projectId="p" siteId="s" />);
    const frame = await screen.findByTitle("IP advanced scanner");
    // The message handler only trusts messages from our own iframe's window.
    Object.defineProperty(frame, "contentWindow", { value: window, configurable: true });
    fireEvent(
      window,
      new MessageEvent("message", {
        source: window as Window,
        data: { type: "sct-write-request", id: "w1", method: "PUT", path: "/points/1", body: "{}" },
      }),
    );
    expect(
      await screen.findByRole("dialog", { name: /confirm device write/i }),
    ).toBeInTheDocument();
    // Same component instance, new scanner (mirrors ModulePage's route switch).
    rerender(<AdvancedScannerPanel proto="bacnet" projectId="p" siteId="s" />);
    expect(await screen.findByTitle("BACnet advanced scanner")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /confirm device write/i })).toBeNull();
  });

  it("does not send a stale write decision or token to the newly switched scanner", async () => {
    // REV: a write confirm started on IP must not post its id + token to the
    // BACnet iframe when the user switches scanners before the confirm resolves.
    let releaseConfirm: (v: unknown) => void = () => {};
    const fetchMock = vi.fn((url: string) =>
      url.endsWith("/confirm-write")
        ? new Promise((res) => {
            releaseConfirm = res as (v: unknown) => void;
          })
        : Promise.resolve({ ok: true }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = render(<AdvancedScannerPanel proto="ip" projectId="p" siteId="s" />);

    const ipFrame = await screen.findByTitle("IP advanced scanner");
    const ipWindow = { postMessage: vi.fn() };
    Object.defineProperty(ipFrame, "contentWindow", { value: ipWindow, configurable: true });
    // Raise a write request from the IP iframe (source must be our own contentWindow).
    fireEvent(
      window,
      new MessageEvent("message", {
        source: ipWindow as unknown as Window,
        data: { type: "sct-write-request", id: "w-ip", method: "PUT", path: "/points/1", body: "{}" },
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Send write" }));

    // Switch to BACnet (same instance) while the confirm is still hanging.
    rerender(<AdvancedScannerPanel proto="bacnet" projectId="p" siteId="s" />);
    const bacnetFrame = await screen.findByTitle("BACnet advanced scanner");
    const bacnetWindow = { postMessage: vi.fn() };
    Object.defineProperty(bacnetFrame, "contentWindow", { value: bacnetWindow, configurable: true });

    // Release the original IP confirm: it now resolves with a token.
    releaseConfirm({ ok: true, json: async () => ({ token: "tok-ip" }) });
    await new Promise((r) => setTimeout(r, 0));

    // The old IP write decision must not have crossed into the BACnet scanner.
    expect(bacnetWindow.postMessage).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: "sct-write-decision" }),
      expect.anything(),
    );
  });

  it("keeps the switched-to scanner ready when a stale health recheck from the old one fails", async () => {
    // BF-RACE-1: an IP iframe (re)load whose health recheck rejects AFTER the
    // panel switched to BACnet must not overwrite the ready BACnet panel with the
    // stale IP error.
    let ipHealthCalls = 0;
    let releaseIpRecheck: (v: unknown) => void = () => {};
    const hungIpRecheck = new Promise((res) => {
      releaseIpRecheck = res as (v: unknown) => void;
    });
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/v1/scanners/ip/raw/api/health") {
        ipHealthCalls += 1;
        // 1st call = preflight (ok); 2nd = the onLoad recheck we hang, then fail.
        return ipHealthCalls >= 2 ? hungIpRecheck : Promise.resolve({ ok: true });
      }
      return Promise.resolve({ ok: true }); // sessions + BACnet health
    });
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = render(<AdvancedScannerPanel proto="ip" projectId="p" siteId="s" />);

    const ipFrame = await screen.findByTitle("IP advanced scanner");
    fireEvent.load(ipFrame); // onLoad -> recheckHealth(ip), hangs on the 2nd health call

    // Switch to BACnet; it loads and becomes ready while the IP recheck is pending.
    rerender(<AdvancedScannerPanel proto="bacnet" projectId="p" siteId="s" />);
    expect(await screen.findByTitle("BACnet advanced scanner")).toBeInTheDocument();

    // Now the stale IP recheck fails.
    releaseIpRecheck({ ok: false, status: 503 });
    await new Promise((r) => setTimeout(r, 0));

    // The BACnet panel stays; the stale IP error was dropped.
    expect(screen.getByTitle("BACnet advanced scanner")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
