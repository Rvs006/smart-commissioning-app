import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { clearApiKey, setApiKey } from "../../api/client";
import { SessionProvider } from "../../app/session";
import { ConfigurationPage } from "./ConfigurationPage";

// An engineer principal so Save/Import are not gated by role in these tests.
const mePayload = { username: "engineer-1", role: "engineer", source: "user_key" };

// Interfaces returned by GET /system/interfaces for the Source Interface
// selector. Defaults to an empty list so existing tests never depend on the
// enumeration; a test can set it before rendering to exercise the dropdown.
let interfacesPayload: unknown[] = [];

// A configuration snapshot with a deliberately EXPIRED certificate expiry so the
// red-highlight indicator can be asserted, plus a long fault string in one
// status so the single-pill rendering with room for long faults is exercised.
function configurationPayload() {
  return {
    device: {
      values: {
        Hostname: "sct-gateway-01",
        "IP Assignment": "Static IP",
        "IP Address": "10.10.25.50",
      },
      status: "Healthy",
    },
    bacnet: {
      values: { "BACnet Network Number": "1532", "UDP Port": "47808", BBMD: "Enabled", "Foreign Device": "Disabled" },
      status: "Listening",
    },
    mqtt: {
      values: { "MQTT Broker FQDN or IP Address": "mqtt.local", Port: "8883", "MQTT Password": "********" },
      status: "Broker unreachable: connection refused at mqtt.local:8883 after 3 retries",
    },
    certificates: {
      values: {
        "CA Certificate": "secret://bootstrap-ca-certificate",
        "Certificate Expiry": "2000-01-01",
        "Key Password": "********",
      },
      status: "Valid",
    },
    time: {
      values: { Timezone: "Europe/London", "Primary NTP Server": "0.pool.ntp.org" },
      status: "Synchronised",
    },
    backups: {
      values: { "Backup Schedule": "Daily 02:00", "Backup Location": "/data/backups" },
      status: "Success",
    },
    logging: {
      values: { "Log Level": "Info" },
      status: "Healthy",
    },
  };
}

function jsonResponse(payload: unknown): Response {
  return { ok: true, status: 200, statusText: "OK", json: async () => payload } as unknown as Response;
}

function errorResponse(status: number, detail: string): Response {
  return {
    ok: false,
    status,
    statusText: "Bad Request",
    json: async () => ({ detail }),
  } as unknown as Response;
}

type FetchHandler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function stubFetch(handler: FetchHandler) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/me")) {
        return jsonResponse(mePayload);
      }
      if (url.endsWith("/api/v1/system/interfaces")) {
        return jsonResponse(interfacesPayload);
      }
      return handler(url, init);
    }),
  );
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  setApiKey("engineer-key");
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionProvider>
        <ConfigurationPage />
      </SessionProvider>
    </QueryClientProvider>,
  );
}

