import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  confirmNmapInstallation,
  detectNmapInstallations,
  type NmapDetectedInstallationResponse,
} from "../../api/client";
import { mutationKeys } from "../../api/queryKeys";
import { useSession } from "../../app/sessionContext";
import { NmapInstallationIdentity } from "./NmapInstallationIdentity";

type Props = {
  enabled: boolean;
};

export function NmapInstallationInspector({ enabled }: Props) {
  const { apiClient, sessionScopeId } = useSession();
  const [installations, setInstallations] = useState<NmapDetectedInstallationResponse[]>([]);
  const [selectedFingerprint, setSelectedFingerprint] = useState("");
  const [confirmationReason, setConfirmationReason] = useState("");
  const detectMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "nmap.installation.detect"),
    mutationFn: () => detectNmapInstallations({ client: apiClient }),
    onMutate: () => {
      setInstallations([]);
      setSelectedFingerprint("");
      setConfirmationReason("");
    },
    onSuccess: (installations) => {
      setInstallations(installations);
      setSelectedFingerprint(
        installations.find(
          (installation) => installation.state === "available" && installation.fingerprint_sha256,
        )?.fingerprint_sha256 ?? "",
      );
    },
  });
  const confirmMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "nmap.installation.confirm"),
    mutationFn: () =>
      confirmNmapInstallation({
        context: { client: apiClient },
        fingerprintSha256: selectedFingerprint,
        reason: confirmationReason.trim(),
      }),
  });
  const confirmable = installations.filter(
    (
      installation,
    ): installation is NmapDetectedInstallationResponse & { fingerprint_sha256: string } =>
      installation.state === "available" && Boolean(installation.fingerprint_sha256),
  );

  return (
    <section aria-busy={detectMutation.isPending} aria-labelledby="nmap-installation-heading">
      <div className="section-heading">
        <div>
          <h3 id="nmap-installation-heading">Installed Nmap identity</h3>
          <p className="section-copy">
            Inspection uses the current policy allowlist. Only verified identity data appears here.
          </p>
        </div>
      </div>
      <button
        aria-describedby={!enabled ? "nmap-inspection-requirement" : undefined}
        className="secondary-button"
        disabled={!enabled || detectMutation.isPending}
        onClick={() => detectMutation.mutate()}
        type="button"
      >
        {detectMutation.isPending ? "Inspecting Nmap..." : "Inspect installed Nmap"}
      </button>
      {!enabled && (
        <p className="field-note" id="nmap-inspection-requirement">
          Create an internal operator-managed process policy before inspection.
        </p>
      )}

      {detectMutation.isError && (
        <div className="state-panel error" role="alert">
          <strong>Installation inspection failed</strong>
          <span>{detectMutation.error.message}</span>
        </div>
      )}
      {detectMutation.isSuccess && installations.length === 0 && (
        <p aria-live="polite" className="field-note" role="status">
          No policy-matching Nmap installation was detected.
        </p>
      )}
      {detectMutation.isSuccess && installations.length > 0 && (
        <p aria-live="polite" className="field-note" role="status">
          Inspected {installations.length} installation{" "}
          {installations.length === 1 ? "identity" : "identities"}.
        </p>
      )}
      {installations.map((installation, index) => (
        <NmapInstallationIdentity
          installation={installation}
          key={installation.fingerprint_sha256 ?? index}
        />
      ))}

      {confirmable.length > 0 && (
        <div className="stack">
          {confirmable.length > 1 && (
            <label>
              Installation to confirm
              <select
                onChange={(event) => setSelectedFingerprint(event.target.value)}
                value={selectedFingerprint}
              >
                {confirmable.map((installation) => (
                  <option
                    key={installation.fingerprint_sha256}
                    value={installation.fingerprint_sha256}
                  >
                    {installation.publisher ?? "Unknown publisher"}{" "}
                    {installation.version ?? "Unknown version"}
                    {` (${installation.fingerprint_sha256})`}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label>
            Confirmation audit reason
            <input
              maxLength={4096}
              onChange={(event) => setConfirmationReason(event.target.value)}
              value={confirmationReason}
            />
          </label>
          <div className="inline-actions">
            <button
              className="primary-button"
              disabled={
                !selectedFingerprint || !confirmationReason.trim() || confirmMutation.isPending
              }
              onClick={() => confirmMutation.mutate()}
              type="button"
            >
              {confirmMutation.isPending
                ? "Confirming installation..."
                : "Confirm exact installation"}
            </button>
          </div>
        </div>
      )}

      {confirmMutation.isError && (
        <div className="state-panel error" role="alert">
          <strong>Installation was not confirmed</strong>
          <span>{confirmMutation.error.message}</span>
        </div>
      )}
      {confirmMutation.isSuccess && (
        <div aria-live="polite" className="state-panel success" role="status">
          <strong>Exact installation confirmed</strong>
          <span>
            {confirmMutation.data.publisher} {confirmMutation.data.version}, fingerprint{" "}
            {confirmMutation.data.fingerprint_sha256}
          </span>
        </div>
      )}
    </section>
  );
}
