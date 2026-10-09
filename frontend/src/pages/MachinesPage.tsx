import { Fragment, useEffect, useState, type FormEvent } from "react";

import { api, type GameDisk, type Machine, type MachineMode } from "../api";
import { AgentState } from "../components/AgentState";
import { ErrorBanner } from "../components/ErrorBanner";
import { ImageStatus } from "../components/ImageStatus";
import { MachineDetails } from "../components/MachineDetails";
import { StatusBadge } from "../components/StatusBadge";
import { useAsyncAction } from "../useAsyncAction";

interface Props {
  disks: GameDisk[];
  machines: Machine[];
  reload: () => Promise<void>;
}

const CLIENT_OFF = "The client PC must be powered off or disconnected.";
// Game disk letters; A-C are floppy and system drives.
const LETTERS = "DEFGHIJKLMNOPQRSTUVWXYZ".split("");
const MOVES_NOW = "Running PCs move the game disk within a minute; close games on them first.";

export function MachinesPage({ disks, machines, reload }: Props) {
  const { busy, error, setError, run } = useAsyncAction();
  const [name, setName] = useState("");
  const [mac, setMac] = useState("");
  const [iqn, setIqn] = useState("");
  const [mode, setMode] = useState<MachineMode>("disk");
  const [diskId, setDiskId] = useState("");
  const [allLetter, setAllLetter] = useState("D");
  // The machine whose hardware and network are shown.
  const [open, setOpen] = useState<number | null>(null);
  // Versions of each game disk in use, for the pin drop-down.
  const [versions, setVersions] = useState<Record<number, string[]>>({});

  const published = disks.filter((d) => d.published);
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

  async function create(e: FormEvent) {
    e.preventDefault();
    const ok = await run("create", () =>
      api.createMachine({
        name: name.trim(),
        mode,
        mac: mac.trim() || null,
        initiator_iqn: iqn.trim() || null,
        game_disk_id: mode === "disk" && diskId ? Number(diskId) : null,
      }),
    );
    if (ok) {
      setName("");
      setMac("");
      setIqn("");
    }
    await reload();
  }

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
    const msg =
      `Apply the writeback of ${m.name} to "${diskName(m.game_disk_id)}"?\n\n` +
      "It becomes the new version of the game disk. Every other PC gets it at its next reboot.\n\n" +
      `${m.name} must be powered off.`;
    if (!window.confirm(msg)) return;
    await run(`apply-${m.id}`, () => api.applyWritebacks(m.id));
    await reload();
  }

  async function remove(m: Machine) {
    if (!window.confirm(`Delete ${m.name} and its disk on the host?\n\n${CLIENT_OFF}`)) return;
    await run(`delete-${m.id}`, () => api.deleteMachine(m.id));
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
            placeholder="Windows PC name"
            required
            maxLength={15}
          />
        </label>
        <label>
          MAC
          <input value={mac} onChange={(e) => setMac(e.target.value)} placeholder="optional" />
        </label>
        <label>
          Initiator IQN
          <input
            value={iqn}
            onChange={(e) => setIqn(e.target.value)}
            placeholder={`iqn.1991-05.com.microsoft:${name.trim().toLowerCase() || "<name>"}`}
          />
        </label>
        <label>
          Mode
          <select value={mode} onChange={(e) => setMode(e.target.value as MachineMode)}>
            <option value="disk">Disk</option>
            <option value="boot">Boot (later)</option>
          </select>
        </label>
        <label>
          Game disk
          <select
            value={diskId}
            onChange={(e) => setDiskId(e.target.value)}
            disabled={mode !== "disk"}
          >
            <option value="">None</option>
            {published.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" disabled={busy !== null}>
          {busy === "create" ? "Adding…" : "Add machine"}
        </button>
      </form>

      {machines.some((m) => m.mode === "disk") && (
        <div className="card form-row">
          <label>
            Game disk letter for all Disk Mode PCs
            <select value={allLetter} onChange={(e) => setAllLetter(e.target.value)}>
              {LETTERS.map((l) => (
                <option key={l} value={l}>
                  {l}:
                </option>
              ))}
            </select>
          </label>
          <button type="button" onClick={setLetterAll} disabled={busy !== null}>
            {busy === "letter-all" ? "Applying…" : "Apply to all"}
          </button>
        </div>
      )}

      {machines.length === 0 ? (
        <p className="empty">No machines yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Mode</th>
              <th>MAC</th>
              <th>Game disk</th>
              <th>Drive</th>
              <th>Status</th>
              <th>Agent</th>
              <th>Keep writeback</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {machines.map((m) => (
              <Fragment key={m.id}>
                <tr>
                  <td>
                    <div className="strong">{m.name}</div>
                    <div className="mono muted small">{m.initiator_iqn}</div>
                  </td>
                  <td>{m.mode === "disk" ? "Disk" : "Boot"}</td>
                  <td className="mono">{m.mac ?? "—"}</td>
                  <td>
                    {m.status === "editing" ? (
                      <span className="muted">Editing {diskName(m.editing_disk_id)}</span>
                    ) : m.mode === "disk" ? (
                      <select
                        aria-label={`Game disk for ${m.name}`}
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
                    ) : (
                      <span className="muted">—</span>
                    )}
                    {m.mode === "disk" && m.game_disk_id !== null && m.status !== "editing" && (
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
                  </td>
                  <td>
                    {m.mode === "disk" ? (
                      <select
                        aria-label={`Drive letter for ${m.name}`}
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
                    ) : (
                      <span className="muted">D:</span>
                    )}
                    {m.reported_drive_letter && m.reported_drive_letter !== m.drive_letter && (
                      <div>
                        <span
                          className="badge badge-warn"
                          title={`${m.drive_letter}: is taken on this PC, so the game disk got ${m.reported_drive_letter}:`}
                        >
                          Got {m.reported_drive_letter}:
                        </span>
                      </div>
                    )}
                  </td>
                  <td>
                    <StatusBadge status={m.status} />
                    {m.last_error && <div className="error-text small">{m.last_error}</div>}
                  </td>
                  <td>
                    <AgentState machine={m} />
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Keep writeback of ${m.name}`}
                      checked={m.keep_writeback}
                      onChange={(e) => keep(m, e.target.checked)}
                      disabled={busy !== null || m.mode !== "disk"}
                    />
                  </td>
                  <td className="actions">
                    <button
                      type="button"
                      className="secondary"
                      aria-expanded={open === m.id}
                      onClick={() => setOpen(open === m.id ? null : m.id)}
                    >
                      {open === m.id ? "Hide details" : "Details"}
                    </button>
                    {m.keep_writeback && m.status === "provisioned" && (
                      <button
                        type="button"
                        onClick={() => apply(m)}
                        disabled={busy !== null || m.session_active === true}
                        title={m.session_active ? `Shut down ${m.name} first` : undefined}
                      >
                        {busy === `apply-${m.id}` ? "Applying…" : "Apply writebacks"}
                      </button>
                    )}
                    <button
                      type="button"
                      className="danger"
                      onClick={() => remove(m)}
                      disabled={busy !== null || m.status === "editing"}
                      title={m.status === "editing" ? "Finish editing the game disk first" : undefined}
                    >
                      {busy === `delete-${m.id}` ? "Deleting…" : "Delete"}
                    </button>
                  </td>
                </tr>
                {open === m.id && (
                  <tr>
                    <td colSpan={9}>
                      <MachineDetails machine={m} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
