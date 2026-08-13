import type { NmapDeploymentPolicyResponse } from "../../api/client";

type Props = {
  errorMessage?: string;
  loading: boolean;
  policies?: NmapDeploymentPolicyResponse[];
};

export function NmapPolicyHistory({ errorMessage, loading, policies }: Props) {
  return (
    <section aria-labelledby="nmap-policy-history-heading">
      <div className="section-heading">
        <h3 id="nmap-policy-history-heading">Policy history</h3>
      </div>
      {errorMessage ? (
        <div className="state-panel error" role="alert">
          <strong>Policy history unavailable</strong>
          <span>{errorMessage}</span>
        </div>
      ) : loading ? (
        <p aria-live="polite" className="field-note" role="status">
          Loading policy history.
        </p>
      ) : (policies?.length ?? 0) === 0 ? (
        <p className="field-note" role="status">
          No policy revisions exist. Nmap is disabled by default.
        </p>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table">
            <caption className="sr-only">Append-only Nmap deployment policy history</caption>
            <thead>
              <tr>
                <th scope="col">Revision</th>
                <th scope="col">Lane and mode</th>
                <th scope="col">Permitted sites</th>
                <th scope="col">Exact policy</th>
                <th scope="col">Review</th>
              </tr>
            </thead>
            <tbody>
              {policies?.map((policy) => (
                <tr key={policy.policy_id}>
                  <td>{policy.revision}</td>
                  <td>
                    {policy.deployment_lane}
                    <span>{policy.provider_mode}</span>
                  </td>
                  <td>
                    {policy.permitted_project_sites
                      .map((scope) => `${scope.project_id} / ${scope.site_id}`)
                      .join(", ")}
                  </td>
                  <td>
                    <div
                      aria-label={`Exact policy revision ${policy.revision}`}
                      className="detail-list"
                      role="group"
                    >
                      <PolicyValue label="Publishers" values={policy.permitted_publishers} />
                      <PolicyValue label="Versions" values={policy.permitted_versions} />
                      <PolicyValue label="Deployment owner" values={[policy.deployment_owner]} />
                      <PolicyValue label="Update owner" values={[policy.update_owner]} />
                      <PolicyValue
                        label="Installation responsibility"
                        values={[policy.operator_install_responsibility]}
                      />
                      <PolicyValue
                        label="Reviewed version policy"
                        values={[policy.reviewed_version_policy]}
                      />
                      <PolicyValue
                        code
                        label="Signer SHA-256"
                        values={policy.permitted_signer_sha256}
                      />
                      <PolicyValue
                        code
                        label="Executable SHA-256"
                        values={policy.permitted_executable_sha256}
                      />
                      <PolicyValue
                        code
                        label="Data manifest SHA-256"
                        values={policy.permitted_data_manifest_sha256}
                      />
                      <PolicyValue
                        code
                        label="Licence SHA-256"
                        values={policy.permitted_licence_sha256}
                      />
                      <PolicyValue label="NPSL versions" values={policy.permitted_npsl_versions} />
                      <PolicyValue
                        label="Fixed profiles"
                        values={policy.profile_policy.permitted_profiles}
                      />
                      <PolicyValue
                        code
                        label="Reviewed scripts"
                        values={policy.reviewed_scripts.map(
                          (script) => `${script.name}: ${script.sha256}`,
                        )}
                      />
                      <PolicyValue
                        label="No-redistribution acknowledgement"
                        values={[
                          policy.acknowledged_no_redistribution ? "Recorded" : "Not recorded",
                        ]}
                      />
                    </div>
                  </td>
                  <td>
                    {policy.reason}
                    <span>by {policy.created_by}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function PolicyValue({
  code = false,
  label,
  values,
}: {
  code?: boolean;
  label: string;
  values: readonly string[];
}) {
  return (
    <div className="detail-row">
      <span>{label}</span>
      <strong>
        {values.length === 0 ? "None" : code ? <code>{values.join(", ")}</code> : values.join(", ")}
      </strong>
    </div>
  );
}
