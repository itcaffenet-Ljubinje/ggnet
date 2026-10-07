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

  const assigned = (id: number) => machines.filter((m) => m.game_disk_id === id).length;

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
                  ) : (
                    <span className="badge badge-idle">Draft</span>
                  )}
                </td>
                <td>{assigned(d.id)}</td>
                <td className="mono muted">{d.zvol_path}</td>
                <td className="actions">
                  {!d.published && (
                    <button type="button" onClick={() => publish(d)} disabled={busy !== null}>
                      {busy === `publish-${d.id}` ? "Publishing…" : "Publish"}
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
