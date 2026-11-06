import { useEffect, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  HardDrive,
  KeyRound,
  Lock,
  LockOpen,
  Monitor,
  Plus,
  Power,
  Radar,
  RotateCw,
  Search,
  Settings as SettingsIcon,
  Trash2,
  Upload,
  X,
  Zap,
} from "lucide-react";

import {
  api,
  applyWritebacksChecked,
  preselectDisk,
  type ApplyChoice,
  type BulkKind,
  type BulkResult,
  type Discovered,
  type GameDisk,
  type Machine,
  type MachineMode,
  type MachineTraffic,
} from "../api";
import { AgentState, isOnline } from "../components/AgentState";
import { DiscoveredPanel } from "../components/DiscoveredPanel";
import { ErrorBanner } from "../components/ErrorBanner";
import { ImageStatus } from "../components/ImageStatus";
import { MachineSettings } from "../components/MachineSettings";
import { StatusBadge } from "../components/StatusBadge";
import { formatBytes, formatRate } from "../format";
import { useAsyncAction } from "../useAsyncAction";

interface Props {
  disks: GameDisk[];
  machines: Machine[];
  /** Sent / Received / Speed per machine id, read live from LIO. */
  traffic?: Record<number, MachineTraffic>;
  /** PCs that asked for a disk but are not registered. */
  discovered?: Discovered[];
  reload: () => Promise<void>;
}

const CLIENT_OFF = "The client PC must be powered off or disconnected.";
// Game disk letters; A-C are floppy and system drives.
const LETTERS = "DEFGHIJKLMNOPQRSTUVWXYZ".split("");
const MOVES_NOW = "Running PCs move the game disk within a minute; close games on them first.";

type Filter = "all" | "online" | "offline" | "writeback";
const hasWriteback = (m: Machine) => m.keep_writeback || m.writeback_dirty || m.status === "editing";

