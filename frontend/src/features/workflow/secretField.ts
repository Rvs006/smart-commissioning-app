/**
 * A secret sentinel is the all-asterisk placeholder the API returns for stored
 * password fields. The form swaps it out on focus and restores it on blur when
 * the user leaves the field empty.
 */
export function isSecretSentinel(value: string): boolean {
  return value.length > 0 && /^\*+$/.test(value);
}

/**
 * Masks secret material for read-only display: server references keep a short
 * identifiable prefix, everything else is replaced character-for-character.
 */
export function maskSecretValue(value: string): string {
  if (!value) {
    return "";
  }
  if (value.startsWith("secret://")) {
    return `${value.slice(0, 24)}...`;
  }
  return value.replace(/./g, "*");
}
