import { useEffect, useRef, useState } from "react";
import {
  createScanAuthorization,
  getValidationRun,
  startAuthorizedMqttPublish,
  startDirectMqttPublish,
  startMqttPublishPreview,
  type RunRecord,
  type ScanAuthorizationV1,
  type SessionBoundApiClient,
} from "../../api/client";
import type { WorkspaceRef } from "../../app/sessionScope";

// Sealed one-message publish (M5 PR-A). Compose -> Preview (no send) -> Approve
// (admin) -> Send (replays the frozen bytes) -> Result. Built from SCT's existing
// dialog / state-panel / data-table vocabulary. In a frictionless deployment
// (authorizationEnforced=false) it collapses to Compose -> Confirm -> Send: the
// backend seals the exact bytes server-side and records the sender, and the
// confirm step is the operator's last look at the topic, QoS/retain and payload
// before they reach live equipment (there is no approver to catch a typo here).

type Props = {
  workspace: WorkspaceRef;
  apiClient?: SessionBoundApiClient;
  authorizationEnforced?: boolean;
  defaultTopic?: string;
  // GAP-M7: "Write config…" opens this modal prefilled from the focused asset's
  // config topic + last-seen config payload, with retain defaulting on.
  defaultPayload?: string;
  defaultRetain?: boolean;
  /**
   * QoS the dialog opens on. The vendored tool's config editor ships QoS 1 and
   * retain ticked (scanners/vendor/mqtt-discovery/public/index.html:215-216),
   * so "Write config" must open the same way or an operator following that tool
   * silently sends a config at QoS 0. A plain "Publish message..." keeps 0.
   */
  defaultQos?: 0 | 1 | 2;
  onClose: () => void;
};

type Stage = "compose" | "confirm" | "preview" | "result";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

// Distinguishable from a rejected request: the publish was ACCEPTED and may well
// have reached the broker, we just stopped watching it. Offering "send again"
// after this would risk a duplicate write to live equipment.
class RunPollTimeout extends Error {
  constructor(readonly runId: string) {
    super("The run did not finish in time.");
    this.name = "RunPollTimeout";
  }
}

