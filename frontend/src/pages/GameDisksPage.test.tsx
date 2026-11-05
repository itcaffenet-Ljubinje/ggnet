import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { calls, disk, machine, stubFetch } from "../test/fixtures";

/** Opens the ⋮ menu of `name` and picks `item`. */
async function menu(name: string, item: string) {
  await userEvent.click(screen.getByRole("button", { name: `Actions for ${name}` }));
  await userEvent.click(screen.getByRole("menuitem", { name: item }));
}
import { GameDisksPage } from "./GameDisksPage";

describe("GameDisksPage", () => {
  it("lists disks with their state and assigned machines", () => {
    render(
      <GameDisksPage
        disks={[disk(), disk({ id: 2, name: "valorant", published: false, snapshot: null })]}
        machines={[machine({ game_disk_id: 1 }), machine({ id: 2, name: "pc02", game_disk_id: 1 })]}
        reload={vi.fn()}
      />,
    );
    const rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("cs2");
    expect(rows[1]).toHaveTextContent("Published @base");
    expect(rows[1]).toHaveTextContent("2");
    expect(rows[2]).toHaveTextContent("Draft");
    // Only the draft can be published.
    expect(screen.getAllByRole("button", { name: "Publish" })).toHaveLength(1);
  });

  it("sums up images, kinds and PCs that use them", () => {
    render(
      <GameDisksPage
        disks={[disk(), disk({ id: 3, name: "win11", kind: "os", size_gb: 100, published: false, snapshot: null })]}
        machines={[machine({ game_disk_id: 1 }), machine({ id: 2, name: "pc02", game_disk_id: null })]}
        reload={vi.fn()}
      />,
    );
    const card = (title: string) => screen.getByText(title, { selector: ".kpi-title" }).closest(".kpi-card")!;
    expect(card("Images")).toHaveTextContent("2");
    expect(card("Images")).toHaveTextContent("1 draft");
    expect(card("OS images")).toHaveTextContent("1");
    expect(card("In use")).toHaveTextContent("of 2 PCs");
  });

  it("creates a disk and reloads", async () => {
    const fetchMock = stubFetch(201, disk());
    const reload = vi.fn().mockResolvedValue(undefined);
    render(<GameDisksPage disks={[]} machines={[]} reload={reload} />);

    await userEvent.type(screen.getByLabelText("Name"), "cs2");
    await userEvent.clear(screen.getByLabelText("Size (GB)"));
    await userEvent.type(screen.getByLabelText("Size (GB)"), "50");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(calls(fetchMock)).toEqual([
      { url: "/api/v1/game-disks", method: "POST", body: { name: "cs2", size_gb: 50, kind: "game" } },
    ]);
    expect(reload).toHaveBeenCalled();
    expect(screen.getByLabelText("Name")).toHaveValue("");
  });

  it("does nothing when publish is not confirmed", async () => {
    const fetchMock = stubFetch();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(
      <GameDisksPage
        disks={[disk({ published: false, snapshot: null })]}
        machines={[]}
        reload={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("renames a disk in the Edit dialog", async () => {
    const fetchMock = stubFetch(200, disk({ name: "steam-main" }));
    const reload = vi.fn().mockResolvedValue(undefined);
    render(<GameDisksPage disks={[disk()]} machines={[]} reload={reload} />);
    await menu("cs2", "Edit");
    const dialog = screen.getByRole("dialog", { name: "Edit cs2" });
    await userEvent.clear(within(dialog).getByLabelText(/^Name/));
    await userEvent.type(within(dialog).getByLabelText(/^Name/), " Steam-Main ");
    await userEvent.click(within(dialog).getByRole("button", { name: /Save/ }));
    expect(calls(fetchMock)).toEqual([
      { url: "/api/v1/game-disks/1", method: "PATCH", body: { name: "steam-main" } },
    ]);
    expect(reload).toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("grows a disk by +50/+100/+200 GB after confirmation and shows what to do", async () => {
    const fetchMock = stubFetch(200, {
      disk: disk({ size_gb: 250 }),
      version: "v2",
      extended: true,
      note: "The data partition was extended too.",
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<GameDisksPage disks={[disk()]} machines={[]} reload={vi.fn().mockResolvedValue(undefined)} />);
    await menu("cs2", "Edit");
    const dialog = screen.getByRole("dialog", { name: "Edit cs2" });
    await userEvent.click(within(dialog).getByRole("button", { name: "+50 GB" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "+100 GB" }));
    expect(within(dialog).getByLabelText("Size (GB)")).toHaveValue(250);
    await userEvent.click(within(dialog).getByLabelText(/Extend the data partition/));
    await userEvent.click(within(dialog).getByRole("button", { name: /Save/ }));
    expect(confirm.mock.calls[0][0]).toContain("from 100 GB to 250 GB");
    expect(calls(fetchMock)).toEqual([
      { url: "/api/v1/game-disks/1/grow", method: "POST", body: { size_gb: 250, extend: false } },
    ]);
    expect(await screen.findByRole("status")).toHaveTextContent("The data partition was extended too.");
  });

  it("does nothing when the size or the name did not change, or growing is not confirmed", async () => {
    const fetchMock = stubFetch();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<GameDisksPage disks={[disk()]} machines={[]} reload={vi.fn()} />);
    await menu("cs2", "Edit");
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await menu("cs2", "Edit");
    await userEvent.click(screen.getByRole("button", { name: "+200 GB" }));
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the backend error when delete fails", async () => {
    stubFetch(409, { detail: { error: "Game disk 'cs2' is assigned to 1 machine(s); move them first" } });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<GameDisksPage disks={[disk()]} machines={[]} reload={vi.fn()} />);

    await menu("cs2", "Remove");
    expect(await screen.findByRole("alert")).toHaveTextContent("assigned to 1 machine(s)");
  });

  it("starts editing a draft on a free PC", async () => {
    const fetchMock = stubFetch(200, disk({ published: false, snapshot: null, editor_id: 1 }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const reload = vi.fn().mockResolvedValue(undefined);
    render(
      <GameDisksPage
        disks={[disk({ published: false, snapshot: null })]}
        machines={[
          machine(),
          // Not offered: it already has a game disk.
          machine({ id: 2, name: "pc02", game_disk_id: 9, status: "provisioned" }),
        ]}
        reload={reload}
      />,
    );
    const select = screen.getByLabelText("PC to edit cs2 on");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["PC…", "pc01"]);
    expect(screen.getByRole("button", { name: "Edit on PC" })).toBeDisabled();

    await userEvent.selectOptions(select, "1");
    await userEvent.click(screen.getByRole("button", { name: "Edit on PC" }));
    expect(calls(fetchMock)).toEqual([
      { url: "/api/v1/game-disks/1/edit", method: "POST", body: { machine_id: 1 } },
    ]);
    expect(reload).toHaveBeenCalled();
  });

  it("shows the editor and finishes editing only when the PC is off", async () => {
    const draft = disk({ published: false, snapshot: null, editor_id: 1 });
    const editor = machine({ status: "editing", editing_disk_id: 1, session_active: true });
    const { rerender } = render(<GameDisksPage disks={[draft]} machines={[editor]} reload={vi.fn()} />);

    expect(screen.getByText("Editing on pc01")).toBeInTheDocument();
    // No publish or new edit while the master is mapped to a PC.
    expect(screen.queryByRole("button", { name: "Publish" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit on PC" })).toBeNull();
    expect(screen.getByRole("button", { name: "Finish editing" })).toBeDisabled();

    const fetchMock = stubFetch(200, disk({ published: false, snapshot: null }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    rerender(
      <GameDisksPage
        disks={[draft]}
        machines={[{ ...editor, session_active: false }]}
        reload={vi.fn().mockResolvedValue(undefined)}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Finish editing" }));
    expect(calls(fetchMock)).toEqual([
      { url: "/api/v1/game-disks/1/finish-edit", method: "POST", body: undefined },
    ]);
  });

  it("opens an image's details page from its name or the menu, and goes back", async () => {
    stubFetch(200, []);
    render(
      <GameDisksPage
        disks={[disk(), disk({ id: 2, name: "draft", published: false, snapshot: null })]}
        machines={[]}
        reload={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "cs2" }));
    expect(screen.getByRole("heading", { level: 2, name: "cs2" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Snapshots" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Writebacks" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Images" }));
    await menu("draft", "Details");
    expect(screen.getByText(/A draft has no snapshots or writebacks yet/)).toBeInTheDocument();
  });

  it("copies an image under a new name", async () => {
    const fetchMock = stubFetch(201, disk({ id: 5, name: "cs2-b", copy_source: "tank/ggnet/images/cs2@base" }));
    const prompt = vi.spyOn(window, "prompt").mockReturnValue(" CS2-B ");
    const reload = vi.fn().mockResolvedValue(undefined);
    render(<GameDisksPage disks={[disk()]} machines={[]} reload={reload} />);
    await menu("cs2", "Copy");
    expect(prompt.mock.calls[0][1]).toBe("cs2-copy");
    expect(calls(fetchMock)).toEqual([{ url: "/api/v1/game-disks/1/copy", method: "POST", body: { name: "cs2-b" } }]);
    expect(reload).toHaveBeenCalled();
  });

  it("cancels a running import from its menu", async () => {
    const fetchMock = stubFetch(204);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(
      <GameDisksPage
        disks={[disk({ id: 4, name: "win", published: false, snapshot: null, copy_source: "/imports/a.vhdx", copy_percent: 7 })]}
        machines={[]}
        reload={vi.fn().mockResolvedValue(undefined)}
      />,
    );
    expect(screen.getByText("Importing 7%")).toBeInTheDocument();
    await menu("win", "Cancel import");
    expect(calls(fetchMock)).toEqual([{ url: "/api/v1/imports/4/cancel", method: "POST", body: undefined }]);
  });

  it("shows a running and a failed copy with their limits", async () => {
    render(
      <GameDisksPage
        disks={[
          disk({ id: 2, name: "busy", published: false, snapshot: null, copy_source: "x@base", copy_percent: 42 }),
          disk({ id: 3, name: "broken", published: false, snapshot: null, copy_source: "x@base", copy_error: "out of space" }),
        ]}
        machines={[]}
        reload={vi.fn()}
      />,
    );
    expect(screen.getByText("Copying 42%")).toBeInTheDocument();
    expect(screen.getByText("Copy failed")).toHaveAttribute("title", "out of space");
    // A copy is no draft: no Edit on PC or Publish.
    expect(screen.queryByRole("button", { name: /Publish/ })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Actions for busy" }));
    expect(screen.getByRole("menuitem", { name: "Remove" })).toBeDisabled();
    expect(screen.queryByRole("menuitem", { name: "Copy" })).toBeNull();
  });

  it("offers Remove only for an image no PC uses", async () => {
    render(<GameDisksPage disks={[disk()]} machines={[machine({ game_disk_id: 1 })]} reload={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for cs2" }));
    expect(screen.getByRole("menuitem", { name: "Remove" })).toBeDisabled();
  });

  it("lists OS images and game disks apart and creates an OS image", async () => {
    const fetchMock = stubFetch(201, disk({ id: 3, name: "win11", kind: "os" }));
    render(
      <GameDisksPage
        disks={[disk(), disk({ id: 3, name: "win11", kind: "os", zvol_path: "tank/ggnet/images/win11" })]}
        machines={[machine({ mode: "boot", os_disk_id: 3, game_disk_id: 1 })]}
        reload={vi.fn().mockResolvedValue(undefined)}
      />,
    );
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(["OS images1", "Game disks1"]);
    const [osTable, gameTable] = screen.getAllByRole("table");
    expect(osTable).toHaveTextContent("win11");
    expect(osTable).not.toHaveTextContent("cs2");
    expect(gameTable).toHaveTextContent("cs2");

    await userEvent.type(screen.getByLabelText("Name"), "win11-b");
    await userEvent.selectOptions(screen.getByLabelText("Kind"), "os");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(calls(fetchMock).filter((c) => c.method === "POST")[0].body).toMatchObject({ kind: "os" });
  });

  it("prepares an OS image from its dialog, with Defender off on request; game disks have none", async () => {
    const fetchMock = stubFetch(200, disk({ id: 3, name: "win11", kind: "os", snapshot: "v2" }));
    const reload = vi.fn().mockResolvedValue(undefined);
    render(
      <GameDisksPage
        disks={[disk(), disk({ id: 3, name: "win11", kind: "os" })]}
        machines={[]}
        reload={reload}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Actions for cs2" }));
    expect(screen.queryByRole("menuitem", { name: "Prepare image" })).toBeNull();
    await userEvent.keyboard("{Escape}");
    await menu("win11", "Prepare image");
    const dialog = screen.getByRole("dialog", { name: "Prepare win11" });
    expect(calls(fetchMock)).toEqual([]);                      // nothing until confirmed
    await userEvent.click(within(dialog).getByLabelText("Turn off Microsoft Defender"));
    await userEvent.click(within(dialog).getByLabelText(/Turn off background services/));   // on by default
    await userEvent.click(within(dialog).getByRole("button", { name: /Prepare image/ }));
    expect(calls(fetchMock).filter((c) => c.method !== "GET")).toEqual([
      { url: "/api/v1/game-disks/3/prepare", method: "POST", body: { defender_off: true, services_off: false } },
    ]);
    expect(reload).toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

});
