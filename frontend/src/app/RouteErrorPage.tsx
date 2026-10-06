import { useEffect, useState, type CSSProperties } from "react";
import { useRouteError } from "react-router";
import { RouteLoadingFallback } from "./RouteLoadingFallback";
import { isStaleBuildError, reloadOnceForStaleBuild } from "./staleBuild";

const pageStyle: CSSProperties = {
  alignItems: "center",
  background: "var(--bg)",
  color: "var(--ink)",
  display: "flex",
  flexDirection: "column",
  gap: "12px",
  justifyContent: "center",
  minHeight: "100dvh",
  padding: "24px",
  textAlign: "center",
};

const messageStyle: CSSProperties = {
  lineHeight: 1.5,
  margin: 0,
  maxWidth: "52ch",
};

export function RouteErrorPage() {
  const error = useRouteError();
  const staleBuild = isStaleBuildError(error);
  const [reloading, setReloading] = useState(staleBuild);

  useEffect(() => {
    if (staleBuild && !reloadOnceForStaleBuild()) {
      setReloading(false);
    }
  }, [staleBuild]);

  if (reloading) {
    return <RouteLoadingFallback />;
  }

  return (
    <main role="alert" style={pageStyle}>
      <h1 style={{ fontSize: "20px", margin: 0 }}>This page did not load</h1>
      {staleBuild ? (
        <p style={messageStyle}>
          This window is running a different Smart Commissioning version from the one now serving it. Close any
          older Smart Commissioning window, then reload. The launcher prints the address to use.
        </p>
      ) : (
        <p style={messageStyle}>{error instanceof Error ? error.message : "Unexpected application error."}</p>
      )}
      <button className="primary-button" type="button" onClick={() => window.location.reload()}>
        Reload
      </button>
    </main>
  );
}
