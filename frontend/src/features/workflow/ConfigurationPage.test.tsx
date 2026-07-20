import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { clearApiKey, setApiKey } from "../../api/client";
import { ENGINEER_REQUIRED_TOOLTIP } from "../../app/sessionContext";
import { SessionProvider } from "../../app/session";
import { ConfigurationPage } from "./ConfigurationPage";

// An engineer principal so Save/Import are not gated by role in these tests.
const mePayload = { username: "engineer-1", role: "engineer", source: "user_key" };

// Interfaces returned by GET /system/interfaces for the Source Interface
// selector. Defaults to an empty list so existing tests never depend on the
// enumeration; a test can set it before rendering to exercise the dropdown.
let interfacesPayload: unknown[] = [];
// When true, GET /system/interfaces fails (500) so the enumerationFailed
// branch of the details panel can be exercised.
let interfacesFailure = false;

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
      values: {
        "MQTT Broker FQDN or IP Address": "mqtt.local",
        Port: "8883",
        "Use TLS": "Enabled",
        "MQTT Password": "********",
      },
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

// A full SystemInterface fixture (all nine contract fields) with overridable
// parts, so each test only spells out what it exercises.
function interfaceFixture(overrides: Record<string, unknown> = {}) {
  return {
    name: "Ethernet 3",
    ipv4: "192.168.1.10",
    prefix_length: 24,
    cidr: "192.168.1.10/24",
    is_up: true,
    adapter_type: "ethernet",
    subnet_mask: "255.255.255.0",
    gateway: "192.168.1.1",
    dns_servers: ["192.168.1.53", "8.8.8.8"],
    ...overrides,
  };
}

// The base configuration payload with a Source Interface value stored in the
// device section, so the NIC selector and its details panel render.
function payloadWithSourceInterface(value: string) {
  const base = configurationPayload();
  return {
    ...base,
    device: {
      ...base.device,
      values: { ...base.device.values, "Source Interface": value },
    },
  };
}

