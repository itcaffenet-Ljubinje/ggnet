import { useCallback, useEffect, useState } from "react";

import { api, type GameDisk, type Snapshot, type Writeback } from "../api";
import { formatBytes } from "../format";
import { useAsyncAction } from "../useAsyncAction";
import { ErrorBanner } from "./ErrorBanner";

interface Props {
  disk: GameDisk;
  /** Reload disks and machines in the parent after an action. */
  onChanged: () => Promise<void>;
  /** Changes on every parent reload, so the lists follow the host. */
  refreshKey?: unknown;
}

/** Versions (snapshots) and PC writebacks of one game disk, read live from ZFS. */
export function DiskDetails({ disk, onChanged, refreshKey }: Props) {
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

  function makeActive(s: Snapshot) {
    const msg =
      `Make version ${s.name} of "${disk.name}" active?\n\n` +
      "Every PC moves to it at its next reboot. PCs with Keep Writeback stay where they are.";
    if (window.confirm(msg)) act(`active-${s.name}`, () => api.setActiveSnapshot(disk.id, s.name));
  }

  function remove(s: Snapshot) {
    if (window.confirm(`Delete version ${s.name} of "${disk.name}"? This cannot be undone.`)) {
      act(`delete-${s.name}`, () => api.deleteSnapshot(disk.id, s.name));
    }
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

      <h3>Versions</h3>
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
              <th />
            </tr>
          </thead>
          <tbody>
            {snapshots.map((s) => {
              const users = [...s.machines, ...s.pinned.filter((p) => !s.machines.includes(p))];
              const used = users.length > 0;
              return (
                <tr key={s.name}>
                  <td className="strong">
                    @{s.name}
                    {s.active && <span className="badge badge-provisioned">Active</span>}
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
                  <td className="actions">
                    {!s.active && (
                      <button type="button" onClick={() => makeActive(s)} disabled={busy !== null}>
                        {busy === `active-${s.name}` ? "Activating…" : "Make active"}
                      </button>
                    )}
                    <button
                      type="button"
                      className="danger"
                      onClick={() => remove(s)}
                      disabled={busy !== null || s.active || used}
                      title={
                        s.active
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
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <h3>Writebacks</h3>
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
              <th />
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
                <td className="actions">
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
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
