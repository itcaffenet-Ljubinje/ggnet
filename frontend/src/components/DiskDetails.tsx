import { useCallback, useEffect, useState, type ReactNode } from "react";
import { GitBranch, HardDrive, Lock, LockOpen } from "lucide-react";

import { api, applyWritebacksChecked, type ApplyChoice, type Image, type Snapshot, type Writeback } from "../api";
import { formatBytes } from "../format";
import { useAsyncAction } from "../useAsyncAction";
import { ErrorBanner } from "./ErrorBanner";
import { UpdateBadge } from "./UpdateBadge";

interface Props {
  disk: Image;
  /** Reload disks and machines in the parent after an action. */
  onChanged: () => Promise<void>;
  /** Changes on every parent reload, so the lists follow the host. */
  refreshKey?: unknown;
  /** Shown above the lists once they are read (the details page's summary). */
  summary?: (snapshots: Snapshot[], writebacks: Writeback[]) => ReactNode;
  /** Staff only look: the action columns are left out. */
  canManage?: boolean;
}

/** Versions (snapshots) and PC writebacks of one game image, read live from ZFS. */
export function DiskDetails({ disk, onChanged, refreshKey, summary, canManage = true }: Props) {
  const { busy, error, setError, run } = useAsyncAction();
  const [snapshots, setSnapshots] = useState<Snapshot[] | null>(null);
  const [writebacks, setWritebacks] = useState<Writeback[] | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, w] = await Promise.all([api.listSnapshots(disk.id), api.listWritebacks(disk.id)]);
      setSnapshots(s);
      setWritebacks(w);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [disk.id, setError]);

  useEffect(() => {
    load();
  }, [load, disk.snapshot, refreshKey]);

  async function act(key: string, action: () => Promise<unknown>) {
    await run(key, action);
    await Promise.all([load(), onChanged()]);
  }

  async function editComment(s: Snapshot) {
    const text = window.prompt(
      `Comment on @${s.name} of ${disk.name} (who, when, what changed). Empty removes it.`,
      s.comment ?? "",
    );
    if (text === null) return;
    await act(`comment-${s.name}`, () => api.setSnapshotComment(disk.id, s.name, text.trim()));
  }

  function makeActive(s: Snapshot) {
    const msg =
      `Make version ${s.name} of "${disk.name}" active?\n\n` +
      "Every PC moves to it at its next reboot. PCs with Keep Writeback stay where they are.";
    if (window.confirm(msg)) act(`active-${s.name}`, () => api.setActiveSnapshot(disk.id, s.name));
  }

  function toggleLock(s: Snapshot) {
    const msg = s.locked
      ? `Unlock version ${s.name} of "${disk.name}"?\n\nAutomatic cleanup may delete it again once it is old and unused.`
      : `Lock version ${s.name} of "${disk.name}"?\n\nIt is kept until it is unlocked: automatic cleanup skips it and it cannot be deleted.`;
    if (window.confirm(msg)) act(`lock-${s.name}`, () => api.setSnapshotLock(disk.id, s.name, !s.locked));
  }

  function remove(s: Snapshot) {
    if (window.confirm(`Delete version ${s.name} of "${disk.name}"? This cannot be undone.`)) {
      act(`delete-${s.name}`, () => api.deleteSnapshot(disk.id, s.name));
    }
  }

  /** Why the versions older than `index` cannot be deleted, or null when they can. */
  function mergeBlocker(index: number): string | null {
    const older = snapshots!.slice(0, index);
    const active = older.find((o) => o.active);
    if (active) return `@${active.name} is active; make this or a newer version active first`;
    const locked = older.filter((o) => o.locked).map((o) => `@${o.name}`);
    if (locked.length > 0) return `Older versions are locked (${locked.join(", ")}); unlock them first`;
    const pinned = older.flatMap((o) => o.pinned);
    if (pinned.length > 0) return `Older versions are pinned on ${pinned.join(", ")}; unpin them first`;
    const users = [...new Set(older.flatMap((o) => o.machines))];
    if (users.length > 0) {
      return `Older versions are still used by ${users.join(", ")}; they move off at their next reboot`;
    }
    return null;
  }

  /** Why the versions newer than `index` cannot be deleted, or null when they can. */
  function newerBlocker(index: number): string | null {
    const newer = snapshots!.slice(index + 1);
    const locked = newer.filter((o) => o.locked).map((o) => `@${o.name}`);
    if (locked.length > 0) return `Newer versions are locked (${locked.join(", ")}); unlock them first`;
    const pinned = newer.flatMap((o) => o.pinned);
    if (pinned.length > 0) return `Newer versions are pinned on ${pinned.join(", ")}; unpin them first`;
    const users = [...new Set(newer.flatMap((o) => o.machines))];
    if (users.length > 0) {
      return `Newer versions are still used by ${users.join(", ")}; make this one active, and go back once they rebooted onto it`;
    }
    return null;
  }

  function goBack(s: Snapshot, index: number) {
    const newer = snapshots!.slice(index + 1).map((o) => `@${o.name}`);
    const msg =
      `Go back to @${s.name} of "${disk.name}"?\n\n` +
      `@${s.name} becomes active and ${newer.join(", ")} ${newer.length === 1 ? "is" : "are"} deleted. ` +
      "This cannot be undone.";
    if (window.confirm(msg)) act(`back-${s.name}`, () => api.deleteNewerSnapshots(disk.id, s.name));
  }

  function mergeIntoBase(s: Snapshot, index: number) {
    const older = snapshots!.slice(0, index).map((o) => `@${o.name}`);
    const msg =
      `Make @${s.name} the new @base of "${disk.name}"?\n\n` +
      `Deleted: ${older.join(", ")}. This cannot be undone.\n` +
      `@${s.name} is renamed to @base with all its changes; PCs on it keep running.`;
    if (window.confirm(msg)) act(`merge-${s.name}`, () => api.mergeIntoBase(disk.id, s.name));
  }

  // Only a writeback of the newest version can be applied (the backend refuses others).
  const latest = snapshots && snapshots.length > 0 ? snapshots[snapshots.length - 1].name : null;

  function apply(w: Writeback) {
    if (w.machine_id === null) return;
    const msg =
      `Apply the writeback of ${w.machine_name} to "${disk.name}"?\n\n` +
      "It becomes a new version. Every other PC gets it at its next reboot.";
    if (!window.confirm(msg)) return;
    const comment = window.prompt("Comment for the new version (who, what changed). Optional.", "");
    if (comment === null) return;
    // Only this disk's writeback; the PC's other one (Boot Mode) stays kept.
    const choice: ApplyChoice = {
      os: disk.kind === "os",
      game: disk.kind !== "os",
      ...(comment.trim() ? { comment: comment.trim() } : {}),
    };
    const id = w.machine_id;
    act(`apply-${id}`, () => applyWritebacksChecked(id, choice));
  }

  function discard(w: Writeback) {
    const lost = w.keep_writeback
      ? "It keeps its writeback, so changes not applied yet are lost too."
      : "Everything it wrote since its last reboot is lost.";
    const msg = `Discard the writeback of ${w.machine_name}?\n\n${lost} It gets a clean copy of the active version.`;
    if (w.machine_id !== null && window.confirm(msg)) {
      const id = w.machine_id;
      act(`discard-${id}`, () => api.discardWriteback(id));
    }
  }

  return (
    <div className="details">
      <ErrorBanner message={error} onClose={() => setError(null)} />

      {summary && snapshots !== null && writebacks !== null && summary(snapshots, writebacks)}

      <h3 className="title-with-icon">
        <GitBranch size={16} aria-hidden />
        Snapshots
      </h3>
      {snapshots === null ? (
        <p className="muted small">Loading…</p>
      ) : snapshots.length === 0 ? (
        <p className="muted small">No versions yet: publish the disk first.</p>
      ) : (
        <table aria-label={`Versions of ${disk.name}`}>
          <thead>
            <tr>
              <th>Version</th>
              <th>Created</th>
              <th>Data</th>
              <th>Only in this version</th>
              <th>Used by</th>
              <th>Comment</th>
              {canManage && <th />}
            </tr>
          </thead>
          <tbody>
            {snapshots.map((s, i) => {
              const blocker = i > 0 ? mergeBlocker(i) : null;
              const users = [...s.machines, ...s.pinned.filter((p) => !s.machines.includes(p))];
              const used = users.length > 0;
              return (
                <tr key={s.name}>
                  <td className="strong">
                    @{s.name}
                    {s.locked && (
                      <span className="lock-mark" title="Locked: never deleted until unlocked">
                        <Lock size={13} aria-label="Locked" />
                      </span>
                    )}
                    {s.active && <span className="badge badge-provisioned">Active</span>}
                    {s.update && s.update.length > 0 && <UpdateBadge signs={s.update} />}
                    {i === snapshots.length - 1 && (
                      <span className="badge badge-idle" title="The newest version: only PCs on it can apply writebacks">
                        Latest
                      </span>
                    )}
                  </td>
                  <td>{new Date(s.created_at).toLocaleString()}</td>
                  <td>{formatBytes(s.referenced_bytes)}</td>
                  <td>{formatBytes(s.used_bytes)}</td>
                  <td>
                    {used ? (
                      users.map((u) => (s.pinned.includes(u) ? `${u} (pinned)` : u)).join(", ")
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    {s.comment ? <span className="small">{s.comment}</span> : <span className="muted">—</span>}
                    {s.retention && (
                      <div className="small muted" title="Automatic removal (Settings → Retention)">
                        Retention: {s.retention}
                      </div>
                    )}
                  </td>
                  {canManage && (
                    <td className="actions">
                      <button
                        type="button"
                        className="icon-btn"
                        onClick={() => toggleLock(s)}
                        disabled={busy !== null}
                        aria-label={`${s.locked ? "Unlock" : "Lock"} @${s.name}`}
                        title={s.locked ? "Unlock" : "Lock: keep this version until unlocked"}
                      >
                        {s.locked ? <LockOpen size={14} /> : <Lock size={14} />}
                      </button>
                      <button
                        type="button"
                        className="secondary"
                        onClick={() => editComment(s)}
                        disabled={busy !== null}
                        aria-label={`Comment on @${s.name}`}
                      >
                        {busy === `comment-${s.name}` ? "Saving…" : "Comment"}
                      </button>
                      {!s.active && (
                        <button type="button" onClick={() => makeActive(s)} disabled={busy !== null}>
                          {busy === `active-${s.name}` ? "Activating…" : "Make active"}
                        </button>
                      )}
                      {i < snapshots.length - 1 && (
                        <button
                          type="button"
                          onClick={() => goBack(s, i)}
                          disabled={busy !== null || newerBlocker(i) !== null}
                          title={newerBlocker(i) ?? `Make @${s.name} active and delete every newer version`}
                        >
                          {busy === `back-${s.name}` ? "Going back…" : "Go back here"}
                        </button>
                      )}
                      {i > 0 && (
                        <button
                          type="button"
                          onClick={() => mergeIntoBase(s, i)}
                          disabled={busy !== null || blocker !== null}
                          title={blocker ?? `Delete every older version and rename @${s.name} to @base`}
                        >
                          {busy === `merge-${s.name}` ? "Merging…" : "Merge into base"}
                        </button>
                      )}
                      <button
                        type="button"
                        className="danger"
                        onClick={() => remove(s)}
                        disabled={busy !== null || s.active || used || s.locked === true}
                        title={
                          s.locked
                            ? "Locked; unlock it first"
                            : s.active
                            ? "The active version cannot be deleted"
                            : s.pinned.length > 0
                              ? "PCs are pinned to it; unpin them first"
                              : used
                                ? "PCs still run on it; they move off at their next reboot"
                                : undefined
                        }
                      >
                        {busy === `delete-${s.name}` ? "Deleting…" : "Delete"}
                      </button>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <h3 className="title-with-icon">
        <HardDrive size={16} aria-hidden />
        Writebacks
      </h3>
      {writebacks === null ? (
        <p className="muted small">Loading…</p>
      ) : writebacks.length === 0 ? (
        <p className="muted small">No PC has this disk.</p>
      ) : (
        <table aria-label={`Writebacks of ${disk.name}`}>
          <thead>
            <tr>
              <th>PC</th>
              <th>Version</th>
              <th>Written</th>
              <th>Connected</th>
              {canManage && <th />}
            </tr>
          </thead>
          <tbody>
            {writebacks.map((w) => (
              <tr key={w.zvol}>
                <td className="strong">
                  {w.machine_name}
                  {w.machine_id === null && (
                    <div className="muted small" title={w.zvol}>
                      no machine record
                    </div>
                  )}
                </td>
                <td>
                  @{w.snapshot}
                  {w.keep_writeback ? (
                    <span className="badge badge-warn">Keep writeback</span>
                  ) : (
                    w.outdated && (
                      <span className="badge badge-warn">→ @{disk.snapshot} at reboot</span>
                    )
                  )}
                </td>
                <td>{formatBytes(w.used_bytes)}</td>
                <td>{w.session_active === null ? "—" : w.session_active ? "Yes" : "No"}</td>
                {canManage && (
                  <td className="actions">
                    {w.machine_id !== null && w.keep_writeback && (
                      <button
                        type="button"
                        onClick={() => apply(w)}
                        disabled={busy !== null || w.session_active === true || w.snapshot !== latest}
                        title={
                          w.session_active
                            ? `Shut down ${w.machine_name} first`
                            : w.snapshot !== latest
                              ? `Only a writeback of the newest version (@${latest}) can be applied`
                              : "Make this writeback the next version of the image"
                        }
                      >
                        {busy === `apply-${w.machine_id}` ? "Applying…" : "Apply"}
                      </button>
                    )}
                    {w.machine_id !== null && (
                      <button
                        type="button"
                        className="danger"
                        onClick={() => discard(w)}
                        disabled={busy !== null || w.session_active === true}
                        title={w.session_active ? `Shut down ${w.machine_name} first` : undefined}
                      >
                        {busy === `discard-${w.machine_id}` ? "Discarding…" : "Discard"}
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
