import { Fragment, useState, type FormEvent } from "react";
import {
  CheckCircle2,
  Copy,
  Cpu,
  Gamepad2,
  HardDriveDownload,
  HardDrive,
  Info,
  Layers,
  Monitor,
  Pencil,
  Plus,
  Trash2,
  Wand2,
  XCircle,
  type LucideIcon,
} from "lucide-react";

import {
  api,
  isImport,
  needsHardware,
  type DiskKind,
  type GameDisk,
  type Machine,
  type PrepareOptions,
} from "../api";
import { ActionMenu, type MenuAction } from "../components/ActionMenu";
import { EditImage } from "../components/EditImage";
import { ImportImage } from "../components/ImportImage";
import { AddHardwareWizard } from "../components/AddHardwareWizard";
import { PrepareImage } from "../components/PrepareImage";
import { ErrorBanner } from "../components/ErrorBanner";
import { useAsyncAction } from "../useAsyncAction";
import { ImageDetailsPage } from "./ImageDetailsPage";

/** A copy that is still running (a failed one keeps its error instead). */
const copying = (d: GameDisk) => !!d.copy_source && !d.copy_error;

interface Props {
  disks: GameDisk[];
  machines: Machine[];
  reload: () => Promise<void>;
}

export function GameDisksPage({ disks, machines, reload }: Props) {
  const { busy, error, setError, run } = useAsyncAction();
  const [name, setName] = useState("");
  const [sizeGb, setSizeGb] = useState("100");
  const [kind, setKind] = useState<DiskKind>("game");
  // Per draft disk: the machine picked to fill it on.
  const [editOn, setEditOn] = useState<Record<number, string>>({});
  // The image shown on its own details page.
  const [open, setOpen] = useState<number | null>(null);
  // The image in the Edit dialog, and what the last edit asks to do in Windows.
  const [editing, setEditing] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // The OS image in the Prepare image dialog.
  const [preparing, setPreparing] = useState<number | null>(null);
  // The OS image in the Add new hardware wizard.
  const [addingHardware, setAddingHardware] = useState<number | null>(null);
  const [importing, setImporting] = useState(false);

  const assigned = (id: number) =>
    machines.filter((m) => m.game_disk_id === id || m.os_disk_id === id).length;
  const machine = (id: number | null) => machines.find((m) => m.id === id);
  // A draft can be filled only on a disk-mode PC that has no game disk.
  const free = machines.filter(
    (m) => m.mode === "disk" && m.status === "idle" && m.game_disk_id === null,
  );

  async function create(e: FormEvent) {
    e.preventDefault();
    const ok = await run("create", () => api.createDisk(name.trim(), Number(sizeGb), kind));
    if (ok) setName("");
    await reload();
  }

  async function publish(disk: GameDisk) {
    const msg =
      `Publish "${disk.name}"?\n\n` +
      "This takes the @base snapshot and makes the master read-only. " +
      "Fill it with data first; after publishing it cannot be changed.";
    if (!window.confirm(msg)) return;
    await run(`publish-${disk.id}`, () => api.publishDisk(disk.id));
    await reload();
  }

  async function startEdit(disk: GameDisk) {
    const m = machine(Number(editOn[disk.id]));
    if (!m) return;
    const msg =
      `Edit "${disk.name}" on ${m.name}?\n\n` +
      "The master becomes that PC's game disk (through ggnet-agent). Initialize and format it " +
      "in Disk Management, install the games, then shut the PC down and click Finish editing.";
    if (!window.confirm(msg)) return;
    await run(`edit-${disk.id}`, () => api.startEdit(disk.id, m.id));
    await reload();
  }

  async function finishEdit(disk: GameDisk) {
    const name = machine(disk.editor_id)?.name ?? "the PC";
    if (!window.confirm(`Finish editing "${disk.name}"?\n\nThe master is removed from ${name}.`)) return;
    await run(`finish-${disk.id}`, () => api.finishEdit(disk.id));
    await reload();
  }

  async function prepare(disk: GameDisk, options: PrepareOptions) {
    const ok = await run(`prepare-${disk.id}`, () => api.prepareImage(disk.id, options));
    if (ok) setPreparing(null);
    await reload();
  }

  async function cancelImport(disk: GameDisk) {
    if (!window.confirm(`Stop importing "${disk.name}"? What was imported so far is removed.`)) return;
    await run(`cancel-${disk.id}`, () => api.cancelImport(disk.id));
    await reload();
  }

  async function copy(disk: GameDisk) {
    const what = disk.kind === "os" ? "OS image" : "game disk";
    const next = window.prompt(
      `Copy version @${disk.snapshot} of ${what} "${disk.name}" into a new ${what}.\n\n` +
        "The copy is independent: it has its own data and either can be deleted later. It takes " +
        "a few minutes per 100 GB and runs in the background. Name of the copy:",
      `${disk.name}-copy`.slice(0, 32),
    );
    const name = next?.trim().toLowerCase();
    if (!name) return;
    await run(`copy-${disk.id}`, () => api.copyDisk(disk.id, name));
    await reload();
  }

  async function remove(disk: GameDisk) {
    const what = disk.kind === "os" ? "OS image" : "game disk";
    if (!window.confirm(`Delete ${what} "${disk.name}" and its zvol on the host?`)) return;
    await run(`delete-${disk.id}`, () => api.deleteDisk(disk.id));
    await reload();
  }

  function menuFor(d: GameDisk): MenuAction[] {
    const users = assigned(d.id);
    return [
      { label: "Details", icon: Info, onSelect: () => setOpen(d.id) },
      {
        label: "Edit",
        icon: Pencil,
        onSelect: () => setEditing(d.id),
        disabled: busy !== null || copying(d) || d.editor_id !== null,
        title: "Name and size",
      },
      ...(d.published
        ? [
            {
              label: "Copy",
              icon: Copy,
              onSelect: () => copy(d),
              disabled: busy !== null,
              title: `New independent image from @${d.snapshot}`,
            },
          ]
        : []),
      ...(d.published && d.kind === "os"
        ? [
            {
              label: "Prepare image",
              icon: Wand2,
              onSelect: () => setPreparing(d.id),
              disabled: busy !== null,
              title: "New version with the server's agent and the settings every PC needs",
            },
            {
              label: "Add new hardware",
              icon: Cpu,
              onSelect: () => setAddingHardware(d.id),
              disabled: busy !== null,
              title: "Add a new board type's network card to the image (once per board type)",
            },
          ]
        : []),
      ...(copying(d) && isImport(d.copy_source)
        ? [
            {
              label: "Cancel import",
              icon: XCircle,
              onSelect: () => cancelImport(d),
              disabled: busy !== null,
              title: "Stop the import; the disk can then be removed",
            },
          ]
        : []),
      {
        label: "Remove",
        icon: Trash2,
        onSelect: () => remove(d),
        danger: true,
        disabled: busy !== null || users > 0 || d.editor_id !== null || copying(d),
        title: copying(d)
          ? "Wait until the copy is done"
          : users > 0
            ? `Used by ${users} PC(s); move them to another image first`
            : d.editor_id !== null
              ? "Finish editing first"
              : undefined,
      },
    ];
  }

  const editedDisk = disks.find((d) => d.id === editing);
  const editDialog = editedDisk && (
    <EditImage
      disk={editedDisk}
      onClose={() => setEditing(null)}
      onSaved={async (note) => {
        setEditing(null);
        setNotice(note);
        await reload();
      }}
    />
  );
  const preparedDisk = disks.find((d) => d.id === preparing);
  const prepareDialog = preparedDisk && (
    <PrepareImage
      disk={preparedDisk}
      busy={busy === `prepare-${preparedDisk.id}`}
      onClose={() => setPreparing(null)}
      onPrepare={(options) => prepare(preparedDisk, options)}
    />
  );
  const hardwareDisk = disks.find((d) => d.id === addingHardware);
  const hardwareDialog = hardwareDisk && (
    <AddHardwareWizard disk={hardwareDisk} onClose={() => setAddingHardware(null)} onChanged={reload} />
  );
  const noticeBanner = notice && (
    <div className="banner banner-info" role="status">
      <span>{notice}</span>
      <button type="button" className="icon-btn" onClick={() => setNotice(null)} aria-label="Dismiss">
        ×
      </button>
    </div>
  );

  const shown = disks.find((d) => d.id === open);
  if (shown) {
    return (
      <>
        <ErrorBanner message={error} onClose={() => setError(null)} />
        {noticeBanner}
        {editDialog}
        {prepareDialog}
        {hardwareDialog}
        <ImageDetailsPage
          disk={shown}
          machines={machines}
          reload={reload}
          onBack={() => setOpen(null)}
          onAddHardware={() => setAddingHardware(shown.id)}
          menu={<ActionMenu label={`Actions for ${shown.name}`} actions={menuFor(shown).filter((a) => a.label !== "Details")} />}
        />
      </>
    );
  }

  function section(title: string, Icon: LucideIcon, list: GameDisk[], empty: string) {
    return (
      <>
        <h2 className="section-title title-with-icon">
          <Icon size={16} aria-hidden />
          {title}
          <span className="tab-badge" aria-label={`${list.length} in all`}>
            {list.length}
          </span>
        </h2>
        {list.length === 0 ? (
          <p className="empty">{empty}</p>
        ) : (
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Size</th>
                  <th>State</th>
                  <th>Machines</th>
                  <th>ZFS path</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {list.map((d) => (
                  <Fragment key={d.id}>
                    <tr>
                      <td className="strong">
                        <button
                          type="button"
                          className="link-btn name-with-icon"
                          onClick={() => setOpen(d.id)}
                          title="Details: snapshots and writebacks"
                        >
                          {d.kind === "os" ? (
                            <Monitor size={15} className="kind-icon kind-os" aria-hidden />
                          ) : (
                            <Gamepad2 size={15} className="kind-icon kind-game" aria-hidden />
                          )}
                          {d.name}
                        </button>
                      </td>
                      <td>{d.size_gb} GB</td>
                      <td>
                        {copying(d) ? (
                          <span className="badge badge-editing" title={`From ${d.copy_source}`}>
                            {isImport(d.copy_source) ? "Importing" : "Copying"} {d.copy_percent ?? 0}%
                          </span>
                        ) : d.copy_error ? (
                          <span className="badge badge-error" title={d.copy_error}>
                            {isImport(d.copy_source) ? "Import failed" : "Copy failed"}
                          </span>
                        ) : needsHardware(d) ? (
                          <span className="badge badge-warn" title="No PC can boot it yet: run Add new hardware">
                            Needs hardware
                          </span>
                        ) : d.published ? (
                          <span className="badge badge-provisioned">Published @{d.snapshot}</span>
                        ) : d.editor_id !== null ? (
                          <span className="badge badge-editing">
                            Editing on {machine(d.editor_id)?.name ?? `#${d.editor_id}`}
                          </span>
                        ) : (
                          <span className="badge badge-idle">Draft</span>
                        )}
                      </td>
                      <td>{assigned(d.id)}</td>
                      <td className="mono muted">{d.zvol_path}</td>
                      <td className="actions">
                        {!d.published && d.editor_id === null && !d.copy_source && (
                          <>
                            <select
                              aria-label={`PC to edit ${d.name} on`}
                              value={editOn[d.id] ?? ""}
                              onChange={(e) => setEditOn({ ...editOn, [d.id]: e.target.value })}
                              disabled={busy !== null}
                            >
                              <option value="">PC…</option>
                              {free.map((m) => (
                                <option key={m.id} value={m.id}>
                                  {m.name}
                                </option>
                              ))}
                            </select>
                            <button
                              type="button"
                              onClick={() => startEdit(d)}
                              disabled={busy !== null || !editOn[d.id]}
                            >
                              <Pencil size={14} />
                              {busy === `edit-${d.id}` ? "Starting…" : "Edit on PC"}
                            </button>
                            <button type="button" onClick={() => publish(d)} disabled={busy !== null}>
                              <CheckCircle2 size={14} />
                              {busy === `publish-${d.id}` ? "Publishing…" : "Publish"}
                            </button>
                          </>
                        )}
                        {d.editor_id !== null && (
                          <button
                            type="button"
                            onClick={() => finishEdit(d)}
                            disabled={busy !== null || machine(d.editor_id)?.session_active === true}
                            title={
                              machine(d.editor_id)?.session_active
                                ? `Shut down ${machine(d.editor_id)?.name} first`
                                : undefined
                            }
                          >
                            <CheckCircle2 size={14} />
                            {busy === `finish-${d.id}` ? "Finishing…" : "Finish editing"}
                          </button>
                        )}
                        <ActionMenu label={`Actions for ${d.name}`} actions={menuFor(d)} />
                      </td>
                    </tr>
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </>
    );
  }

  return (
    <section>
      <ErrorBanner message={error} onClose={() => setError(null)} />
      {noticeBanner}
      {editDialog}
      {prepareDialog}
      {hardwareDialog}

      <ImageKpis disks={disks} machines={machines} />

      <form className="card form-row" onSubmit={create} aria-label="New image">
        <label>
          Name
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. steam-main"
            required
            maxLength={32}
          />
        </label>
        <label>
          Kind
          <select value={kind} onChange={(e) => setKind(e.target.value as DiskKind)}>
            <option value="game">Game disk</option>
            <option value="os">OS image (Boot Mode)</option>
          </select>
        </label>
        <label>
          Size (GB)
          <input
            type="number"
            min={1}
            max={16384}
            value={sizeGb}
            onChange={(e) => setSizeGb(e.target.value)}
            required
          />
        </label>
        <button type="submit" disabled={busy !== null}>
          <Plus size={14} />
          {busy === "create" ? "Creating…" : "Create"}
        </button>
        <button
          type="button"
          className="secondary"
          onClick={() => setImporting(true)}
          disabled={busy !== null}
          title="A new image from a VHD, VHDX, QCOW2, VMDK or RAW file"
        >
          <HardDriveDownload size={14} />
          Import
        </button>
      </form>
      {importing && (
        <ImportImage
          onClose={() => setImporting(false)}
          onStarted={async () => {
            setImporting(false);
            await reload();
          }}
        />
      )}

      {section("OS images", Monitor, disks.filter((d) => d.kind === "os"), "No OS images yet.")}
      {section("Game disks", Gamepad2, disks.filter((d) => d.kind !== "os"), "No game disks yet.")}
    </section>
  );
}

function ImageKpis({ disks, machines }: { disks: GameDisk[]; machines: Machine[] }) {
  const os = disks.filter((d) => d.kind === "os");
  const games = disks.filter((d) => d.kind !== "os");
  const drafts = disks.filter((d) => !d.published).length;
  const using = machines.filter((m) => m.game_disk_id !== null || m.os_disk_id !== null).length;
  const totalGb = disks.reduce((sum, d) => sum + d.size_gb, 0);
  const cards = [
    { title: "Images", value: disks.length, sub: `${totalGb} GB in all${drafts ? ` · ${drafts} draft` : ""}`, icon: Layers, tone: "blue" },
    { title: "OS images", value: os.length, sub: "Windows for Boot Mode PCs", icon: Monitor, tone: "purple" },
    { title: "Game disks", value: games.length, sub: "shared D: for every PC", icon: Gamepad2, tone: "green" },
    { title: "In use", value: using, sub: `of ${machines.length} PCs have an image`, icon: HardDrive, tone: "amber" },
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
