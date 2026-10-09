import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { calls, disk, machine, stubFetch } from "../test/fixtures";
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

  it("creates a disk and reloads", async () => {
    const fetchMock = stubFetch(201, disk());
    const reload = vi.fn().mockResolvedValue(undefined);
    render(<GameDisksPage disks={[]} machines={[]} reload={reload} />);

    await userEvent.type(screen.getByLabelText("Name"), "cs2");
    await userEvent.clear(screen.getByLabelText("Size (GB)"));
    await userEvent.type(screen.getByLabelText("Size (GB)"), "50");
    await userEvent.click(screen.getByRole("button", { name: "Create disk" }));

    expect(calls(fetchMock)).toEqual([
      { url: "/api/v1/game-disks", method: "POST", body: { name: "cs2", size_gb: 50 } },
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

  it("shows the backend error when delete fails", async () => {
    stubFetch(409, { detail: { error: "Game disk 'cs2' is assigned to 1 machine(s); move them first" } });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<GameDisksPage disks={[disk()]} machines={[]} reload={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
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
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["PC…", "pc01"]);
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

  it("offers details only for published disks", () => {
    render(
      <GameDisksPage
        disks={[disk(), disk({ id: 2, name: "draft", published: false, snapshot: null })]}
        machines={[]}
        reload={vi.fn()}
      />,
    );
    const buttons = screen.getAllByRole("button", { name: "Details" });
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveAttribute("aria-expanded", "false");
  });
});
