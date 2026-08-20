import { useState } from "react";
import {
  createScanAuthorization,
  getValidationRun,
  startAuthorizedMqttPublish,
  startMqttPublishPreview,
  type RunRecord,
  type ScanAuthorizationV1,
  type SessionBoundApiClient,
} from "../../api/client";
import type { WorkspaceRef } from "../../app/sessionScope";

// Sealed one-message publish (M5 PR-A). Compose -> Preview (no send) -> Approve
// (admin) -> Send (replays the frozen bytes) -> Result. Built from SCT's existing
// dialog / state-panel / data-table vocabulary.

type Props = {
  workspace: WorkspaceRef;
  apiClient?: SessionBoundApiClient;
  defaultTopic?: string;
  onClose: () => void;
};

type Stage = "compose" | "preview" | "result";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

async function pollRun(runId: string, apiClient?: SessionBoundApiClient): Promise<RunRecord> {
  const context = apiClient ? { client: apiClient } : undefined;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const run = await getValidationRun(runId, context);
    if (TERMINAL.has(run.status)) {
      return run;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("The run did not finish in time.");
}

function planField(run: RunRecord | null, key: string): unknown {
  const plan = (run?.result_summary as { dry_run_plan?: Record<string, unknown> } | undefined)?.dry_run_plan;
  return plan?.[key];
}

export function MqttPublishModal({ workspace, apiClient, defaultTopic, onClose }: Props) {
  const context = apiClient ? { client: apiClient } : undefined;
  const [stage, setStage] = useState<Stage>("compose");
  const [topic, setTopic] = useState(defaultTopic ?? "");
  const [payload, setPayload] = useState("");
  const [qos, setQos] = useState(0);
  const [retain, setRetain] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [previewRun, setPreviewRun] = useState<RunRecord | null>(null);
  const [ticket, setTicket] = useState("");
  const [purpose, setPurpose] = useState("");
  const [authorization, setAuthorization] = useState<ScanAuthorizationV1 | null>(null);
  const [sendRun, setSendRun] = useState<RunRecord | null>(null);

  const fail = (err: unknown) => setError(err instanceof Error ? err.message : "The request failed.");

  const doPreview = async () => {
    setBusy(true);
    setError(null);
    try {
      const accepted = await startMqttPublishPreview({ workspace, topic, payload, qos, retain, context });
      const run = await pollRun(accepted.run_id, apiClient);
      if (run.status !== "succeeded") {
        setError((run.error_message as string) || "The preview failed.");
      } else {
        setPreviewRun(run);
        setStage("preview");
      }
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  const doApprove = async () => {
    if (!previewRun) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const now = new Date();
      const auth = await createScanAuthorization({
        previewRunId: previewRun.run_id,
        ticket,
        purpose,
        notBefore: new Date(now.getTime() - 60_000).toISOString(),
        notAfter: new Date(now.getTime() + 60 * 60_000).toISOString(),
        context,
      });
      setAuthorization(auth);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  const doSend = async () => {
    if (!previewRun || !authorization) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const accepted = await startAuthorizedMqttPublish({
        workspace,
        previewRunId: previewRun.run_id,
        scanAuthorizationId: authorization.authorization_id,
        context,
      });
      setSendRun(await pollRun(accepted.run_id, apiClient));
      setStage("result");
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  const publishEvidence = (sendRun?.result_summary as { publish?: Record<string, unknown> } | undefined)?.publish;

  return (
    <div className="surface mqtt-publish-modal" role="dialog" aria-labelledby="mqtt-publish-heading">
      <div className="surface-heading">
        <div>
          <h3 id="mqtt-publish-heading">Publish a message</h3>
          <p className="section-copy">
            A preview shows the exact bytes and sends nothing. An admin approves that exact message before it can be
            sent to the live broker.
          </p>
        </div>
        <button className="secondary-button compact" onClick={onClose} type="button">
          Close
        </button>
      </div>

      {error && (
        <div className="state-panel error" role="alert">
          <strong>Problem</strong>
          <span>{error}</span>
        </div>
      )}

      {stage === "compose" && (
        <div className="form-stack">
          <label className="field-control">
            Topic
            <input onChange={(event) => setTopic(event.target.value)} value={topic} />
          </label>
          <label className="field-control">
            Payload
            <textarea onChange={(event) => setPayload(event.target.value)} rows={4} value={payload} />
          </label>
          <label className="field-control">
            QoS
            <select onChange={(event) => setQos(Number(event.target.value))} value={qos}>
              <option value={0}>0</option>
              <option value={1}>1</option>
              <option value={2}>2</option>
            </select>
          </label>
          <label className="confirm-row">
            <input checked={retain} onChange={(event) => setRetain(event.target.checked)} type="checkbox" />
            Retain
          </label>
          <button
            className="primary-button compact"
            disabled={busy || topic.trim().length === 0}
            onClick={() => void doPreview()}
            type="button"
          >
            {busy ? "Previewing…" : "Preview — nothing is sent"}
          </button>
        </div>
      )}

      {stage === "preview" && previewRun && (
        <div className="form-stack">
          <div className="state-panel" role="status">
            <strong>Preview — nothing has been sent</strong>
            <span>An admin must approve this exact message before an engineer can send it.</span>
          </div>
          <div className="data-table-wrap">
            <table className="data-table">
              <tbody>
                <tr>
                  <td>Topic</td>
                  <td>{String(planField(previewRun, "targets"))}</td>
                </tr>
                <tr>
                  <td>Payload SHA-256</td>
                  <td>{String(planField(previewRun, "payload_sha256"))}</td>
                </tr>
                <tr>
                  <td>Payload bytes</td>
                  <td>{String(planField(previewRun, "payload_bytes"))}</td>
                </tr>
                <tr>
                  <td>QoS / retain</td>
                  <td>
                    {String(planField(previewRun, "qos"))} / {planField(previewRun, "retain") ? "yes" : "no"}
                  </td>
                </tr>
                <tr>
                  <td>Broker</td>
                  <td>
                    {String(planField(previewRun, "broker_host"))}:{String(planField(previewRun, "broker_port"))}
                    {planField(previewRun, "use_tls") ? " (TLS)" : ""}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          {authorization ? (
            <div className="state-panel success" role="status">
              <strong>Approved</strong>
              <span>Authorization {authorization.authorization_id} is ready. Send when you are.</span>
            </div>
          ) : (
            <div className="form-stack">
              <span className="section-copy">An admin approves this exact preview:</span>
              <label className="field-control">
                Change ticket
                <input onChange={(event) => setTicket(event.target.value)} value={ticket} />
              </label>
              <label className="field-control">
                Purpose
                <input onChange={(event) => setPurpose(event.target.value)} value={purpose} />
              </label>
              <button
                className="secondary-button compact"
                disabled={busy || ticket.trim().length === 0 || purpose.trim().length === 0}
                onClick={() => void doApprove()}
                type="button"
              >
                {busy ? "Approving…" : "Approve this exact message"}
              </button>
            </div>
          )}

          <button
            className="primary-button compact"
            disabled={busy || !authorization}
            onClick={() => void doSend()}
            type="button"
          >
            {busy ? "Sending…" : "Send to live equipment"}
          </button>
        </div>
      )}

      {stage === "result" && sendRun && (
        <div className="form-stack">
          {sendRun.status === "succeeded" && publishEvidence ? (
            <div className="state-panel success" role="status">
              <strong>Sent</strong>
              <span>
                Accepted by the sidecar&apos;s MQTT client (not a broker delivery acknowledgement). Topic{" "}
                {String(publishEvidence.topic)}, authorized by {String(publishEvidence.authorized_by)}.
              </span>
            </div>
          ) : (
            <div className="state-panel error" role="alert">
              <strong>Not sent</strong>
              <span>{(sendRun.error_message as string) || "The publish failed."} A new preview and approval are required to retry.</span>
            </div>
          )}
          <button className="secondary-button compact" onClick={onClose} type="button">
            Done
          </button>
        </div>
      )}
    </div>
  );
}