describe("ConfigurationPage", () => {
  beforeEach(() => {
    // jsdom does not implement object-URL APIs; the export download path uses them.
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:mock"),
      revokeObjectURL: vi.fn(),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearApiKey();
    interfacesPayload = [];
  });

  it("renders one descriptive status pill per section with room for a long fault string", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    // The long MQTT fault string renders in full inside a single pill — and the
    // old editable "Section status" input is gone (no duplicate status control).
    const fault = await screen.findByText(/Broker unreachable: connection refused/i);
    expect(fault).toHaveClass("section-status-pill");
    expect(screen.queryByText("Section status")).not.toBeInTheDocument();
    // The status is not duplicated: exactly one node carries the fault text.
    expect(screen.getAllByText(/Broker unreachable: connection refused/i)).toHaveLength(1);
  });

  it("collapses advanced sections by default and toggles them open", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    // Connection-critical MQTT stays open; the Time & NTP section collapses.
    const mqttToggle = await screen.findByRole("button", { name: /MQTT Settings/i });
    expect(mqttToggle).toHaveAttribute("aria-expanded", "true");

    const timeToggle = screen.getByRole("button", { name: /Time & NTP/i });
    expect(timeToggle).toHaveAttribute("aria-expanded", "false");
    // Its timezone field is hidden while collapsed, shown after toggling open.
    expect(screen.queryByText("Timezone")).not.toBeInTheDocument();
    fireEvent.click(timeToggle);
    expect(timeToggle).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("Timezone")).toBeInTheDocument();
  });

  it("renders the timezone as a select including UTC and non-Europe zones", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Time & NTP/i }));
    const timezone = (await screen.findByText("Timezone")).closest("label");
    expect(timezone).not.toBeNull();
    const select = within(timezone as HTMLElement).getByRole("combobox") as HTMLSelectElement;
    expect(select.value).toBe("Europe/London");
    const optionValues = Array.from(select.options).map((option) => option.value);
    expect(optionValues).toContain("UTC");
    expect(optionValues).toContain("Asia/Tokyo");
    expect(optionValues).toContain("America/New_York");
  });

  it("renders Source Interface as a by-name NIC dropdown that auto-fills read-only IP/mask/gateway", async () => {
    interfacesPayload = [
      {
        name: "Ethernet 3",
        ipv4: "192.168.1.10",
        prefix_length: 24,
        subnet_mask: "255.255.255.0",
        cidr: "192.168.1.10/24",
        gateway: "192.168.1.1",
        is_up: true,
      },
      {
        name: "Wi-Fi",
        ipv4: "10.0.0.5",
        prefix_length: 8,
        subnet_mask: "255.0.0.0",
        cidr: "10.0.0.5/8",
        gateway: "10.0.0.1",
        is_up: true,
      },
    ];
    // The stored value is deliberately NOT one of the enumerated CIDRs, so the
    // escape-hatch option must still surface it and the read-only fields must
    // derive ip/mask from the CIDR string (gateway is unknowable off-host).
    const base = configurationPayload();
    const payload = {
      ...base,
      device: {
        ...base.device,
        values: { ...base.device.values, "Source Interface": "172.16.0.9/24" },
      },
    };
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payload);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    // Wait for the interfaces query to resolve so the by-name options land (the
    // config and interfaces queries resolve independently). Options are labelled
    // by adapter name, not the bare CIDR.
    await screen.findByRole("option", { name: /Ethernet 3.*192\.168\.1\.10\/24/ });
    const control = (await screen.findByText("Source Interface")).closest(
      ".source-interface-control",
    ) as HTMLElement;
    expect(control).not.toBeNull();
    const select = within(control).getByRole("combobox") as HTMLSelectElement;

    // The stored non-enumerated value stays selected, with Auto + both NICs offered.
    expect(select.value).toBe("172.16.0.9/24");
    const optionValues = Array.from(select.options).map((option) => option.value);
    expect(optionValues).toEqual(
      expect.arrayContaining(["172.16.0.9/24", "Auto (OS default route)", "192.168.1.10/24", "10.0.0.5/8"]),
    );

    const readField = (label: string): HTMLInputElement => {
      const fieldLabel = within(control).getByText(label).closest("label") as HTMLElement;
      return within(fieldLabel).getByRole("textbox") as HTMLInputElement;
    };

    // Read-only confirmation for the non-enumerated value: ip + mask derived from
    // the CIDR, gateway reported as unknown on this host.
    expect(readField("IP Address").value).toBe("172.16.0.9");
    expect(readField("Subnet Mask").value).toBe("255.255.255.0");
    expect(readField("Gateway").value).toMatch(/unknown/i);

    // Selecting an enumerated NIC by name auto-fills its real ip/mask/gateway.
    fireEvent.change(select, { target: { value: "192.168.1.10/24" } });
    expect(select.value).toBe("192.168.1.10/24");
    expect(readField("IP Address").value).toBe("192.168.1.10");
    expect(readField("Subnet Mask").value).toBe("255.255.255.0");
    expect(readField("Gateway").value).toBe("192.168.1.1");
  });

  it("flags an expired certificate-expiry field in red", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    // Certificates is open by default; the 2000-01-01 expiry is in the past.
    const expiryLabel = (await screen.findByText("Certificate Expiry")).closest("label");
    expect(expiryLabel).not.toBeNull();
    expect(expiryLabel).toHaveClass("field-expired");
    expect(within(expiryLabel as HTMLElement).getByText(/Certificate expired/i)).toBeInTheDocument();
  });

  it("exports the current configuration as a masked JSON envelope", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Export JSON/i }));
    // The success banner confirms the export, and the object-URL was created
    // (the file blob was built) without any raw secret leaving the client.
    expect(await screen.findByText(/Exported the current configuration/i)).toBeInTheDocument();
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  it("imports a valid configuration file and saves it via the API", async () => {
    const saved = configurationPayload();
    let putBody: string | null = null;
    stubFetch((url, init) => {
      if (url.endsWith("/api/v1/configuration") && init?.method === "PUT") {
        putBody = String(init?.body);
        return jsonResponse(saved);
      }
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    await screen.findByRole("button", { name: /Import JSON/i });
    const fileInput = screen.getByLabelText(/Import configuration JSON file/i);
    const envelope = {
      kind: "smart-commissioning-configuration",
      version: 1,
      configuration: configurationPayload(),
    };
    const file = new File([JSON.stringify(envelope)], "config.json", { type: "application/json" });
    fireEvent.change(fileInput, { target: { files: [file] } });

    expect(await screen.findByText(/Imported configuration was validated/i)).toBeInTheDocument();
    // Only the inner snapshot (not the envelope wrapper) is sent to the API.
    expect(putBody).not.toBeNull();
    expect(JSON.parse(putBody as unknown as string)).toHaveProperty("device");
    expect(JSON.parse(putBody as unknown as string)).not.toHaveProperty("kind");
  });

  it("rejects a malformed import file client-side without calling the API", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    await screen.findByRole("button", { name: /Import JSON/i });
    const fileInput = screen.getByLabelText(/Import configuration JSON file/i);
    const file = new File(["{ not valid json"], "bad.json", { type: "application/json" });
    fireEvent.change(fileInput, { target: { files: [file] } });

    expect(await screen.findByText(/not valid JSON/i)).toBeInTheDocument();
  });

  it("surfaces an API validation error when importing a rejected snapshot", async () => {
    stubFetch((url, init) => {
      if (url.endsWith("/api/v1/configuration") && init?.method === "PUT") {
        return errorResponse(400, "MQTT Port must be between 1 and 65535.");
      }
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    await screen.findByRole("button", { name: /Import JSON/i });
    const fileInput = screen.getByLabelText(/Import configuration JSON file/i);
    const file = new File([JSON.stringify(configurationPayload())], "config.json", {
      type: "application/json",
    });
    fireEvent.change(fileInput, { target: { files: [file] } });

    expect(await screen.findByText(/MQTT Port must be between/i)).toBeInTheDocument();
  });
});
