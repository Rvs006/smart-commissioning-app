import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  getBacnetExportAssetsPath,
  getConfiguration,
  getSystemInterfaces,
} from "../../api/client";
import { queryKeys } from "../../api/queryKeys";
import { ENGINEER_REQUIRED_TOOLTIP, useSession } from "../../app/sessionContext";
import { SourceInterfaceDetails } from "../workflow/SourceInterfaceDetails";
import { resolveBacnetInstanceRange } from "../workflow/buildDiscoveryParameters";
import { BacnetEvidenceCards } from "./BacnetEvidenceCards";
import { ScannerScreen, type SetupCell } from "./ScannerScreen";
import { useScannerDownload, useScannerRun } from "./useScannerRun";

export function BacnetScannerPage() {
  const { apiClient, sessionScopeId, workspace: workspaceRef } = useSession();
  const run = useScannerRun("bacnet");
  const assetsDownload = useScannerDownload();

  const [instanceLow, setInstanceLow] = useState("");
  const [instanceHigh, setInstanceHigh] = useState("");
  const [discoverMs, setDiscoverMs] = useState("");
  const [ignoreRegister, setIgnoreRegister] = useState(false);

  const configurationQuery = useQuery({
    queryFn: ({ signal }) => getConfiguration({ client: apiClient, signal }),
    queryKey: [
      ...queryKeys.workspace(sessionScopeId, workspaceRef),
      "configuration",
      "panel-config",
    ],
    staleTime: 30_000,
  });
  const systemInterfacesQuery = useQuery({
    queryFn: ({ signal }) => getSystemInterfaces({ client: apiClient, signal }),
    queryKey: queryKeys.interfaces(sessionScopeId, workspaceRef),
  });

  const sourceInterfaceCidr = (() => {
    const config = configurationQuery.data;
    if (!config) return undefined;
    for (const section of Object.values(config)) {
      if (section.values["Source Interface"]) return section.values["Source Interface"];
    }
    return undefined;
  })();

  // Pair-or-neither: a half-filled range would silently degrade to a global
  // Who-Is on the sidecar, so Start stays blocked until it is whole or empty.
  const instanceRange = resolveBacnetInstanceRange(instanceLow, instanceHigh);

  const setupCells: SetupCell[] = [
    {
      label: "Source interface · frozen",
      value: sourceInterfaceCidr ? "From Configuration" : "Auto (OS default route)",
      sub: sourceInterfaceCidr ? `${sourceInterfaceCidr} · UDP 47808` : "UDP 47808",
    },
    {
      label: "Instance range",
      value:
        instanceRange.low !== undefined && instanceRange.high !== undefined
          ? `${instanceRange.low} – ${instanceRange.high}`
          : "Global Who-Is",
      sub: instanceRange.error ? "invalid range" : undefined,
    },
    {
      label: "Discovery window",
      value: discoverMs ? `${discoverMs} ms` : "engine default",
    },
    { label: "Method", value: "Who-Is / I-Am", sub: "vendored BACnet scanner" },
  ];

  return (
    <ScannerScreen
      inputs={{
        bacnetDiscoverMs: discoverMs,
        bacnetInstanceHigh: instanceHigh,
        bacnetInstanceLow: instanceLow,
        ignoreRegister,
      }}
      evidenceCards={<BacnetEvidenceCards run={run} />}
      onIgnoreRegisterChange={setIgnoreRegister}
      purpose="Who-Is sweep for devices and their objects — native bacnet_scanner run."
      resultsActions={
        <>
          <button
            className="secondary-button compact"
            disabled={
              !run.canEngineer || !run.saveableDeviceCount || assetsDownload.pendingKey !== null
            }
            onClick={() => {
              if (run.activeRun) {
                void assetsDownload.download({
                  fallbackFilename: `bacnet-assets-${run.activeRun.runId}.zip`,
                  key: "bacnet-assets",
                  path: getBacnetExportAssetsPath(run.activeRun.runId),
                });
              }
            }}
            title={
              run.canEngineer
                ? "Download every discovered device's object list as per-asset JSON + XLSX in a ZIP, rebuilt from this run's saved results."
                : ENGINEER_REQUIRED_TOOLTIP
            }
            type="button"
          >
            {assetsDownload.pendingKey === "bacnet-assets"
              ? "Exporting assets..."
              : "Export assets & points"}
          </button>
          {assetsDownload.error && (
            <span className="error-text">Export assets failed: {assetsDownload.error}</span>
          )}
        </>
      }
      run={run}
      setupCells={setupCells}
      setupFields={
        <>
          <div className="scanner-setup-span">
            <strong className="eyebrow">Source Interface</strong>
            <SourceInterfaceDetails
              enumerationFailed={systemInterfacesQuery.isError}
              enumerationPending={systemInterfacesQuery.isLoading}
              interfaces={
                Array.isArray(systemInterfacesQuery.data) ? systemInterfacesQuery.data : []
              }
              value={sourceInterfaceCidr ?? ""}
            />
          </div>
          <label>
            Device instance range — low
            <input
              inputMode="numeric"
              onChange={(event) => setInstanceLow(event.target.value)}
              placeholder="e.g. 1000"
              value={instanceLow}
            />
          </label>
          <label>
            Device instance range — high
            <input
              inputMode="numeric"
              onChange={(event) => setInstanceHigh(event.target.value)}
              placeholder="e.g. 1999"
              value={instanceHigh}
            />
            <small>Leave both blank for a global Who-Is across all device instances.</small>
          </label>
          <label>
            Discovery window (ms)
            <input
              inputMode="numeric"
              onChange={(event) => setDiscoverMs(event.target.value)}
              placeholder="e.g. 5000"
              value={discoverMs}
            />
            <small>How long to listen for I-Am replies. Blank uses the default window.</small>
          </label>
        </>
      }
      startBlockedReason={instanceRange.error}
      startLabel="Send Who-Is"
    />
  );
}
