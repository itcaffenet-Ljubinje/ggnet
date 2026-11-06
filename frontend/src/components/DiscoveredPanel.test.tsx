import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Discovered } from "../api";
import { calls, disk, machine, stubFetch } from "../test/fixtures";
import { useAsyncAction } from "../useAsyncAction";
import { DiscoveredPanel, suggestName } from "./DiscoveredPanel";

function found(over: Partial<Discovered> = {}): Discovered {
  return {
    id: 7,
    mac: "aa:bb:cc:dd:ee:01",
    source: "agent",
    hostname: "DESKTOP-1A2B3C",
    ip_address: "192.168.0.21",
    agent_version: "0.1.9",
    seen_count: 3,
    ignored: false,
    first_seen_at: "2026-10-14T10:00:00Z",
    last_seen_at: "2026-10-14T10:05:00Z",
    ...over,
  };
}

const DISKS = [disk(), disk({ id: 5, name: "win11", kind: "os" })];

function Panel({ discovered, reload }: { discovered: Discovered[]; reload: () => Promise<void> }) {
  const { busy, run } = useAsyncAction();
  return <DiscoveredPanel discovered={discovered} disks={DISKS} busy={busy !== null} run={run} reload={reload} />;
}

describe("DiscoveredPanel", () => {
  it("is hidden when nothing was discovered", () => {
    const { container } = render(<Panel discovered={[]} reload={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("adds a PC under the name typed, in Disk Mode when its agent knocked", async () => {
    const fetchMock = stubFetch(201, machine());
    const reload = vi.fn().mockResolvedValue(undefined);
    render(<Panel discovered={[found()]} reload={reload} />);

    const name = screen.getByLabelText("Name for aa:bb:cc:dd:ee:01");
    expect(name).toHaveValue("desktop-1a2b3c");
    await userEvent.clear(name);
    await userEvent.type(name, "vip-05");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(calls(fetchMock)).toEqual([
      { url: "/api/v1/discovery/7/claim", method: "POST", body: { name: "vip-05", mode: "disk", game_image_id: 1 } },
    ]);
    expect(reload).toHaveBeenCalled();
  });

  it("suggests Boot Mode for a PC that booted from the network", () => {
    render(<Panel discovered={[found({ source: "pxe", hostname: null })]} reload={vi.fn()} />);
    expect(screen.getByLabelText("Mode for aa:bb:cc:dd:ee:01")).toHaveValue("boot");
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();   // no name yet
  });

  it("shows the network card iPXE read", () => {
    render(
      <Panel
        discovered={[found({ source: "pxe", boot_nic: "VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16" })]}
        reload={vi.fn()}
      />,
    );
    expect(screen.getByText("Realtek 8168 rev 16")).toBeInTheDocument();
  });

  it("ignores a PC after confirming", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchMock = stubFetch(200, found({ ignored: true }));
    render(<Panel discovered={[found()]} reload={vi.fn().mockResolvedValue(undefined)} />);
    await userEvent.click(screen.getByRole("button", { name: "Ignore" }));
    expect(calls(fetchMock)).toEqual([{ url: "/api/v1/discovery/7/ignore", method: "POST", body: undefined }]);
  });

  it("makes a valid machine name from a Windows name", () => {
    expect(suggestName("DESKTOP-1A2B3C")).toBe("desktop-1a2b3c");
    expect(suggestName("-My PC_01")).toBe("my-pc-01");
    expect(suggestName("A-VERY-LONG-COMPUTER-NAME")).toHaveLength(15);
    expect(suggestName(null)).toBe("");
  });

  it("adds a network-booted PC in Boot Mode with its OS image and game image", async () => {
    const fetchMock = stubFetch(201, machine({ mode: "boot" }));
    render(<Panel discovered={[found({ source: "pxe", hostname: null })]} reload={vi.fn().mockResolvedValue(undefined)} />);

    expect(screen.getByRole("heading", { name: "New PCs (1)" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Name for aa:bb:cc:dd:ee:01"), "pc-03");
    // The only OS image and the only game image are picked already.
    expect(screen.getByLabelText("OS image for aa:bb:cc:dd:ee:01")).toHaveValue("5");
    expect(screen.getByLabelText("Game image for aa:bb:cc:dd:ee:01")).toHaveValue("1");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(calls(fetchMock)).toEqual([
      {
        url: "/api/v1/discovery/7/claim",
        method: "POST",
        body: { name: "pc-03", mode: "boot", game_image_id: 1, os_image_id: 5 },
      },
    ]);
  });

  it("does not add a Boot Mode PC without an OS image", async () => {
    render(<Panel discovered={[found({ source: "pxe", hostname: null })]} reload={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Name for aa:bb:cc:dd:ee:01"), "pc-03");
    await userEvent.selectOptions(screen.getByLabelText("OS image for aa:bb:cc:dd:ee:01"), "");
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
  });
});
