import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AgentPackage } from "../api";
import { calls, stubFetch } from "../test/fixtures";
import { ClientApp } from "./ClientApp";

const PKG: AgentPackage = {
  available: true,
  directory: "/var/lib/ggnet/agent",
  version: "0.1.9",
  server_url: "http://192.168.0.40:8088",
  install_command: "irm http://192.168.0.40:8088/api/v1/agent/bootstrap.ps1 | iex",
  files: [{ name: "ggnet-agent.exe", size_bytes: 76 * 1024 * 1024, sha256: "50a2663409fc19e6" }],
  auto_update: true,
  outdated: [],
};

describe("ClientApp", () => {
  it("shows the version, the install command and the downloads", async () => {
    stubFetch(200, PKG);
    render(<ClientApp />);
    expect(await screen.findByLabelText("Install command")).toHaveValue(PKG.install_command);
    expect(screen.getByText("0.1.9")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download .zip" })).toHaveAttribute("href", "/api/v1/agent/download.zip");
    expect(screen.getByRole("link", { name: "ggnet-agent.exe" })).toHaveAttribute(
      "href",
      "/api/v1/agent/download/ggnet-agent.exe",
    );
    expect(screen.getByText("76.0 MB")).toBeInTheDocument();
  });

  it("copies the command, also without navigator.clipboard (plain http)", async () => {
    stubFetch(200, PKG);
    const exec = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, "execCommand", { value: exec, configurable: true });
    vi.stubGlobal("navigator", { ...navigator, clipboard: undefined });
    render(<ClientApp />);
    await userEvent.click(await screen.findByRole("button", { name: "Copy" }));
    expect(exec).toHaveBeenCalledWith("copy");
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("says where to put the files when the package is missing", async () => {
    stubFetch(200, { ...PKG, available: false, version: null, files: [] });
    render(<ClientApp />);
    expect(await screen.findByText(/not on the server yet/)).toHaveTextContent("/var/lib/ggnet/agent");
    expect(screen.queryByLabelText("Install command")).toBeNull();
  });

  it("turns automatic agent updates off and lists the PCs with an older agent", async () => {
    const mock = stubFetch(200, { ...PKG, outdated: ["server", "vip-01"] });
    render(<ClientApp />);
    expect(await screen.findByLabelText("Outdated agents")).toHaveTextContent("Updating on 2 PCs: server, vip-01");
    mock.mockImplementation(async () => new Response(JSON.stringify({ auto_update: false }), { status: 200 }));
    await userEvent.click(screen.getByRole("checkbox", { name: /Update the agent on Disk Mode PCs/ }));
    expect(calls(mock).at(-1)).toMatchObject({ url: "/api/v1/settings/agent", method: "PUT", body: { auto_update: false } });
    expect(screen.getByRole("checkbox", { name: /Update the agent/ })).not.toBeChecked();
    expect(screen.getByLabelText("Outdated agents")).toHaveTextContent("Older agent on 2 PCs");
  });

  it("staff see the setting but cannot change it", async () => {
    stubFetch(200, PKG);
    render(<ClientApp canManage={false} />);
    expect(await screen.findByRole("checkbox", { name: /Update the agent/ })).toBeDisabled();
  });
});
