import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { MqttLiveFocused } from "../../api/client";
import { MqttFocusedDetail } from "./MqttFocusedDetail";

function focused(overrides: Partial<MqttLiveFocused> = {}): MqttLiveFocused {
  return {
    asset: "AHU-01",
    key: "AHU-01",
    matched: true,
    schema: "udmi-v2",
    rate: 1.5,
    count: 42,
    topics: ["udmi/site/x/ahu/01/events/pointset"],
    topicsDetail: [
      {
        topic: "udmi/site/x/ahu/01/events/pointset",
        schema: "udmi-v2",
        count: 42,
        rate: 1.5,
        retained: false,
        history: [
          { ts: 1_000, raw: '{"seq":1}' },
          { ts: 2_000, raw: '{"seq":2}' },
        ],
      },
    ],
    lastTopic: "udmi/site/x/ahu/01/events/pointset",
    livePoints: [
      { name: "supply_air_temp", value: 14.2, unit: "degC", ts: 2_000 },
      { name: "rogue_point", value: 1, unit: "", ts: 2_000 },
    ],
    lastPayload: '{"seq":2}',
    issues: 0,
    comparison: {
      matched: 1,
      missing: 1,
      extra: 1,
      matchedNames: ["supply_air_temp"],
      missingNames: ["return_air_temp"],
      extraNames: ["rogue_point"],
      expected: 2,
    },
    meta: {
      asset: "AHU-01",
      type: "AHU",
      topic: "udmi/site/x/ahu/01/events/pointset",
      schema: "udmi-v2",
      site: "S",
      location: "Plant",
      description: "Air handling unit",
      points: [{ name: "supply_air_temp", unit: "degC" }],
    },
    udmi: { gatewayId: "GW-1", version: "1.5.2" },
    configTopic: "udmi/site/x/ahu/01/config",
    configPayload: '{"version":"1"}',
    ...overrides,
  };
}

describe("MqttFocusedDetail", () => {
  it("RAG-verdicts live points against the register in the Points tab (GAP-M1)", () => {
    render(<MqttFocusedDetail canEngineer focused={focused()} onWriteConfig={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Points" }));
    // supply_air_temp is expected -> matched; rogue_point is not -> extra;
    // return_air_temp was expected but never seen -> appended as missing.
    expect(screen.getByText("matched")).toBeInTheDocument();
    expect(screen.getByText("extra")).toBeInTheDocument();
    expect(screen.getByText("missing")).toBeInTheDocument();
    expect(screen.getByText("return_air_temp")).toBeInTheDocument();
  });

  it("leaves points neutral when the asset is not in the register", () => {
    render(
      <MqttFocusedDetail
        canEngineer
        focused={focused({ matched: false, meta: null })}
        onWriteConfig={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Points" }));
    expect(screen.queryByText("matched")).not.toBeInTheDocument();
    expect(screen.queryByText("missing")).not.toBeInTheDocument();
  });

  it("opens the prefilled write-config lane (GAP-M7)", () => {
    const onWriteConfig = vi.fn();
    render(<MqttFocusedDetail canEngineer focused={focused()} onWriteConfig={onWriteConfig} />);
    fireEvent.click(screen.getByRole("button", { name: /Write config/ }));
    expect(onWriteConfig).toHaveBeenCalledWith("udmi/site/x/ahu/01/config", '{"version":"1"}');
  });

  it("hides the write-config button when the asset has no config topic", () => {
    render(
      <MqttFocusedDetail canEngineer focused={focused({ configTopic: "" })} onWriteConfig={vi.fn()} />,
    );
    expect(screen.queryByRole("button", { name: /Write config/ })).not.toBeInTheDocument();
  });

  it("scrubs payload history and pauses the live stream (GAP-M1)", () => {
    render(<MqttFocusedDetail canEngineer focused={focused()} onWriteConfig={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Live payload" }));
    // Live payload is the freshest; Pause is available on the Live view.
    const pause = screen.getByRole("button", { name: "Pause" });
    expect(pause).toBeEnabled();
    fireEvent.click(pause);
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
  });
});
