// Hashed chunk names change every release. A page built by an older release
// (a tab left open across an upgrade, or an old instance still on the port)
// asks for chunks the current dist does not have, and the import rejects.
const STALE_BUILD_PATTERN =
  /dynamically imported module|Importing a module script failed|Unable to preload CSS/i;
const RELOAD_STAMP_KEY = "sct-stale-build-reload-at";
const RELOAD_WINDOW_MS = 10_000;

export function isStaleBuildError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return STALE_BUILD_PATTERN.test(message);
}

/**
 * Reload once to pick up the current index.html. A second failure inside the
 * window means a reload will not fix it, so return false and let the caller
 * show the error instead of looping. No storage, no reload: same reason.
 */
export function reloadOnceForStaleBuild(reload: () => void = () => window.location.reload()): boolean {
  try {
    const last = Number(window.sessionStorage.getItem(RELOAD_STAMP_KEY) ?? 0);
    if (Date.now() - last < RELOAD_WINDOW_MS) {
      return false;
    }
    window.sessionStorage.setItem(RELOAD_STAMP_KEY, String(Date.now()));
  } catch {
    return false;
  }
  reload();
  return true;
}
