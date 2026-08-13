import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createNmapDeploymentPolicy,
  listNmapDeploymentPolicies,
  type NmapDeploymentPolicyCreateRequest,
} from "../../api/client";
import { mutationKeys, queryKeys } from "../../api/queryKeys";
import { useSession } from "../../app/sessionContext";
import { NmapInstallationInspector } from "./NmapInstallationInspector";
import { NmapPolicyEditor } from "./NmapPolicyEditor";
import { NmapPolicyHistory } from "./NmapPolicyHistory";

export function NmapAuthorityPanel() {
  const { canAdmin, me } = useSession();
  return canAdmin && me?.global_scope ? <NmapAuthorityAdmin /> : null;
}

function NmapAuthorityAdmin() {
  const { apiClient, sessionScopeId, workspace } = useSession();
  const queryClient = useQueryClient();
  const policyQueryKey = queryKeys.nmapPolicies(sessionScopeId);
  const policiesQuery = useQuery({
    queryFn: () => listNmapDeploymentPolicies({ client: apiClient }),
    queryKey: policyQueryKey,
  });
  const createPolicyMutation = useMutation({
    mutationKey: mutationKeys.action(sessionScopeId, "nmap.policy.create"),
    mutationFn: (request: NmapDeploymentPolicyCreateRequest) =>
      createNmapDeploymentPolicy(request, { client: apiClient }),
    onSuccess: (policy) => {
      queryClient.setQueryData(policyQueryKey, (current: unknown) =>
        Array.isArray(current) ? [policy, ...current] : [policy],
      );
    },
  });

  return (
    <section aria-labelledby="nmap-authority-heading" className="surface">
      <div className="section-heading">
        <div>
          <h2 id="nmap-authority-heading">Nmap deployment authority</h2>
          <p className="section-copy">
            Global administrators bind an operator-managed installation to this deployment and its
            permitted sites. Nmap remains off until an explicit policy and exact confirmation exist.
          </p>
        </div>
      </div>

      <NmapPolicyEditor
        onCreate={(request) => createPolicyMutation.mutate(request)}
        pending={createPolicyMutation.isPending}
        workspace={workspace}
      />

      {createPolicyMutation.isError && (
        <div className="state-panel error" role="alert">
          <strong>Policy revision was not created</strong>
          <span>{createPolicyMutation.error.message}</span>
        </div>
      )}
      {createPolicyMutation.isSuccess && (
        <div aria-live="polite" className="state-panel success" role="status">
          <strong>Policy revision {createPolicyMutation.data.revision} created</strong>
          <span>The append-only policy history now includes this review.</span>
        </div>
      )}

      <NmapInstallationInspector
        enabled={policiesQuery.data?.[0]?.provider_mode === "internal_operator_managed"}
      />

      <NmapPolicyHistory
        errorMessage={policiesQuery.isError ? policiesQuery.error.message : undefined}
        loading={policiesQuery.isLoading}
        policies={policiesQuery.data}
      />
    </section>
  );
}
