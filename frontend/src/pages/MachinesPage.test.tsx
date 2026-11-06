import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Machine } from "../api";
import { calls, disk, machine, stubFetch } from "../test/fixtures";
import { MachinesPage } from "./MachinesPage";

// The page reads each disk's versions by itself (GET); tests check the actions.
const actions = (mock: ReturnType<typeof vi.fn>) => calls(mock).filter((c) => c.method !== "GET");

const DISKS = [disk(), disk({ id: 2, name: "draft", published: false, snapshot: null })];
const OS_IMAGE = disk({ id: 3, name: "win11", kind: "os" });

describe("MachinesPage", () => {
  it("offers only published disks", () => {
    render(<MachinesPage disks={DISKS} machines={[machine()]} reload={vi.fn()} />);
    const select = screen.getByLabelText("Game disk for pc01");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["None", "cs2"]);
  });

  it("creates a machine by MAC with a disk; there is no IQN field", async () => {
    const fetchMock = stubFetch(201, machine());
    const reload = vi.fn().mockResolvedValue(undefined);
    render(<MachinesPage disks={DISKS} machines={[]} reload={reload} />);

    await userEvent.click(screen.getByRole("button", { name: "Add machine" }));
    expect(screen.queryByLabelText(/IQN/i)).toBeNull();
    await userEvent.type(screen.getByLabelText("Name"), "pc01");
    await userEvent.type(screen.getByLabelText("MAC"), "10:ff:e0:66:be:3a");
    await userEvent.selectOptions(screen.getByLabelText("Game disk"), "1");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(actions(fetchMock)).toEqual([
      {
        url: "/api/v1/machines",
        method: "POST",
        body: { name: "pc01", mode: "disk", mac: "10:ff:e0:66:be:3a", game_disk_id: 1, os_disk_id: null },
      },
    ]);
    expect(reload).toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "Add machine" })).toBeNull();
  });

  it("does not add a machine without a MAC", async () => {
    const fetchMock = stubFetch(201, machine());
    render(<MachinesPage disks={DISKS} machines={[]} reload={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Add machine" }));
    await userEvent.type(screen.getByLabelText("Name"), "pc01");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(actions(fetchMock)).toEqual([]);
  });

  it("a boot mode machine gets an OS image and a game disk", async () => {
    const fetchMock = stubFetch(201, machine({ mode: "boot" }));
    render(<MachinesPage disks={[...DISKS, OS_IMAGE]} machines={[]} reload={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Add machine" }));
    expect(screen.queryByLabelText("OS image")).toBeNull();
    await userEvent.type(screen.getByLabelText("Name"), "pc01");
    await userEvent.type(screen.getByLabelText("MAC"), "18:c0:4d:4f:ab:a8");
    await userEvent.selectOptions(screen.getByLabelText("Mode"), "boot");
    // OS images are offered for the OS, not as game disks.
    expect(screen.getByLabelText("Game disk")).not.toHaveTextContent("win11");
    await userEvent.selectOptions(screen.getByLabelText("OS image"), "3");
    await userEvent.selectOptions(screen.getByLabelText("Game disk"), "1");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(actions(fetchMock)[0].body).toMatchObject({ mode: "boot", os_disk_id: 3, game_disk_id: 1 });
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

  it("says whether the writeback is clean, in use, pending reset or kept", () => {
    const pc = (id: number, overrides: Partial<Machine>) =>
      machine({ id, name: `pc0${id}`, game_disk_id: 1, status: "provisioned", ...overrides });
    render(
      <MachinesPage
        disks={DISKS}
        machines={[
          pc(1, {}),
          pc(2, { writeback_dirty: true, session_active: true }),
          pc(3, { writeback_dirty: true, session_active: false }),
          pc(4, { writeback_dirty: true, keep_writeback: true }),
        ]}
        reload={vi.fn()}
      />,
    );
    expect(screen.getByText("Clean")).toBeInTheDocument();
    expect(screen.getByText("In use")).toBeInTheDocument();
    expect(screen.getByText("Pending reset")).toBeInTheDocument();
    expect(screen.getByText("Kept")).toBeInTheDocument();
    expect(screen.queryByText("Changed")).not.toBeInTheDocument();
  });

  it("says Starting while a boot mode PC goes from iPXE to Windows", () => {
    const now = Date.now();
    const iso = (ms: number) => new Date(now - ms).toISOString();
    const pc = (id: number, overrides: Partial<Machine>) =>
      machine({ id, name: `pc0${id}`, mode: "boot", game_disk_id: 1, status: "provisioned",
                writeback_dirty: true, session_active: false, ...overrides });
    render(
      <MachinesPage
        disks={DISKS}
        machines={[
          pc(1, { ipxe_at: iso(30_000), session_changed_at: iso(10_000) }),       // iPXE handed over
          pc(2, { ipxe_at: iso(30 * 60_000), session_changed_at: iso(60_000) }),  // long ago: it went off
        ]}
        reload={vi.fn()}
      />,
    );
    const row = (name: string) => screen.getByText(name).closest("tr")!;
    expect(within(row("pc01")).getByText("Starting")).toBeInTheDocument();
    expect(within(row("pc02")).getByText("Pending reset")).toBeInTheDocument();
  });

  it("shows how much a PC wrote and flags a large writeback", () => {
    const pc = (id: number, overrides: Partial<Machine>) =>
      machine({ id, name: `pc0${id}`, game_disk_id: 1, status: "provisioned", ...overrides });
    render(
      <MachinesPage
        disks={DISKS}
        machines={[
          pc(1, { writeback_dirty: true, session_active: true, writeback_bytes: 6.5 * 2 ** 30 }),
          pc(2, { writeback_dirty: true, session_active: true, writeback_bytes: 25 * 2 ** 30, writeback_large: true }),
        ]}
        reload={vi.fn()}
      />,
    );
    const row = (name: string) => screen.getByText(name).closest("tr")!;
    expect(within(row("pc01")).getByText("In use · 6.50 GB")).toHaveClass("badge-blue");
    const large = within(row("pc02")).getByText(/In use · 25\.0 GB/);
    expect(large).toHaveClass("badge-warn");
    expect(large).toHaveAttribute("title", expect.stringContaining("Nothing is done to it"));
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
    vi.spyOn(window, "prompt").mockReturnValue("");
    const m = machine({ game_disk_id: 1, status: "provisioned", keep_writeback: true });
    render(<MachinesPage disks={DISKS} machines={[m]} reload={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Apply writebacks" }));
    expect(actions(fetchMock)).toEqual([
      { url: "/api/v1/machines/1/apply-writebacks", method: "POST", body: undefined },
    ]);
  });

  it("does not apply when the comment prompt is cancelled", async () => {
    const fetchMock = stubFetch(200, machine());
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(window, "prompt").mockReturnValue(null);
    const m = machine({ game_disk_id: 1, status: "provisioned", keep_writeback: true });
    render(<MachinesPage disks={DISKS} machines={[m]} reload={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Apply writebacks" }));
    expect(actions(fetchMock)).toEqual([]);
  });

  it("a boot mode PC can apply only its OS writeback", async () => {
    const fetchMock = stubFetch(200, machine());
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(window, "prompt").mockReturnValue("  AB drivers  ");
    const m = machine({
      mode: "boot",
      game_disk_id: 1,
      os_disk_id: 3,
      status: "provisioned",
      keep_writeback: true,
    });
    render(<MachinesPage disks={[...DISKS, OS_IMAGE]} machines={[m]} reload={vi.fn()} />);

    await userEvent.selectOptions(screen.getByLabelText("What to apply from pc01"), "os");
    await userEvent.click(screen.getByRole("button", { name: "Apply writebacks" }));
    expect(confirm.mock.calls[0][0]).toContain('the OS image "win11"');
    expect(confirm.mock.calls[0][0]).toContain("The game writeback is not applied");
    expect(actions(fetchMock)).toEqual([
      {
        url: "/api/v1/machines/1/apply-writebacks",
        method: "POST",
        body: { os: true, game: false, comment: "AB drivers" },
      },
    ]);
  });

  it("asks before applying an OS writeback whose Windows update is unfinished", async () => {
    const refused = {
      detail: { error: "Windows on 'pc01' has not finished an update (pending.xml).", update_pending: ["pending.xml"] },
    };
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
      JSON.parse(String(init?.body ?? "{}")).ignore_update
        ? new Response(JSON.stringify(machine()), { status: 200 })
        : new Response(JSON.stringify(refused), { status: 409 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(window, "prompt").mockReturnValue("");
    const m = machine({ mode: "boot", game_disk_id: 1, os_disk_id: 3, status: "provisioned", keep_writeback: true });
    render(<MachinesPage disks={[...DISKS, OS_IMAGE]} machines={[m]} reload={vi.fn()} />);

    await userEvent.selectOptions(screen.getByLabelText("What to apply from pc01"), "os");
    await userEvent.click(screen.getByRole("button", { name: "Apply writebacks" }));
    await vi.waitFor(() => expect(confirm).toHaveBeenCalledTimes(2));
    expect(confirm.mock.calls[1][0]).toContain("has not finished an update");
    expect(confirm.mock.calls[1][0]).toContain("Apply anyway?");
    expect(actions(fetchMock).map((c) => c.body)).toEqual([
      { os: true, game: false },
      { os: true, game: false, ignore_update: true },
    ]);
  });

  it("shows the boot mode PC's OS image and game disk with their versions", () => {
    const m = machine({
      mode: "boot",
      game_disk_id: 1,
      os_disk_id: 3,
      status: "provisioned",
      clone_snapshot: "tank/ggnet/images/cs2@v2",
      os_clone_snapshot: "tank/ggnet/images/win11@base",
    });
    render(<MachinesPage disks={[...DISKS, OS_IMAGE]} machines={[m]} reload={vi.fn()} />);
    expect(screen.getByText("@base")).toBeInTheDocument();
    expect(screen.getByText("@v2")).toBeInTheDocument();
  });

  it("summarises the machines in cards and never shows an IQN", () => {
    const now = new Date().toISOString();
    const machines = [
      machine({ id: 1, name: "pc01", last_seen_at: now, keep_writeback: true }),
      machine({ id: 2, name: "pc02", mode: "boot" }),
    ];
    render(<MachinesPage disks={DISKS} machines={machines} reload={vi.fn()} />);
    const card = (title: string) =>
      screen
        .getAllByText(title)
        .find((el) => el.classList.contains("kpi-title"))!
        .closest(".kpi-card") as HTMLElement;
    expect(within(card("Machines")).getByText("2")).toBeInTheDocument();
    expect(within(card("Machines")).getByText("1 Boot Mode · 1 Disk Mode")).toBeInTheDocument();
    expect(within(card("Online")).getByText("1")).toBeInTheDocument();
    expect(within(card("Writebacks")).getByText("1")).toBeInTheDocument();
    expect(screen.queryByText(/^iqn\./)).toBeNull();
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

  it("shows the image status", () => {
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

  it("turns an offline PC on with Wake-on-LAN, without asking", async () => {
    const fetchMock = stubFetch(200, machine());
    const confirm = vi.spyOn(window, "confirm");
    render(
      <MachinesPage
        disks={[]}
        machines={[machine({ mac: "aa:bb:cc:dd:ee:01" }), machine({ id: 2, name: "pc02", initiator_iqn: "iqn.x:pc02" })]}
        reload={vi.fn().mockResolvedValue(undefined)}
      />,
    );
    expect(screen.getByRole("button", { name: "Turn on pc02" })).toBeDisabled();   // no MAC known
    await userEvent.click(screen.getByRole("button", { name: "Turn on pc01" }));
    expect(confirm).not.toHaveBeenCalled();
    expect(actions(fetchMock)).toEqual([{ url: "/api/v1/machines/1/power", method: "POST", body: { action: "on" } }]);
  });

  it("asks before shutting down or restarting an online PC", async () => {
    const fetchMock = stubFetch(200, machine());
    vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const online = machine({ last_seen_at: new Date().toISOString() });
    render(<MachinesPage disks={[]} machines={[online]} reload={vi.fn().mockResolvedValue(undefined)} />);

    expect(screen.queryByRole("button", { name: "Turn on pc01" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Shut down pc01" }));   // cancelled
    await userEvent.click(screen.getByRole("button", { name: "Restart pc01" }));
    expect(actions(fetchMock)).toEqual([{ url: "/api/v1/machines/1/power", method: "POST", body: { action: "reboot" } }]);
  });

  it("shows a pending shutdown and blocks another command", () => {
    const m = machine({ last_seen_at: new Date().toISOString(), pending_command: "shutdown" });
    render(<MachinesPage disks={[]} machines={[m]} reload={vi.fn()} />);
    expect(screen.getByText("Shutting down…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart pc01" })).toBeDisabled();
  });

  it("finds machines by name, IP, MAC or disk", async () => {
    const machines = [
      machine({ id: 1, name: "pc01", mac: "aa:bb:cc:dd:ee:01", game_disk_id: 1, status: "provisioned" }),
      machine({ id: 2, name: "vip05", mac: "10:ff:e0:66:be:3a", reported_ip: "192.168.0.25" }),
    ];
    render(<MachinesPage disks={DISKS} machines={machines} reload={vi.fn()} />);
    const search = screen.getByLabelText("Search machines");

    await userEvent.type(search, "be:3a");
    expect(screen.queryByText("pc01")).toBeNull();
    expect(screen.getByText("vip05")).toBeInTheDocument();
    await userEvent.clear(search);
    await userEvent.type(search, "cs2");
    expect(screen.getByText("pc01")).toBeInTheDocument();
    expect(screen.queryByText("vip05")).toBeNull();
    await userEvent.clear(search);
    await userEvent.type(search, "nothing");
    expect(screen.getByText("No machines match the filter.")).toBeInTheDocument();
  });

  it("filters online, offline and writeback PCs, with counts", async () => {
    const machines = [
      machine({ id: 1, name: "pc01", last_seen_at: new Date().toISOString() }),
      machine({ id: 2, name: "pc02", game_disk_id: 1, status: "provisioned", keep_writeback: true }),
    ];
    render(<MachinesPage disks={DISKS} machines={machines} reload={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Online (1)" }));
    expect(screen.getByText("pc01")).toBeInTheDocument();
    expect(screen.queryByText("pc02")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Writeback (1)" }));
    expect(screen.queryByText("pc01")).toBeNull();
    expect(screen.getByText("pc02")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "All (2)" }));
    expect(screen.getByText("pc01")).toBeInTheDocument();
  });

  it("opens the settings from the PC's name and deletes from its icon", async () => {
    const fetchMock = stubFetch(204);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<MachinesPage disks={DISKS} machines={[machine()]} reload={vi.fn().mockResolvedValue(undefined)} />);

    await userEvent.click(screen.getByRole("button", { name: "pc01" }));
    expect(screen.getByRole("dialog", { name: "Settings of pc01" })).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Close" })[0]);
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Delete pc01" }));
    expect(actions(fetchMock)).toEqual([{ url: "/api/v1/machines/1", method: "DELETE", body: undefined }]);
  });

  it("shows whether a PC booted with Secure Boot", () => {
    const hw = { nic: null, cpu: null, gpus: [], motherboard: null, memory_bytes: null };
    const up = { last_seen_at: new Date().toISOString() };
    render(
      <MachinesPage
        disks={DISKS}
        machines={[
          machine({ id: 1, name: "pc01", ...up, hardware: { ...hw, secure_boot: true } }),
          machine({ id: 2, name: "pc02", ...up, hardware: { ...hw, secure_boot: false } }),
          machine({ id: 3, name: "pc03", ...up }),
          machine({ id: 4, name: "pc04", mok_state: "requested", hardware: { ...hw, secure_boot: false } }),
          machine({ id: 5, name: "pc05", ...up, agent_stopped: true, hardware: { ...hw, secure_boot: true } }),
        ]}
        reload={vi.fn()}
      />,
    );
    const row = (name: string) => screen.getByText(name).closest("tr")!;
    expect(within(row("pc04")).getByLabelText("Secure Boot key: confirm at next boot")).toBeInTheDocument();
    expect(within(row("pc01")).getByLabelText("Secure Boot on")).toBeInTheDocument();
    expect(within(row("pc02")).getByLabelText("Secure Boot off")).toBeInTheDocument();
    expect(within(row("pc03")).queryByLabelText(/Secure Boot/)).toBeNull();
    // Off: its last start says nothing about the next one.
    expect(within(row("pc05")).queryByLabelText(/Secure Boot/)).toBeNull();
  });

  describe("bulk", () => {
    const three = [
      machine({ id: 1, name: "pc01", game_disk_id: 1 }),
      machine({ id: 2, name: "pc02", initiator_iqn: "iqn.x:pc02", game_disk_id: 1 }),
      machine({ id: 3, name: "pc03", initiator_iqn: "iqn.x:pc03", game_disk_id: 2 }),
    ];

    it("shows the bar only with a selection, and select all picks every PC", async () => {
      render(<MachinesPage disks={DISKS} machines={three} reload={vi.fn()} />);
      expect(screen.queryByLabelText("Bulk actions")).toBeNull();
      await userEvent.click(screen.getByLabelText("Select all machines"));
      expect(screen.getByLabelText("Bulk actions")).toHaveTextContent("3 of 3 selected");
      // Different game disks: no shared version to pin.
      expect(screen.queryByLabelText("Version for selected")).toBeNull();
      await userEvent.click(screen.getByLabelText("Clear selection"));
      expect(screen.queryByLabelText("Bulk actions")).toBeNull();
    });

    it("turns the selected PCs on and reports the ones that failed", async () => {
      const fetchMock = stubFetch(200, [
        { machine_id: 1, name: "pc01", ok: true, error: null },
        { machine_id: 2, name: "pc02", ok: false, error: "'pc02' has no MAC address" },
      ]);
      render(<MachinesPage disks={DISKS} machines={three} reload={vi.fn().mockResolvedValue(undefined)} />);
      await userEvent.click(screen.getByLabelText("Select pc01"));
      await userEvent.click(screen.getByLabelText("Select pc02"));
      await userEvent.click(within(screen.getByLabelText("Bulk actions")).getByRole("button", { name: "Turn on" }));

      expect(actions(fetchMock)).toEqual([
        { url: "/api/v1/machines/bulk", method: "POST", body: { machine_ids: [1, 2], action: "on" } },
      ]);
      const report = await screen.findByRole("status");
      expect(report).toHaveTextContent("Turn on: done on 1, failed on 1");
      expect(report).toHaveTextContent("pc02: 'pc02' has no MAC address");
    });

    it("asks before a bulk shutdown, naming the PCs", async () => {
      const fetchMock = stubFetch(200, []);
      const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
      render(<MachinesPage disks={DISKS} machines={three} reload={vi.fn()} />);
      await userEvent.click(screen.getByLabelText("Select pc01"));
      await userEvent.click(within(screen.getByLabelText("Bulk actions")).getByRole("button", { name: "Shutdown" }));
      expect(confirm.mock.calls[0][0]).toContain("Shut down 1 PC(s)?");
      expect(confirm.mock.calls[0][0]).toContain("pc01");
      expect(actions(fetchMock)).toEqual([]);
    });

    it("assigns a disk and pins a shared version on the selected PCs", async () => {
      const fetchMock = vi.fn().mockImplementation(async (url: string) =>
        new Response(JSON.stringify(url.endsWith("/snapshots") ? [{ name: "base" }] : []), { status: 200 }),
      );
      vi.stubGlobal("fetch", fetchMock);
      vi.spyOn(window, "confirm").mockReturnValue(true);
      render(<MachinesPage disks={DISKS} machines={three} reload={vi.fn().mockResolvedValue(undefined)} />);
      await userEvent.click(screen.getByLabelText("Select pc01"));
      await userEvent.click(screen.getByLabelText("Select pc02"));
      const bar = screen.getByLabelText("Bulk actions");

      await userEvent.selectOptions(within(bar).getByLabelText("Game disk for selected"), "none");
      await userEvent.click(within(bar).getAllByRole("button", { name: "Apply" })[0]);
      await within(bar).findByRole("option", { name: "Pin @base" });
      await userEvent.selectOptions(within(bar).getByLabelText("Version for selected"), "base");
      await userEvent.click(within(bar).getAllByRole("button", { name: "Apply" })[1]);

      expect(actions(fetchMock)).toEqual([
        { url: "/api/v1/machines/bulk", method: "POST", body: { machine_ids: [1, 2], action: "assign", game_disk_id: null } },
        { url: "/api/v1/machines/bulk", method: "POST", body: { machine_ids: [1, 2], action: "pin", snapshot: "base" } },
      ]);
    });
  });
});
