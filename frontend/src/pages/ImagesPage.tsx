import { Fragment, useEffect, useState } from "react";
import {
  ArchiveRestore,
  CheckCircle2,
  Copy,
  Cpu,
  FileOutput,
  Gamepad2,
  HardDriveDownload,
  HardDrive,
  Info,
  Layers,
  Monitor,
  Pencil,
  Plus,
  ScanSearch,
  Star,
  StarOff,
  Trash2,
  Wand2,
  XCircle,
  type LucideIcon,
} from "lucide-react";

import {
  api,
  isImport,
  isRestore,
  needsHardware,
  type Image,
  type ImageUsage,
  type Machine,
  type PrepareOptions,
} from "../api";
import { formatBytes } from "../format";
import { ActionMenu, type MenuAction } from "../components/ActionMenu";
import { BackupRestore } from "../components/BackupRestore";
import { CopyImage } from "../components/CopyImage";
import { ExportImage } from "../components/ExportImage";
import { CreateImage } from "../components/CreateImage";
import { EditImage } from "../components/EditImage";
import { ImportImage } from "../components/ImportImage";
import { AddHardwareWizard } from "../components/AddHardwareWizard";
import { PrepareImage } from "../components/PrepareImage";
import { UpdateBadge, useImageUpdates } from "../components/UpdateBadge";
import { ErrorBanner } from "../components/ErrorBanner";
import { useAsyncAction } from "../useAsyncAction";
import { ImageDetailsPage } from "./ImageDetailsPage";

/** A copy that is still running (a failed one keeps its error instead). */
const copying = (d: Image) => !!d.copy_source && !d.copy_error;

interface Props {
  disks: Image[];
  machines: Machine[];
  reload: () => Promise<void>;
  /** Admins manage the images; staff only look at them. */
  canManage?: boolean;
}

