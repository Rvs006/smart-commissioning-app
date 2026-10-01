import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getDiscoveryPoints, type DiscoveryRowRecord } from "../../api/client";
import { queryKeys } from "../../api/queryKeys";
import { formatBacnetRouters } from "../workflow/ipDiscoveryModel";
import { isPlainObject } from "../../utils/isPlainObject";
import type { ScannerRunController } from "./useScannerRun";

function cell(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return "[Unserializable value]";
    }
  }
  return String(value);
}

function pointRow(point: DiscoveryRowRecord) {
  const attributes = isPlainObject(point.attributes) ? point.attributes : {};
  const observedValue = isPlainObject(point.observed_value) ? point.observed_value.value : undefined;
  const readError = attributes.read_error ?? point.read_error;
  return {
    device:
      attributes.device_instance ?? point.device_ref ?? point.device_instance ?? point.instance,
    object: point.point_name ?? point.point_id ?? point.object_key ?? point.object_name,
    outcome: readError ? "Read failed" : (point.outcome ?? point.status ?? "Read"),
    position: point.position,
    units: point.units ?? point.property ?? point.property_name,
    value: observedValue ?? point.value ?? point.present_value,
  };
}

/**
 * The two BACnet evidence panels the v0.1.58 module page rendered below the
 * results table: the Who-Is-Router reply summary, and the server-paged,
 * searchable point rows. Carried over unchanged in behaviour (same endpoints,
 * same bounded paging, same honest empty states) so the rebuild loses nothing.
 */
export function BacnetEvidenceCards({ run }: { run: ScannerRunController }) {
  const {
    activeRun,
    activeRunAuthoritativelyTerminal,
    apiClient,
    results,
    runAccessClosed,
    sessionScopeId,
    workspaceRef,
  } = run;
  const [search, setSearch] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);

  useEffect(() => {
    setSearch("");
    setCursor(null);
  }, [activeRun?.runId, activeRun?.epoch]);

  const pointsQuery = useQuery({
    enabled: !runAccessClosed && Boolean(activeRun) && activeRunAuthoritativelyTerminal,
    queryKey: runAccessClosed
      ? [...queryKeys.workspace(sessionScopeId, workspaceRef), "bacnet-points", "closed"]
      : activeRun?.ref
        ? [
            ...queryKeys.run(sessionScopeId, workspaceRef, activeRun.ref),
            "bacnet-points",
            "epoch",
            activeRun.epoch,
            cursor,
            search,
          ]
        : [...queryKeys.workspace(sessionScopeId, workspaceRef), "bacnet-points", "none"],
    queryFn: ({ signal }) =>
      getDiscoveryPoints(activeRun?.runId ?? "", {
        after: cursor,
        context: { client: apiClient, signal },
        limit: 100,
        search: search || undefined,
      }),
  });

  if (!activeRun || runAccessClosed || !activeRunAuthoritativelyTerminal) {
    return null;
  }

  const routers = formatBacnetRouters(results?.result_summary?.routers);

  return (
    <>
      {routers !== null && (
        <section aria-labelledby="bacnet-routers-heading" className="scanner-card">
          <div className="scanner-card-head">
            <h2 id="bacnet-routers-heading">Routers / BBMDs</h2>
            <span className="results-filter-count">
              {`${routers.length} router${routers.length === 1 ? "" : "s"}`}
            </span>
          </div>
          <div className="scanner-card-body">
            <p className="scanner-detail-note">
              BACnet/IP routers and BBMDs that answered Who-Is-Router during discovery, and the
              remote network numbers they advertise.
            </p>
          </div>
          {routers.length === 0 ? (
            <div className="empty-workspace">
              <strong>No BACnet routers responded</strong>
              <span>
                No Who-Is-Router replies were heard during discovery — a recorded result, not an
                error.
              </span>
            </div>
          ) : (
            <div className="data-table-wrap results-scroll scanner-table-wrap">
              <table className="data-table scanner-table">
                <thead>
                  <tr>
                    <th scope="col">Router address</th>
                    <th scope="col">Reachable networks</th>
                  </tr>
                </thead>
                <tbody>
                  {routers.map((router) => (
                    <tr key={router.address}>
                      <td className="mono">{router.address}</td>
                      <td className="mono">{router.networks || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      <section aria-labelledby="bacnet-points-heading" className="scanner-card">
        <div className="scanner-card-head">
          <h2 id="bacnet-points-heading">Points / live data</h2>
          <span className="results-filter-count">
            {pointsQuery.data ? `${pointsQuery.data.total} total` : "Loading point count"}
          </span>
        </div>
        <div className="results-filter-bar scanner-filter-bar">
          <label className="results-filter-text">
            Search points
            <input
              onChange={(event) => {
                setSearch(event.target.value);
                setCursor(null);
              }}
              placeholder="Device, object, units or value"
              value={search}
            />
          </label>
        </div>
        {pointsQuery.isError ? (
          <div className="state-panel error" role="alert">
            <strong>Point view unavailable</strong>
            <span>
              {pointsQuery.error instanceof Error
                ? pointsQuery.error.message
                : "The sealed point page could not be read."}
            </span>
          </div>
        ) : pointsQuery.isLoading ? (
          <div className="state-panel" role="status">
            <strong>Loading point page</strong>
            <span>The first bounded page is being verified.</span>
          </div>
        ) : (pointsQuery.data?.points?.length ?? 0) === 0 ? (
          <div className="empty-workspace">
            <strong>{search ? "No points match" : "No point rows"}</strong>
            <span>
              {search
                ? "Clear the search to inspect the complete sealed page."
                : "This run did not produce point evidence."}
            </span>
          </div>
        ) : (
          <>
            <div className="data-table-wrap results-scroll scanner-table-wrap">
              <table className="data-table scanner-table">
                <thead>
                  <tr>
                    <th scope="col">Position</th>
                    <th scope="col">Device</th>
                    <th scope="col">Object</th>
                    <th scope="col">Units</th>
                    <th scope="col">Value</th>
                    <th scope="col">Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {pointsQuery.data?.points.map((point, index) => {
                    const row = pointRow(point);
                    return (
                      <tr key={String(point.id ?? row.position ?? index)}>
                        <td className="mono">{cell(row.position)}</td>
                        <td className="mono">{cell(row.device)}</td>
                        <td>{cell(row.object)}</td>
                        <td>{cell(row.units)}</td>
                        <td className="mono">{cell(row.value)}</td>
                        <td>{cell(row.outcome)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {pointsQuery.data?.has_more && pointsQuery.data.next_cursor && (
              <div className="scanner-action-row">
                <button
                  className="secondary-button compact"
                  onClick={() => setCursor(pointsQuery.data?.next_cursor ?? null)}
                  type="button"
                >
                  Next point page
                </button>
              </div>
            )}
          </>
        )}
      </section>
    </>
  );
}
