import packageMetadata from "../../package.json";

export function resolveAppVersion(buildStamp = import.meta.env.VITE_APP_VERSION): string {
  return buildStamp?.trim() || packageMetadata.version;
}
