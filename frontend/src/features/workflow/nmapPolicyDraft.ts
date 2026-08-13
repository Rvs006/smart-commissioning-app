import type {
  NmapDeploymentLane,
  NmapDeploymentPolicyCreateRequest,
  NmapProfileName,
  NmapProjectSiteScope,
  NmapProviderMode,
  NmapReviewedScript,
} from "../../api/client";

export type NmapPolicyText = {
  deploymentOwner: string;
  installationResponsibility: string;
  updateOwner: string;
  reviewedVersionPolicy: string;
  publishers: string;
  versions: string;
  signerHashes: string;
  executableHashes: string;
  dataManifestHashes: string;
  licenceHashes: string;
  npslVersions: string;
  reviewedScripts: string;
  reason: string;
};

export const EMPTY_NMAP_POLICY_TEXT: NmapPolicyText = {
  dataManifestHashes: "",
  deploymentOwner: "",
  executableHashes: "",
  installationResponsibility: "",
  licenceHashes: "",
  npslVersions: "",
  publishers: "",
  reason: "",
  reviewedScripts: "",
  reviewedVersionPolicy: "",
  signerHashes: "",
  updateOwner: "",
  versions: "",
};

export function canonicalNmapValues<T extends string>(values: T[]): T[] {
  return Array.from(new Set(values.map((value) => value.trim() as T).filter(Boolean))).sort();
}

function canonicalCasefold(values: string[]): string[] {
  const byKey = new Map<string, string>();
  for (const value of values.map((item) => item.trim()).filter(Boolean)) {
    byKey.set(value.toLocaleLowerCase("en-US"), value);
  }
  return Array.from(byKey.values()).sort((left, right) => {
    const leftKey = left.toLocaleLowerCase("en-US");
    const rightKey = right.toLocaleLowerCase("en-US");
    return leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0;
  });
}

function lines(value: string): string[] {
  return value.split(/\r?\n/);
}

function parseReviewedScripts(
  value: string,
): { ok: true; scripts: NmapReviewedScript[] } | { ok: false } {
  const scripts: NmapReviewedScript[] = [];
  const names = new Set<string>();
  for (const line of lines(value)
    .map((item) => item.trim())
    .filter(Boolean)) {
    const [name, hash, ...extra] = line.split(/\s*,\s*/);
    if (
      !name ||
      !hash ||
      extra.length > 0 ||
      !/^[a-z0-9][a-z0-9_-]*$/.test(name) ||
      !/^[0-9a-f]{64}$/.test(hash) ||
      names.has(name)
    ) {
      return { ok: false };
    }
    names.add(name);
    scripts.push({ name, schema_version: "1.0", sha256: hash });
  }
  scripts.sort((left, right) => (left.name < right.name ? -1 : left.name > right.name ? 1 : 0));
  return { ok: true, scripts };
}

