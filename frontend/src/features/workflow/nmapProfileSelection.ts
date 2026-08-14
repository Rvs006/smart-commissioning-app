import type { NmapProfileName } from "../../api/client";

export function resolvePermittedNmapProfile(
  selectedProfile: NmapProfileName,
  permittedProfiles: readonly NmapProfileName[],
): NmapProfileName | undefined {
  return permittedProfiles.includes(selectedProfile) ? selectedProfile : permittedProfiles[0];
}
