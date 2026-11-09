import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { General as GeneralInfo } from "../api";
import { calls } from "../test/fixtures";
import { General } from "./General";

const INFO: GeneralInfo = {
  hostname: "pve",
  version: "0.2.0",
  install: "development",
  app_dir: "/root/ggnet/backend",
  config_file: "/var/lib/ggnet-dev/config.toml",
  server_ip: "192.168.0.40",
  proxmox: "pve-manager/9.1.1",
  kernel: "7.0.14-28-pve",
  uptime_seconds: 7200,
  database: { kind: "postgresql", version: "17.11", size_bytes: 12 * 1024 * 1024, backups: [] },
  services: [
    { unit: "ggnet", label: "ggNet", purpose: "web UI", state: "active", sub_state: "running", since: null,
      restartable: true, note: null },
    { unit: "ggnet-pxe", label: "Network boot", purpose: "PXE", state: "failed", sub_state: "failed", since: null,
      restartable: true, note: null },
    { unit: "rtslib-fb-targetctl", label: "iSCSI target", purpose: "disks", state: "active", sub_state: "exited",
      since: null, restartable: false, note: "Restarting it would drop every PC's disk at once" },
  ],
  guests: ["VM 106 ccDISK"],
  online_pcs: ["komp-01"],
  running_tasks: [],
};

function stub(info: GeneralInfo = INFO) {
  const mock = vi.fn().mockImplementation(async (url: string) =>
    new Response(JSON.stringify(url.endsWith("/general") ? info : { restarting_self: false, services: [] }),
                 { status: url.endsWith("/reboot") ? 202 : 200 }),
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

describe("General", () => {
  it("shows the server and its services; the iSCSI target cannot be restarted", async () => {
    stub();
    render(<General />);
    expect(await screen.findByText("pve-manager/9.1.1")).toBeInTheDocument();
    expect(screen.getByText(/PostgreSQL 17.11/)).toBeInTheDocument();
    expect(screen.getByText("DEV")).toBeInTheDocument();
    expect(screen.getByText(/Development: runs from the source checkout/)).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart Network boot" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restart iSCSI target" })).toBeNull();
    expect(screen.getByTitle(/drop every PC's disk/)).toHaveTextContent("not from here");
  });

  it("restarts a service after confirmation", async () => {
    const mock = stub();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<General />);
    await userEvent.click(await screen.findByRole("button", { name: "Restart Network boot" }));
    expect(calls(mock).filter((c) => c.method === "POST")).toEqual([
      { url: "/api/v1/server/services/ggnet-pxe/restart", method: "POST", body: undefined },
    ]);
    expect(await screen.findByText("Network boot restarted.")).toBeInTheDocument();
  });

  it("reboots only after REBOOT is typed, naming what it stops", async () => {
    const mock = stub();
    render(<General />);
    await userEvent.click(await screen.findByRole("button", { name: "Reboot…" }));
    const dialog = screen.getByRole("dialog", { name: "Reboot the server" });
    expect(dialog).toHaveTextContent("komp-01");
    expect(dialog).toHaveTextContent("VM 106 ccDISK");
    const go = within(dialog).getByRole("button", { name: "Reboot now" });
    expect(go).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Confirmation word"), "REBOOT");
    await userEvent.click(go);
    expect(calls(mock).filter((c) => c.method === "POST")).toEqual([
      { url: "/api/v1/server/reboot", method: "POST", body: { confirm: "REBOOT" } },
    ]);
    expect(await within(dialog).findByText(/reboots in a few seconds/)).toBeInTheDocument();
  });

  it("refuses to reboot while a task runs", async () => {
    stub({ ...INFO, running_tasks: ["Backup of games"] });
    render(<General />);
    await userEvent.click(await screen.findByRole("button", { name: "Reboot…" }));
    const dialog = screen.getByRole("dialog", { name: "Reboot the server" });
    expect(dialog).toHaveTextContent("Running now: Backup of games");
    await userEvent.type(within(dialog).getByLabelText("Confirmation word"), "REBOOT");
    expect(within(dialog).getByRole("button", { name: "Reboot now" })).toBeDisabled();
  });
});