// A configuration payload whose logging section carries the real v0.1.13 fields
// (no syslog fields), so the logging-destination tests render the section.
function loggingPayload(loggingOverrides: Record<string, string> = {}) {
  const base = configurationPayload();
  return {
    ...base,
    logging: {
      values: {
        "Log Level": "Info",
        "Log Retention": "30 days",
        "Diagnostics Mode": "Disabled",
        "Log Upload URL": "",
        "Log Upload Token": "********",
        ...loggingOverrides,
      },
      status: "Local file",
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
        return interfacesFailure
          ? errorResponse(500, "interface enumeration failed")
          : jsonResponse(interfacesPayload);
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
    interfacesFailure = false;
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

  it("shows a non-revealing 'Saved — hidden' indicator for a stored password and never reveals the sentinel", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    // MQTT Settings is expanded by default, so the password field is present.
    const passwordLabel = (await screen.findByText("MQTT Password")).closest("label");
    expect(passwordLabel).not.toBeNull();
    const input = within(passwordLabel as HTMLElement).getByDisplayValue("********") as HTMLInputElement;
    // The adjacent indicator must not pollute the input's accessible name.
    expect(input).toHaveAccessibleName("MQTT Password");

    // A stored secret is the write-only sentinel, not the real password. There
    // is no Show toggle to reveal it — clicking Show would only render eight
    // literal asterisks (ISSUE-1). Instead a non-revealing "Saved — hidden"
    // indicator and a hint explaining how to replace it are shown, and the input
    // stays masked so the sentinel can never render as text.
    expect(input.type).toBe("password");
    expect((passwordLabel as HTMLElement).querySelector(".secret-stored-note")).not.toBeNull();
    expect(within(passwordLabel as HTMLElement).getByText(/never displayed/i)).toBeInTheDocument();
    expect(
      within(passwordLabel as HTMLElement).queryByRole("button", { name: /Show MQTT Password/i }),
    ).toBeNull();
  });

  it("restores a working repeatable Show/Hide toggle once a new password is typed", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const passwordLabel = (await screen.findByText("MQTT Password")).closest("label");
    const input = within(passwordLabel as HTMLElement).getByDisplayValue("********") as HTMLInputElement;

    // Focusing the stored-secret field blanks it (the onFocus swap); typing a
    // replacement leaves the sentinel behind, so the real Show/Hide toggle
    // returns and the "Saved — hidden" indicator disappears.
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "s3cret-new" } });
    expect((passwordLabel as HTMLElement).querySelector(".secret-stored-note")).toBeNull();

    // Masked by default, revealed on click, re-hidden on a second click, and it
    // keeps working past the first toggle (the original one-view bug).
    expect(input.type).toBe("password");
    fireEvent.click(within(passwordLabel as HTMLElement).getByRole("button", { name: /Show MQTT Password/i }));
    expect(input.type).toBe("text");
    fireEvent.click(within(passwordLabel as HTMLElement).getByRole("button", { name: /Hide MQTT Password/i }));
    expect(input.type).toBe("password");
    fireEvent.click(within(passwordLabel as HTMLElement).getByRole("button", { name: /Show MQTT Password/i }));
    expect(input.type).toBe("text");
  });

  it("does not misread operator-typed asterisks in a blank password field as a saved secret (ISSUE-1)", async () => {
    const base = configurationPayload();
    const blankPassword = {
      ...base,
      mqtt: { ...base.mqtt, values: { ...base.mqtt.values, "MQTT Password": "" } },
    };
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(blankPassword);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const passwordLabel = (await screen.findByText("MQTT Password")).closest("label");
    expect(passwordLabel).not.toBeNull();
    const input = (passwordLabel as HTMLElement).querySelector("input") as HTMLInputElement;

    // Blank server value => an ordinary field: no stored-secret indicator and a
    // live Show/Hide toggle.
    expect((passwordLabel as HTMLElement).querySelector(".secret-stored-note")).toBeNull();
    expect(
      within(passwordLabel as HTMLElement).getByRole("button", { name: /Show MQTT Password/i }),
    ).toBeInTheDocument();

    // Asterisks the operator types are their OWN draft, never the server-echoed
    // sentinel: the field must not flip to "Saved — hidden" or drop the toggle,
    // and Show must still reveal exactly what was typed (ISSUE-1).
    fireEvent.change(input, { target: { value: "****" } });
    expect((passwordLabel as HTMLElement).querySelector(".secret-stored-note")).toBeNull();
    expect(within(passwordLabel as HTMLElement).queryByText(/never displayed/i)).toBeNull();
    fireEvent.click(within(passwordLabel as HTMLElement).getByRole("button", { name: /Show MQTT Password/i }));
    expect(input.type).toBe("text");
    expect(input.value).toBe("****");
  });

  it("renders a secure/non-secure MQTT connection selector (Use TLS)", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const useTlsLabel = (await screen.findByText("Use TLS")).closest("label");
    expect(useTlsLabel).not.toBeNull();
    const select = within(useTlsLabel as HTMLElement).getByRole("combobox") as HTMLSelectElement;
    expect(select.value).toBe("Enabled");
    const optionValues = Array.from(select.options).map((option) => option.value);
    expect(optionValues).toEqual(["Enabled", "Disabled"]);

    // The operator can switch to a non-secure connection.
    fireEvent.change(select, { target: { value: "Disabled" } });
    expect(select.value).toBe("Disabled");
  });

  // v0.1.12 — the Foreign Device unlock.
  //
  // Until now the UI refused to let Foreign Device be enabled while BBMD was
  // Enabled, and the seeded default is BBMD=Enabled. So on a default install the
  // one setting BACnet discovery depends on could not be turned on at all: the
  // select was disabled, changeValue rejected the edit, flipping BBMD back to
  // Enabled force-reset it, and the load-time normalizer reset it again on every
  // page load. A field engineer hit exactly this and got a silent zero-device
  // scan. The two controls are independent — this app is never itself a BBMD —
  // so all four blockers are gone and these tests hold them gone.
  //
  // The fixture is Pete's shape: BBMD "Enabled", Foreign Device "Disabled".
  it("lets Foreign Device be enabled while BBMD is Enabled (v0.1.12 unlock)", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const fdLabel = (await screen.findByText("Foreign Device")).closest("label");
    expect(fdLabel).not.toBeNull();
    const select = within(fdLabel as HTMLElement).getByRole("combobox") as HTMLSelectElement;
    expect(select.disabled).toBe(false);
    expect(within(fdLabel as HTMLElement).queryByText(/Locked because BBMD is enabled/i)).not.toBeInTheDocument();

    // The edit sticks — changeValue no longer swallows it.
    fireEvent.change(select, { target: { value: "Enabled" } });
    expect(select.value).toBe("Enabled");
  });

  it("does not reset Foreign Device when BBMD is switched back to Enabled", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const fdSelect = within((await screen.findByText("Foreign Device")).closest("label") as HTMLElement).getByRole(
      "combobox",
    ) as HTMLSelectElement;
    const bbmdSelect = within(screen.getByText("BBMD").closest("label") as HTMLElement).getByRole(
      "combobox",
    ) as HTMLSelectElement;

    fireEvent.change(fdSelect, { target: { value: "Enabled" } });
    fireEvent.change(bbmdSelect, { target: { value: "Disabled" } });
    fireEvent.change(bbmdSelect, { target: { value: "Enabled" } });

    // The old auto-reset made this "Disabled" — silently discarding the
    // operator's choice as a side effect of touching an unrelated toggle.
    expect(fdSelect.value).toBe("Enabled");
    expect(bbmdSelect.value).toBe("Enabled");
  });

  it("loads a saved Foreign Device = Enabled snapshot without resetting it", async () => {
    // The load-time normalizer was the deepest of the four blockers: even with
    // the select enabled, a stored FD=Enabled alongside BBMD=Enabled was reset
    // to Disabled on every load, so the value could never survive a refresh.
    const payload = configurationPayload();
    // BBMD stays Enabled (fixture default) — the combination the normalizer used
    // to treat as impossible.
    payload.bacnet.values["Foreign Device"] = "Enabled";
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payload);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const fdSelect = within((await screen.findByText("Foreign Device")).closest("label") as HTMLElement).getByRole(
      "combobox",
    ) as HTMLSelectElement;
    expect(fdSelect.value).toBe("Enabled");
  });

  it("tells the operator Foreign Device is the setting discovery uses, and BBMD is not", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const fdLabel = (await screen.findByText("Foreign Device")).closest("label");
    expect(fdLabel).toHaveAttribute("title", expect.stringMatching(/discovery actually uses/i));
    expect(fdLabel).toHaveAttribute("title", expect.stringMatching(/reaches devices on other subnets/i));
    // The stale "(locked when BBMD is enabled)" claim is gone.
    expect(fdLabel).not.toHaveAttribute("title", expect.stringMatching(/locked/i));

    const bbmdLabel = screen.getByText("BBMD").closest("label");
    expect(bbmdLabel).toHaveAttribute("title", expect.stringMatching(/Discovery does not read this toggle/i));
  });

  it("warns that a TLS connection to an IP literal needs the certificate SAN", async () => {
    const payload = configurationPayload();
    // Use TLS stays Enabled (fixture default); the broker is a bare IP literal.
    payload.mqtt.values["MQTT Broker FQDN or IP Address"] = "10.0.0.5";
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payload);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const brokerLabel = (await screen.findByText("MQTT Broker FQDN or IP Address")).closest("label");
    expect(brokerLabel).not.toBeNull();
    expect(
      within(brokerLabel as HTMLElement).getByText(/certificate to list this IP address \(SAN\)/i),
    ).toBeInTheDocument();
  });

  it("does not show the TLS-by-IP SAN hint when the broker is a hostname", async () => {
    // Fixture default: Use TLS Enabled, broker host "mqtt.local" (not an IP).
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const brokerLabel = (await screen.findByText("MQTT Broker FQDN or IP Address")).closest("label");
    expect(brokerLabel).not.toBeNull();
    expect(
      within(brokerLabel as HTMLElement).queryByText(/certificate to list this IP address \(SAN\)/i),
    ).not.toBeInTheDocument();
  });

  it("warns when Use TLS is Disabled but the port is the standard TLS port 8883", async () => {
    const payload = configurationPayload();
    // Port stays 8883 (fixture default); flip Use TLS off to create the mismatch.
    payload.mqtt.values["Use TLS"] = "Disabled";
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payload);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const useTlsLabel = (await screen.findByText("Use TLS")).closest("label");
    expect(useTlsLabel).not.toBeNull();
    expect(
      within(useTlsLabel as HTMLElement).getByText(/Port 8883 is the standard TLS port/i),
    ).toBeInTheDocument();
  });

  it("does not warn about a port mismatch for the matched Use TLS Enabled + port 8883 pairing", async () => {
    // Fixture default: Use TLS Enabled, Port 8883 — the expected, matched pairing.
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const useTlsLabel = (await screen.findByText("Use TLS")).closest("label");
    expect(useTlsLabel).not.toBeNull();
    expect(within(useTlsLabel as HTMLElement).queryByText(/standard TLS port/i)).not.toBeInTheDocument();
    expect(within(useTlsLabel as HTMLElement).queryByText(/standard plaintext port/i)).not.toBeInTheDocument();
  });

  it("renders Source Interface as a select of enumerated NICs plus Auto, keeping a stored non-enumerated value", async () => {
    interfacesPayload = [
      interfaceFixture(),
      interfaceFixture({
        adapter_type: "wifi",
        cidr: "10.0.0.5/8",
        dns_servers: [],
        gateway: "10.0.0.1",
        ipv4: "10.0.0.5",
        name: "Wi-Fi",
        prefix_length: 8,
        subnet_mask: "255.0.0.0",
      }),
    ];
    // The stored value is deliberately NOT one of the enumerated CIDRs, so the
    // FieldControl !options.includes(value) escape hatch must still surface it.
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("172.16.0.9/24"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    // Wait for the interfaces query to resolve so the enumerated options land
    // (the config query and interfaces query resolve independently). Labels
    // now carry the adapter name and type tag; VALUES stay the bare cidr.
    await screen.findByRole("option", { name: /192\.168\.1\.10\/24 — Ethernet 3/ });
    const sourceLabel = (await screen.findByText("Source Interface")).closest("label");
    expect(sourceLabel).not.toBeNull();
    const select = within(sourceLabel as HTMLElement).getByRole("combobox") as HTMLSelectElement;
    // The stored non-enumerated value stays selected and rendered.
    expect(select.value).toBe("172.16.0.9/24");
    const optionValues = Array.from(select.options).map((option) => option.value);
    expect(optionValues).toContain("Auto (OS default route)");
    expect(optionValues).toContain("192.168.1.10/24");
    expect(optionValues).toContain("10.0.0.5/8");
    expect(optionValues).toContain("172.16.0.9/24");
  });

  it("lists virtual adapters with a pick-with-care tag instead of hiding them", async () => {
    interfacesPayload = [
      interfaceFixture(),
      interfaceFixture({
        adapter_type: "virtual",
        cidr: "172.28.0.1/20",
        dns_servers: [],
        gateway: null,
        ipv4: "172.28.0.1",
        name: "vEthernet (WSL)",
        prefix_length: 20,
        subnet_mask: "255.255.240.0",
      }),
      interfaceFixture({
        adapter_type: "wifi",
        cidr: "10.0.0.5/8",
        dns_servers: [],
        gateway: "10.0.0.1",
        ipv4: "10.0.0.5",
        name: "Wi-Fi",
        prefix_length: 8,
        subnet_mask: "255.0.0.0",
      }),
    ];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("Auto (OS default route)"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    await screen.findByRole("option", { name: /192\.168\.1\.10\/24 — Ethernet 3/ });
    const sourceLabel = (await screen.findByText("Source Interface")).closest("label");
    const select = within(sourceLabel as HTMLElement).getByRole("combobox") as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((option) => option.value);
    expect(optionValues).toContain("192.168.1.10/24");
    expect(optionValues).toContain("10.0.0.5/8");
    // Virtual adapters are offered (on Hyper-V vSwitch hosts they can be the
    // only routable NIC) but carry an explicit pick-with-care label.
    expect(optionValues).toContain("172.28.0.1/20");
    expect(
      screen.getByRole("option", {
        name: /172\.28\.0\.1\/20 — vEthernet \(WSL\) \(Virtual — pick only if this adapter carries the site network/,
      }),
    ).toBeInTheDocument();
  });

  it("offers an only-up-virtual adapter without auto-picking it or hinting (Hyper-V host)", async () => {
    // Pete's field case (2026-07-14): the machine's only routable adapter is
    // a Hyper-V vEthernet flagged virtual. It must be OFFERED (the fix), but
    // never auto-picked by the wired-first default, and the multi-adapter
    // Auto hint must stay quiet.
    interfacesPayload = [
      interfaceFixture({
        adapter_type: "virtual",
        cidr: "10.10.90.10/24",
        dns_servers: [],
        gateway: "10.10.90.1",
        ipv4: "10.10.90.10",
        name: "vEthernet (OT)",
        prefix_length: 24,
        subnet_mask: "255.255.255.0",
      }),
    ];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface(""));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    await screen.findByRole("option", { name: /10\.10\.90\.10\/24 — vEthernet \(OT\)/ });
    const sourceLabel = (await screen.findByText("Source Interface")).closest("label");
    const select = within(sourceLabel as HTMLElement).getByRole("combobox") as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toContain("10.10.90.10/24");
    // Never-chosen stays Auto: virtual adapters are excluded from the
    // wired-first default even when they are the only adapter up.
    expect(select.value).toBe("Auto (OS default route)");
    expect(screen.queryByText(/Multiple active adapters detected/i)).not.toBeInTheDocument();
  });

  it("tags the Wi-Fi option as not recommended for commissioning traffic", async () => {
    interfacesPayload = [
      interfaceFixture(),
      interfaceFixture({
        adapter_type: "wifi",
        cidr: "10.0.0.5/8",
        dns_servers: [],
        gateway: "10.0.0.1",
        ipv4: "10.0.0.5",
        name: "Wi-Fi",
        prefix_length: 8,
        subnet_mask: "255.0.0.0",
      }),
    ];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("Auto (OS default route)"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const wifiOption = await screen.findByRole("option", {
      name: /not recommended for commissioning traffic/,
    });
    expect((wifiOption as HTMLOptionElement).value).toBe("10.0.0.5/8");
  });

  it("shows read-only OS details for the selected source interface", async () => {
    interfacesPayload = [interfaceFixture()];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("192.168.1.10/24"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    const subnetMask = await screen.findByDisplayValue("255.255.255.0");
    expect(subnetMask).toHaveAttribute("readonly");
    for (const detail of ["Ethernet 3 (Ethernet)", "192.168.1.1", "192.168.1.53", "8.8.8.8"]) {
      const input = screen.getByDisplayValue(detail);
      expect(input).toHaveAttribute("readonly");
    }
    expect(screen.getByText(/Windows manages these adapter settings/i)).toBeInTheDocument();
    // The read-only laptop values are visibly and programmatically grouped so
    // they cannot be confused with the editable planned-device fields above.
    expect(screen.getByText(/Selected adapter — this laptop, read-only/i)).toBeInTheDocument();
    expect(
      screen.getByRole("group", { name: /Selected adapter \(this laptop, read-only\)/i }),
    ).toBeInTheDocument();
  });

  it("renders an em-dash for a null gateway and a missing secondary DNS", async () => {
    interfacesPayload = [interfaceFixture({ dns_servers: ["192.168.1.53"], gateway: null })];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("192.168.1.10/24"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    await screen.findByDisplayValue("192.168.1.53");
    // Default gateway (null) and Secondary DNS (absent) both render as "—".
    expect(screen.getAllByDisplayValue("—")).toHaveLength(2);
  });

  it("shows the Auto multi-adapter hint when more than one eligible adapter is up", async () => {
    interfacesPayload = [
      interfaceFixture(),
      interfaceFixture({
        adapter_type: "usb_ethernet",
        cidr: "10.20.30.7/24",
        dns_servers: [],
        gateway: null,
        ipv4: "10.20.30.7",
        name: "Ethernet 4",
      }),
    ];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("Auto (OS default route)"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    expect(await screen.findByText(/Multiple active adapters detected/i)).toBeInTheDocument();
  });

  it("does not show the Auto hint when only one eligible adapter is up", async () => {
    interfacesPayload = [
      interfaceFixture(),
      interfaceFixture({
        cidr: "192.168.99.4/24",
        dns_servers: [],
        gateway: null,
        ipv4: "192.168.99.4",
        is_up: false,
        name: "Ethernet 2",
      }),
      // An UP virtual adapter must not count toward the hint: WSL/Hyper-V
      // laptops always have one, and the hint is about real site adapters.
      interfaceFixture({
        adapter_type: "virtual",
        cidr: "172.28.0.1/20",
        dns_servers: [],
        gateway: null,
        ipv4: "172.28.0.1",
        name: "vEthernet (WSL)",
        prefix_length: 20,
        subnet_mask: "255.255.240.0",
      }),
    ];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("Auto (OS default route)"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    await screen.findByRole("option", { name: /192\.168\.1\.10\/24 — Ethernet 3/ });
    expect(screen.queryByText(/Multiple active adapters detected/i)).not.toBeInTheDocument();
  });

  it("does not show the Auto hint for one adapter carrying two IPv4 addresses", async () => {
    // The backend emits one SystemInterface entry per AF_INET address of the
    // SAME adapter (secondary static IP — a common field pattern). One
    // physical adapter is up, so the multi-adapter hint must stay hidden.
    interfacesPayload = [
      interfaceFixture(),
      interfaceFixture({ cidr: "10.0.50.2/24", ipv4: "10.0.50.2" }),
    ];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("Auto (OS default route)"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    await screen.findByRole("option", { name: /192\.168\.1\.10\/24 — Ethernet 3/ });
    expect(screen.queryByText(/Multiple active adapters detected/i)).not.toBeInTheDocument();
  });

  it("treats a stored case-variant of Auto as Auto: hint shows and no stray option renders", async () => {
    // Backend validation and dispatch casefold the sentinel, so an imported
    // "auto (os default route)" is a saved, valid Auto value. The hint must
    // fire and the select must show the canonical Auto option, not an extra
    // lowercase escape-hatch option.
    interfacesPayload = [
      interfaceFixture(),
      interfaceFixture({
        adapter_type: "usb_ethernet",
        cidr: "10.20.30.7/24",
        dns_servers: [],
        gateway: null,
        ipv4: "10.20.30.7",
        name: "Ethernet 4",
      }),
    ];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("auto (os default route)"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    expect(await screen.findByText(/Multiple active adapters detected/i)).toBeInTheDocument();
    const sourceLabel = (await screen.findByText("Source Interface")).closest("label");
    const select = within(sourceLabel as HTMLElement).getByRole("combobox") as HTMLSelectElement;
    expect(select.value).toBe("Auto (OS default route)");
    const optionValues = Array.from(select.options).map((option) => option.value);
    expect(optionValues).not.toContain("auto (os default route)");
  });

  it("defaults an unset Source Interface to the first up wired adapter", async () => {
    // Stored value is empty (never chosen). The down Ethernet is skipped and
    // Wi-Fi is never a default, so the up USB-Ethernet adapter is pre-selected
    // in the dropdown as ordinary draft state (saved like a manual pick).
    interfacesPayload = [
      interfaceFixture({
        cidr: "192.168.99.4/24",
        dns_servers: [],
        gateway: null,
        ipv4: "192.168.99.4",
        is_up: false,
        name: "Ethernet 2",
      }),
      interfaceFixture({
        adapter_type: "usb_ethernet",
        cidr: "10.20.30.7/24",
        dns_servers: [],
        gateway: null,
        ipv4: "10.20.30.7",
        name: "Ethernet 4",
      }),
      interfaceFixture({
        adapter_type: "wifi",
        cidr: "10.0.0.5/8",
        dns_servers: [],
        gateway: "10.0.0.1",
        ipv4: "10.0.0.5",
        name: "Wi-Fi",
        prefix_length: 8,
        subnet_mask: "255.0.0.0",
      }),
    ];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface(""));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    await screen.findByRole("option", { name: /10\.20\.30\.7\/24 — Ethernet 4/ });
    const sourceLabel = (await screen.findByText("Source Interface")).closest("label");
    const select = within(sourceLabel as HTMLElement).getByRole("combobox") as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe("10.20.30.7/24"));
  });

  it("keeps Auto when no wired adapter is up for an unset Source Interface", async () => {
    // Only Wi-Fi is up: the wired-first default must NOT fire, so the empty
    // value stays and continues to behave as Auto, exactly as before.
    interfacesPayload = [
      interfaceFixture({ is_up: false }),
      interfaceFixture({
        adapter_type: "wifi",
        cidr: "10.0.0.5/8",
        dns_servers: [],
        gateway: "10.0.0.1",
        ipv4: "10.0.0.5",
        name: "Wi-Fi",
        prefix_length: 8,
        subnet_mask: "255.0.0.0",
      }),
    ];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface(""));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    await screen.findByRole("option", { name: /10\.0\.0\.5\/8 — Wi-Fi/ });
    const sourceLabel = (await screen.findByText("Source Interface")).closest("label");
    const select = within(sourceLabel as HTMLElement).getByRole("combobox") as HTMLSelectElement;
    // The draft value stays empty, and an empty value has no matching option,
    // so the DOM select falls back to the first option — the Auto sentinel.
    expect(select.value).toBe("Auto (OS default route)");
    expect(
      screen.getByText(/Auto: Windows picks the sending adapter via its default route/i),
    ).toBeInTheDocument();
  });

  it("never overrides a saved explicit Auto with the wired default", async () => {
    // An up Ethernet adapter is available, but the SAVED value is the Auto
    // sentinel, so it must stay selected. (A saved concrete NIC staying
    // selected is covered by the non-enumerated-value test above.)
    interfacesPayload = [interfaceFixture()];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("Auto (OS default route)"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    await screen.findByRole("option", { name: /192\.168\.1\.10\/24 — Ethernet 3/ });
    const sourceLabel = (await screen.findByText("Source Interface")).closest("label");
    const select = within(sourceLabel as HTMLElement).getByRole("combobox") as HTMLSelectElement;
    expect(select.value).toBe("Auto (OS default route)");
  });

  it("always renders the Windows-manages copy in the device section", async () => {
    // No Source Interface value stored and no enumerated interfaces: the
    // details panel still states that Windows owns adapter settings.
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    expect(await screen.findByText(/Windows manages these adapter settings/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Auto: Windows picks the sending adapter via its default route/i),
    ).toBeInTheDocument();
  });

  it("flags a stored source interface that is not in the list without promising a scan failure", async () => {
    // Virtual adapters are enumerated too now, so "not listed" means the
    // adapter is genuinely absent (unplugged/disabled/IP changed). The copy
    // names the likely causes without promising a specific outcome, and its
    // Auto suggestion must carry the BACnet caveat (a live BACnet scan
    // refuses to run on Auto), matching the backend guard messages.
    interfacesPayload = [interfaceFixture()];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("172.16.0.9/24"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    expect(
      await screen.findByText(/not in the list of adapters on this machine/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/a BACnet scan\s+requires a specific adapter/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/set Source Interface back to Auto/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Scans will fail/i)).not.toBeInTheDocument();
  });

  it("shows the enumeration-failed message when GET /system/interfaces errors", async () => {
    interfacesFailure = true;
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("192.168.1.10/24"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    expect(
      await screen.findByText(/Adapter details unavailable \(interface enumeration failed/i),
    ).toBeInTheDocument();
  });

  it("warns when the selected source interface adapter is down", async () => {
    interfacesPayload = [interfaceFixture({ is_up: false })];
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(payloadWithSourceInterface("192.168.1.10/24"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    expect(
      await screen.findByText(/This adapter is currently down — scans from it will fail/i),
    ).toBeInTheDocument();
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

  it("exports the configuration WITH secrets via the engineer-gated endpoint (ITEM-1)", async () => {
    let calledSecretsExport = false;
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration/export-with-secrets")) {
        calledSecretsExport = true;
        return jsonResponse({
          kind: "smart-commissioning-configuration",
          version: 2,
          exported_at: "2026-07-20T10:00:00.000Z",
          project_id: null,
          site_id: null,
          secrets_included: true,
          configuration: configurationPayload(),
          secret_material: {
            "CA Certificate": { secret_ref: "secret://ca", content: "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----" },
          },
        });
      }
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(configurationPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Export with secrets/i }));
    // The success banner warns the file carries secrets in plain text, and the
    // separate with-secrets endpoint was called (not the masked default export).
    expect(await screen.findByText(/PLAIN TEXT/i)).toBeInTheDocument();
    expect(calledSecretsExport).toBe(true);
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  it("imports a v2 with-secrets envelope via POST /configuration/import (ITEM-1)", async () => {
    let postBody: string | null = null;
    stubFetch((url, init) => {
      if (url.endsWith("/api/v1/configuration/import") && init?.method === "POST") {
        postBody = String(init?.body);
        return jsonResponse(configurationPayload());
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
      version: 2,
      secrets_included: true,
      configuration: configurationPayload(),
      secret_material: {
        "CA Certificate": { secret_ref: "secret://ca", content: "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----" },
      },
    };
    const file = new File([JSON.stringify(envelope)], "config-with-secrets.json", {
      type: "application/json",
    });
    fireEvent.change(fileInput, { target: { files: [file] } });

    // The secret path routes to POST /configuration/import and sends BOTH the
    // configuration and the secret_material; the success note says secrets were
    // restored on this machine.
    expect(
      await screen.findByText(/restored into this machine's secret store/i),
    ).toBeInTheDocument();
    expect(postBody).not.toBeNull();
    const parsed = JSON.parse(postBody as unknown as string) as Record<string, unknown>;
    expect(parsed).toHaveProperty("configuration");
    expect(parsed).toHaveProperty("secret_material");
  });

  // v0.1.13 — logging destinations. The Logging & Diagnostics section is
  // collapsed by default, so each test expands it via its toggle first.
  it("renders the logging section with a Log Level select and a masked upload token, and no syslog fields", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(loggingPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Logging & Diagnostics/i }));

    const logLevelLabel = (await screen.findByText("Log Level")).closest("label");
    const levelSelect = within(logLevelLabel as HTMLElement).getByRole("combobox") as HTMLSelectElement;
    expect(Array.from(levelSelect.options).map((option) => option.value)).toEqual([
      "Debug",
      "Info",
      "Warning",
      "Error",
    ]);

    const tokenLabel = (await screen.findByText("Log Upload Token")).closest("label");
    const tokenInput = within(tokenLabel as HTMLElement).getByDisplayValue("********") as HTMLInputElement;
    // Masked write-only sentinel: the non-revealing stored-secret indicator
    // stands in for the Show toggle (ISSUE-1), same as MQTT Password.
    expect(tokenInput.type).toBe("password");
    expect((tokenLabel as HTMLElement).querySelector(".secret-stored-note")).not.toBeNull();
    expect(
      within(tokenLabel as HTMLElement).queryByRole("button", { name: /Show Log Upload Token/i }),
    ).toBeNull();

    // The never-wired syslog fields are gone.
    expect(screen.queryByText("Remote Syslog Target")).not.toBeInTheDocument();
    expect(screen.queryByText("Syslog Port")).not.toBeInTheDocument();
  });

  it("disables Upload logs now until a Log Upload URL is set", async () => {
    stubFetch((url) => {
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(loggingPayload());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Logging & Diagnostics/i }));
    const uploadButton = await screen.findByRole("button", { name: /Upload logs now/i });
    // Blank URL -> disabled with an explanatory title.
    expect(uploadButton).toBeDisabled();

    const urlLabel = (await screen.findByText("Log Upload URL")).closest("label");
    const urlInput = within(urlLabel as HTMLElement).getByRole("textbox") as HTMLInputElement;
    fireEvent.change(urlInput, { target: { value: "https://logs.example/up" } });
    expect(uploadButton).toBeEnabled();
  });

  it("gates Upload logs now behind the engineer role", async () => {
    // A viewer principal (not the default engineer) so canEngineer is false.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/me")) {
          return jsonResponse({ username: "viewer-1", role: "viewer", source: "user_key" });
        }
        if (url.endsWith("/api/v1/system/interfaces")) {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/configuration")) {
          return jsonResponse(loggingPayload({ "Log Upload URL": "https://logs.example/up" }));
        }
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Logging & Diagnostics/i }));
    const uploadButton = await screen.findByRole("button", { name: /Upload logs now/i });
    // Even with a URL set, a viewer cannot upload.
    expect(uploadButton).toBeDisabled();
    expect(uploadButton).toHaveAttribute("title", ENGINEER_REQUIRED_TOOLTIP);
  });

  it("renders an honest error panel when the upload endpoint does not respond", async () => {
    stubFetch((url, init) => {
      if (url.endsWith("/api/v1/logs/upload") && init?.method === "POST") {
        return jsonResponse({
          outcome: "no_response",
          status_code: null,
          detail: "ConnectError: no route to host",
          bundle_bytes: 0,
          files: [],
        });
      }
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(loggingPayload({ "Log Upload URL": "https://logs.example/up" }));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Logging & Diagnostics/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Upload logs now/i }));

    expect(
      await screen.findByText(/The upload endpoint did not respond:/i),
    ).toBeInTheDocument();
    // Never a fabricated success.
    expect(screen.queryByText(/Logs uploaded/i)).not.toBeInTheDocument();
  });

  it("renders a success panel naming the uploaded file count", async () => {
    stubFetch((url, init) => {
      if (url.endsWith("/api/v1/logs/upload") && init?.method === "POST") {
        return jsonResponse({
          outcome: "uploaded",
          status_code: 200,
          detail: "Server accepted the bundle (200).",
          bundle_bytes: 1024,
          files: ["app.log"],
        });
      }
      if (url.endsWith("/api/v1/configuration")) {
        return jsonResponse(loggingPayload({ "Log Upload URL": "https://logs.example/up" }));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Logging & Diagnostics/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Upload logs now/i }));

    expect(await screen.findByText(/Uploaded 1 file/i)).toBeInTheDocument();
  });
});
