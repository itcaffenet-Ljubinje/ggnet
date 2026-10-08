import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { calls, disk, machine, stubFetch } from "../test/fixtures";
import { MachinesPage } from "./MachinesPage";

const DISKS = [disk(), disk({ id: 2, name: "draft", published: false, snapshot: null })];

describe("MachinesPage", () => {
  it("offers only published disks", () => {
    render(<MachinesPage disks={DISKS} machines={[machine()]} reload={vi.fn()} />);
    const select = screen.getByLabelText("Game disk for pc01");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["None", "cs2"]);
  });

  it("creates a machine with a disk; empty fields are sent as null", async () => {
    const fetchMock = stubFetch(201, machine());
    const reload = vi.fn().mockResolvedValue(undefined);
    render(<MachinesPage disks={DISKS} machines={[]} reload={reload} />);

    await userEvent.type(screen.getByLabelText("Name"), "pc01");
    await userEvent.selectOptions(screen.getByLabelText("Game disk"), "1");
    await userEvent.click(screen.getByRole("button", { name: "Add machine" }));

    expect(calls(fetchMock)).toEqual([
      {
        url: "/api/v1/machines",
        method: "POST",
        body: { name: "pc01", mode: "disk", mac: null, initiator_iqn: null, game_disk_id: 1 },
      },
    ]);
    expect(reload).toHaveBeenCalled();
  });

  it("boot mode machines get no disk", async () => {
    const fetchMock = stubFetch(201, machine({ mode: "boot" }));
    render(<MachinesPage disks={DISKS} machines={[]} reload={vi.fn()} />);

    await userEvent.type(screen.getByLabelText("Name"), "pc01");
    await userEvent.selectOptions(screen.getByLabelText("Game disk"), "1");
    await userEvent.selectOptions(screen.getByLabelText("Mode"), "boot");
    expect(screen.getByLabelText("Game disk")).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Add machine" }));

    expect(calls(fetchMock)[0].body).toMatchObject({ mode: "boot", game_disk_id: null });
  });

  it("assigns a disk to an idle machine without asking", async () => {
    const fetchMock = stubFetch(200, machine());
    const confirm = vi.spyOn(window, "confirm");
    render(<MachinesPage disks={DISKS} machines={[machine()]} reload={vi.fn()} />);

    await userEvent.selectOptions(screen.getByLabelText("Game disk for pc01"), "1");
    expect(confirm).not.toHaveBeenCalled();
    expect(calls(fetchMock)).toEqual([
      { url: "/api/v1/machines/1/assign", method: "POST", body: { game_disk_id: 1 } },
    ]);
  });

  it("asks before removing a disk and stops when cancelled", async () => {
    const fetchMock = stubFetch();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const m = machine({ game_disk_id: 1, status: "provisioned" });
    render(<MachinesPage disks={DISKS} machines={[m]} reload={vi.fn()} />);

    await userEvent.selectOptions(screen.getByLabelText("Game disk for pc01"), "");
    expect(confirm.mock.calls[0][0]).toContain("powered off");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("has no manual reset; writebacks are discarded by the server", () => {
    const m = machine({ game_disk_id: 1, status: "provisioned" });
    render(<MachinesPage disks={DISKS} machines={[m]} reload={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Reset" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply writebacks" })).not.toBeInTheDocument();
  });

  it("turns keep writeback on without asking", async () => {
    const fetchMock = stubFetch(200, machine());
    const confirm = vi.spyOn(window, "confirm");
    const m = machine({ game_disk_id: 1, status: "provisioned" });
    render(<MachinesPage disks={DISKS} machines={[m]} reload={vi.fn()} />);

    await userEvent.click(screen.getByLabelText("Keep writeback of pc01"));
    expect(confirm).not.toHaveBeenCalled();
    expect(calls(fetchMock)).toEqual([
      { url: "/api/v1/machines/1/keep-writeback", method: "PUT", body: { enabled: true } },
    ]);
  });

  it("asks before turning keep writeback off", async () => {
    const fetchMock = stubFetch();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const m = machine({ game_disk_id: 1, status: "provisioned", keep_writeback: true });
    render(<MachinesPage disks={DISKS} machines={[m]} reload={vi.fn()} />);

    await userEvent.click(screen.getByLabelText("Keep writeback of pc01"));
    expect(confirm.mock.calls[0][0]).toContain("discarded");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("applies writebacks after confirmation", async () => {
    const fetchMock = stubFetch(200, machine());
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const m = machine({ game_disk_id: 1, status: "provisioned", keep_writeback: true });
    render(<MachinesPage disks={DISKS} machines={[m]} reload={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Apply writebacks" }));
    expect(calls(fetchMock)).toEqual([
      { url: "/api/v1/machines/1/apply-writebacks", method: "POST", body: undefined },
    ]);
  });

  it("cannot apply while the PC is connected", () => {
    const m = machine({
      game_disk_id: 1,
      status: "provisioned",
      keep_writeback: true,
      session_active: true,
    });
    render(<MachinesPage disks={DISKS} machines={[m]} reload={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Apply writebacks" })).toBeDisabled();
  });

  it("shows status, last error and outdated badge", () => {
    const m = machine({
      game_disk_id: 1,
      status: "error",
      last_error: "Resetting clone failed: dataset is busy",
      outdated: true,
    });
    render(<MachinesPage disks={DISKS} machines={[m]} reload={vi.fn()} />);
    expect(screen.getByText("Error")).toBeInTheDocument();
    expect(screen.getByText("Update available")).toBeInTheDocument();
    expect(screen.getByText(/dataset is busy/)).toBeInTheDocument();
  });

  it("shows the backend error when an action fails", async () => {
    stubFetch(502, { detail: { error: "Creating the iSCSI target failed", machine_id: 1 } });
    render(<MachinesPage disks={DISKS} machines={[machine()]} reload={vi.fn()} />);

    await userEvent.selectOptions(screen.getByLabelText("Game disk for pc01"), "1");
    expect(await screen.findByRole("alert")).toHaveTextContent("Creating the iSCSI target failed");
  });

  it("changes one machine's letter without asking while it is offline", async () => {
    const fetchMock = stubFetch(200, machine({ drive_letter: "G" }));
    const confirm = vi.spyOn(window, "confirm");
    render(<MachinesPage disks={[]} machines={[machine()]} reload={vi.fn().mockResolvedValue(undefined)} />);

    await userEvent.selectOptions(screen.getByLabelText("Drive letter for pc01"), "G");
    expect(confirm).not.toHaveBeenCalled();
    expect(calls(fetchMock)).toEqual([
      { url: "/api/v1/machines/1", method: "PATCH", body: { drive_letter: "G" } },
    ]);
  });

  it("asks before moving the disk of a running PC", async () => {
    const fetchMock = stubFetch();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<MachinesPage disks={[]} machines={[machine({ iscsi_connected: true })]} reload={vi.fn()} />);
    await userEvent.selectOptions(screen.getByLabelText("Drive letter for pc01"), "E");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sets the letter for all disk mode PCs", async () => {
    const fetchMock = stubFetch(200, []);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<MachinesPage disks={[]} machines={[machine()]} reload={vi.fn().mockResolvedValue(undefined)} />);

    await userEvent.selectOptions(screen.getByLabelText("Game disk letter for all Disk Mode PCs"), "F");
    await userEvent.click(screen.getByRole("button", { name: "Apply to all" }));
    expect(calls(fetchMock)).toEqual([
      { url: "/api/v1/machines/drive-letter", method: "PUT", body: { drive_letter: "F" } },
    ]);
  });

  it("shows the letter the PC actually got; boot mode is always D:", () => {
    render(
      <MachinesPage
        disks={[]}
        machines={[
          machine({ drive_letter: "D", reported_drive_letter: "E" }),
          machine({ id: 2, name: "boot01", mode: "boot", initiator_iqn: "iqn.1991-05.com.microsoft:boot01" }),
        ]}
        reload={vi.fn()}
      />,
    );
    expect(screen.getByText("Got E:")).toBeInTheDocument();
    expect(screen.queryByLabelText("Drive letter for boot01")).toBeNull();
  });
});
