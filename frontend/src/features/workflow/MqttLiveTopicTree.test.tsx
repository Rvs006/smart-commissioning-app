import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { MqttLiveTreeNode } from "../../api/client";
import { MqttLiveTopicTree } from "./MqttLiveTopicTree";

const tree: MqttLiveTreeNode[] = [
  {
    n: "site",
    p: "site",
    t: 2,
    m: 10,
    r: 1.5,
    mt: 1,
    ch: [
      {
        n: "ahu-1",
        p: "site/ahu-1",
        t: 1,
        m: 6,
        r: 1,
        mt: 1,
        a: "AHU-1",
        ch: [
          { n: "pointset", p: "site/ahu-1/pointset", t: 1, m: 6, r: 1, mt: 1, leaf: 1, sc: "udmi", c: 6 },
        ],
      },
    ],
  },
];

describe("MqttLiveTopicTree", () => {
  it("does not mount collapsed children until the node is expanded", () => {
    render(<MqttLiveTopicTree lastActivity={null} totalTopics={3} tree={tree} treeShown={3} />);
    expect(screen.getByText("site")).toBeInTheDocument();
    // Collapsed by default: the child rows are absent from the DOM entirely.
    expect(screen.queryByText("ahu-1")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "site" }));
    expect(screen.getByText("ahu-1")).toBeInTheDocument();
  });

  it("flashes rows whose path is in the latest activity pulse", async () => {
    const { container, rerender } = render(
      <MqttLiveTopicTree lastActivity={null} totalTopics={3} tree={tree} treeShown={3} />,
    );
    expect(container.querySelector("tr.mqtt-tree-flash")).toBeNull();
    rerender(
      <MqttLiveTopicTree lastActivity={{ paths: ["site"], at: 1 }} totalTopics={3} tree={tree} treeShown={3} />,
    );
    await waitFor(() => expect(container.querySelector("tr.mqtt-tree-flash")).not.toBeNull());
  });

  it("renders an empty state when no topics have been seen", () => {
    render(<MqttLiveTopicTree lastActivity={null} totalTopics={0} tree={[]} treeShown={0} />);
    expect(screen.getByText(/No topics seen yet/)).toBeInTheDocument();
  });

  it("focuses an asset when its Focus button is clicked", () => {
    const onFocus = vi.fn();
    render(<MqttLiveTopicTree lastActivity={null} onFocus={onFocus} totalTopics={3} tree={tree} treeShown={3} />);
    fireEvent.click(screen.getByRole("button", { name: "site" })); // expand to reveal the asset node
    fireEvent.click(screen.getByRole("button", { name: /Focus AHU-1/ }));
    expect(onFocus).toHaveBeenCalledWith("AHU-1");
  });
});