async function pollRun(runId: string, apiClient?: SessionBoundApiClient): Promise<RunRecord> {
  const context = apiClient ? { client: apiClient } : undefined;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const run = await getValidationRun(runId, context);
    if (TERMINAL.has(run.status)) {
      return run;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new RunPollTimeout(runId);
}

function planField(run: RunRecord | null, key: string): unknown {
  const plan = (run?.result_summary as { dry_run_plan?: Record<string, unknown> } | undefined)?.dry_run_plan;
  return plan?.[key];
}

export function MqttPublishModal({
  workspace,
  apiClient,
  authorizationEnforced = true,
  defaultTopic,
  defaultPayload,
  defaultRetain,
  defaultQos,
  onClose,
}: Props) {
  const context = apiClient ? { client: apiClient } : undefined;
  const [stage, setStage] = useState<Stage>("compose");
  const [topic, setTopic] = useState(defaultTopic ?? "");
  const [payload, setPayload] = useState(defaultPayload ?? "");
  const [qos, setQos] = useState<number>(defaultQos ?? 0);
  const [retain, setRetain] = useState(defaultRetain ?? false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [previewRun, setPreviewRun] = useState<RunRecord | null>(null);
  const [ticket, setTicket] = useState("");
  const [purpose, setPurpose] = useState("");
  const [authorization, setAuthorization] = useState<ScanAuthorizationV1 | null>(null);
  const [sendRun, setSendRun] = useState<RunRecord | null>(null);
  // The confirm step is the last gate before a live write. Focus lands on Cancel
  // (not Send) so a stray Enter or Space cannot be the keystroke that publishes,
  // and so the dialog's own text is what a screen reader announces on arrival.
  const confirmCancelRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (stage === "confirm") {
      confirmCancelRef.current?.focus();
    }
  }, [stage]);

  const fail = (err: unknown) => setError(err instanceof Error ? err.message : "The request failed.");

  // `busy` is React state, so it is not applied until React commits. Two clicks
  // delivered in the same tick both sail past `disabled={busy}` and both fire the
  // request, and the backend creates one run per accepted request: a fast
  // double-click published TWICE while the dialog showed a single "Sent". A ref
  // flips synchronously, so the second click returns before it can reach the
  // network. `busy` stays as the visual disabled state.
  const sendingRef = useRef(false);
  const singleFlight = async (operation: () => Promise<void>): Promise<void> => {
    if (sendingRef.current) {
      return;
    }
    sendingRef.current = true;
    setBusy(true);
    setError(null);
    try {
      await operation();
    } finally {
      // Cleared on every outcome (success, error, poll timeout) so the dialog is
      // never wedged shut after a failure the operator can retry.
      sendingRef.current = false;
      setBusy(false);
    }
  };

  const doPreview = () =>
    singleFlight(async () => {
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
      }
    });

  const doDirectSend = () =>
    // Frictionless: no preview/approval. The backend seals the bytes server-side.
    singleFlight(async () => {
      try {
        const accepted = await startDirectMqttPublish({ workspace, topic, payload, qos, retain, context });
        setSendRun(await pollRun(accepted.run_id, apiClient));
        setStage("result");
      } catch (err) {
        if (err instanceof RunPollTimeout) {
          // The send was accepted; only our watch gave up. Move OFF the confirm
          // step, so its live "Send to device" button cannot publish a second copy
          // of a message that may already be on the wire, and name the run instead.
          setSendRun({ run_id: err.runId, status: "running" } as RunRecord);
          setStage("result");
        } else {
          fail(err);
        }
      }
    });

  const doApprove = () =>
    singleFlight(async () => {
      if (!previewRun) {
        return;
      }
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
      }
    });

  const doSend = () =>
    singleFlight(async () => {
      if (!previewRun || !authorization) {
        return;
      }
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
      }
    });

  const publishEvidence = (sendRun?.result_summary as { publish?: Record<string, unknown> } | undefined)?.publish;

  return (
    <div className="surface mqtt-publish-modal" role="dialog" aria-labelledby="mqtt-publish-heading">
      <div className="surface-heading">
        <div>
          <h3 id="mqtt-publish-heading">Publish a message</h3>
          <p className="section-copy">
            {authorizationEnforced
              ? "A preview shows the exact bytes and sends nothing. An admin approves that exact message before it can be sent to the live broker."
              : "This message sends directly through the held live session. The exact bytes are recorded as evidence."}
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
          {authorizationEnforced ? (
            <button
              className="primary-button compact"
              disabled={busy || topic.trim().length === 0}
              onClick={() => void doPreview()}
              type="button"
            >
              {busy ? "Previewing…" : "Preview — nothing is sent"}
            </button>
          ) : (
            <button
              className="primary-button compact"
              disabled={busy || topic.trim().length === 0}
              onClick={() => setStage("confirm")}
              type="button"
            >
              Send to live equipment
            </button>
          )}
        </div>
      )}

      {stage === "confirm" && (
        <div
          aria-labelledby="mqtt-publish-confirm-heading"
          className="form-stack"
          role="alertdialog"
        >
          <div className="state-panel warning">
            <strong id="mqtt-publish-confirm-heading">Confirm the write</strong>
            <span>
              This publishes to a live device and can change how the equipment operates. Nothing has
              been sent yet.
            </span>
          </div>
          <div className="data-table-wrap">
            <table className="data-table">
              <tbody>
                <tr>
                  <td>Topic</td>
                  <td>{topic}</td>
                </tr>
                <tr>
                  <td>QoS / retain</td>
                  <td>
                    {qos} / {retain ? "retained" : "not retained"}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <span className="section-copy">Payload</span>
          <pre className="mqtt-payload-view">{payload}</pre>
          <div className="inline-actions">
            <button
              className="secondary-button compact"
              disabled={busy}
              ref={confirmCancelRef}
              onClick={() => {
                // Drop a previous send's error too: the operator is going back to
                // edit, and a stale Problem banner over a new draft reads as if
                // the new one already failed.
                setError(null);
                setStage("compose");
              }}
              type="button"
            >
              Cancel
            </button>
            <button
              className="primary-button compact"
              disabled={busy}
              onClick={() => void doDirectSend()}
              type="button"
            >
              {busy ? "Sending…" : "Send to device"}
            </button>
          </div>
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
          ) : TERMINAL.has(sendRun.status) ? (
            <div className="state-panel error" role="alert">
              <strong>Not sent</strong>
              <span>{(sendRun.error_message as string) || "The publish failed."} A new preview and approval are required to retry.</span>
            </div>
          ) : (
            // Accepted, still not terminal when we stopped polling. Claiming
            // either outcome would be a guess, and "retry" could double-publish.
            <div className="state-panel warning" role="status">
              <strong>Still running</strong>
              <span>
                The publish was accepted as run {sendRun.run_id} and had not finished when this
                dialog stopped watching it. Check that run in Run History before sending again; it
                may already have reached the broker.
              </span>
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
