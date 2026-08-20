import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { SessionBoundApiClient } from "../../api/client";
import { SessionContext, type SessionContextValue } from "../../app/sessionContext";
import { createSessionScopeId, DEFAULT_WORKSPACE } from "../../app/sessionScope";
import { NmapAuthorityPanel } from "./NmapAuthorityPanel";

type RequestHandler = (path: string, init?: RequestInit) => unknown | Promise<unknown>;

function renderPanel(
  input: { admin?: boolean; globalScope?: boolean; request?: RequestHandler } = {},
) {
  const sessionScopeId = createSessionScopeId();
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (input.request) {
      return input.request(path, init);
    }
    if (path === "/nmap/policies") {
      return [];
    }
    throw new Error(`Unexpected request: ${path}`);
  });
  const controller = new AbortController();
  const apiClient = {
    abort: () => controller.abort(),
    downloadFile: vi.fn(),
    fetchRaw: vi.fn(),
    request,
    sessionScopeId,
    signal: controller.signal,
    workspace: DEFAULT_WORKSPACE,
  } as unknown as SessionBoundApiClient;
  const admin = input.admin ?? true;
  const session: SessionContextValue = {
    apiClient,
    authorizationEnforced: true,
    canAdmin: admin,
    canEngineer: admin,
    error: null,
    hasApiKey: true,
    isLoading: false,
    me: {
      effective_scopes: [],
      global_scope: input.globalScope ?? admin,
      role: admin ? "admin" : "engineer",
      source: "user_key",
      username: admin ? "admin-1" : "engineer-1",
    },
    role: admin ? "admin" : "engineer",
    sessionScopeId,
    signIn: vi.fn(),
    signOut: vi.fn(),
    workspace: DEFAULT_WORKSPACE,
  };
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });

  return {
    request,
    ...render(
      <QueryClientProvider client={queryClient}>
        <SessionContext.Provider value={session}>
          <NmapAuthorityPanel />
        </SessionContext.Provider>
      </QueryClientProvider>,
    ),
  };
}

