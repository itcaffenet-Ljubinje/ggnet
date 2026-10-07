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
});
