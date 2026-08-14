import {
  approveDetectedNmap,
  confirmNmapInstallation,
  createNmapDeploymentPolicy,
  detectNmapInstallations,
  listNmapDeploymentPolicies,
} from "./client";

const policyRequest = {
  acknowledged_no_redistribution: true,
  deployment_lane: "internal_same_organization" as const,
  deployment_owner: "Facilities IT",
  max_data_files: 8192,
  max_file_bytes: 67_108_864,
  max_manifest_bytes: 536_870_912,
  operator_install_responsibility: "Facilities IT installs and services Nmap.",
  permitted_data_manifest_sha256: ["c".repeat(64)],
  permitted_executable_sha256: ["b".repeat(64)],
  permitted_licence_sha256: ["d".repeat(64)],
  permitted_npsl_versions: ["1.1"],
  permitted_project_sites: [{ project_id: "project-a", site_id: "site-1" }],
  permitted_publishers: ["Insecure.Com LLC"],
  permitted_signer_sha256: ["a".repeat(64)],
  permitted_versions: ["7.98"],
  profile_policy: {
    permitted_profiles: ["host_discovery", "tcp_connect_inventory"] as const,
    schema_version: "1.0" as const,
  },
  provider_mode: "internal_operator_managed" as const,
  reason: "Approve the exact operator-managed installation lane.",
  reviewed_scripts: [],
  reviewed_version_policy: "Nmap 7.98 and NPSL 1.1 are reviewed.",
  update_owner: "Facilities IT",
};

describe("Nmap administration API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists the append-only deployment policy history", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("[]", {
        headers: { "Content-Type": "application/json" },
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(listNmapDeploymentPolicies()).resolves.toEqual([]);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/v1/nmap/policies");
    expect(fetchMock.mock.calls[0]?.[1]).toBeUndefined();
  });

  it("approves one detected local Nmap installation without policy form data", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ state: "available", provider: "nmap" }), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await approveDetectedNmap({ projectId: "project-a", siteId: "site-1" });

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/v1/nmap/approve-detected");
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ project_id: "project-a", site_id: "site-1" });
  });

  it("creates an exact deployment policy revision", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ...policyRequest, policy_id: "policy-1", revision: 1 }), {
        headers: { "Content-Type": "application/json" },
        status: 201,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await createNmapDeploymentPolicy(policyRequest);

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/v1/nmap/policies");
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual(policyRequest);
  });

  it("detects installations through the admin-only inspection endpoint", async () => {
    const detected = [
      {
        data_file_count: 17,
        data_manifest_sha256: "c".repeat(64),
        data_total_bytes: 400_000_000,
        display_name: "Nmap 7.98",
        executable_sha256: "b".repeat(64),
        fingerprint_sha256: "e".repeat(64),
        licence_sha256: "d".repeat(64),
        npcap_state: "raw_capable",
        npcap_version: "1.82",
        npsl_version: "1.1",
        provider: "nmap",
        publisher: "Insecure.Com LLC",
        raw_capable: true,
        reason: "available",
        registry_view: "64",
        reviewed_scripts: [],
        schema_version: "1.0",
        signer_sha256: "a".repeat(64),
        state: "available",
        version: "7.98",
      },
    ];
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(detected), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(detectNmapInstallations()).resolves.toEqual(detected);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/v1/nmap/installations/detected");
  });

  it("confirms only the selected fingerprint with an audit reason", async () => {
    const fingerprint = "e".repeat(64);
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          confirmation_id: "confirmation-1",
          fingerprint_sha256: fingerprint,
          schema_version: "1.0",
        }),
        { headers: { "Content-Type": "application/json" }, status: 201 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await confirmNmapInstallation({
      fingerprintSha256: fingerprint,
      reason: "Bind the exact inspected installation.",
    });

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/v1/nmap/installations/confirm");
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      fingerprint_sha256: fingerprint,
      reason: "Bind the exact inspected installation.",
    });
  });
});