export function ImagesPage({ disks, machines, reload, canManage = true }: Props) {
  const { busy, error, setError, run } = useAsyncAction();
  const updates = useImageUpdates(disks);
  const usage = useImageUsage(disks);
  const [creating, setCreating] = useState(false);
  // The image in the Copy dialog, and in the Export dialog.
  const [copyFrom, setCopyFrom] = useState<number | null>(null);
  const [exportFrom, setExportFrom] = useState<number | null>(null);
  const [search, setSearch] = useState("");
  const needle = search.trim().toLowerCase();
  const matching = needle ? disks.filter((d) => d.name.includes(needle)) : disks;
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
  const [backingUp, setBackingUp] = useState(false);

  const assigned = (id: number) =>
    machines.filter((m) => m.game_image_id === id || m.os_image_id === id).length;
  const machine = (id: number | null) => machines.find((m) => m.id === id);
  // A draft can be filled only on a disk-mode PC that has no game image.
  const free = machines.filter(
    (m) => m.mode === "disk" && m.status === "idle" && m.game_image_id === null,
  );

  async function publish(disk: Image) {
    const msg =
      `Publish "${disk.name}"?\n\n` +
      "This takes the @base snapshot and makes the master read-only. " +
      "Fill it with data first; after publishing it cannot be changed.";
    if (!window.confirm(msg)) return;
    await run(`publish-${disk.id}`, () => api.publishDisk(disk.id));
    await reload();
  }

  async function startEdit(disk: Image) {
    const m = machine(Number(editOn[disk.id]));
    if (!m) return;
    const msg =
      `Edit "${disk.name}" on ${m.name}?\n\n` +
      "The master becomes that PC's game image (through ggnet-agent). Initialize and format it " +
      "in Disk Management, install the games, then shut the PC down and click Finish editing.";
    if (!window.confirm(msg)) return;
    await run(`edit-${disk.id}`, () => api.startEdit(disk.id, m.id));
    await reload();
  }

  async function finishEdit(disk: Image) {
    const name = machine(disk.editor_id)?.name ?? "the PC";
    if (!window.confirm(`Finish editing "${disk.name}"?\n\nThe master is removed from ${name}.`)) return;
    await run(`finish-${disk.id}`, () => api.finishEdit(disk.id));
    await reload();
  }

  async function prepare(disk: Image, options: PrepareOptions) {
    const ok = await run(`prepare-${disk.id}`, () => api.prepareImage(disk.id, options));
    if (ok) setPreparing(null);
    await reload();
  }

  async function checkHardware(disk: Image) {
    await run(`check-${disk.id}`, async () => {
      const checked = await api.checkHardware(disk.id);
      const models = checked.nic_models ?? [];
      setNotice(
        models.length
          ? `${disk.name}@${checked.snapshot} has these network cards: ${models.join(", ")}.`
          : `${disk.name}@${checked.snapshot} has no network card installed yet: run Add new hardware.`,
      );
    });
    await reload();
  }

  async function cancelImport(disk: Image) {
    if (!window.confirm(`Stop importing "${disk.name}"? What was imported so far is removed.`)) return;
    await run(`cancel-${disk.id}`, () => api.cancelImport(disk.id));
    await reload();
  }

  function copy(disk: Image) {
    setCopyFrom(disk.id);
  }

  async function remove(disk: Image) {
    const what = disk.kind === "os" ? "OS image" : "game image";
    if (!window.confirm(`Delete ${what} "${disk.name}" and its zvol on the host?`)) return;
    await run(`delete-${disk.id}`, () => api.deleteDisk(disk.id));
    await reload();
  }

  async function makeDefault(d: Image, isDefault: boolean) {
    await run(`default-${d.id}`, () => api.setDefaultDisk(d.id, isDefault));
    await reload();
  }

  function menuFor(d: Image): MenuAction[] {
    if (!canManage) return [{ label: "Details", icon: Info, onSelect: () => setOpen(d.id) }];
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
            {
              label: "Export",
              icon: FileOutput,
              onSelect: () => setExportFrom(d.id),
              disabled: busy !== null,
              title: "One version into a VHDX or VHD file on a USB disk or the imports folder",
            },
          ]
        : []),
      ...(d.published
        ? [
            {
              label: d.is_default ? "Remove default" : "Make default",
              icon: d.is_default ? StarOff : Star,
              onSelect: () => makeDefault(d, !d.is_default),
              disabled: busy !== null,
              title: d.is_default
                ? `New PCs no longer get ${d.name}`
                : `New PCs get this ${d.kind === "os" ? "OS image" : "game image"}`,
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
            {
              label: "Check network cards",
              icon: ScanSearch,
              onSelect: () => checkHardware(d),
              disabled: busy !== null,
              title: "Read the network cards installed in the active version from the image itself",
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
  const exportDisk = disks.find((d) => d.id === exportFrom);
  const exportDialog = exportDisk && (
    <ExportImage image={exportDisk} compressRatio={usage[exportDisk.id]?.compress_ratio}
                 onClose={() => setExportFrom(null)} />
  );
  const copyDisk = disks.find((d) => d.id === copyFrom);
  const copyDialog = copyDisk && (
    <CopyImage image={copyDisk} onClose={() => setCopyFrom(null)} onStarted={reload} />
  );
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

  // The image whose details are open, in a dialog over the list.
  const shown = disks.find((d) => d.id === open);
  const detailsDialog = shown && (
    <ImageDetailsPage
      disk={shown}
      machines={machines}
      reload={reload}
      onClose={() => setOpen(null)}
      banner={
        <>
          <ErrorBanner message={error} onClose={() => setError(null)} />
          {noticeBanner}
        </>
      }
      onAddHardware={canManage ? () => setAddingHardware(shown.id) : undefined}
      canManage={canManage}
      menu={
        canManage ? (
          <ActionMenu label={`Actions for ${shown.name}`} actions={menuFor(shown).filter((a) => a.label !== "Details")} />
        ) : undefined
      }
    />
  );

  function section(title: string, Icon: LucideIcon, list: Image[], empty: string) {
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
                  <th>Type</th>
                  <th>Default</th>
                  <th>State</th>
                  <th title="Space on the pool, with all versions">Size</th>
                  <th title="The disk's size as the PCs see it">Volume</th>
                  <th>Compression</th>
                  <th>Versions</th>
                  <th>Machines</th>
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
                      <td>{d.kind === "os" ? "OS image" : "Game image"}</td>
                      <td>
                        {d.is_default && (
                          <span className="default-mark" title="New PCs get this one">
                            <Star size={14} aria-label="Default" />
                          </span>
                        )}
                      </td>
                      <td>
                        {copying(d) ? (
                          <span className="badge badge-editing" title={`From ${d.copy_source}`}>
                            {isRestore(d.copy_source) ? "Restoring" : isImport(d.copy_source) ? "Importing" : "Copying"}{" "}
                            {d.copy_percent ?? 0}%
                          </span>
                        ) : d.copy_error ? (
                          <span className="badge badge-error" title={d.copy_error}>
                            {isRestore(d.copy_source) ? "Restore failed" : isImport(d.copy_source) ? "Import failed" : "Copy failed"}
                          </span>
                        ) : needsHardware(d) ? (
                          <span className="badge badge-warn" title="No PC can boot it yet: run Add new hardware">
                            Needs hardware
                          </span>
                        ) : d.published ? (
                          <>
                            <span className="badge badge-provisioned">Published @{d.snapshot}</span>
                            {updates[d.id]?.state === "pending" && updates[d.id].snapshot === d.snapshot && (
                              <> <UpdateBadge signs={updates[d.id].signs} /></>
                            )}
                          </>
                        ) : d.editor_id !== null ? (
                          <span className="badge badge-editing">
                            Editing on {machine(d.editor_id)?.name ?? `#${d.editor_id}`}
                          </span>
                        ) : (
                          <span className="badge badge-idle">Draft</span>
                        )}
                      </td>
                      <td>{usage[d.id] ? formatBytes(usage[d.id].used_bytes) : "—"}</td>
                      <td>{usage[d.id] ? formatBytes(usage[d.id].volume_bytes) : `${d.size_gb} GB`}</td>
                      <td>{usage[d.id]?.compress_ratio ? `${usage[d.id].compress_ratio!.toFixed(2)}×` : "—"}</td>
                      <td>{usage[d.id] ? usage[d.id].versions : "—"}</td>
                      <td>{assigned(d.id)}</td>
                      <td className="actions">
                        {canManage && !d.published && d.editor_id === null && !d.copy_source && (
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
                        {canManage && d.editor_id !== null && (
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
      {!shown && (
        <>
          <ErrorBanner message={error} onClose={() => setError(null)} />
          {noticeBanner}
        </>
      )}
      {/* Before the other dialogs: the ones its menu opens stack on top of it. */}
      {detailsDialog}
      {editDialog}
      {prepareDialog}
      {hardwareDialog}

      <ImageKpis disks={disks} machines={machines} />

      <div className="card form-row images-toolbar">
        {canManage && (
          <>
        <button type="button" onClick={() => setCreating(true)} disabled={busy !== null}>
          <Plus size={14} />
          Create image
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
        <button
          type="button"
          className="secondary"
          onClick={() => setBackingUp(true)}
          title="Back up images with all their versions to a USB disk, or restore one"
        >
          <ArchiveRestore size={14} />
          Backup / Restore
        </button>
          </>
        )}
        <input
          type="search"
          className="images-search"
          placeholder="Search the images…"
          aria-label="Search the images"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      {creating && <CreateImage onClose={() => setCreating(false)} onCreated={reload} />}
      {copyDialog}
      {exportDialog}
      {backingUp && <BackupRestore images={disks} onClose={() => setBackingUp(false)} onRestored={reload} />}
      {importing && (
        <ImportImage
          onClose={() => setImporting(false)}
          onStarted={async () => {
            setImporting(false);
            await reload();
          }}
        />
      )}

      {section("OS Images", Monitor, matching.filter((d) => d.kind === "os"), search ? "No OS image matches." : "No OS images yet.")}
      {section("Game Images", Gamepad2, matching.filter((d) => d.kind !== "os"), search ? "No game image matches." : "No game images yet.")}
    </section>
  );
}

function ImageKpis({ disks, machines }: { disks: Image[]; machines: Machine[] }) {
  const os = disks.filter((d) => d.kind === "os");
  const games = disks.filter((d) => d.kind !== "os");
  const drafts = disks.filter((d) => !d.published).length;
  const using = machines.filter((m) => m.game_image_id !== null || m.os_image_id !== null).length;
  const totalGb = disks.reduce((sum, d) => sum + d.size_gb, 0);
  const cards = [
    { title: "Images", value: disks.length, sub: `${totalGb} GB in all${drafts ? ` · ${drafts} draft` : ""}`, icon: Layers, tone: "blue" },
    { title: "OS Images", value: os.length, sub: "Windows for Boot Mode PCs", icon: Monitor, tone: "purple" },
    { title: "Game Images", value: games.length, sub: "shared D: for every PC", icon: Gamepad2, tone: "green" },
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

/** Each image's space, compression and versions from ZFS, by disk id; read again when the images change. */
function useImageUsage(disks: Image[]): Record<number, ImageUsage> {
  const [usage, setUsage] = useState<Record<number, ImageUsage>>({});
  const key = disks.map((d) => `${d.id}@${d.snapshot}:${d.copy_percent ?? ""}`).join(",");
  useEffect(() => {
    if (!key) return;
    let stopped = false;
    api
      .getImageUsage()
      .then((list) => {
        if (!stopped) setUsage(Object.fromEntries(list.map((u) => [u.image_id, u])));
      })
      .catch(() => {
        // Only numbers: the list works without them.
      });
    return () => {
      stopped = true;
    };
  }, [key]);
  return usage;
}
