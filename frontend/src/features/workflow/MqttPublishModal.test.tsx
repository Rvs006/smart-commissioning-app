import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../api/client", () => ({
  createScanAuthorization: vi.fn(),
  getValidationRun: vi.fn(),
  startAuthorizedMqttPublish: vi.fn(),
  startDirectMqttPublish: vi.fn(),
  startMqttPublishPreview: vi.fn(),
}));

import {
  createScanAuthorization,
  getValidationRun,
  startAuthorizedMqttPublish,
  startDirectMqttPublish,
  startMqttPublishPreview,
} from "../../api/client";
import { MqttPublishModal } from "./MqttPublishModal";

const workspace = { projectId: "p", siteId: "s" };

const previewRun = {
  run_id: "prev1",
  status: "succeeded",
  result_summary: {
    dry_run_plan: {
      targets: "site/ahu-1/cmd",
      payload_sha256: "abc123",
      payload_bytes: 8,
      qos: 0,
      retain: false,
      broker_host: "broker.example.local",
      broker_port: 8883,
      use_tls: true,
    },
  },
};

// Two native clicks inside ONE act() block: React has not committed `busy`
// between them, so the button's `disabled` attribute is still stale when the
// second lands. This is the real-world fast double-click the guard exists for;
// RTL's fireEvent act-wraps each call and would commit in between, which is
// exactly why a fireEvent-based version of this test passes even unguarded.
async function doubleClick(element: HTMLElement): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

function fillCompose(): void {
  fireEvent.change(screen.getByLabelText("Topic"), { target: { value: "site/ahu-1/cmd" } });
  fireEvent.change(screen.getByLabelText("Payload"), { target: { value: '{"cmd":1}' } });
}

async function reachApprovedPreview(): Promise<void> {
  vi.mocked(startMqttPublishPreview).mockResolvedValue({ run_id: "prev1" } as never);
  vi.mocked(getValidationRun).mockImplementation((runId: string) =>
    Promise.resolve((runId === "prev1" ? previewRun : { run_id: runId }) as never),
  );
  vi.mocked(createScanAuthorization).mockResolvedValue({ authorization_id: "auth1" } as never);

  render(<MqttPublishModal onClose={() => {}} workspace={workspace} />);
  fillCompose();
  fireEvent.click(screen.getByRole("button", { name: /Preview/ }));
  await screen.findByText(/nothing has been sent/i);

  fireEvent.change(screen.getByLabelText("Change ticket"), { target: { value: "CHG-1" } });
  fireEvent.change(screen.getByLabelText("Purpose"), { target: { value: "commissioning" } });
  fireEvent.click(screen.getByRole("button", { name: /Approve this exact message/ }));
  await screen.findByText(/Authorization auth1 is ready/);
}

