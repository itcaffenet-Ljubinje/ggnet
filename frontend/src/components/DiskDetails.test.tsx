import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Snapshot, Writeback } from "../api";
import { calls, disk } from "../test/fixtures";
import { DiskDetails } from "./DiskDetails";

const SNAPSHOTS: Snapshot[] = [
  {
    name: "base",
    created_at: "2026-10-08T21:12:00Z",
    used_bytes: 4096,
    referenced_bytes: 1073741824,
    active: false,
    machines: ["pc02"],
  },
  {
    name: "v1old",
    created_at: "2026-10-09T08:00:00Z",
    used_bytes: 0,
    referenced_bytes: 1073741824,
    active: false,
    machines: [],
  },
  {
    name: "v2",
    created_at: "2026-10-09T10:24:00Z",
    used_bytes: 0,
    referenced_bytes: 3 * 1073741824,
    active: true,
    machines: ["pc01"],
  },
];

const WRITEBACKS: Writeback[] = [
  {
    machine_id: 1,
    machine_name: "pc01",
    zvol: "tank/ggnet/writebacks/pc01",
    snapshot: "v2",
    used_bytes: 8192,
    keep_writeback: true,
    session_active: false,
    outdated: false,
  },
  {
    machine_id: 2,
    machine_name: "pc02",
    zvol: "tank/ggnet/writebacks/pc02",
    snapshot: "base",
    used_bytes: 5 * 1048576,
    keep_writeback: false,
    session_active: true,
    outdated: true,
  },
];

/** Answer by URL: the two lists, and {} for every action. */
function stubHost() {
  const mock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (method === "DELETE") return new Response(null, { status: 204 });
    const body = url.endsWith("/snapshots") ? SNAPSHOTS : url.endsWith("/writebacks") ? WRITEBACKS : {};
    return new Response(JSON.stringify(body), { status: 200 });
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

function renderDetails() {
  const onChanged = vi.fn().mockResolvedValue(undefined);
  render(<DiskDetails disk={disk({ snapshot: "v2" })} onChanged={onChanged} />);
  return onChanged;
}

const actions = (mock: ReturnType<typeof vi.fn>) => calls(mock).filter((c) => c.method !== "GET");

describe("DiskDetails", () => {
  it("lists versions and writebacks read from the host", async () => {
    stubHost();
    renderDetails();

    const versions = await screen.findByRole("table", { name: "Versions of cs2" });
    const rows = within(versions).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("@base");
    expect(rows[1]).toHaveTextContent("1.00 GB");
    expect(rows[1]).toHaveTextContent("pc02");
    expect(rows[3]).toHaveTextContent("@v2Active");
    // Neither the active version nor one a PC runs on can be deleted.
    const del = within(versions).getAllByRole("button", { name: "Delete" });
    expect(del.map((b) => (b as HTMLButtonElement).disabled)).toEqual([true, false, true]);

    const wb = screen.getByRole("table", { name: "Writebacks of cs2" });
    expect(within(wb).getAllByRole("row")[1]).toHaveTextContent("Keep writeback");
    expect(within(wb).getAllByRole("row")[2]).toHaveTextContent("→ @v2 at reboot");
    expect(within(wb).getAllByRole("row")[2]).toHaveTextContent("5.00 MB");
    // A connected PC cannot be discarded.
    const discard = within(wb).getAllByRole("button", { name: "Discard" });
    expect(discard.map((b) => (b as HTMLButtonElement).disabled)).toEqual([false, true]);
  });

  it("makes an older version active after confirmation", async () => {
    const mock = stubHost();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const onChanged = renderDetails();

    const versions = await screen.findByRole("table", { name: "Versions of cs2" });
    await userEvent.click(within(versions).getAllByRole("button", { name: "Make active" })[0]);
    expect(actions(mock)).toEqual([
      { url: "/api/v1/game-disks/1/active-snapshot", method: "PUT", body: { snapshot: "base" } },
    ]);
    expect(onChanged).toHaveBeenCalled();
  });

  it("deletes an unused version", async () => {
    const mock = stubHost();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderDetails();

    const versions = await screen.findByRole("table", { name: "Versions of cs2" });
    await userEvent.click(within(versions).getAllByRole("button", { name: "Delete" })[1]);
    expect(actions(mock)).toEqual([
      { url: "/api/v1/game-disks/1/snapshots/v1old", method: "DELETE", body: undefined },
    ]);
  });

  it("discards a writeback and warns that a kept one loses unapplied changes", async () => {
    const mock = stubHost();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderDetails();

    const wb = await screen.findByRole("table", { name: "Writebacks of cs2" });
    await userEvent.click(within(wb).getAllByRole("button", { name: "Discard" })[0]);
    expect(confirm.mock.calls[0][0]).toContain("changes not applied yet are lost");
    expect(actions(mock)).toEqual([
      { url: "/api/v1/machines/1/discard-writeback", method: "POST", body: undefined },
    ]);
  });

  it("does nothing when an action is not confirmed", async () => {
    const mock = stubHost();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderDetails();

    const versions = await screen.findByRole("table", { name: "Versions of cs2" });
    await userEvent.click(within(versions).getAllByRole("button", { name: "Make active" })[0]);
    expect(actions(mock)).toEqual([]);
  });
});
