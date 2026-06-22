// Light/dark theme handling for the Electracom-styled console. Mirrors the
// reference app: the choice is stored in localStorage and applied via the
// `data-theme` attribute on <html>, which the warm token palette keys off.

const STORAGE_KEY = "sc.theme";

export type ThemeMode = "light" | "dark";

export function getTheme(): ThemeMode {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

export function applyTheme(mode: ThemeMode): void {
  if (mode === "dark") {
    document.documentElement.setAttribute("data-theme", "dark");
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
}

export function toggleTheme(): ThemeMode {
  const next: ThemeMode = getTheme() === "dark" ? "light" : "dark";
  applyTheme(next);
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // Private mode / storage disabled — theme still applies for this session.
  }
  return next;
}

// Run once at startup (before React renders) to avoid a flash of the wrong theme.
export function initTheme(): void {
  let stored: string | null;
  try {
    stored = localStorage.getItem(STORAGE_KEY);
  } catch {
    stored = null;
  }
  applyTheme(stored === "dark" ? "dark" : "light");
}
