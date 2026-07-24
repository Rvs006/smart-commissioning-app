import type { CSSProperties } from "react";

const routeLoadingStyle: CSSProperties = {
  alignItems: "center",
  background: "var(--bg)",
  color: "var(--ink)",
  display: "flex",
  justifyContent: "center",
  minHeight: "100dvh",
  padding: "24px",
};

const routeLoadingMessageStyle: CSSProperties = {
  fontSize: "15px",
  fontWeight: 700,
  lineHeight: 1.5,
  margin: 0,
  textAlign: "center",
};

export function RouteLoadingFallback() {
  return (
    <main aria-busy="true" style={routeLoadingStyle}>
      <p role="status" style={routeLoadingMessageStyle}>
        Loading Smart Commissioning...
      </p>
    </main>
  );
}
