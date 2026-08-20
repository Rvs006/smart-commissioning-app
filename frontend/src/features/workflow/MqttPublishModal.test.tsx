import { fireEvent, render, screen } from "@testing-library/react";
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

    await screen.findByText(/Sent/);
    expect(startDirectMqttPublish).toHaveBeenCalledWith(
      expect.objectContaining({ topic: "site/ahu-1/cmd", payload: '{"cmd":1}' }),
    );
    // The sealed preview path was never touched.
    expect(startMqttPublishPreview).not.toHaveBeenCalled();
    expect(createScanAuthorization).not.toHaveBeenCalled();
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
