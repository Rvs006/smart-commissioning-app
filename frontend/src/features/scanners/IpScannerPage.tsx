import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getConfiguration, getSystemInterfaces } from "../../api/client";
import { queryKeys } from "../../api/queryKeys";
import { useSession } from "../../app/sessionContext";
import { SourceInterfaceDetails } from "../workflow/SourceInterfaceDetails";
import { hostRangeFromCidr } from "../workflow/ipRange";
import { ScannerScreen, type SetupCell } from "./ScannerScreen";
import { useScannerRun } from "./useScannerRun";

export function IpScannerPage() {
  const { apiClient, sessionScopeId, workspace: workspaceRef } = useSession();
  const run = useScannerRun("ip");

  const [scanRangeStart, setScanRangeStart] = useState("");
  const [scanRangeEnd, setScanRangeEnd] = useState("");
  const [probeTimeout, setProbeTimeout] = useState("1000");
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

  // Auto-subnet: prefill the range from the configured Source Interface's cidr
  // the first time it loads, only when the operator has not typed one. Same
  // behaviour (and the same one-shot guard) as the v0.1.58 module page.
  const autoSubnetApplied = useRef(false);
  useEffect(() => {
    if (autoSubnetApplied.current || !configurationQuery.data) return;
    autoSubnetApplied.current = true;
    if (scanRangeStart || scanRangeEnd) return;
    const range = sourceInterfaceCidr ? hostRangeFromCidr(sourceInterfaceCidr) : null;
    if (range) {
      setScanRangeStart(range.start);
      setScanRangeEnd(range.end);
    }
  }, [configurationQuery.data, scanRangeEnd, scanRangeStart, sourceInterfaceCidr]);

  const setupCells: SetupCell[] = [
    {
      label: "Source interface",
      value: sourceInterfaceCidr ? "From Configuration" : "Auto (OS default route)",
      sub: sourceInterfaceCidr,
    },
    {
      label: "Range",
      value: scanRangeStart || "not set",
      sub: scanRangeEnd ? `— ${scanRangeEnd}` : "single host",
    },
    { label: "Per-host timeout", value: probeTimeout ? `${probeTimeout} ms` : "engine default" },
    { label: "Method", value: "TCP connect", sub: "vendored IP scanner" },
  ];

  const startBlockedReason = scanRangeStart.trim() === "" ? "Enter a start address to scan." : null;

  return (
    <ScannerScreen
      inputs={{ ignoreRegister, probeTimeout, scanRangeEnd, scanRangeStart }}
      onIgnoreRegisterChange={setIgnoreRegister}
      purpose="Find reachable, missing and unexpected hosts — native ip_scanner run."
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
            Start IP
            <input
              inputMode="decimal"
              onChange={(event) => setScanRangeStart(event.target.value)}
              placeholder="10.0.10.1"
              value={scanRangeStart}
            />
          </label>
          <label>
            End IP
            <input
              inputMode="decimal"
              onChange={(event) => setScanRangeEnd(event.target.value)}
              placeholder="10.0.10.254"
              value={scanRangeEnd}
            />
            <small>A start address is required. Leave the end blank to scan a single host.</small>
          </label>
          <label>
            Per-probe timeout (ms)
            <input
              inputMode="numeric"
              onChange={(event) => setProbeTimeout(event.target.value)}
              placeholder="1000"
              value={probeTimeout}
            />
            <small>How long to wait for each host to answer. Blank uses the default.</small>
          </label>
        </>
      }
      startBlockedReason={startBlockedReason}
      startLabel="Start scan"
    />
  );
}
