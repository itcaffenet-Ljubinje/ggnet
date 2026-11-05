import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { WinPe } from "../api";
import { topLevelFiles, WinPeSetup } from "./WinPeSetup";

const PE: WinPe = {
  folder: "/var/lib/ggnet/winpe",
  ready: false,
  files: [
    { name: "wimboot", present: true, size: 76064 },
    { name: "BCD", present: false, size: null },
    { name: "boot.sdi", present: false, size: null },
    { name: "boot.wim", present: false, size: null },
  ],
  scanner: true,
  isos: [{ path: "/var/lib/vz/template/iso/Windows11.iso", name: "Windows11.iso", size: 5 * 1024 ** 3 }],
  iso_folders: ["/var/lib/vz/template/iso"],
  drivers_folder: "/var/lib/ggnet/winpe-drivers",
  packages: [
    {
      name: "intel-pro1000",
      files: 14,
      size: 2_000_000,
      devices: ["I217-LM", "I218-LM", "I219-LM", "I219-V", "I210"].map((n, i) => ({
        chip: `8086-15b${i}`,
        vendor: "Intel",
        name: `Intel(R) Ethernet Connection ${n}`,
      })),
    },
  ],
  pc_chips: [
    { chip: "10ec-8125", vendor: "Realtek", name: null, package: null, pcs: 2 },
    { chip: "8086-15b3", vendor: "Intel", name: "Intel(R) Ethernet Connection I219-V", package: "intel-pro1000", pcs: 1 },
  ],
};

function stub() {
  const calls: { method: string; url: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ method, url });
      if (method === "PUT" || (method === "POST" && url.endsWith("/upload")))
        return new Response(null, { status: 204 });
      if (method === "POST" && url.endsWith("/drivers/realtek-pcie"))
        return new Response(JSON.stringify({ ...PE.packages[0], name: "realtek-pcie" }), { status: 201 });
      return new Response(JSON.stringify(PE), { status: 200 });
    }),
  );
  return calls;
}

const file = (path: string) => {
  const f = new File(["x"], path.split("/").pop()!);
  Object.defineProperty(f, "webkitRelativePath", { value: path });
  return f;
};

describe("WinPeSetup", () => {
  it("shows what is missing and takes Windows PE from an ISO", async () => {
    const calls = stub();
    render(<WinPeSetup />);
    expect(await screen.findByText("Not ready")).toBeInTheDocument();
    expect(screen.getByText("BCD missing")).toBeInTheDocument();
    expect(screen.getByLabelText("Windows ISO")).toHaveValue("/var/lib/vz/template/iso/Windows11.iso");
    await userEvent.click(screen.getByRole("button", { name: "Take Windows PE" }));
    expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/winpe/from-iso")).toBe(true);
  });

  it("lists the driver packages and the PCs' chips", async () => {
    stub();
    render(<WinPeSetup />);
    const drivers = await screen.findByRole("generic", { name: "Network drivers for Windows PE" });
    expect(within(drivers).getByText(/Intel Ethernet Connection I217-LM, .*I219-V$/)).toBeInTheDocument();
    await userEvent.click(within(drivers).getByRole("button", { name: "+1 more" }));
    expect(within(drivers).getByText(/I210$/)).toBeInTheDocument();
    const chips = screen.getByRole("generic", { name: "Network chips of the PCs" });
    expect(within(chips).getByText("none")).toBeInTheDocument();
    expect(within(chips).getByText("Realtek 10ec-8125")).toBeInTheDocument();
  });

  it("uploads a driver folder's own files and adds the package", async () => {
    const calls = stub();
    render(<WinPeSetup />);
    await userEvent.type(await screen.findByLabelText("Package name"), "realtek-pcie");
    fireEvent.change(screen.getByLabelText("Driver folder"), {
      target: { files: [file("64/rt640x64.sys"), file("64/ws640x64.inf"), file("64/sub/readme.txt")] },
    });
    await userEvent.click(screen.getByRole("button", { name: "Add (2 files)" }));
    await vi.waitFor(() =>
      expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual(
        expect.arrayContaining([
          "POST /api/v1/winpe/drivers/realtek-pcie/upload",
          "PUT /api/v1/winpe/drivers/realtek-pcie/files/rt640x64.sys",
          "PUT /api/v1/winpe/drivers/realtek-pcie/files/ws640x64.inf",
          "POST /api/v1/winpe/drivers/realtek-pcie",
        ]),
      ),
    );
    expect(calls.some((c) => c.url.endsWith("readme.txt"))).toBe(false);
  });

  it("takes only the top-level files of a picked folder", () => {
    expect(topLevelFiles([file("NDIS68/e1d.inf"), file("NDIS68/x/y.inf")]).map((f) => f.name)).toEqual(["e1d.inf"]);
  });
});
