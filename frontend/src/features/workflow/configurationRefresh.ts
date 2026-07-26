import type { ConfigurationSectionKey, ConfigurationSnapshot } from "../../api/client";

/**
 * Three-way merge for background refreshes. Server changes replace untouched
 * fields, while values edited since the previous saved snapshot survive.
 */
export function mergeConfigurationRefresh(
  previousSaved: ConfigurationSnapshot,
  currentDraft: ConfigurationSnapshot,
  refreshed: ConfigurationSnapshot,
): ConfigurationSnapshot {
  const merged = { ...refreshed };
  for (const section of Object.keys(refreshed) as ConfigurationSectionKey[]) {
    const nextValues = { ...refreshed[section].values };
    for (const [field, draftValue] of Object.entries(currentDraft[section].values)) {
      if (draftValue !== previousSaved[section].values[field]) {
        nextValues[field] = draftValue;
      }
    }
    merged[section] = { ...refreshed[section], values: nextValues };
  }
  return merged;
}
