import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Retention, RetentionAction, Storage } from "../api";
import { calls } from "../test/fixtures";
import { SettingsPage } from "./SettingsPage";

const DEFAULTS: Retention = {
  enabled: false,
  dry_run: true,
  reserved_percent: 15,
  warning_percent: 80,
  unused_snapshot_days: 14,
  keep_newest_snapshots: 3,
  inactive_writeback_hours: 24,
  saved: false,
  last_run: null,
};

const STORAGE: Storage = {
  pool: "STORAGE-01",
  total_bytes: 10 * 1024 ** 4,
  used_bytes: 3 * 1024 ** 4,
  available_bytes: 7 * 1024 ** 4,
  used_percent: 30,
  reserved_bytes: 0,
  warning_percent: 80,
  warning: false,
};

const PLAN: RetentionAction[] = [
  { kind: "snapshot", target: "cs2@v2", reason: "not used, 30 days old", done: false, error: null },
];

/** GET retention answers `retention`; PUT echoes the body as saved; preview answers PLAN. */
function stubHost(retention: Retention = DEFAULTS) {
  const mock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    let body: unknown = retention;
    if (method === "PUT") body = { ...JSON.parse(init!.body as string), saved: true, last_run: null };
    if (url.endsWith("/preview")) body = PLAN;
    if (url.endsWith("/run")) body = { at: "2026-10-09T13:00:00Z", dry_run: false, actions: [] };
    return new Response(JSON.stringify(body), { status: 200 });
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

const actions = (mock: ReturnType<typeof vi.fn>) => calls(mock).filter((c) => c.method !== "GET");

describe("SettingsPage", () => {
  it("shows pool usage and that the defaults are not applied yet", async () => {
    stubHost();
    render(<SettingsPage storage={STORAGE} reload={vi.fn()} />);
    expect(await screen.findByText(/Not saved yet/)).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Pool used" })).toHaveAttribute("aria-valuenow", "30");
    expect(screen.getByLabelText("Storage")).toHaveTextContent("3.00 TB of 10.0 TB");
    expect(screen.getByRole("button", { name: "Run now" })).toBeDisabled();
  });

  it("asks before reserving pool space and saves", async () => {
    const mock = stubHost();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<SettingsPage storage={STORAGE} reload={vi.fn().mockResolvedValue(undefined)} />);

    await userEvent.click(await screen.findByLabelText("Run automatically every hour"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(confirm.mock.calls[0][0]).toContain("Reserve 15 % (1.50 TB) of pool STORAGE-01");
    expect(confirm.mock.calls[0][0]).toContain("Proxmox VMs");
    expect(actions(mock)).toEqual([
      {
        url: "/api/v1/settings/retention",
        method: "PUT",
        body: {
          enabled: true,
          dry_run: true,
          reserved_percent: 15,
          warning_percent: 80,
          unused_snapshot_days: 14,
          keep_newest_snapshots: 3,
          inactive_writeback_hours: 24,
        },
      },
    ]);
    expect(await screen.findByRole("button", { name: "Run now" })).toBeEnabled();
  });

  it("does not save when the reservation is not confirmed", async () => {
    const mock = stubHost();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<SettingsPage storage={STORAGE} reload={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Save" }));
    expect(actions(mock)).toEqual([]);
  });

  it("previews what would be deleted without deleting", async () => {
    const mock = stubHost();
    render(<SettingsPage storage={STORAGE} reload={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Preview" }));
    const preview = await screen.findByLabelText("Preview");
    expect(within(preview).getByText("Version cs2@v2")).toBeInTheDocument();
    expect(actions(mock).map((c) => c.url)).toEqual(["/api/v1/settings/retention/preview"]);
  });

  it("runs now after confirmation once saved, and shows the last run", async () => {
    const saved: Retention = {
      ...DEFAULTS,
      saved: true,
      last_run: {
        at: "2026-10-09T12:00:00Z",
        dry_run: false,
        actions: [{ ...PLAN[0], done: true }],
      },
    };
    const mock = stubHost(saved);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<SettingsPage storage={STORAGE} reload={vi.fn().mockResolvedValue(undefined)} />);

    const last = await screen.findByLabelText("Last run");
    expect(within(last).getByText("Deleted")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run now" }));
    expect(actions(mock).map((c) => c.url)).toEqual(["/api/v1/settings/retention/run"]);
  });
});
