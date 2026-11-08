import { useCallback, useEffect, useState } from "react";
import { Archive, ArchiveRestore, History, Usb, X } from "lucide-react";

import {
  api,
  type BackupJob,
  type BackupLocation,
  type BackupLocationOpen,
  type FoundBackup,
  type Image,
} from "../api";
import { formatBytes, formatDuration } from "../format";
import { ErrorBanner } from "./ErrorBanner";

type Tab = "backup" | "restore" | "history";
const POLL_MS = 3000;
const NAME = /^[a-z0-9-]{1,32}$/;

interface Props {
  images: Image[];
  onClose: () => void;
  /** After a restore was started: the new image shows in Images ("Restoring"). */
  onRestored: () => Promise<void>;
}

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/**
 * Images → Backup / Restore: images with all their versions to a USB disk or
 * a backup folder of the server, a backup back as a new image, and the
 * history of both.
 */
export function BackupRestore({ images, onClose, onRestored }: Props) {
  const [tab, setTab] = useState<Tab>("backup");
  const [locations, setLocations] = useState<BackupLocation[] | null>(null);
  const [location, setLocation] = useState("");
  const [opened, setOpened] = useState<BackupLocationOpen | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [jobs, setJobs] = useState<BackupJob[]>([]);

  const loadJobs = useCallback(async () => {
    try {
      setJobs(await api.backupJobs());
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  useEffect(() => {
    api
      .backupLocations()
      .then((list) => {
        setLocations(list);
        // Only one place to choose: take it; otherwise the admin picks.
        setLocation((cur) => cur || (list.length === 1 ? list[0].id : ""));
      })
      .catch((e) => setError(errorText(e)));
    loadJobs();
  }, [loadJobs]);

  // While a job runs, follow it.
  const active = jobs.some((j) => j.status === "queued" || j.status === "running");
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(loadJobs, POLL_MS);
    return () => window.clearInterval(timer);
  }, [active, loadJobs]);

  // Open the chosen location read-only: free space and its backups. A backup
  // mounts it writable only when it starts.
  useEffect(() => {
    setOpened(null);
    if (!location || tab === "history") return;
    let stopped = false;
    api
      .openBackupLocation(location, false)
      .then((o) => !stopped && setOpened(o))
      .catch((e) => !stopped && setError(errorText(e)));
    return () => {
      stopped = true;
    };
  }, [location, tab]);

  async function act(call: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await call();
    } catch (e) {
      setError(errorText(e));
    }
    setBusy(false);
  }

  const chosen = locations?.find((l) => l.id === location);
  const tabs: { id: Tab; label: string; icon: typeof Archive }[] = [
    { id: "backup", label: "Backup", icon: Archive },
    { id: "restore", label: "Restore", icon: ArchiveRestore },
    { id: "history", label: "History", icon: History },
  ];

  return (
    <div className="modal-backdrop">
      <div className="modal-content modal-wide" role="dialog" aria-label="Backup / Restore">
        <div className="modal-header">
          <div className="title-with-icon">
            <ArchiveRestore size={20} aria-hidden />
            <h2>Backup / Restore</h2>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <div className="modal-body">
          <ErrorBanner message={error} onClose={() => setError(null)} />
          <div className="subnav" role="tablist" aria-label="Backup or restore">
            {tabs.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                className={`subnav-btn ${tab === id ? "active" : ""}`}
                onClick={() => setTab(id)}
              >
                <Icon size={14} />
                {label}
              </button>
            ))}
          </div>

          {tab !== "history" && (
            <div className="backup-location">
              <label>
                Location
                <select value={location} onChange={(e) => setLocation(e.target.value)} disabled={!locations?.length}>
                  {locations?.length === 0 ? (
                    <option value="">No USB disk or backup folder found</option>
                  ) : (
                    locations &&
                    locations.length > 1 && <option value="">Choose a disk…</option>
                  )}
                  {locations?.map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.label}
                      {l.size ? ` · ${formatBytes(l.size)}` : ""}
                    </option>
                  ))}
                </select>
              </label>
              {opened && (
                <span className="small muted">
                  Free space: <span className="strong">{formatBytes(opened.free_bytes)}</span>
                </span>
              )}
              {chosen?.kind === "usb" && (
                <button
                  type="button"
                  className="secondary small"
                  disabled={busy || active}
                  title={active ? "Wait until the backup or restore is done" : "Unmount it so it can be unplugged"}
                  onClick={() =>
                    act(async () => {
                      await api.ejectBackupLocation(location);
                      setOpened(null);
                      setError(null);
                      window.alert("The USB disk can be unplugged now.");
                    })
                  }
                >
                  <Usb size={13} />
                  Eject
                </button>
              )}
            </div>
          )}
          {locations?.length === 0 && tab !== "history" && (
            <p className="small warn-text">
              Plug a USB disk (FAT32, exFAT, NTFS or ext4) into the server, or set backup folders in config.toml
              ([paths] backup_dirs), then open this again.
            </p>
          )}

          {tab === "backup" && (
            <BackupTab
              images={images}
              opened={opened}
              busy={busy || !opened}
              start={(ids) =>
                act(async () => {
                  await api.startBackup(ids, location);
                  await loadJobs();
                  setTab("history");
                })
              }
            />
          )}
          {tab === "restore" && (
            <RestoreTab
              images={images}
              opened={opened}
              busy={busy || !opened}
              start={(folder, name) =>
                act(async () => {
                  await api.startRestore(location, folder, name);
                  await Promise.all([loadJobs(), onRestored()]);
                  setTab("history");
                })
              }
            />
          )}
          {tab === "history" && (
            <HistoryTab
              jobs={jobs}
              busy={busy}
              cancel={(id) => act(async () => (await api.cancelBackupJob(id), await loadJobs()))}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function BackupTab({
  images,
  opened,
  busy,
  start,
}: {
  images: Image[];
  opened: BackupLocationOpen | null;
  busy: boolean;
  start: (ids: number[]) => void;
}) {
  const published = images.filter((i) => i.published);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [sizes, setSizes] = useState<Record<number, number | null>>({});

  async function toggle(id: number) {
    const next = new Set(picked);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setPicked(next);
    if (next.has(id) && !(id in sizes)) {
      try {
        const r = await api.backupEstimate(id);
        setSizes((s) => ({ ...s, [id]: r.bytes }));
      } catch {
        setSizes((s) => ({ ...s, [id]: null }));
      }
    }
  }

  const ids = published.filter((i) => picked.has(i.id)).map((i) => i.id);
  const total = ids.reduce((sum, id) => sum + (sizes[id] ?? 0), 0);
  const unknown = ids.some((id) => sizes[id] === undefined);
  const tooBig = opened !== null && !unknown && total > opened.free_bytes;
  return (
    <>
      <p className="small muted">
        Each image is backed up with all its versions, comments and network cards, into ggnet-backups on the chosen
        disk. PCs keep running meanwhile.
      </p>
      <table>
        <thead>
          <tr>
            <th />
            <th>Image</th>
            <th>Type</th>
            <th>Backup size</th>
          </tr>
        </thead>
        <tbody>
          {published.map((i) => (
            <tr key={i.id}>
              <td>
                <input
                  type="checkbox"
                  aria-label={`Back up ${i.name}`}
                  checked={picked.has(i.id)}
                  onChange={() => toggle(i.id)}
                />
              </td>
              <td className="strong">{i.name}</td>
              <td>{i.kind === "os" ? "OS image" : "Game image"}</td>
              <td>
                {!picked.has(i.id)
                  ? ""
                  : sizes[i.id] === undefined
                    ? "…"
                    : sizes[i.id] === null
                      ? "unknown"
                      : formatBytes(sizes[i.id]!)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="actions-row backup-footer">
        <span className="small">
          Total size: <span className="strong">{unknown ? "…" : formatBytes(total)}</span>
        </span>
        {tooBig && <span className="small error-text">Not enough free space on this disk</span>}
        <button type="button" disabled={busy || ids.length === 0 || tooBig} onClick={() => start(ids)}>
          <Archive size={14} />
          Back up {ids.length > 1 ? `${ids.length} images` : ""}
        </button>
      </div>
    </>
  );
}

function RestoreTab({
  images,
  opened,
  busy,
  start,
}: {
  images: Image[];
  opened: BackupLocationOpen | null;
  busy: boolean;
  start: (folder: string, name: string) => void;
}) {
  const [picked, setPicked] = useState<FoundBackup | null>(null);
  const [name, setName] = useState("");
  const taken = new Set(images.map((i) => i.name));

  function pick(b: FoundBackup) {
    setPicked(b);
    let n = b.image;
    for (let i = 2; taken.has(n); i++) n = `${b.image}-${i}`.slice(0, 32);
    setName(n);
  }

  if (!opened) return <p className="muted small">Loading…</p>;
  if (opened.backups.length === 0) return <p className="small muted">No ggNet backups on this disk.</p>;
  const nameOk = NAME.test(name) && !taken.has(name);
  return (
    <>
      <table>
        <thead>
          <tr>
            <th />
            <th>Image</th>
            <th>Made</th>
            <th>Versions</th>
            <th>Size</th>
          </tr>
        </thead>
        <tbody>
          {opened.backups.map((b) => (
            <tr key={b.folder} className={b.complete ? undefined : "muted"}>
              <td>
                <input
                  type="radio"
                  name="backup"
                  aria-label={`Restore ${b.image} of ${b.created_at}`}
                  checked={picked?.folder === b.folder}
                  disabled={!b.complete}
                  onChange={() => pick(b)}
                />
              </td>
              <td className="strong">
                {b.image} <span className="muted small">{b.kind === "os" ? "OS image" : "Game image"}</span>
                {b.problem && <div className="small error-text">{b.problem}</div>}
              </td>
              <td>{b.created_at ? new Date(b.created_at).toLocaleString() : "—"}</td>
              <td title={b.versions.map((v) => `@${v}`).join(", ")}>
                {b.versions.length} {b.active && <span className="muted small">(active @{b.active})</span>}
              </td>
              <td>{formatBytes(b.bytes)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {picked && (
        <div className="actions-row backup-footer">
          <label>
            New image name
            <input value={name} onChange={(e) => setName(e.target.value.toLowerCase())} aria-invalid={!nameOk} />
          </label>
          {taken.has(name) && <span className="small error-text">An image with this name exists</span>}
          <button type="button" disabled={busy || !nameOk} onClick={() => start(picked.folder, name)}>
            <ArchiveRestore size={14} />
            Restore
          </button>
        </div>
      )}
      <p className="small muted">
        A restore always makes a new image; no image is overwritten. Each part of the backup is checked as it is read.
      </p>
    </>
  );
}

const STATUS: Record<BackupJob["status"], [string, string]> = {
  queued: ["Queued", "badge-idle"],
  running: ["Running", "badge-editing"],
  done: ["Completed", "badge-provisioned"],
  failed: ["Failed", "badge-error"],
  cancelled: ["Cancelled", "badge-warn"],
};

function HistoryTab({ jobs, busy, cancel }: { jobs: BackupJob[]; busy: boolean; cancel: (id: number) => void }) {
  // A running job stops once the disk took what it was writing: show that it was asked to.
  const [cancelling, setCancelling] = useState<Set<number>>(new Set());
  if (jobs.length === 0) return <p className="small muted">No backup or restore yet.</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Action</th>
          <th>Image</th>
          <th>Location</th>
          <th>Status</th>
          <th>Size</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {jobs.map((j) => {
          const [label, badge] = STATUS[j.status];
          const percent = j.total_bytes ? Math.min(100, Math.floor((100 * j.done_bytes) / j.total_bytes)) : null;
          return (
            <tr key={j.id}>
              <td className="small">{new Date(j.created_at).toLocaleString()}</td>
              <td>{j.kind === "backup" ? "Backup" : j.kind === "export" ? `Export (${(j.format ?? "").toUpperCase()})` : "Restore"}</td>
              <td className="strong">
                {j.image_name}
                {j.kind === "export" && j.snapshot ? `@${j.snapshot}` : ""}
              </td>
              <td className="small">{j.location_label}</td>
              <td>
                <span className={`badge ${badge}`} title={j.error ?? undefined}>
                  {label}
                  {j.status === "running" && percent !== null ? ` ${percent}%` : ""}
                </span>
                {j.status === "running" && <Speed job={j} />}
                {j.error && j.status === "failed" && <div className="small error-text">{j.error}</div>}
              </td>
              <td className="small">{j.total_bytes ? formatBytes(j.total_bytes) : "—"}</td>
              <td>
                {(j.status === "queued" || j.status === "running") && (
                  <button
                    type="button"
                    className="secondary small"
                    disabled={busy || cancelling.has(j.id)}
                    onClick={() => {
                      setCancelling(new Set(cancelling).add(j.id));
                      cancel(j.id);
                    }}
                  >
                    {cancelling.has(j.id) ? "Cancelling…" : "Cancel"}
                  </button>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** "6.9 MB/s · 21 min left" from the bytes done since the job started. */
function Speed({ job }: { job: BackupJob }) {
  if (!job.started_at || !job.total_bytes || job.done_bytes <= 0) return null;
  const seconds = (Date.now() - new Date(job.started_at).getTime()) / 1000;
  if (seconds < 5) return null;
  const rate = job.done_bytes / seconds;
  const left = Math.max(0, (job.total_bytes - job.done_bytes) / rate);
  return (
    <div className="small muted">
      {formatBytes(rate)}/s · {formatDuration(left * 1000)} left
    </div>
  );
}

