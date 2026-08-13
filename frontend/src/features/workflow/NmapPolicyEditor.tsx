import { useState } from "react";
import type {
  NmapDeploymentLane,
  NmapDeploymentPolicyCreateRequest,
  NmapProfileName,
  NmapProjectSiteScope,
  NmapProviderMode,
} from "../../api/client";
import type { WorkspaceRef } from "../../app/sessionScope";
import { NmapProcessTrustFields, NmapScopeEditor, NmapTextField } from "./NmapPolicyFields";
import {
  buildNmapPolicyRequest,
  canonicalNmapValues,
  EMPTY_NMAP_POLICY_TEXT,
  type NmapPolicyText,
} from "./nmapPolicyDraft";

type Props = {
  onCreate: (request: NmapDeploymentPolicyCreateRequest) => void;
  pending: boolean;
  workspace: WorkspaceRef;
};

export function NmapPolicyEditor({ onCreate, pending, workspace }: Props) {
  const [deploymentLane, setDeploymentLane] = useState<NmapDeploymentLane>(
    "internal_same_organization",
  );
  const [providerMode, setProviderMode] = useState<NmapProviderMode>("disabled");
  const [text, setText] = useState<NmapPolicyText>(EMPTY_NMAP_POLICY_TEXT);
  const [scopes, setScopes] = useState<NmapProjectSiteScope[]>([
    { project_id: workspace.projectId, site_id: workspace.siteId },
  ]);
  const [profiles, setProfiles] = useState<NmapProfileName[]>([]);
  const [acknowledged, setAcknowledged] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const processMode = providerMode === "internal_operator_managed";
  const external = deploymentLane === "external_customer";

  const setField = (field: keyof NmapPolicyText, value: string) => {
    setText((current) => ({ ...current, [field]: value }));
  };

  const toggleProfile = (profile: NmapProfileName) => {
    setProfiles((current) =>
      current.includes(profile)
        ? current.filter((value) => value !== profile)
        : canonicalNmapValues(current.concat(profile)),
    );
  };

  const createPolicy = () => {
    const result = buildNmapPolicyRequest({
      acknowledged,
      deploymentLane,
      profiles,
      providerMode,
      scopes,
      text,
    });
    if (!result.ok) {
      setValidationError(result.error);
      return;
    }
    setValidationError(null);
    onCreate(result.request);
  };

  return (
    <form
      aria-label="Create Nmap deployment policy"
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        createPolicy();
      }}
    >
      <div className="field-grid">
        <label>
          Deployment lane
          <select
            onChange={(event) => {
              const lane = event.target.value as NmapDeploymentLane;
              setDeploymentLane(lane);
              if (lane === "external_customer") {
                setProviderMode("disabled");
                setAcknowledged(false);
              }
            }}
            value={deploymentLane}
          >
            <option value="internal_same_organization">Internal, same organization</option>
            <option value="external_customer">External customer</option>
          </select>
        </label>
        <label>
          Provider mode
          <select
            aria-controls="nmap-process-policy-fields"
            aria-expanded={processMode}
            onChange={(event) => {
              const mode = event.target.value as NmapProviderMode;
              setProviderMode(mode);
              if (mode === "disabled") {
                setAcknowledged(false);
              }
            }}
            value={providerMode}
          >
            <option value="disabled">Disabled</option>
            <option disabled={external} value="operator_xml_import">
              Operator XML import
            </option>
            <option disabled={external} value="internal_operator_managed">
              Operator-managed Nmap process
            </option>
          </select>
        </label>
        <NmapTextField
          label="Deployment owner"
          onChange={(value) => setField("deploymentOwner", value)}
          value={text.deploymentOwner}
        />
        <NmapTextField
          label="Update owner"
          onChange={(value) => setField("updateOwner", value)}
          value={text.updateOwner}
        />
        <NmapTextField
          label="Installation responsibility"
          onChange={(value) => setField("installationResponsibility", value)}
          value={text.installationResponsibility}
        />
        <NmapTextField
          label="Reviewed version policy"
          onChange={(value) => setField("reviewedVersionPolicy", value)}
          value={text.reviewedVersionPolicy}
        />
      </div>

      <NmapScopeEditor onChange={setScopes} scopes={scopes} />

      {processMode && (
        <NmapProcessTrustFields
          profiles={profiles}
          text={text}
          onField={setField}
          onProfile={toggleProfile}
        />
      )}

      {providerMode !== "disabled" && (
        <label className="confirm-row">
          <input
            checked={acknowledged}
            onChange={(event) => setAcknowledged(event.target.checked)}
            type="checkbox"
          />
          I acknowledge that Nmap is not redistributed by this application and is installed and
          maintained by the operator.
        </label>
      )}

      <NmapTextField
        label="Policy audit reason"
        onChange={(value) => setField("reason", value)}
        value={text.reason}
      />
      {external && (
        <p className="field-note" role="status">
          External customer deployments must remain disabled.
        </p>
      )}
      {validationError && (
        <div className="state-panel error" role="alert">
          <strong>Policy is incomplete</strong>
          <span>{validationError}</span>
        </div>
      )}
      <div className="inline-actions">
        <button className="primary-button" disabled={pending} type="submit">
          {pending ? "Creating policy..." : "Create policy revision"}
        </button>
      </div>
    </form>
  );
}
