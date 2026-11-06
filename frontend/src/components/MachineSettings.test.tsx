import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Machine } from "../api";
import { calls, disk, machine, stubFetch } from "../test/fixtures";
import { MachineSettings } from "./MachineSettings";

const DISKS = [
  disk(),
  disk({ id: 2, name: "games-b" }),
  disk({ id: 5, name: "win11", kind: "os", zvol_path: "tank/ggnet/images/win11" }),
];
const actions = (mock: ReturnType<typeof vi.fn>) => calls(mock).filter((c) => c.method !== "GET");

function open(m: Machine) {
  const reload = vi.fn().mockResolvedValue(undefined);
  const onClose = vi.fn();
  render(<MachineSettings machine={m} disks={DISKS} onClose={onClose} reload={reload} />);
  return { reload, onClose };
}

describe("MachineSettings", () => {
  it("renames a PC without disks, sending only what changed; the MAC is only shown", async () => {
    const fetchMock = stubFetch(200, machine());
    const { reload } = open(machine({ mac: "aa:bb:cc:dd:ee:01" }));

    const name = screen.getByLabelText("Name");
    await userEvent.clear(name);
    await userEvent.type(name, "pc-07");
    await userEvent.click(screen.getByRole("tab", { name: "Network" }));
    expect(screen.getByText("aa:bb:cc:dd:ee:01")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /MAC/ })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(actions(fetchMock)).toEqual([{ url: "/api/v1/machines/1", method: "PATCH", body: { name: "pc-07" } }]);
    expect(reload).toHaveBeenCalled();
    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("renames a PC with disks after a confirmation", async () => {
    const fetchMock = stubFetch(200, machine());
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    open(machine({ game_image_id: 1, status: "provisioned" }));

    const name = screen.getByLabelText("Name");
    expect(name).toBeEnabled();
    await userEvent.clear(name);
    await userEvent.type(name, "pc-13");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(confirm.mock.calls[0][0]).toContain("must be powered off");
    expect(actions(fetchMock)).toEqual([{ url: "/api/v1/machines/1", method: "PATCH", body: { name: "pc-13" } }]);
  });

  it("does not rename when the confirmation is cancelled", async () => {
    const fetchMock = stubFetch(200, machine());
    vi.spyOn(window, "confirm").mockReturnValue(false);
    open(machine({ game_image_id: 1, status: "provisioned" }));
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(actions(fetchMock)).toEqual([]);
  });

  it("asks before changing a Boot Mode PC's disks and sends the OS image too", async () => {
    const fetchMock = stubFetch(200, machine());
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    open(machine({ mode: "boot", game_image_id: 1, os_image_id: 5, status: "provisioned" }));

    await userEvent.click(screen.getByRole("tab", { name: "Disks" }));
    await userEvent.selectOptions(screen.getByLabelText("Game image"), "2");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(confirm.mock.calls[0][0]).toContain("everything the PC wrote is lost");
    expect(actions(fetchMock)).toEqual([
      { url: "/api/v1/machines/1/assign", method: "POST", body: { game_image_id: 2, os_image_id: 5 } },
    ]);
  });

  it("changes nothing when the disk change is not confirmed", async () => {
    const fetchMock = stubFetch(200, machine());
    vi.spyOn(window, "confirm").mockReturnValue(false);
    open(machine({ game_image_id: 1, status: "provisioned" }));
    await userEvent.click(screen.getByRole("tab", { name: "Disks" }));
    await userEvent.selectOptions(screen.getByLabelText("Game image"), "2");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(actions(fetchMock)).toEqual([]);
  });

  it("turns Keep Writeback on and pins a version", async () => {
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      const body = url.endsWith("/snapshots")
        ? [{ name: "base" }, { name: "v2" }]
        : machine();
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    open(machine({ game_image_id: 1, status: "provisioned" }));

    await userEvent.click(screen.getByLabelText(/Keep writeback/));
    await userEvent.click(screen.getByRole("tab", { name: "Disks" }));
    await screen.findByRole("option", { name: "Pin @base" });
    await userEvent.selectOptions(screen.getByLabelText("Game image version"), "base");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(actions(fetchMock)).toEqual([
      { url: "/api/v1/machines/1/keep-writeback", method: "PUT", body: { enabled: true } },
      { url: "/api/v1/machines/1/pin", method: "PUT", body: { snapshot: "base" } },
    ]);
  });

  it("shows the hardware the agent reported, and never an IQN", async () => {
    stubFetch(200, []);
    open(
      machine({
        agent_version: "0.1.11",
        hardware: {
          cpu: "Intel i5-10400F",
          gpus: ["GTX 1660"],
          motherboard: "Gigabyte H410M H",
          memory_bytes: 16 * 1024 ** 3,
          nic: "Realtek PCIe GbE",
        },
      }),
    );
    await userEvent.click(screen.getByRole("tab", { name: "Hardware" }));
    expect(screen.getByText("Gigabyte H410M H")).toBeInTheDocument();
    expect(screen.getByText("0.1.11")).toBeInTheDocument();
    expect(screen.getByText(/read by iPXE at the next network boot/)).toBeInTheDocument();
    expect(screen.queryByText(/iqn\./)).toBeNull();
  });

  it("switches a Disk Mode PC with a disk to Boot Mode: disks off, mode, new disks", async () => {
    const fetchMock = stubFetch(200, machine());
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    open(machine({ game_image_id: 1, status: "provisioned" }));

    await userEvent.click(screen.getByRole("tab", { name: "Disks" }));
    await userEvent.selectOptions(screen.getByLabelText("Mode"), "boot");
    await userEvent.selectOptions(screen.getByLabelText("OS image"), "5");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(confirm.mock.calls[0][0]).toContain("Switch pc01 to Boot Mode");
    expect(actions(fetchMock)).toEqual([
      { url: "/api/v1/machines/1/assign", method: "POST", body: { game_image_id: null } },
      { url: "/api/v1/machines/1", method: "PATCH", body: { mode: "boot" } },
      { url: "/api/v1/machines/1/assign", method: "POST", body: { game_image_id: 1, os_image_id: 5 } },
    ]);
  });

  it("does not switch to Boot Mode without an OS image", async () => {
    const fetchMock = stubFetch(200, machine());
    open(machine({ game_image_id: 1, status: "provisioned" }));
    await userEvent.click(screen.getByRole("tab", { name: "Disks" }));
    await userEvent.selectOptions(screen.getByLabelText("Mode"), "boot");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByRole("alert")).toHaveTextContent("needs an OS image and a game image");
    expect(actions(fetchMock)).toEqual([]);
  });
});
