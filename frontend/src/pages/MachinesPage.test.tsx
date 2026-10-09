import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Machine } from "../api";
import { calls, disk, machine, stubFetch } from "../test/fixtures";
import { MachinesPage } from "./MachinesPage";

// The page reads each disk's versions by itself (GET); tests check the actions.
const actions = (mock: ReturnType<typeof vi.fn>) => calls(mock).filter((c) => c.method !== "GET");

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

    expect(actions(fetchMock)).toEqual([
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

    expect(actions(fetchMock)[0].body).toMatchObject({ mode: "boot", game_disk_id: null });
  });

  it("assigns a disk to an idle machine without asking", async () => {
    const fetchMock = stubFetch(200, machine());
    const confirm = vi.spyOn(window, "confirm");
    render(<MachinesPage disks={DISKS} machines={[machine()]} reload={vi.fn()} />);

    await userEvent.selectOptions(screen.getByLabelText("Game disk for pc01"), "1");
    expect(confirm).not.toHaveBeenCalled();
    expect(actions(fetchMock)).toEqual([
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
    expect(actions(fetchMock)).toEqual([]);
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
    expect(actions(fetchMock)).toEqual([
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
    expect(actions(fetchMock)).toEqual([]);
  });

  it("applies writebacks after confirmation", async () => {
    const fetchMock = stubFetch(200, machine());
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const m = machine({ game_disk_id: 1, status: "provisioned", keep_writeback: true });
    render(<MachinesPage disks={DISKS} machines={[m]} reload={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Apply writebacks" }));
    expect(actions(fetchMock)).toEqual([
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

  it("shows status and last error", () => {
    const m = machine({
      game_disk_id: 1,
      status: "error",
      last_error: "Resetting clone failed: dataset is busy",
    });
    render(<MachinesPage disks={DISKS} machines={[m]} reload={vi.fn()} />);
    expect(screen.getByText("Error")).toBeInTheDocument();
    expect(screen.getByText(/dataset is busy/)).toBeInTheDocument();
  });

  it("shows the image status like ggRock", () => {
    const disks = [disk({ snapshot: "v2" })];
    const on = (overrides: Partial<Machine>) =>
      machine({ game_disk_id: 1, status: "provisioned", clone_snapshot: "tank/ggnet/images/cs2@v2", ...overrides });
    render(
      <MachinesPage
        disks={disks}
        machines={[
          on({ id: 1, name: "pc01" }),
          on({ id: 2, name: "pc02", clone_snapshot: "tank/ggnet/images/cs2@base", outdated: true }),
          on({ id: 3, name: "pc03", clone_snapshot: "tank/ggnet/images/cs2@base", pinned_snapshot: "base" }),
          on({ id: 4, name: "pc04", pinned_snapshot: "base", outdated: true }),
          on({ id: 5, name: "pc05", keep_writeback: true, clone_snapshot: "tank/ggnet/images/cs2@base" }),
        ]}
        reload={vi.fn()}
      />,
    );
    const row = (name: string) => screen.getByText(name).closest("tr")!;
    expect(row("pc01")).toHaveTextContent("@v2");                  // green: on the active version
    expect(row("pc02")).toHaveTextContent("→ @v2 at reboot");      // follows the active version
    expect(row("pc03")).toHaveTextContent("Pinned @base");         // red: pinned
    expect(row("pc04")).toHaveTextContent("→ @base at reboot");    // just pinned to an older one
    expect(row("pc05")).toHaveTextContent("Keep writeback · @base");
  });

  it("pins a machine to a version and back", async () => {
    const mock = vi.fn().mockImplementation(async (url: string) => {
      const body = url.endsWith("/snapshots") ? [{ name: "base" }, { name: "v2" }] : machine();
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", mock);
    const m = machine({ game_disk_id: 1, status: "provisioned", clone_snapshot: "tank/ggnet/images/cs2@base" });
    render(<MachinesPage disks={DISKS} machines={[m]} reload={vi.fn().mockResolvedValue(undefined)} />);

    const select = screen.getByLabelText("Version for pc01");
    await screen.findByRole("option", { name: "Pin @v2" });
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Follow active (@base)",
      "Pin @base",
      "Pin @v2",
    ]);
    await userEvent.selectOptions(select, "v2");
    await userEvent.selectOptions(select, "");
    expect(actions(mock)).toEqual([
      { url: "/api/v1/machines/1/pin", method: "PUT", body: { snapshot: "v2" } },
      { url: "/api/v1/machines/1/pin", method: "PUT", body: { snapshot: null } },
    ]);
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
    expect(actions(fetchMock)).toEqual([
      { url: "/api/v1/machines/1", method: "PATCH", body: { drive_letter: "G" } },
    ]);
  });

  it("asks before moving the disk of a running PC", async () => {
    const fetchMock = stubFetch();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<MachinesPage disks={[]} machines={[machine({ iscsi_connected: true })]} reload={vi.fn()} />);
    await userEvent.selectOptions(screen.getByLabelText("Drive letter for pc01"), "E");
    expect(actions(fetchMock)).toEqual([]);
  });

  it("sets the letter for all disk mode PCs", async () => {
    const fetchMock = stubFetch(200, []);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<MachinesPage disks={[]} machines={[machine()]} reload={vi.fn().mockResolvedValue(undefined)} />);

    await userEvent.selectOptions(screen.getByLabelText("Game disk letter for all Disk Mode PCs"), "F");
    await userEvent.click(screen.getByRole("button", { name: "Apply to all" }));
    expect(actions(fetchMock)).toEqual([
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

  it("shows sent, received and speed per machine", () => {
    const MB = 1024 * 1024;
    render(
      <MachinesPage
        disks={DISKS}
        machines={[machine(), machine({ id: 2, name: "pc02", initiator_iqn: "iqn.1991-05.com.microsoft:pc02" })]}
        traffic={{
          1: { machine_id: 1, sent_bytes: 1200 * MB, received_bytes: 40 * MB, sent_bps: 10 * MB, received_bps: 0 },
        }}
        reload={vi.fn()}
      />,
    );
    const cell = screen.getByLabelText("Traffic");
    expect(cell).toHaveTextContent("Sent 1.17 GB · Received 40.0 MB");
    expect(cell).toHaveTextContent("↓ 10.0 MB/s · ↑ 0 B/s");
    expect(screen.getAllByLabelText("Traffic")).toHaveLength(1);   // pc02 has no counters
  });
});
