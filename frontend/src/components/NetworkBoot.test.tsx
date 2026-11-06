import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PxeStatus } from "../api";
import { calls } from "../test/fixtures";
import { NetworkBoot } from "./NetworkBoot";

const PXE: PxeStatus = {
  unit: "ggnet-pxe",
  installed: true,
  state: "active",
  sub_state: "running",
  since: "2026-10-18T19:00:00Z",
  tftp_root: "/var/lib/ggnet/tftp",
  files: { "snponly.efi": true, "ipxe.efi": true, "autoexec.ipxe": false },
};

const BOOT = {
  gateway: "none",
  unregistered: "wait",
  mok_enroll: false,
  mok_password: "ggnet",
  new_pc_name: "pc-{nn}",
  saved: false,
};
const DISKS = [
  { id: 6, name: "games", kind: "game", published: true, snapshot: "base", is_default: true },
  { id: 9, name: "hw-test", kind: "os", published: true, snapshot: "v2" },
];
const KEY = { subject: "CN = ggNet Secure Boot (pve)", fingerprint: "FA:23:CA:52", expires: null, ipxe_signed: true };

function stubHost(boot: Record<string, unknown> = BOOT, pxe: PxeStatus = PXE) {
  const mock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    let body: unknown = url.endsWith("/game-disks") ? DISKS : url.endsWith("/pxe") || url.endsWith("/restart") ? pxe : boot;
    if (method === "PUT") body = { ...JSON.parse(init!.body as string), saved: true };
    return new Response(JSON.stringify(body), { status: 200 });
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

const actions = (mock: ReturnType<typeof vi.fn>) => calls(mock).filter((c) => c.method !== "GET");

describe("NetworkBoot", () => {
  it("shows the PXE service and its missing TFTP files", async () => {
    stubHost();
    render(<NetworkBoot />);
    expect(await screen.findByText("Running")).toBeInTheDocument();
    expect(screen.getByText("ggnet-pxe")).toBeInTheDocument();
    expect(screen.getByText("autoexec.ipxe missing")).toBeInTheDocument();
    expect(screen.getByText(/values from config.toml/)).toBeInTheDocument();
  });

  it("shows a stopped or missing service", async () => {
    stubHost(undefined, { ...PXE, installed: false, state: "inactive", sub_state: "dead", since: null });
    render(<NetworkBoot />);
    expect(await screen.findByText("Not installed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart" })).toBeDisabled();
  });

  it("restarts the service after confirmation", async () => {
    const mock = stubHost();
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<NetworkBoot />);
    await userEvent.click(await screen.findByRole("button", { name: "Restart" }));
    expect(actions(mock)).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Restart" }));
    expect(confirm.mock.calls[0][0]).toContain("Restart ggnet-pxe?");
    expect(actions(mock)).toEqual([{ url: "/api/v1/pxe/restart", method: "POST", body: undefined }]);
  });

  it("saves another gateway and booting unregistered PCs locally", async () => {
    const mock = stubHost();
    render(<NetworkBoot />);
    await userEvent.selectOptions(await screen.findByLabelText(/Gateway for Windows/), "ip");
    await userEvent.type(screen.getByLabelText("Gateway address"), "192.168.0.40");
    await userEvent.selectOptions(screen.getByLabelText(/Unregistered PCs/), "local");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(actions(mock)).toEqual([
      {
        url: "/api/v1/settings/boot",
        method: "PUT",
        body: {
          gateway: "192.168.0.40",
          unregistered: "local",
          mok_enroll: false,
          mok_password: "ggnet",
          new_pc_name: "pc-{nn}",
        },
      },
    ]);
    expect(await screen.findByText(/applies to the next PC that boots/)).toBeInTheDocument();
  });

  it("shows a saved address as Another address", async () => {
    stubHost({ ...BOOT, gateway: "10.0.0.1", saved: true });
    render(<NetworkBoot />);
    expect(await screen.findByLabelText("Gateway address")).toHaveValue("10.0.0.1");
    expect(screen.queryByText(/values from config.toml/)).toBeNull();
  });

  it("says when there is no Secure Boot key yet", async () => {
    stubHost();
    render(<NetworkBoot />);
    expect(await screen.findByText(/No Secure Boot key on this server yet/)).toBeInTheDocument();
  });

  it("shows the Secure Boot key and offers its certificate", async () => {
    stubHost(undefined, {
      ...PXE,
      secure_boot: {
        subject: "CN = ggNet Secure Boot (pve)",
        fingerprint: "FA:23:CA:52",
        expires: "2036-10-16T00:33:03Z",
        ipxe_signed: true,
      },
    });
    render(<NetworkBoot />);
    expect(await screen.findByText("CN = ggNet Secure Boot (pve)")).toBeInTheDocument();
    expect(screen.getByText("FA:23:CA:52")).toBeInTheDocument();
    expect(screen.getByText("Signed with this key")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Download certificate/ })).toHaveAttribute(
      "href",
      "/api/v1/boot/secureboot.cer",
    );
  });

  it("warns when iPXE is not signed with the key", async () => {
    stubHost(undefined, {
      ...PXE,
      secure_boot: { subject: "CN = x", fingerprint: "AA", expires: null, ipxe_signed: false },
    });
    render(<NetworkBoot />);
    expect(await screen.findByText(/Not signed with this key/)).toBeInTheDocument();
  });

  it("turns on key enrollment by the agents with a MokManager password", async () => {
    const mock = stubHost(BOOT, { ...PXE, secure_boot: KEY });
    render(<NetworkBoot />);
    await userEvent.click(await screen.findByLabelText(/Prepare the Secure Boot key on PCs/));
    const password = screen.getByLabelText(/MokManager password/);
    await userEvent.clear(password);
    await userEvent.type(password, "cafe2026");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(actions(mock)).toEqual([
      {
        url: "/api/v1/settings/boot",
        method: "PUT",
        body: {
          gateway: "none",
          unregistered: "wait",
          mok_enroll: true,
          mok_password: "cafe2026",
          new_pc_name: "pc-{nn}",
        },
      },
    ]);
  });

  it("cannot prepare the key before the server has one", async () => {
    stubHost();
    render(<NetworkBoot />);
    expect(await screen.findByLabelText(/Prepare the Secure Boot key on PCs/)).toBeDisabled();
  });

  it("saves the New PC name and shows the default game disk new PCs get", async () => {
    const mock = stubHost();
    render(<NetworkBoot />);
    const name = await screen.findByLabelText("Name");
    expect(await screen.findByText("games")).toBeInTheDocument();
    await userEvent.clear(name);
    await userEvent.type(name, "komp-{{nn}");
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));
    expect(actions(mock)[0].body).toEqual({
      gateway: "none",
      unregistered: "wait",
      mok_enroll: false,
      mok_password: "ggnet",
      new_pc_name: "komp-{nn}",
    });
  });
});