describe("NmapAuthorityPanel", () => {
  it("conceals global Nmap authority and makes no admin request for an engineer", () => {
    const { request } = renderPanel({ admin: false });

    expect(
      screen.queryByRole("heading", { name: "Nmap deployment authority" }),
    ).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });

  it("conceals deployment authority from an admin without global scope", () => {
    const { request } = renderPanel({ admin: true, globalScope: false });

    expect(
      screen.queryByRole("heading", { name: "Nmap deployment authority" }),
    ).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });

  it("starts disabled and keeps enabled modes unavailable for an external deployment", async () => {
    renderPanel();

    expect(await screen.findByRole("heading", { name: "Nmap deployment authority" })).toBeVisible();
    const mode = screen.getByRole("combobox", { name: "Provider mode" }) as HTMLSelectElement;
    expect(mode.value).toBe("disabled");
    mode.focus();
    expect(mode).toHaveFocus();
    expect(screen.getByRole("button", { name: "Inspect installed Nmap" })).toBeDisabled();
    const createPolicy = screen.getByRole("button", { name: "Create policy revision" });
    createPolicy.focus();
    expect(createPolicy).toHaveFocus();

    fireEvent.change(mode, { target: { value: "internal_operator_managed" } });
    fireEvent.click(
      screen.getByRole("checkbox", { name: /I acknowledge that Nmap is not redistributed/i }),
    );

    fireEvent.change(screen.getByRole("combobox", { name: "Deployment lane" }), {
      target: { value: "external_customer" },
    });

    expect(mode.value).toBe("disabled");
    expect(
      within(mode).getByRole("option", { name: "Operator-managed Nmap process" }),
    ).toBeDisabled();
    expect(within(mode).getByRole("option", { name: "Operator XML import" })).toBeDisabled();
    expect(
      screen.queryByRole("checkbox", { name: /I acknowledge that Nmap is not redistributed/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/External customer deployments must remain disabled/i)).toBeVisible();
  });

  it("creates the exact internal process policy from labeled controls", async () => {
    const calls: Array<{ path: string; init?: RequestInit }> = [];
    renderPanel({
      request: (path, init) => {
        calls.push({ path, init });
        if (path === "/nmap/policies" && !init) {
          return [];
        }
        if (path === "/nmap/policies" && init?.method === "POST") {
          return {
            ...JSON.parse(String(init.body)),
            created_at: "2026-08-12T12:00:00Z",
            created_by: "admin-1",
            deployment_id: "deployment-1",
            network_executor_id: "executor-1",
            organization_internal: true,
            policy_id: "policy-1",
            revision: 1,
            reviewed_at: "2026-08-12T12:00:00Z",
            schema_version: "1.0",
            supersedes_policy_id: null,
          };
        }
        throw new Error(`Unexpected request: ${path}`);
      },
    });
    await screen.findByRole("heading", { name: "Nmap deployment authority" });

    fireEvent.change(screen.getByRole("combobox", { name: "Provider mode" }), {
      target: { value: "internal_operator_managed" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Deployment owner" }), {
      target: { value: "Facilities IT" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Installation responsibility" }), {
      target: { value: "Facilities IT installs and services Nmap." },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Update owner" }), {
      target: { value: "Facilities IT" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Reviewed version policy" }), {
      target: { value: "Nmap 7.98 and NPSL 1.1 are reviewed." },
    });
    const values: Array<[string, string]> = [
      ["Permitted publisher", "Insecure.Com LLC"],
      ["Permitted Nmap version", "7.98"],
      ["Signer SHA-256", "a".repeat(64)],
      ["Executable SHA-256", "b".repeat(64)],
      ["Data manifest SHA-256", "c".repeat(64)],
      ["Licence SHA-256", "d".repeat(64)],
      ["NPSL version", "1.1"],
    ];
    for (const [name, value] of values) {
      fireEvent.change(screen.getByRole("textbox", { name }), { target: { value } });
    }
    fireEvent.click(screen.getByRole("checkbox", { name: "Host discovery" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "TCP connect inventory" }));
    fireEvent.click(
      screen.getByRole("checkbox", { name: /I acknowledge that Nmap is not redistributed/i }),
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Policy audit reason" }), {
      target: { value: "Approve the exact operator-managed installation lane." },
    });
    fireEvent.submit(screen.getByRole("form", { name: "Create Nmap deployment policy" }));

    await waitFor(() =>
      expect(
        calls.some((call) => call.path === "/nmap/policies" && call.init?.method === "POST"),
      ).toBe(true),
    );
    const body = JSON.parse(String(calls.find((call) => call.init?.method === "POST")?.init?.body));
    expect(body).toEqual({
      acknowledged_no_redistribution: true,
      deployment_lane: "internal_same_organization",
      deployment_owner: "Facilities IT",
      max_data_files: 8192,
      max_file_bytes: 67_108_864,
      max_manifest_bytes: 536_870_912,
      operator_install_responsibility: "Facilities IT installs and services Nmap.",
      permitted_data_manifest_sha256: ["c".repeat(64)],
      permitted_executable_sha256: ["b".repeat(64)],
      permitted_licence_sha256: ["d".repeat(64)],
      permitted_npsl_versions: ["1.1"],
      permitted_project_sites: [{ project_id: "demo-project", site_id: "demo-site" }],
      permitted_publishers: ["Insecure.Com LLC"],
      permitted_signer_sha256: ["a".repeat(64)],
      permitted_versions: ["7.98"],
      profile_policy: {
        permitted_profiles: ["host_discovery", "tcp_connect_inventory"],
        schema_version: "1.0",
      },
      provider_mode: "internal_operator_managed",
      reason: "Approve the exact operator-managed installation lane.",
      reviewed_scripts: [],
      reviewed_version_policy: "Nmap 7.98 and NPSL 1.1 are reviewed.",
      update_owner: "Facilities IT",
    });
  });

  it("inspects and confirms an exact path-free installation identity", async () => {
    const fingerprint = "e".repeat(64);
    const calls: Array<{ path: string; init?: RequestInit }> = [];
    const detected = {
      data_file_count: 17,
      data_manifest_sha256: "c".repeat(64),
      data_total_bytes: 400_000_000,
      display_name: "Nmap 7.98",
      executable_path: "C:\\Program Files\\Nmap\\nmap.exe",
      executable_sha256: "b".repeat(64),
      fingerprint_sha256: fingerprint,
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
    };
    const { container } = renderPanel({
      request: (path, init) => {
        calls.push({ path, init });
        if (path === "/nmap/policies") {
          return [
            {
              acknowledged_no_redistribution: true,
              created_at: "2026-08-12T12:00:00Z",
              created_by: "admin-1",
              deployment_id: "deployment-1",
              deployment_lane: "internal_same_organization",
              deployment_owner: "Facilities IT",
              max_data_files: 8192,
              max_file_bytes: 67_108_864,
              max_manifest_bytes: 536_870_912,
              network_executor_id: "executor-1",
              operator_install_responsibility: "Facilities IT installs Nmap.",
              organization_internal: true,
              permitted_data_manifest_sha256: ["c".repeat(64)],
              permitted_executable_sha256: ["b".repeat(64)],
              permitted_licence_sha256: ["d".repeat(64)],
              permitted_npsl_versions: ["1.1"],
              permitted_project_sites: [{ project_id: "demo-project", site_id: "demo-site" }],
              permitted_publishers: ["Insecure.Com LLC"],
              permitted_signer_sha256: ["a".repeat(64)],
              permitted_versions: ["7.98"],
              policy_id: "policy-1",
              profile_policy: {
                permitted_profiles: ["tcp_connect_inventory"],
                schema_version: "1.0",
              },
              provider_mode: "internal_operator_managed",
              reason: "Approved process policy.",
              revision: 1,
              reviewed_at: "2026-08-12T12:00:00Z",
              reviewed_scripts: [],
              reviewed_version_policy: "Nmap 7.98 is reviewed.",
              schema_version: "1.0",
              supersedes_policy_id: null,
              update_owner: "Facilities IT",
            },
          ];
        }
        if (path === "/nmap/installations/detected") {
          return [detected];
        }
        if (path === "/nmap/installations/confirm" && init?.method === "POST") {
          return {
            ...detected,
            confirmation_id: "confirmation-1",
            confirmed_at: "2026-08-12T12:05:00Z",
            confirmed_by: "admin-1",
            deployment_id: "deployment-1",
            network_executor_id: "executor-1",
            policy_id: "policy-1",
            policy_revision: 1,
          };
        }
        throw new Error(`Unexpected request: ${path}`);
      },
    });
    await screen.findByText("Approved process policy.");
    const exactPolicy = screen.getByRole("group", { name: "Exact policy revision 1" });
    expect(within(exactPolicy).getByText("Insecure.Com LLC")).toBeVisible();
    expect(within(exactPolicy).getByText("tcp_connect_inventory")).toBeVisible();
    expect(within(exactPolicy).getByText("d".repeat(64))).toBeVisible();
    expect(within(exactPolicy).getByText("1.1")).toBeVisible();
    expect(within(exactPolicy).getByText("Recorded")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Inspect installed Nmap" }));
    const identity = await screen.findByRole("group", {
      name: `Detected Nmap identity ${fingerprint}`,
    });
    expect(within(identity).getByText("Insecure.Com LLC")).toBeVisible();
    expect(within(identity).getByText("7.98")).toBeVisible();
    expect(within(identity).getByText(fingerprint)).toBeVisible();
    expect(within(identity).getByText("1.1")).toBeVisible();
    expect(within(identity).getByText("d".repeat(64))).toBeVisible();
    expect(container).not.toHaveTextContent("C:\\Program Files");
    expect(
      screen.queryByLabelText(/path|command|argument|download|elevation/i),
    ).not.toBeInTheDocument();

    const confirm = screen.getByRole("button", { name: "Confirm exact installation" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Confirmation audit reason" }), {
      target: { value: "Bind the exact inspected installation." },
    });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    await screen.findByText(/Exact installation confirmed/i);
    const confirmation = calls.find((call) => call.path === "/nmap/installations/confirm");
    expect(JSON.parse(String(confirmation?.init?.body))).toEqual({
      fingerprint_sha256: fingerprint,
      reason: "Bind the exact inspected installation.",
    });
  });
});