describe("MqttPublishModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("previews, seals the exact bytes, and shows the payload digest — nothing sent yet", async () => {
    vi.mocked(startMqttPublishPreview).mockResolvedValue({ run_id: "prev1" } as never);
    vi.mocked(getValidationRun).mockResolvedValue(previewRun as never);

    render(<MqttPublishModal onClose={() => {}} workspace={workspace} />);
    fillCompose();
    fireEvent.click(screen.getByRole("button", { name: /Preview — nothing is sent/ }));

    await screen.findByText("abc123");
    // The preview only ran a dry preview; no authorized publish was issued.
    expect(startAuthorizedMqttPublish).not.toHaveBeenCalled();
    expect(startMqttPublishPreview).toHaveBeenCalledTimes(1);
  });

  it("runs the full ceremony and reports an honest sidecar acceptance", async () => {
    await reachApprovedPreview();
    vi.mocked(startAuthorizedMqttPublish).mockResolvedValue({ run_id: "send1" } as never);
    vi.mocked(getValidationRun).mockImplementation((runId: string) =>
      Promise.resolve(
        (runId === "send1"
          ? {
              run_id: "send1",
              status: "succeeded",
              result_summary: { publish: { topic: "site/ahu-1/cmd", authorized_by: "admin", accepted_by_sidecar: true, delivery_confirmed: false } },
            }
          : previewRun) as never,
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: /Send to live equipment/ }));

    await screen.findByText(/Sent/);
    expect(screen.getByText(/not a broker delivery acknowledgement/i)).toBeInTheDocument();
    // The frozen preview run id is what got replayed, with the one-use authorization.
    expect(startAuthorizedMqttPublish).toHaveBeenCalledWith(
      expect.objectContaining({ previewRunId: "prev1", scanAuthorizationId: "auth1" }),
    );
  });

  it("frictionless mode sends directly with no preview or approval", async () => {
    vi.mocked(startDirectMqttPublish).mockResolvedValue({ run_id: "send1" } as never);
    vi.mocked(getValidationRun).mockResolvedValue({
      run_id: "send1",
      status: "succeeded",
      result_summary: { publish: { topic: "site/ahu-1/cmd", authorized_by: "shared-key", accepted_by_sidecar: true, delivery_confirmed: false } },
    } as never);

    render(<MqttPublishModal authorizationEnforced={false} onClose={() => {}} workspace={workspace} />);
    fillCompose();
    // No preview button in frictionless mode; a direct Send instead.
    expect(screen.queryByRole("button", { name: /Preview/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Send to live equipment/ }));

    // There is no approver here, so the operator confirms the exact write first.
    expect(screen.getByText("Confirm the write")).toBeInTheDocument();
    expect(startDirectMqttPublish).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Send to device/ }));

    await screen.findByText(/Sent/);
    expect(startDirectMqttPublish).toHaveBeenCalledWith(
      expect.objectContaining({ topic: "site/ahu-1/cmd", payload: '{"cmd":1}' }),
    );
    // The sealed preview path was never touched.
    expect(startMqttPublishPreview).not.toHaveBeenCalled();
    expect(createScanAuthorization).not.toHaveBeenCalled();
  });

  it("frictionless confirm shows the exact write and cancelling sends nothing", async () => {
    render(<MqttPublishModal authorizationEnforced={false} onClose={() => {}} workspace={workspace} />);
    fillCompose();
    fireEvent.change(screen.getByLabelText("QoS"), { target: { value: "1" } });
    fireEvent.click(screen.getByLabelText("Retain"));
    fireEvent.click(screen.getByRole("button", { name: /Send to live equipment/ }));

    // Topic, QoS/retain and the payload are all on screen before anything goes out.
    expect(screen.getByText("site/ahu-1/cmd")).toBeInTheDocument();
    expect(screen.getByText("1 / retained")).toBeInTheDocument();
    expect(screen.getByText('{"cmd":1}')).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    // Back to compose with the message intact, and not one byte published.
    expect(startDirectMqttPublish).not.toHaveBeenCalled();
    expect(screen.queryByText("Confirm the write")).toBeNull();
    expect(screen.getByLabelText("Topic")).toHaveValue("site/ahu-1/cmd");
  });

  it("confirm step is an alertdialog and puts focus on Cancel, not Send", async () => {
    render(<MqttPublishModal authorizationEnforced={false} onClose={() => {}} workspace={workspace} />);
    fillCompose();
    fireEvent.click(screen.getByRole("button", { name: /Send to live equipment/ }));

    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveAccessibleName("Confirm the write");
    // A stray Enter or Space must not be the keystroke that writes to equipment.
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("a poll timeout leaves the accepted run named instead of a live Send button", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(startDirectMqttPublish).mockResolvedValue({ run_id: "send-slow" } as never);
      // Never terminal: pollRun exhausts its attempts and times out.
      vi.mocked(getValidationRun).mockResolvedValue({ run_id: "send-slow", status: "running" } as never);

      render(<MqttPublishModal authorizationEnforced={false} onClose={() => {}} workspace={workspace} />);
      fillCompose();
      fireEvent.click(screen.getByRole("button", { name: /Send to live equipment/ }));
      fireEvent.click(screen.getByRole("button", { name: /Send to device/ }));
      // pollRun gives up after 80 attempts spaced 500ms apart; act() flushes the
      // state updates the timeout path schedules once the timers have run.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(80 * 500 + 1000);
      });

      // The publish was accepted, so the dialog must not offer a second send.
      expect(screen.getByText("Still running")).toBeInTheDocument();
      expect(screen.getByText(/run send-slow/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Send to device/ })).toBeNull();
      expect(screen.queryByText("Not sent")).toBeNull();
      expect(startDirectMqttPublish).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  // `busy` is React state, so it is not applied until React commits: before the
  // synchronous guard, two clicks in the same tick both reached the network and
  // the backend made a run for each, publishing twice to live equipment while
  // the dialog showed one "Sent".
  it("a double-click on the direct send publishes exactly once", async () => {
    vi.mocked(startDirectMqttPublish).mockResolvedValue({ run_id: "send1" } as never);
    vi.mocked(getValidationRun).mockResolvedValue({
      run_id: "send1",
      status: "succeeded",
      result_summary: { publish: { topic: "site/ahu-1/cmd", authorized_by: "shared-key" } },
    } as never);

    render(<MqttPublishModal authorizationEnforced={false} onClose={() => {}} workspace={workspace} />);
    fillCompose();
    fireEvent.click(screen.getByRole("button", { name: /Send to live equipment/ }));

    await doubleClick(screen.getByRole("button", { name: /Send to device/ }));

    await screen.findByText(/Sent/);
    expect(startDirectMqttPublish).toHaveBeenCalledTimes(1);
  });

  it("a double-click on the authorized send publishes exactly once", async () => {
    await reachApprovedPreview();
    vi.mocked(startAuthorizedMqttPublish).mockResolvedValue({ run_id: "send1" } as never);
    vi.mocked(getValidationRun).mockImplementation((runId: string) =>
      Promise.resolve(
        (runId === "send1"
          ? {
              run_id: "send1",
              status: "succeeded",
              result_summary: { publish: { topic: "site/ahu-1/cmd", authorized_by: "admin" } },
            }
          : previewRun) as never,
      ),
    );

    await doubleClick(screen.getByRole("button", { name: /Send to live equipment/ }));

    await screen.findByText(/Sent/);
    // The authorization is one-use, so a second replay would fail anyway; the
    // point is that it is never issued.
    expect(startAuthorizedMqttPublish).toHaveBeenCalledTimes(1);
  });

  it("a double-click on the preview submits exactly once", async () => {
    vi.mocked(startMqttPublishPreview).mockResolvedValue({ run_id: "prev1" } as never);
    vi.mocked(getValidationRun).mockResolvedValue(previewRun as never);

    render(<MqttPublishModal onClose={() => {}} workspace={workspace} />);
    fillCompose();
    await doubleClick(screen.getByRole("button", { name: /Preview — nothing is sent/ }));

    await screen.findByText("abc123");
    expect(startMqttPublishPreview).toHaveBeenCalledTimes(1);
  });

  it("shows an honest failure when the send run fails", async () => {
    await reachApprovedPreview();
    vi.mocked(startAuthorizedMqttPublish).mockResolvedValue({ run_id: "send1" } as never);
    vi.mocked(getValidationRun).mockImplementation((runId: string) =>
      Promise.resolve(
        (runId === "send1"
          ? { run_id: "send1", status: "failed", error_message: "The MQTT client is not connected." }
          : previewRun) as never,
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: /Send to live equipment/ }));

    await screen.findByText(/Not sent/);
    expect(screen.getByText(/not connected/i)).toBeInTheDocument();
  });
});
