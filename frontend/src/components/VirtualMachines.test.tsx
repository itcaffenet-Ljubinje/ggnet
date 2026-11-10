import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Image, Machine } from "../api";
import { CreateVm, VmControls, VmSettingsCard, vmMemoryLimit } from "./VirtualMachines";

vi.mock("@novnc/novnc", () => ({
  default: class extends EventTarget {
    scaleViewport = false;
    disconnect() {}
    focus() {}
    sendCtrlAltDel() {}
  },
}));

const HOST = {
  available: true, problem: null, node: "pve1", cpus: 32, memory_mb: 196608,
  bridges: [
    { name: "vmbr0", cidr: "192.168.0.10/24", ports: "eno1", active: true, comments: null },
    { name: "vmbr1", cidr: null, ports: "ens1f0", active: true, comments: "10G" },
  ],
  storages: [{ name: "tank-vm", type: "zfspool", pool: "tank/vm", avail: null }],
  bridge: "vmbr1", storage: "tank-vm", service_interface: "vmbr1",
  settings: { bridge: null, storage: null, reserved_memory_gb: 16 },
};

function image(id: number, name: string, kind: "os" | "game"): Image {
  return { id, name, kind, published: true, is_default: false } as unknown as Image;
}

function machine(over: Partial<Machine> = {}): Machine {
  return { id: 7, name: "work-vm", vm_id: 104, status: "provisioned", mode: "boot", ...over } as Machine;
}

describe("virtual machines", () => {
  it("keeps the reserved memory for the host", () => {
    expect(vmMemoryLimit(HOST)).toBe(196608 - 16 * 1024);
    expect(vmMemoryLimit({ available: false, problem: "no" })).toBeNull();
  });

  it("creates a VM from an OS image and a game image", async () => {
    const mock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) =>
      new Response(JSON.stringify(init?.method === "POST" ? machine() : HOST), { status: url.endsWith("/vms") ? 201 : 200 }));
    vi.stubGlobal("fetch", mock);
    const onClose = vi.fn();
    render(<CreateVm disks={[image(1, "win11", "os"), image(2, "cs2", "game")]} reload={async () => undefined}
      onClose={onClose} />);
    expect(await screen.findByText(/bridge/)).toBeInTheDocument();
    expect(screen.getByText("vmbr1")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Name"), "work-vm");
    await userEvent.selectOptions(screen.getByLabelText("OS image"), "1");
    await userEvent.selectOptions(screen.getByLabelText("Game image"), "2");
    await userEvent.click(screen.getByRole("button", { name: /Create/ }));
    const post = mock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST")!;
    expect(JSON.parse((post[1] as RequestInit).body as string)).toEqual({
      name: "work-vm", os_image_id: 1, game_image_id: 2, cores: 4, memory_mb: 8192, keep_writeback: true,
      drives: "local",
    });
    expect(onClose).toHaveBeenCalled();
  });

  it("says when the host cannot run VMs", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async () =>
      new Response(JSON.stringify({ available: false, problem: "This host is not a Proxmox VE node" }), { status: 200 })));
    render(<CreateVm disks={[]} reload={async () => undefined} onClose={() => undefined} />);
    expect(await screen.findByText(/need a Proxmox VE host/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create/ })).toBeDisabled();
  });

  it("offers Start for a stopped VM and the console for a running one", async () => {
    const mock = vi.fn().mockImplementation(async () => new Response(JSON.stringify(machine()), { status: 200 }));
    vi.stubGlobal("fetch", mock);
    const vm = { machine_id: 7, vm_id: 104, status: "stopped", cores: 4, memory_mb: 8192, node: "pve1", drives: "local" as const };
    const run = async (_key: string, fn: () => Promise<unknown>) => {
      await fn();
      return true;
    };
    const { rerender } = render(<VmControls machine={machine()} vm={vm} busy={false} run={run} reload={async () => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "Start work-vm" }));
    expect(JSON.parse((mock.mock.calls[0][1] as RequestInit).body as string)).toEqual({ action: "start" });
    rerender(<VmControls machine={machine()} vm={{ ...vm, status: "running" }} busy={false} run={run}
      reload={async () => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "Console of work-vm" }));
    expect(await screen.findByRole("dialog", { name: "Console of work-vm" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start work-vm" })).toBeNull();
    rerender(<VmControls machine={machine()} vm={undefined} busy={false} run={run} reload={async () => undefined} />);
    expect(screen.getByText("VM missing")).toBeInTheDocument();
  });

  it("lists the host's bridges and saves the choice", async () => {
    const mock = vi.fn().mockImplementation(async () => new Response(JSON.stringify(HOST), { status: 200 }));
    vi.stubGlobal("fetch", mock);
    render(<VmSettingsCard />);
    const bridge = await screen.findByLabelText("Network bridge");
    expect(await screen.findByRole("option", { name: "Automatic (vmbr1)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /vmbr1 · 10G/ })).toBeInTheDocument();
    await userEvent.selectOptions(bridge, "vmbr0");
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));
    const put = mock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT")!;
    expect(JSON.parse((put[1] as RequestInit).body as string)).toEqual({
      bridge: "vmbr0", storage: null, reserved_memory_gb: 16,
    });
  });
});
