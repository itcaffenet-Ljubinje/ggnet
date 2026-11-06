import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { BackupJob, FoundBackup } from "../api";
import { disk } from "../test/fixtures";
import { BackupRestore } from "./BackupRestore";

const FOUND: FoundBackup = {
  folder: "/run/ggnet/usb/sdk1/ggnet-backups/win11-20261106-200000",
  image: "win11",
  kind: "os",
  created_at: "2026-11-06T20:00:00+00:00",
  versions: ["base", "v2"],
  active: "v2",
  bytes: 9 * 1024 ** 3,
  complete: true,
  problem: null,
};

const JOB: BackupJob = {
  id: 1, kind: "backup", image_id: 3, image_name: "win11", location: "usb:sdk1", location_label: "USB SanDisk (vfat)",
  folder: FOUND.folder, status: "running", done_bytes: 3 * 1024 ** 3, total_bytes: 9 * 1024 ** 3, error: null,
  created_at: "2026-11-06T20:00:00Z", started_at: "2026-11-06T20:00:01Z", finished_at: null,
};

function stub(free = 28 * 1024 ** 3, jobs: BackupJob[] = []) {
  const calls: { method: string; url: string; body?: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status });
      if (url.endsWith("/backups/locations"))
        return json([{ id: "usb:sdk1", kind: "usb", label: "USB SanDisk (vfat)", size: 32 * 1024 ** 3, path: null }]);
      if (url.includes("/open")) return json({ id: "usb:sdk1", path: "/run/ggnet/usb/sdk1", free_bytes: free, backups: [FOUND] });
      if (url.includes("/estimate")) return json({ image_id: 3, bytes: 9 * 1024 ** 3 });
      if (url.endsWith("/backups/jobs")) return json(jobs);
      if (url.endsWith("/backups") && method === "POST") return json([JOB], 201);
      if (url.endsWith("/backups/restore")) return json({ ...JOB, kind: "restore" }, 201);
      return json({});
    }),
  );
  return calls;
}

const IMAGES = [disk({ id: 3, name: "win11", kind: "os" }), disk({ id: 1, name: "cs2" })];

describe("BackupRestore", () => {
  it("backs up the chosen images to the only USB disk, opened read-only until the backup starts", async () => {
    const calls = stub();
    render(<BackupRestore images={IMAGES} onClose={vi.fn()} onRestored={vi.fn()} />);
    expect(await screen.findByText("28.0 GB")).toBeInTheDocument();             // free space
    expect(calls.some((c) => c.url.endsWith("/locations/usb%3Asdk1/open?writable=false"))).toBe(true);
    expect(calls.some((c) => c.url.includes("writable=true"))).toBe(false);
    await userEvent.click(screen.getByLabelText("Back up win11"));
    expect(await screen.findAllByText("9.00 GB")).toHaveLength(2);              // row and total
    await userEvent.click(screen.getByRole("button", { name: /Back up/ }));
    expect(calls.find((c) => c.method === "POST" && c.url === "/api/v1/backups")?.body).toEqual({
      image_ids: [3], location: "usb:sdk1",
    });
    expect(await screen.findByRole("tab", { name: "History", selected: true })).toBeInTheDocument();
  });

  it("does not start a backup that does not fit", async () => {
    stub(4 * 1024 ** 3);
    render(<BackupRestore images={IMAGES} onClose={vi.fn()} onRestored={vi.fn()} />);
    await screen.findByText("4.00 GB");
    await userEvent.click(screen.getByLabelText("Back up win11"));
    expect(await screen.findByText("Not enough free space on this disk")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Back up/ })).toBeDisabled();
  });

  it("restores a backup as a new image under a free name", async () => {
    const calls = stub();
    const onRestored = vi.fn().mockResolvedValue(undefined);
    render(<BackupRestore images={IMAGES} onClose={vi.fn()} onRestored={onRestored} />);
    await userEvent.click(screen.getByRole("tab", { name: "Restore" }));
    await userEvent.click(await screen.findByLabelText(/Restore win11 of/));
    expect(screen.getByLabelText("New image name")).toHaveValue("win11-2");      // win11 exists
    expect(calls.some((c) => c.url.endsWith("/open?writable=false"))).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Restore" }));
    expect(calls.find((c) => c.url.endsWith("/backups/restore"))?.body).toEqual({
      location: "usb:sdk1", folder: FOUND.folder, name: "win11-2",
    });
    expect(onRestored).toHaveBeenCalled();
  });

  it("shows the history with progress and can cancel a running job", async () => {
    const started = new Date(Date.now() - 600_000).toISOString();          // 3 GB in 10 min
    const calls = stub(28 * 1024 ** 3, [{ ...JOB, started_at: started }, { ...JOB, id: 2, status: "failed", error: "zfs send failed" }]);
    render(<BackupRestore images={IMAGES} onClose={vi.fn()} onRestored={vi.fn()} />);
    await userEvent.click(screen.getByRole("tab", { name: "History" }));
    const rows = await screen.findAllByRole("row");
    expect(within(rows[1]).getByText("Running 33%")).toBeInTheDocument();
    expect(within(rows[2]).getByText("zfs send failed")).toBeInTheDocument();
    expect(within(rows[1]).getByText(/^5\.1\d MB\/s · 2\d min left$/)).toBeInTheDocument();
    await userEvent.click(within(rows[1]).getByRole("button", { name: "Cancel" }));
    expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/jobs/1/cancel"))).toBe(true);
    expect(await within(rows[1]).findByRole("button", { name: "Cancelling…" })).toBeDisabled();
  });

  it("lets the admin choose when there are several disks, and opens none before", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        calls.push(url);
        return new Response(
          JSON.stringify(
            url.endsWith("/backups/locations")
              ? [
                  { id: "usb:sdj1", kind: "usb", label: "USB SanDisk (ntfs)", size: 1, path: null },
                  { id: "dir:0", kind: "dir", label: "/mnt/backup", size: null, path: "/mnt/backup" },
                ]
              : [],
          ),
        );
      }),
    );
    render(<BackupRestore images={IMAGES} onClose={vi.fn()} onRestored={vi.fn()} />);
    expect(await screen.findByRole("option", { name: "Choose a disk…" })).toBeInTheDocument();
    expect(screen.getByLabelText("Location")).toHaveValue("");
    expect(calls.some((u) => u.includes("/open"))).toBe(false);
  });
});
