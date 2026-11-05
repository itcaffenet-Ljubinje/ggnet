import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ImportSources } from "../api";
import { calls } from "../test/fixtures";
import { ImportImage, nameFromFile } from "./ImportImage";

const SOURCES: ImportSources = {
  folder: "/STORAGE-01/ggnet/imports",
  files: [{ path: "/STORAGE-01/ggnet/imports/PC-07 Win11.vhdx", name: "PC-07 Win11.vhdx", size: 55e9, format: "vhdx" }],
  usb: [
    { name: "sdj1", path: "/dev/sdj1", size: 1e12, fstype: "exfat", label: "IMAGES", model: "SanDisk", mounted: null },
  ],
};

function stub() {
  let opened = false;
  const mock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    let body: unknown = opened
      ? { ...SOURCES, usb: [{ ...SOURCES.usb[0], mounted: "/run/ggnet/usb/sdj1" }] }
      : SOURCES;
    if (url.endsWith("/eject")) opened = false;
    if (url.endsWith("/open")) opened = true;
    if (url.endsWith("/open"))
      body = { part: "sdj1", folder: "/run/ggnet/usb/sdj1", files: [
        { path: "/run/ggnet/usb/sdj1/games.qcow2", name: "games.qcow2", size: 1e12, format: "qcow2" },
      ] };
    if (method === "POST" && url.endsWith("/v1/imports")) body = { id: 9 };
    return new Response(JSON.stringify(body), { status: method === "POST" && url.endsWith("/v1/imports") ? 201 : 200 });
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => vi.restoreAllMocks());

describe("ImportImage", () => {
  it("suggests an image name from the file name", () => {
    expect(nameFromFile("PC-07 Win11.vhdx")).toBe("pc-07-win11");
    expect(nameFromFile("__Games!!.QCOW2")).toBe("games");
  });

  it("imports a file from the imports folder as an OS image", async () => {
    const mock = stub();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const onStarted = vi.fn().mockResolvedValue(undefined);
    render(<ImportImage onClose={vi.fn()} onStarted={onStarted} />);
    await userEvent.click(await screen.findByRole("radio"));
    expect(screen.getByLabelText("Name")).toHaveValue("pc-07-win11");
    expect(screen.getByLabelText(/Prepare image for all PCs/)).toBeChecked();
    await userEvent.click(screen.getByLabelText("Turn off Microsoft Defender"));
    await userEvent.click(screen.getByRole("button", { name: /^Import$/ }));
    expect(calls(mock).filter((c) => c.method === "POST")).toEqual([
      {
        url: "/api/v1/imports",
        method: "POST",
        body: {
          name: "pc-07-win11",
          kind: "os",
          path: "/STORAGE-01/ggnet/imports/PC-07 Win11.vhdx",
          prepare: true,
          defender_off: true,
          services_off: true,                      // on unless unticked
        },
      },
    ]);
    expect(onStarted).toHaveBeenCalled();
  });

  it("opens a USB disk read-only, imports a game disk from it, and ejects it", async () => {
    const mock = stub();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<ImportImage onClose={vi.fn()} onStarted={vi.fn().mockResolvedValue(undefined)} />);
    await userEvent.click(await screen.findByRole("tab", { name: /USB disk/ }));
    expect(screen.getByText(/SanDisk · \/dev\/sdj1 · exfat/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Open \(read-only\)/ }));
    await userEvent.click(await screen.findByRole("radio"));
    await userEvent.selectOptions(screen.getByLabelText("Kind"), "game");
    await userEvent.click(screen.getByRole("button", { name: /^Import$/ }));
    const posts = calls(mock).filter((c) => c.method === "POST");
    expect(posts[0]).toEqual({ url: "/api/v1/imports/usb/sdj1/open", method: "POST", body: undefined });
    expect(screen.queryByLabelText(/Prepare image/)).toBeNull();     // a game disk is not prepared
    expect(posts[1].body).toEqual({
      name: "games",
      kind: "game",
      path: "/run/ggnet/usb/sdj1/games.qcow2",
      prepare: false,
      defender_off: false,
      services_off: false,
    });
    await userEvent.click(screen.getByRole("button", { name: "Eject" }));
    expect(calls(mock).filter((c) => c.method === "POST").at(-1)?.url).toBe("/api/v1/imports/usb/sdj1/eject");
  });

  it("deletes a file from the imports folder after asking", async () => {
    const mock = stub();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<ImportImage onClose={vi.fn()} onStarted={vi.fn()} />);
    const list = await screen.findByRole("radiogroup");
    await userEvent.click(within(list).getByRole("button", { name: /Delete PC-07 Win11.vhdx/ }));
    expect(calls(mock).filter((c) => c.method === "DELETE")[0].url).toBe(
      "/api/v1/imports/file?path=%2FSTORAGE-01%2Fggnet%2Fimports%2FPC-07%20Win11.vhdx",
    );
  });
});
