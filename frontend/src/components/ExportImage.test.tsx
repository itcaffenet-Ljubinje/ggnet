import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Snapshot } from "../api";
import { disk } from "../test/fixtures";
import { ExportImage } from "./ExportImage";

const GiB = 1024 ** 3;
const VERSIONS: Partial<Snapshot>[] = [
  { name: "base", active: false, referenced_bytes: 9 * GiB, machines: [], pinned: [] },
  { name: "v2", active: true, referenced_bytes: 10 * GiB, machines: [], pinned: [] },
];

function stub(locations: unknown[]) {
  const calls: { method: string; url: string; body?: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      const body = url.endsWith("/snapshots") ? VERSIONS
        : url.includes("/backups/locations?") ? locations
        : url.includes("/open") ? { id: "x", path: "/x", free_bytes: 200 * GiB, backups: [] }
        : {};
      return new Response(JSON.stringify(body), { status: method === "POST" && url.endsWith("/export") ? 201 : 200 });
    }),
  );
  return calls;
}

describe("ExportImage", () => {
  it("exports the active version to a VHDX named after the image", async () => {
    const calls = stub([{ id: "imports", kind: "imports", label: "Imports folder (/x)", size: null, path: "/x" }]);
    const onClose = vi.fn();
    render(<ExportImage image={disk({ id: 3, name: "win11", kind: "os", snapshot: "v2", size_gb: 100 })}
                        compressRatio={1.2} onClose={onClose} />);
    expect(await screen.findByText(/200.0 GB free there/)).toBeInTheDocument();
    expect(screen.getByLabelText("File name")).toHaveValue("win11-v2.vhdx");
    await userEvent.selectOptions(screen.getByLabelText("Format"), "vhd");
    expect(screen.getByLabelText("File name")).toHaveValue("win11-v2.vhd");
    await userEvent.click(screen.getByRole("button", { name: "Export" }));
    expect(calls.find((c) => c.url.endsWith("/backups/export"))?.body).toEqual({
      image_id: 3, snapshot: "v2", format: "vhd", location: "imports", file_name: "win11-v2.vhd",
    });
    expect(onClose).toHaveBeenCalled();
  });

  it("stops what cannot fit: a VHD over 2040 GB and a file over 4 GB on FAT32", async () => {
    stub([{ id: "usb:sdj1", kind: "usb", label: "USB Ultra (vfat)", size: 32 * GiB, path: null, fstype: "vfat" }]);
    render(<ExportImage image={disk({ id: 6, name: "games", snapshot: "v2", size_gb: 3072 })} onClose={vi.fn()} />);
    expect(await screen.findByText(/FAT32, which takes no file over 4 GB/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Format"), "vhd");
    expect(screen.getByText(/A VHD holds at most 2040 GB/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export" })).toBeDisabled();
  });
});