export function buildNmapPolicyRequest(input: {
  acknowledged: boolean;
  deploymentLane: NmapDeploymentLane;
  profiles: NmapProfileName[];
  providerMode: NmapProviderMode;
  scopes: NmapProjectSiteScope[];
  text: NmapPolicyText;
}): { ok: true; request: NmapDeploymentPolicyCreateRequest } | { ok: false; error: string } {
  const trimmedScopes = input.scopes.map((scope) => ({
    project_id: scope.project_id.trim(),
    site_id: scope.site_id.trim(),
  }));
  if (trimmedScopes.some((scope) => !scope.project_id || !scope.site_id)) {
    return { ok: false, error: "Every permitted scope needs both a project ID and site ID." };
  }
  const scopeKeys = trimmedScopes.map((scope) => `${scope.project_id}\u0000${scope.site_id}`);
  if (new Set(scopeKeys).size !== scopeKeys.length) {
    return { ok: false, error: "Permitted project and site scopes must be unique." };
  }
  const scopes = trimmedScopes.sort((left, right) => {
    const leftKey = `${left.project_id}\u0000${left.site_id}`;
    const rightKey = `${right.project_id}\u0000${right.site_id}`;
    return leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0;
  });
  const requiredText = [
    input.text.deploymentOwner,
    input.text.installationResponsibility,
    input.text.updateOwner,
    input.text.reviewedVersionPolicy,
    input.text.reason,
  ];
  if (requiredText.some((value) => !value.trim()) || scopes.length === 0) {
    return {
      ok: false,
      error: "Complete the owners, responsibility, scope, version review, and audit reason.",
    };
  }
  if (input.providerMode !== "disabled" && !input.acknowledged) {
    return {
      ok: false,
      error: "Acknowledge the operator installation and no-redistribution terms.",
    };
  }
  const reviewedScripts = parseReviewedScripts(input.text.reviewedScripts);
  if (!reviewedScripts.ok) {
    return { ok: false, error: "Each reviewed script must use name, SHA-256." };
  }
  const request: NmapDeploymentPolicyCreateRequest = {
    acknowledged_no_redistribution: input.acknowledged,
    deployment_lane: input.deploymentLane,
    deployment_owner: input.text.deploymentOwner.trim(),
    max_data_files: 8192,
    max_file_bytes: 64 * 1024 * 1024,
    max_manifest_bytes: 512 * 1024 * 1024,
    operator_install_responsibility: input.text.installationResponsibility.trim(),
    permitted_data_manifest_sha256: canonicalNmapValues(lines(input.text.dataManifestHashes)),
    permitted_executable_sha256: canonicalNmapValues(lines(input.text.executableHashes)),
    permitted_licence_sha256: canonicalNmapValues(lines(input.text.licenceHashes)),
    permitted_npsl_versions: canonicalNmapValues(lines(input.text.npslVersions)),
    permitted_project_sites: scopes,
    permitted_publishers: canonicalCasefold(lines(input.text.publishers)),
    permitted_signer_sha256: canonicalNmapValues(lines(input.text.signerHashes)),
    permitted_versions: canonicalNmapValues(lines(input.text.versions)),
    profile_policy: {
      permitted_profiles: canonicalNmapValues(input.profiles),
      schema_version: "1.0",
    },
    provider_mode: input.providerMode,
    reason: input.text.reason.trim(),
    reviewed_scripts: reviewedScripts.scripts,
    reviewed_version_policy: input.text.reviewedVersionPolicy.trim(),
    update_owner: input.text.updateOwner.trim(),
  };
  const hashes = [
    ...request.permitted_signer_sha256,
    ...request.permitted_executable_sha256,
    ...request.permitted_data_manifest_sha256,
    ...request.permitted_licence_sha256,
  ];
  if (hashes.some((hash) => !/^[0-9a-f]{64}$/.test(hash))) {
    return {
      ok: false,
      error: "Every SHA-256 allowlist value must contain 64 lowercase hexadecimal characters.",
    };
  }
  if (request.permitted_npsl_versions.some((version) => !/^[0-9]+(?:\.[0-9]+)*$/.test(version))) {
    return { ok: false, error: "Every NPSL version must use numeric dot-separated components." };
  }
  if (
    input.providerMode === "internal_operator_managed" &&
    [
      request.permitted_publishers,
      request.permitted_versions,
      request.permitted_signer_sha256,
      request.permitted_executable_sha256,
      request.permitted_data_manifest_sha256,
      request.permitted_licence_sha256,
      request.permitted_npsl_versions,
      request.profile_policy.permitted_profiles,
    ].some((values) => values.length === 0)
  ) {
    return {
      ok: false,
      error: "Process mode requires every exact trust allowlist and at least one fixed profile.",
    };
  }
  if (
    request.profile_policy.permitted_profiles.includes("reviewed_script_inventory") &&
    request.reviewed_scripts.length === 0
  ) {
    return {
      ok: false,
      error: "Reviewed script inventory requires at least one reviewed script hash.",
    };
  }
  return { ok: true, request };
}
