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
    pinned: [],
  },
  {
    name: "v1old",
    created_at: "2026-10-09T08:00:00Z",
    used_bytes: 0,
    referenced_bytes: 1073741824,
    active: false,
    machines: [],
    pinned: [],
  },
  {
    name: "v1pin",
    created_at: "2026-10-09T09:00:00Z",
    used_bytes: 0,
    referenced_bytes: 1073741824,
    active: false,
    machines: [],
    pinned: ["pc03"],
  },
  {
    name: "v2",
    created_at: "2026-10-09T10:24:00Z",
    used_bytes: 0,
    referenced_bytes: 3 * 1073741824,
    active: true,
    machines: ["pc01"],
    pinned: [],
    comment: "AB 18.10. Warzone update",
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
    pinned_snapshot: null,
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
    pinned_snapshot: null,
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
    expect(rows[3]).toHaveTextContent("pc03 (pinned)");
    expect(rows[4]).toHaveTextContent("@v2Active");
    // Neither the active version nor one a PC runs on or is pinned to can be deleted.
    const del = within(versions).getAllByRole("button", { name: "Delete" });
    expect(del.map((b) => (b as HTMLButtonElement).disabled)).toEqual([true, false, true, true]);

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
      { url: "/api/v1/images/1/active-snapshot", method: "PUT", body: { snapshot: "base" } },
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
      { url: "/api/v1/images/1/snapshots/v1old", method: "DELETE", body: undefined },
    ]);
  });

  it("offers Merge into base only when no older version is in use", async () => {
    stubHost();
    renderDetails();

    const versions = await screen.findByRole("table", { name: "Versions of cs2" });
    const merge = within(versions).getAllByRole("button", { name: "Merge into base" });
    // @base has no button; for the others pc02 still runs on @base.
    expect(merge).toHaveLength(3);
    expect(merge.every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
    expect(merge[0]).toHaveAttribute("title", expect.stringContaining("used by pc02"));
    expect(merge[2]).toHaveAttribute("title", expect.stringContaining("pinned on pc03"));
  });

  it("merges the newest version into base after confirmation", async () => {
    const free = SNAPSHOTS.map((s) => ({ ...s, machines: s.active ? s.machines : [], pinned: [] }));
    const mock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      const get = (init?.method ?? "GET") === "GET";
      const body = !get ? {} : url.endsWith("/snapshots") ? free : [];
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", mock);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderDetails();

    const versions = await screen.findByRole("table", { name: "Versions of cs2" });
    const merge = within(versions).getAllByRole("button", { name: "Merge into base" });
    await userEvent.click(merge[2]);
    expect(confirm.mock.calls[0][0]).toContain("Deleted: @base, @v1old, @v1pin");
    expect(actions(mock)).toEqual([
      { url: "/api/v1/images/1/snapshots/v2/merge-into-base", method: "POST", body: undefined },
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

  it("shows and edits a version's comment", async () => {
    const fetchMock = stubHost();
    const prompt = vi.spyOn(window, "prompt").mockReturnValue("  new notes ");
    renderDetails();

    const versions = await screen.findByRole("table", { name: "Versions of cs2" });
    expect(within(versions).getAllByRole("row")[4]).toHaveTextContent("AB 18.10. Warzone update");
    await userEvent.click(within(versions).getByRole("button", { name: "Comment on @v2" }));
    expect(prompt.mock.calls[0][1]).toBe("AB 18.10. Warzone update");
    expect(actions(fetchMock)).toEqual([
      { url: "/api/v1/images/1/snapshots/v2/comment", method: "PUT", body: { comment: "new notes" } },
    ]);
  });

  it("marks the latest version and applies a kept writeback of it", async () => {
    const mock = stubHost();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(window, "prompt").mockReturnValue(" Warzone patch ");
    renderDetails();
    const versions = await screen.findByRole("table", { name: "Versions of cs2" });
    expect(within(versions).getAllByRole("row")[4]).toHaveTextContent("Latest");
    expect(within(versions).getAllByRole("row")[1]).not.toHaveTextContent("Latest");

    const wb = screen.getByRole("table", { name: "Writebacks of cs2" });
    // Only pc01 keeps its writeback; pc02 has no Apply.
    const apply = within(wb).getAllByRole("button", { name: "Apply" });
    expect(apply).toHaveLength(1);
    await userEvent.click(apply[0]);
    expect(actions(mock)).toEqual([
      {
        url: "/api/v1/machines/1/apply-writebacks",
        method: "POST",
        body: { os: false, game: true, comment: "Warzone patch" },
      },
    ]);
  });

  it("locks a version, and a locked one cannot be deleted", async () => {
    const mock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const snaps = SNAPSHOTS.map((x) => (x.name === "v1pin" ? { ...x, locked: true } : x));
      const body = url.endsWith("/snapshots") ? snaps : url.endsWith("/writebacks") ? WRITEBACKS : {};
      return new Response(method === "DELETE" ? null : JSON.stringify(body), { status: method === "DELETE" ? 204 : 200 });
    });
    vi.stubGlobal("fetch", mock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderDetails();
    const versions = await screen.findByRole("table", { name: "Versions of cs2" });
    const pinRow = within(versions).getAllByRole("row")[3];
    expect(within(pinRow).getByLabelText("Locked")).toBeInTheDocument();
    expect(within(pinRow).getByRole("button", { name: "Delete" })).toBeDisabled();
    // Merging v2 into base would delete the locked v1pin.
    expect(within(versions).getAllByRole("button", { name: "Merge into base" }).at(-1)).toBeDisabled();

    await userEvent.click(within(versions).getByRole("button", { name: "Lock @v1old" }));
    await userEvent.click(within(versions).getByRole("button", { name: "Unlock @v1pin" }));
    expect(actions(mock)).toEqual([
      { url: "/api/v1/images/1/snapshots/v1old/lock", method: "PUT", body: { locked: true } },
      { url: "/api/v1/images/1/snapshots/v1pin/lock", method: "PUT", body: { locked: false } },
    ]);
  });

  it("leaves the comment alone when the prompt is cancelled", async () => {
    const fetchMock = stubHost();
    vi.spyOn(window, "prompt").mockReturnValue(null);
    renderDetails();
    const versions = await screen.findByRole("table", { name: "Versions of cs2" });
    await userEvent.click(within(versions).getByRole("button", { name: "Comment on @base" }));
    expect(actions(fetchMock)).toEqual([]);
  });

  it("goes back to a version, deleting the newer ones, only when no newer one is in use", async () => {
    const snaps = SNAPSHOTS.map((s) =>
      s.name === "v1old" ? { ...s, retention: "removed automatically from 2026-11-20, unless a PC uses it" } : s,
    );
    const mock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      const get = (init?.method ?? "GET") === "GET";
      return new Response(JSON.stringify(!get ? {} : url.endsWith("/snapshots") ? snaps : []), { status: 200 });
    });
    vi.stubGlobal("fetch", mock);
    renderDetails();
    const versions = await screen.findByRole("table", { name: "Versions of cs2" });
    expect(within(versions).getByText("Retention: removed automatically from 2026-11-20, unless a PC uses it"))
      .toBeInTheDocument();
    const back = within(versions).getAllByRole("button", { name: "Go back here" });
    expect(back).toHaveLength(3);                                  // not on the newest
    expect(back[0]).toBeDisabled();                                // @v1pin is pinned, pc01 runs @v2
    expect(back[2]).toBeDisabled();                                // pc01 still runs @v2

  });

  it("goes back to an older version after confirmation", async () => {
    const free = SNAPSHOTS.map((s) => ({ ...s, machines: [], pinned: [] }));
    const mock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      const get = (init?.method ?? "GET") === "GET";
      return new Response(JSON.stringify(!get ? {} : url.endsWith("/snapshots") ? free : []), { status: 200 });
    });
    vi.stubGlobal("fetch", mock);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderDetails();
    const versions = await screen.findByRole("table", { name: "Versions of cs2" });
    await userEvent.click(within(versions).getAllByRole("button", { name: "Go back here" })[1]);
    expect(confirm.mock.calls.at(-1)?.[0]).toContain("@v1old becomes active and @v1pin, @v2 are deleted");
    expect(actions(mock)).toContainEqual({
      url: "/api/v1/images/1/snapshots/v1old/delete-newer", method: "POST", body: undefined,
    });
  });
});
