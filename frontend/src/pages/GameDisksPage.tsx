import { useState, type FormEvent } from "react";

import { api, type GameDisk, type Machine } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { useAsyncAction } from "../useAsyncAction";

interface Props {
  disks: GameDisk[];
  machines: Machine[];
  reload: () => Promise<void>;
}

export function GameDisksPage({ disks, machines, reload }: Props) {
  const { busy, error, setError, run } = useAsyncAction();
  const [name, setName] = useState("");
  const [sizeGb, setSizeGb] = useState("100");
  // Per draft disk: the machine picked to fill it on.
  const [editOn, setEditOn] = useState<Record<number, string>>({});

  const assigned = (id: number) => machines.filter((m) => m.game_disk_id === id).length;
  const machine = (id: number | null) => machines.find((m) => m.id === id);
  // A draft can be filled only on a disk-mode PC that has no game disk.
  const free = machines.filter(
    (m) => m.mode === "disk" && m.status === "idle" && m.game_disk_id === null,
  );

  async function create(e: FormEvent) {
    e.preventDefault();
    const ok = await run("create", () => api.createDisk(name.trim(), Number(sizeGb)));
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

  async function remove(disk: GameDisk) {
    if (!window.confirm(`Delete game disk "${disk.name}" and its zvol on the host?`)) return;
    await run(`delete-${disk.id}`, () => api.deleteDisk(disk.id));
    await reload();
  }

  return (
    <section>
      <ErrorBanner message={error} onClose={() => setError(null)} />

      <form className="card form-row" onSubmit={create}>
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
          {busy === "create" ? "Creating…" : "Create disk"}
        </button>
      </form>

      {disks.length === 0 ? (
        <p className="empty">No game disks yet.</p>
      ) : (
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
            {disks.map((d) => (
              <tr key={d.id}>
                <td className="strong">{d.name}</td>
                <td>{d.size_gb} GB</td>
                <td>
                  {d.published ? (
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
                  {!d.published && d.editor_id === null && (
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
                        {busy === `edit-${d.id}` ? "Starting…" : "Edit on PC"}
                      </button>
                      <button type="button" onClick={() => publish(d)} disabled={busy !== null}>
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
                      {busy === `finish-${d.id}` ? "Finishing…" : "Finish editing"}
                    </button>
                  )}
                  <button
                    type="button"
                    className="danger"
                    onClick={() => remove(d)}
                    disabled={busy !== null}
                  >
                    {busy === `delete-${d.id}` ? "Deleting…" : "Delete"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