export function MachinesPage({ disks, machines, traffic = {}, discovered = [], reload }: Props) {
  const { busy, error, setError, run } = useAsyncAction();
  const [adding, setAdding] = useState(false);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  // Boot Mode: which kept writebacks Apply Writebacks turns into versions, per machine.
  const [applyWhat, setApplyWhat] = useState<Record<number, ApplyKey>>({});
  const [allLetter, setAllLetter] = useState("D");
  // The machine whose settings dialog is open.
  const [editing, setEditing] = useState<number | null>(null);
  // Bulk operations: the selected machines and the last result.
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [bulkReport, setBulkReport] = useState<{ label: string; results: BulkResult[] } | null>(null);
  const picked = machines.filter((m) => selected.has(m.id));

  function toggle(id: number) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }

  async function runBulk(
    label: string,
    action: BulkKind,
    extra: Parameters<typeof api.bulk>[2] = {},
    confirmText?: string,
  ) {
    if (confirmText && !window.confirm(`${confirmText}\n\n${picked.map((m) => m.name).join(", ")}`)) return;
    await run("bulk", async () => {
      setBulkReport({ label, results: await api.bulk([...selected], action, extra) });
    });
    await reload();
  }
  // Versions of each game disk in use, for the pin drop-down.
  const [versions, setVersions] = useState<Record<number, string[]>>({});

  const published = disks.filter((d) => d.published && d.kind !== "os");
  const inUse = published.filter((d) => machines.some((m) => m.game_disk_id === d.id));
  // Re-read when a disk gets a new version (Apply Writebacks, Make active).
  const versionKey = inUse.map((d) => `${d.id}@${d.snapshot}`).join(",");

  useEffect(() => {
    let cancelled = false;
    Promise.all(
      inUse.map((d) =>
        api
          .listSnapshots(d.id)
          .then((s) => [d.id, s.map((v) => v.name)] as const)
          .catch(() => [d.id, []] as const),
      ),
    ).then((pairs) => {
      if (!cancelled) setVersions(Object.fromEntries(pairs));
    });
    return () => {
      cancelled = true;
    };
  }, [versionKey]); // inUse is derived from versionKey
  const diskName = (id: number | null) => disks.find((d) => d.id === id)?.name ?? "—";

  const online = machines.filter((m) => isOnline(m));
  const counts: Record<Filter, number> = {
    all: machines.length,
    online: online.length,
    offline: machines.length - online.length,
    writeback: machines.filter(hasWriteback).length,
  };
  const needle = search.trim().toLowerCase();
  const shown = machines.filter((m) => {
    if (filter === "online" && !isOnline(m)) return false;
    if (filter === "offline" && isOnline(m)) return false;
    if (filter === "writeback" && !hasWriteback(m)) return false;
    if (!needle) return true;
    return [m.name, m.mac, m.reported_ip, diskName(m.game_disk_id), diskName(m.os_disk_id ?? null)].some((v) =>
      v?.toLowerCase().includes(needle),
    );
  });

  async function assign(m: Machine, value: string) {
    const target = value ? Number(value) : null;
    if (target === m.game_disk_id && m.status === "provisioned") return;
    if (m.game_disk_id !== null || m.status !== "idle") {
      const msg =
        target === null
          ? `Remove the game disk from ${m.name}? Everything the client wrote is lost.`
          : `Switch ${m.name} to "${diskName(target)}"? Everything the client wrote is lost.`;
      if (!window.confirm(`${msg}\n\n${CLIENT_OFF}`)) return;
    }
    await run(`assign-${m.id}`, () => api.assignDisk(m.id, target));
    await reload();
  }

  async function setLetter(m: Machine, letter: string) {
    if (m.iscsi_connected && !window.confirm(`Move the game disk of ${m.name} to ${letter}:?\n\n${MOVES_NOW}`)) {
      return;
    }
    await run(`letter-${m.id}`, () => api.updateMachine(m.id, { drive_letter: letter }));
    await reload();
  }

  async function setLetterAll() {
    if (!window.confirm(`Set the game disk letter of every Disk Mode PC to ${allLetter}:?\n\n${MOVES_NOW}`)) {
      return;
    }
    await run("letter-all", () => api.setDriveLetterAll(allLetter));
    await reload();
  }

  async function power(m: Machine, action: "on" | "shutdown" | "reboot") {
    if (action !== "on") {
      const what = action === "shutdown" ? "Shut down" : "Restart";
      const msg =
        `${what} ${m.name}?\n\n` +
        "The agent gets it with its next heartbeat (within 30 s) and warns whoever sits at the PC 10 s before.";
      if (!window.confirm(msg)) return;
    }
    await run(`power-${m.id}`, () => api.power(m.id, action));
    await reload();
  }

  async function pin(m: Machine, value: string) {
    await run(`pin-${m.id}`, () => api.pinSnapshot(m.id, value || null));
    await reload();
  }

  async function keep(m: Machine, enabled: boolean) {
    if (!enabled) {
      const msg =
        `Stop keeping the writeback of ${m.name}?\n\n` +
        "Changes not applied yet are discarded the next time it disconnects.";
      if (!window.confirm(msg)) return;
    }
    await run(`keep-${m.id}`, () => api.setKeepWriteback(m.id, enabled));
    await reload();
  }

  async function apply(m: Machine) {
    const what: ApplyKey = m.mode === "boot" ? (applyWhat[m.id] ?? "both") : "game";
    const targets = [
      ...(what !== "game" ? [`the OS image "${diskName(m.os_disk_id ?? null)}"`] : []),
      ...(what !== "os" ? [`the game disk "${diskName(m.game_disk_id)}"`] : []),
    ];
    const kept =
      m.mode === "boot" && what !== "both"
        ? `\n\nThe ${what === "os" ? "game" : "OS"} writeback is not applied and stays kept as it is.`
        : "";
    const msg =
      `Apply the writebacks of ${m.name} to ${targets.join(" and ")}?\n\n` +
      "They become new versions. Every other PC gets them at its next reboot." +
      kept +
      `\n\n${m.name} must be powered off.`;
    if (!window.confirm(msg)) return;
    const comment = window.prompt("Comment for the new version (who, what changed). Optional.", "") ?? null;
    if (comment === null) return;
    const choice: ApplyChoice = {
      ...(m.mode === "boot" ? APPLY[what] : {}),
      ...(comment.trim() ? { comment: comment.trim() } : {}),
    };
    await run(`apply-${m.id}`, () =>
      applyWritebacksChecked(m.id, Object.keys(choice).length > 0 ? choice : undefined),
    );
    await reload();
  }

  async function remove(m: Machine) {
    if (!window.confirm(`Delete ${m.name} and its disk on the host?\n\n${CLIENT_OFF}`)) return;
    await run(`delete-${m.id}`, () => api.deleteMachine(m.id));
    await reload();
  }

  const allShownSelected = shown.length > 0 && shown.every((m) => selected.has(m.id));

  return (
    <section className={picked.length > 0 ? "has-bulk-dock" : undefined}>
      <ErrorBanner message={error} onClose={() => setError(null)} />

      <MachineKpis machines={machines} discovered={discovered.length} />

      {editing !== null && machines.some((x) => x.id === editing) && (
        <MachineSettings
          machine={machines.find((x) => x.id === editing)!}
          disks={disks}
          onClose={() => setEditing(null)}
          reload={reload}
        />
      )}

      {adding && (
        <AddMachine disks={disks} busy={busy !== null} run={run} reload={reload} onClose={() => setAdding(false)} />
      )}

      <DiscoveredPanel discovered={discovered} disks={disks} busy={busy !== null} run={run} reload={reload} />

      <div className="toolbar">
        <div className="toolbar-left">
          <div className="search-box">
            <Search size={15} className="search-icon" />
            <input
              type="text"
              placeholder="Search by name, IP, MAC, disk…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              aria-label="Search machines"
            />
          </div>
          <div className="filter-pills" role="group" aria-label="Filter machines">
            {FILTERS.map(([id, label]) => (
              <button
                key={id}
                type="button"
                className={`filter-pill ${filter === id ? "active" : ""}`}
                aria-pressed={filter === id}
                onClick={() => setFilter(id)}
              >
                {label} ({counts[id]})
              </button>
            ))}
          </div>
        </div>
        <div className="toolbar-right">
          {machines.some((m) => m.mode === "disk") && (
            <div className="bulk-disk-assign">
              <select
                aria-label="Game disk letter for all Disk Mode PCs"
                title="Game disk letter for all Disk Mode PCs"
                value={allLetter}
                onChange={(e) => setAllLetter(e.target.value)}
              >
                {LETTERS.map((l) => (
                  <option key={l} value={l}>
                    {l}:
                  </option>
                ))}
              </select>
              <button type="button" className="secondary small" onClick={setLetterAll} disabled={busy !== null}>
                {busy === "letter-all" ? "Applying…" : "Apply to all"}
              </button>
            </div>
          )}
          <button type="button" onClick={() => setAdding(true)}>
            <Plus size={16} />
            Add machine
          </button>
        </div>
      </div>

      {picked.length > 0 && (
        <BulkDock
          machines={picked}
          total={machines.length}
          disks={published}
          versions={versions}
          busy={busy !== null}
          run={runBulk}
          select={(ids) => setSelected(new Set(ids))}
          all={shown}
          clear={() => setSelected(new Set())}
        />
      )}
      {bulkReport && <BulkReport {...bulkReport} onClose={() => setBulkReport(null)} />}

      {shown.length === 0 ? (
        <div className="card empty">
          <Monitor size={36} className="empty-icon" />
          <p className="strong">{machines.length === 0 ? "No machines yet." : "No machines match the filter."}</p>
          <span className="muted small">
            {machines.length === 0
              ? "Start a PC from the network (PXE) and add it from New PCs, or use Add machine."
              : "Clear the search or pick another filter."}
          </span>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="machines-table">
            <thead>
              <tr>
                <th className="th-select">
                  <input
                    type="checkbox"
                    aria-label="Select all machines"
                    checked={allShownSelected}
                    onChange={(e) => setSelected(new Set(e.target.checked ? shown.map((m) => m.id) : []))}
                  />
                </th>
                <th>Station</th>
                <th>MAC</th>
                <th>Disks</th>
                <th>Writeback</th>
                <th title="Since the PC's writeback was last cloned">Network I/O</th>
                <th>Agent</th>
                <th className="th-actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((m) => {
                const up = isOnline(m);
                const commandBusy = busy !== null || m.pending_command !== null;
                return (
                  <tr key={m.id} className={selected.has(m.id) ? "row-selected" : undefined}>
                    <td className="td-select">
                      <input
                        type="checkbox"
                        aria-label={`Select ${m.name}`}
                        checked={selected.has(m.id)}
                        onChange={() => toggle(m.id)}
                      />
                    </td>

                    <td>
                      <div className="station">
                        <span
                          className={`status-dot ${up ? "online" : "offline"}`}
                          title={up ? "Online: the agent reports" : "Offline or not reporting"}
                        />
                        <div>
                          <div className="station-name">
                            <button
                              type="button"
                              className="btn-text"
                              onClick={() => setEditing(m.id)}
                              title={`Settings of ${m.name}`}
                            >
                              {m.name}
                            </button>
                            <SecureBootIcon machine={m} />
                            <span
                              className={`mode-pill ${m.mode === "boot" ? "mode-pill-boot" : "mode-pill-disk"}`}
                              title={m.mode === "boot" ? "Boot Mode: diskless" : "Disk Mode: local Windows"}
                            >
                              {m.mode === "boot" ? "BOOT" : "DISK"}
                            </span>
                          </div>
                          <div className="mono muted small">{m.reported_ip ?? "No IP reported"}</div>
                          {m.status !== "provisioned" && <StatusBadge status={m.status} />}
                          {m.last_error && <div className="error-text small">{m.last_error}</div>}
                        </div>
                      </div>
                    </td>

                    <td className="mono small">{m.mac ?? "—"}</td>

                    <td>
                      {m.status === "editing" ? (
                        <span className="badge badge-warn">Editing {diskName(m.editing_disk_id)}</span>
                      ) : m.mode === "boot" ? (
                        <BootDisks machine={m} diskName={diskName} />
                      ) : (
                        <div className="disk-cell">
                          <div className="disk-line">
                            <select
                              aria-label={`Drive letter for ${m.name}`}
                              className="letter-select"
                              value={m.drive_letter}
                              onChange={(e) => setLetter(m, e.target.value)}
                              disabled={busy !== null}
                            >
                              {LETTERS.map((l) => (
                                <option key={l} value={l}>
                                  {l}:
                                </option>
                              ))}
                            </select>
                            <select
                              aria-label={`Game disk for ${m.name}`}
                              className={m.game_disk_id ? "disk-select assigned" : "disk-select"}
                              value={m.game_disk_id ?? ""}
                              onChange={(e) => assign(m, e.target.value)}
                              disabled={busy !== null}
                            >
                              <option value="">None</option>
                              {published.map((d) => (
                                <option key={d.id} value={d.id}>
                                  {d.name}
                                </option>
                              ))}
                            </select>
                          </div>
                          {m.reported_drive_letter && m.reported_drive_letter !== m.drive_letter && (
                            <span
                              className="badge badge-warn"
                              title={`${m.drive_letter}: is taken on this PC, so the game disk got ${m.reported_drive_letter}:`}
                            >
                              Got {m.reported_drive_letter}:
                            </span>
                          )}
                          {m.game_disk_id !== null && (
                            <div className="image-version">
                              <select
                                aria-label={`Version for ${m.name}`}
                                value={m.pinned_snapshot ?? ""}
                                onChange={(e) => pin(m, e.target.value)}
                                disabled={busy !== null}
                              >
                                <option value="">
                                  Follow active (@{disks.find((d) => d.id === m.game_disk_id)?.snapshot})
                                </option>
                                {(versions[m.game_disk_id] ?? (m.pinned_snapshot ? [m.pinned_snapshot] : [])).map(
                                  (v) => (
                                    <option key={v} value={v}>
                                      Pin @{v}
                                    </option>
                                  ),
                                )}
                              </select>
                              <ImageStatus machine={m} disk={disks.find((d) => d.id === m.game_disk_id)} />
                            </div>
                          )}
                        </div>
                      )}
                    </td>

                    <td>
                      {m.status === "idle" ? (
                        <span className="muted">—</span>
                      ) : m.status !== "editing" && (
                        <div className="writeback-cell">
                          <label className="check small" title="Changes survive reboots; for installing and updating">
                            <input
                              type="checkbox"
                              aria-label={`Keep writeback of ${m.name}`}
                              checked={m.keep_writeback}
                              onChange={(e) => keep(m, e.target.checked)}
                              disabled={busy !== null}
                            />
                            Keep
                          </label>
                          {m.keep_writeback ? (
                            <span className="badge badge-warn">Kept{writebackSize(m)}</span>
                          ) : m.writeback_large ? (
                            <span
                              className="badge badge-warn"
                              title="This PC has written more than the warning size (Settings → Retention). Nothing is done to it; it is discarded as usual when the PC restarts."
                            >
                              <AlertTriangle size={12} />
                              {m.session_active ? "In use" : starting(m) ? "Starting" : "Pending reset"}
                              {writebackSize(m)}
                            </span>
                          ) : m.writeback_dirty && m.session_active ? (
                            <span className="badge badge-blue" title="The PC is on and writing to its disks">
                              In use{writebackSize(m)}
                            </span>
                          ) : m.writeback_dirty && starting(m) ? (
                            <span
                              className="badge badge-blue"
                              title="The PC is starting: the network boot handed over to Windows, which connects in a moment"
                            >
                              Starting{writebackSize(m)}
                            </span>
                          ) : m.writeback_dirty ? (
                            <span
                              className="badge"
                              title="The PC is off; what it wrote is discarded before its next start"
                            >
                              Pending reset{writebackSize(m)}
                            </span>
                          ) : (
                            <span className="badge badge-provisioned">
                              <CheckCircle2 size={12} />
                              Clean
                            </span>
                          )}
                          {m.keep_writeback && m.status === "provisioned" && (
                            <div className="apply-line">
                              {m.mode === "boot" && (
                                <select
                                  aria-label={`What to apply from ${m.name}`}
                                  value={applyWhat[m.id] ?? "both"}
                                  onChange={(e) => setApplyWhat({ ...applyWhat, [m.id]: e.target.value as ApplyKey })}
                                  disabled={busy !== null}
                                >
                                  <option value="both">OS + game</option>
                                  <option value="os">OS only</option>
                                  <option value="game">Game only</option>
                                </select>
                              )}
                              <button
                                type="button"
                                className="small"
                                onClick={() => apply(m)}
                                disabled={busy !== null || m.session_active === true}
                                title={m.session_active ? `Shut down ${m.name} first` : "Make them new versions"}
                              >
                                <Upload size={13} />
                                {busy === `apply-${m.id}` ? "Applying…" : "Apply writebacks"}
                              </button>
                            </div>
                          )}
                        </div>
                      )}
                    </td>

                    <td className="small">
                      <TrafficCell traffic={traffic[m.id]} />
                    </td>

                    <td>
                      <AgentState machine={m} />
                    </td>

                    <td>
                      <div className="actions-cell">
                        {up ? (
                          <>
                            <button
                              type="button"
                              className="icon-btn"
                              onClick={() => power(m, "reboot")}
                              disabled={commandBusy}
                              aria-label={`Restart ${m.name}`}
                              title="Restart"
                            >
                              <RotateCw size={14} />
                            </button>
                            <button
                              type="button"
                              className="icon-btn icon-btn-danger"
                              onClick={() => power(m, "shutdown")}
                              disabled={commandBusy}
                              aria-label={`Shut down ${m.name}`}
                              title="Shut down"
                            >
                              <Power size={14} />
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            className="icon-btn icon-btn-ok"
                            onClick={() => power(m, "on")}
                            disabled={busy !== null || (!m.mac && !m.reported_mac)}
                            aria-label={`Turn on ${m.name}`}
                            title={!m.mac && !m.reported_mac ? "No MAC address yet" : "Turn on (Wake-on-LAN)"}
                          >
                            <Zap size={14} />
                          </button>
                        )}
                        <button
                          type="button"
                          className="icon-btn"
                          onClick={() => setEditing(m.id)}
                          aria-label={`Settings of ${m.name}`}
                          title="Settings"
                        >
                          <SettingsIcon size={14} />
                        </button>
                        <button
                          type="button"
                          className="icon-btn icon-btn-danger"
                          onClick={() => remove(m)}
                          disabled={busy !== null || m.status === "editing"}
                          aria-label={`Delete ${m.name}`}
                          title={m.status === "editing" ? "Finish editing the game disk first" : "Delete"}
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

const FILTERS: [Filter, string][] = [
  ["all", "All"],
  ["online", "Online"],
  ["offline", "Offline"],
  ["writeback", "Writeback"],
];

interface AddMachineProps {
  disks: GameDisk[];
  busy: boolean;
  run: ReturnType<typeof useAsyncAction>["run"];
  reload: () => Promise<void>;
  onClose: () => void;
}

/** Register a PC by hand, by its MAC. A PC started from the network shows up under New PCs instead. */
function AddMachine({ disks, busy, run, reload, onClose }: AddMachineProps) {
  const [name, setName] = useState("");
  const [mac, setMac] = useState("");
  const [mode, setMode] = useState<MachineMode>("disk");
  const gameDisks = disks.filter((d) => d.published && d.kind !== "os");
  const osImages = disks.filter((d) => d.published && d.kind === "os");
  // New PCs get the default images (Images → Make default).
  const [diskId, setDiskId] = useState(preselectDisk(gameDisks));
  const [osDiskId, setOsDiskId] = useState(preselectDisk(osImages));

  async function create(e: FormEvent) {
    e.preventDefault();
    const ok = await run("create", () =>
      api.createMachine({
        name: name.trim(),
        mode,
        mac: mac.trim(),
        game_disk_id: diskId ? Number(diskId) : null,
        os_disk_id: mode === "boot" && osDiskId ? Number(osDiskId) : null,
      }),
    );
    await reload();
    if (ok) onClose();
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-content" role="dialog" aria-label="Add machine" onClick={(e) => e.stopPropagation()}>
        <div className="modal-sheet-grabber" />
        <div className="modal-header">
          <h2>Add machine</h2>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <form onSubmit={create}>
          <div className="modal-body">
            <div className="form-group">
              <label htmlFor="add-name">Name</label>
              <input
                id="add-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. pc-01"
                required
                maxLength={15}
                autoFocus
              />
              <div className="form-hint">Windows gets this computer name in Boot Mode.</div>
            </div>
            <div className="form-group">
              <label htmlFor="add-mac">MAC</label>
              <input
                id="add-mac"
                value={mac}
                onChange={(e) => setMac(e.target.value)}
                placeholder="e.g. 10:ff:e0:66:be:3a"
                required
              />
              <div className="form-hint">The PC's identity: network boot and Wake-on-LAN find it by this.</div>
            </div>
            <div className="form-group">
              <label htmlFor="add-mode">Mode</label>
              <select id="add-mode" value={mode} onChange={(e) => setMode(e.target.value as MachineMode)}>
                <option value="disk">Disk (local Windows)</option>
                <option value="boot">Boot (diskless)</option>
              </select>
            </div>
            {mode === "boot" && (
              <div className="form-group">
                <label htmlFor="add-os">OS image</label>
                <select id="add-os" value={osDiskId} onChange={(e) => setOsDiskId(e.target.value)} required>
                  <option value="">Choose…</option>
                  {osImages.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </select>
                <div className="form-hint">C:, loaded over the network.</div>
              </div>
            )}
            <div className="form-group">
              <label htmlFor="add-disk">Game disk</label>
              <select
                id="add-disk"
                value={diskId}
                onChange={(e) => setDiskId(e.target.value)}
                required={mode === "boot"}
              >
                <option value="">None</option>
                {gameDisks.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="modal-footer">
            <div className="footer-left" />
            <div className="footer-right">
              <button type="button" className="secondary" onClick={onClose} disabled={busy}>
                Cancel
              </button>
              <button type="submit" disabled={busy}>
                <Plus size={14} />
                {busy ? "Adding…" : "Add"}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

/** Sent / Received / Speed for one machine. */
function TrafficCell({ traffic: t }: { traffic: MachineTraffic | undefined }) {
  if (!t) return <span className="muted">Idle</span>;
  return (
    <div aria-label="Traffic">
      <div>
        Sent {formatBytes(t.sent_bytes)} · Received {formatBytes(t.received_bytes)}
      </div>
      {t.sent_bps !== null && t.received_bps !== null && (
        <div>
          <span className="rate-down">↓ {formatRate(t.sent_bps)}</span>
          <span className="muted"> · </span>
          <span className="rate-up">↑ {formatRate(t.received_bps)}</span>
        </div>
      )}
    </div>
  );
}

interface BulkDockProps {
  machines: Machine[];
  total: number;
  disks: GameDisk[];
  versions: Record<number, string[]>;
  busy: boolean;
  run: (label: string, action: BulkKind, extra?: Parameters<typeof api.bulk>[2], confirmText?: string) => void;
  /** The machines shown now, for the quick selections. */
  all: Machine[];
  select: (ids: number[]) => void;
  clear: () => void;
}

/** Bulk operations on the selected machines, as a dock at the bottom of the screen. */
function BulkDock({ machines, total, disks, versions, busy, run, all, select, clear }: BulkDockProps) {
  const [disk, setDisk] = useState("");
  const [version, setVersion] = useState("");
  const [letter, setLetter] = useState("D");
  const n = machines.length;
  // A version can be pinned on several PCs only when they share a game disk.
  const diskIds = new Set(machines.map((m) => m.game_disk_id));
  const shared = diskIds.size === 1 ? [...diskIds][0] : null;
  const sharedVersions = shared !== null ? (versions[shared] ?? []) : [];

  return (
    <div className="bulk-toolbar" role="region" aria-label="Bulk actions">
      <div className="bulk-selection-info">
        <span className="bulk-badge">
          {n} of {total} selected
        </span>
        <div className="bulk-quick-selects">
          <button type="button" className="bulk-quick-btn" onClick={() => select(all.map((m) => m.id))}>
            All
          </button>
          <button
            type="button"
            className="bulk-quick-btn"
            onClick={() => select(all.filter((m) => isOnline(m)).map((m) => m.id))}
          >
            Online
          </button>
          <button
            type="button"
            className="bulk-quick-btn"
            onClick={() => select(all.filter((m) => !isOnline(m)).map((m) => m.id))}
          >
            Offline
          </button>
          <button type="button" className="bulk-quick-btn" aria-label="Clear selection" onClick={clear}>
            <X size={12} />
          </button>
        </div>
      </div>

      <div className="bulk-actions-group">
        <button type="button" className="bulk-btn bulk-btn-wol" disabled={busy} onClick={() => run("Turn on", "on")}>
          <Zap size={14} />
          Turn on
        </button>
        <button
          type="button"
          className="bulk-btn bulk-btn-poweroff"
          disabled={busy}
          onClick={() => run("Shutdown", "shutdown", {}, `Shut down ${n} PC(s)?`)}
        >
          <Power size={14} />
          Shutdown
        </button>
        <button
          type="button"
          className="bulk-btn bulk-btn-reboot"
          disabled={busy}
          onClick={() => run("Reboot", "reboot", {}, `Restart ${n} PC(s)?`)}
        >
          <RotateCw size={14} />
          Reboot
        </button>

        <div className="bulk-disk-assign">
          <select aria-label="Game disk for selected" value={disk} onChange={(e) => setDisk(e.target.value)}>
            <option value="">Game disk…</option>
            <option value="none">None (remove)</option>
            {disks.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="small"
            disabled={busy || !disk}
            onClick={() =>
              run(
                "Game disk",
                "assign",
                { game_disk_id: disk === "none" ? null : Number(disk) },
                `Set the game disk of ${n} PC(s)? Everything they wrote is lost. ${CLIENT_OFF}`,
              )
            }
          >
            Apply
          </button>
        </div>

        {shared !== null && (
          <div className="bulk-disk-assign">
            <select aria-label="Version for selected" value={version} onChange={(e) => setVersion(e.target.value)}>
              <option value="">Version…</option>
              <option value="follow">Follow active</option>
              {sharedVersions.map((v) => (
                <option key={v} value={v}>
                  Pin @{v}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="small"
              disabled={busy || !version}
              onClick={() => run("Version", "pin", { snapshot: version === "follow" ? null : version })}
            >
              Apply
            </button>
          </div>
        )}

        <div className="bulk-disk-assign">
          <select aria-label="Drive letter for selected" value={letter} onChange={(e) => setLetter(e.target.value)}>
            {LETTERS.map((l) => (
              <option key={l} value={l}>
                {l}:
              </option>
            ))}
          </select>
          <button
            type="button"
            className="small"
            disabled={busy}
            onClick={() => run("Drive letter", "drive_letter", { drive_letter: letter }, `${MOVES_NOW}`)}
          >
            Apply
          </button>
        </div>

        <button
          type="button"
          className="bulk-btn"
          disabled={busy || !machines.some((m) => m.keep_writeback)}
          onClick={() => run("Stop keeping writeback", "stop_keep_writeback")}
        >
          Stop keeping writeback
        </button>
      </div>
    </div>
  );
}

/** "Turn on: done on 3, failed on 1" with the reason per failed PC. */
function BulkReport({ label, results, onClose }: { label: string; results: BulkResult[]; onClose: () => void }) {
  const failed = results.filter((r) => !r.ok);
  const done = results.length - failed.length;
  return (
    <div className={failed.length ? "banner banner-warn" : "banner"} role="status">
      <div>
        <span className="strong">{label}</span>: done on {done}
        {failed.length > 0 && `, failed on ${failed.length}`}
        {failed.map((r) => (
          <div key={r.machine_id} className="small">
            {r.name ?? `#${r.machine_id}`}: {r.error}
          </div>
        ))}
      </div>
      <button type="button" className="link" aria-label="Close" onClick={onClose}>
        ×
      </button>
    </div>
  );
}

type ApplyKey = "both" | "os" | "game";
const APPLY: Record<ApplyKey, ApplyChoice> = {
  both: { os: true, game: true },
  os: { os: true, game: false },
  game: { os: false, game: true },
};

/** ggNet2's lock: whether Windows booted with Secure Boot on (anti-cheats need it), as the agent reports. */
// The server's boot window (writebacks.boot_window_seconds, 300 by default).
const BOOT_WINDOW_MS = 5 * 60 * 1000;

/**
 * A Boot Mode PC between iPXE and Windows' own iSCSI login: iPXE's session
 * ends when Windows takes over, so for a moment the PC looks disconnected.
 */
export function starting(m: Machine, now = Date.now()): boolean {
  return (
    m.mode === "boot" &&
    !m.session_active &&
    m.ipxe_at !== null &&
    now - Date.parse(m.ipxe_at) < BOOT_WINDOW_MS &&
    (m.session_changed_at === null || Date.parse(m.session_changed_at) >= Date.parse(m.ipxe_at))
  );
}

/** " · 6.2 GB": what the PC has written into its writebacks, once measured. */
function writebackSize(m: Machine): string {
  return m.writeback_bytes ? ` · ${formatBytes(m.writeback_bytes)}` : "";
}

function SecureBootIcon({ machine: m }: { machine: Machine }) {
  const on = m.hardware?.secure_boot;
  if (m.mok_state === "requested" && !on) {
    return (
      <span
        className="secure-boot-pending"
        title="ggNet Secure Boot key asked for: confirm Enroll MOK in MokManager at the next boot"
        aria-label="Secure Boot key: confirm at next boot"
      >
        <KeyRound size={12} />
      </span>
    );
  }
  // Reported at each start: only a running PC's state is known.
  if (on === undefined || on === null || !isOnline(m)) return null;
  return on ? (
    <span className="secure-boot-on" title="Secure Boot on" aria-label="Secure Boot on">
      <Lock size={12} />
    </span>
  ) : (
    <span className="secure-boot-off" title="Secure Boot off" aria-label="Secure Boot off">
      <LockOpen size={12} />
    </span>
  );
}

/** Boot Mode: the OS image (C:) and game disk (D:) a PC runs, with their versions. */
function BootDisks({ machine: m, diskName }: { machine: Machine; diskName: (id: number | null) => string }) {
  const version = (snap: string | null | undefined) => (snap ? `@${snap.split("@")[1]}` : "");
  return (
    <div className="disk-cell">
      <div className="disk-line">
        <span className="drive-os">C:</span>
        <span className="code-badge">{diskName(m.os_disk_id ?? null)}</span>
        <span className="muted small">{version(m.os_clone_snapshot)}</span>
      </div>
      <div className="disk-line">
        <span className="drive-game">D:</span>
        <span className="code-badge">{diskName(m.game_disk_id)}</span>
        <span className="muted small">{version(m.clone_snapshot)}</span>
      </div>
    </div>
  );
}

/** Summary cards above the machine list. */
function MachineKpis({ machines, discovered }: { machines: Machine[]; discovered: number }) {
  const online = machines.filter((m) => isOnline(m)).length;
  const writebacks = machines.filter((m) => m.keep_writeback || m.writeback_dirty).length;
  const boot = machines.filter((m) => m.mode === "boot").length;
  const cards = [
    { title: "Machines", value: machines.length, sub: `${boot} Boot Mode · ${machines.length - boot} Disk Mode`, icon: Monitor, tone: "blue" },
    { title: "Online", value: online, sub: `${machines.length - online} off or not reporting`, icon: Zap, tone: "green" },
    { title: "Writebacks", value: writebacks, sub: "written since the last reset, or kept", icon: HardDrive, tone: "amber" },
    { title: "New PCs", value: discovered, sub: "knocked but not registered", icon: Radar, tone: "purple" },
  ];
  return (
    <div className="kpi-grid">
      {cards.map(({ title, value, sub, icon: Icon, tone }) => (
        <div key={title} className="kpi-card">
          <div className="kpi-info">
            <span className="kpi-title">{title}</span>
            <span className="kpi-value">{value}</span>
            <span className="kpi-subtitle">{sub}</span>
          </div>
          <div className={`kpi-icon-box ${tone}`}>
            <Icon size={22} />
          </div>
        </div>
      ))}
    </div>
  );
}
