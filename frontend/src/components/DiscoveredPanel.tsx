import { useState } from "react";

import { api, preselectDisk, type Discovered, type Image, type MachineMode } from "../api";
import { formatNic } from "../format";

interface Props {
  discovered: Discovered[];
  /** All disks; published OS images and game images are offered. */
  disks: Image[];
  busy: boolean;
  run: (key: string, fn: () => Promise<unknown>) => Promise<boolean>;
  reload: () => Promise<void>;
}

/** A machine name from a Windows computer name: [a-z0-9-], at most 15 characters. */
export function suggestName(hostname: string | null): string {
  if (!hostname) return "";
  return hostname
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, "-")
    .replace(/^-+/, "")
    .slice(0, 15);
}

/**
 * PCs that asked the server for a disk but are not registered (each gets a
 * PC to Machines on its first network boot). Adding one keeps its MAC, so
 * its IQN is derived from the MAC and the name here becomes its identity.
 */
export function DiscoveredPanel({ discovered, disks, busy, run, reload }: Props) {
  if (discovered.length === 0) return null;
  return (
    <div className="card">
      <h2 className="card-title">New PCs ({discovered.length})</h2>
      <p className="muted small">
        These PCs asked the server for a disk but are not registered. Add one with the name it should have
        (e.g. pc-01, vip-05) and its disks, or ignore it. A PC waiting on its network boot screen boots
        within seconds of being added.
      </p>
      <table>
        <thead>
          <tr>
            <th>MAC</th>
            <th>Windows name</th>
            <th>IP</th>
            <th>Seen by</th>
            <th>Last seen</th>
            <th>Name</th>
            <th>Mode</th>
            <th>Disks</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {discovered.map((d) => (
            <DiscoveredRow key={d.id} d={d} disks={disks} busy={busy} run={run} reload={reload} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DiscoveredRow({ d, disks, busy, run, reload }: { d: Discovered } & Omit<Props, "discovered">) {
  const [name, setName] = useState(suggestName(d.hostname));
  // A PC that booted from the network is a Boot Mode PC; one with an agent runs local Windows.
  const [mode, setMode] = useState<MachineMode>(d.source === "pxe" ? "boot" : "disk");
  const osImages = disks.filter((x) => x.published && x.kind === "os");
  const gameImages = disks.filter((x) => x.published && x.kind !== "os");
  // The default image (Images → Make default), or the only one.
  const [osImageId, setOsImageId] = useState(preselectDisk(osImages));
  const [imageId, setImageId] = useState(preselectDisk(gameImages));
  // A Boot Mode PC cannot boot without both.
  const ready = !!name.trim() && (mode !== "boot" || (!!osImageId && !!imageId));

  async function claim() {
    const body = {
      name: name.trim(),
      mode,
      ...(imageId ? { game_image_id: Number(imageId) } : {}),
      ...(mode === "boot" && osImageId ? { os_image_id: Number(osImageId) } : {}),
    };
    if (await run(`claim-${d.id}`, () => api.claimDiscovered(d.id, body))) {
      await reload();
    }
  }

  async function ignore() {
    if (!window.confirm(`Ignore ${d.mac}? It stays hidden also when it asks again.`)) return;
    await run(`ignore-${d.id}`, () => api.ignoreDiscovered(d.id));
    await reload();
  }

  return (
    <tr>
      <td className="mono">{d.mac}</td>
      <td>{d.hostname ?? <span className="muted">—</span>}</td>
      <td className="mono">{d.ip_address ?? "—"}</td>
      <td>
        {d.source === "pxe" ? "Network boot" : `Agent ${d.agent_version ?? ""}`}
        {d.seen_count > 1 && <div className="muted small">{d.seen_count} times</div>}
        {d.boot_nic && (
          <div className="muted small" title={`PCI\\${d.boot_nic}`}>
            {formatNic(d.boot_nic)}
          </div>
        )}
      </td>
      <td className="small">{new Date(d.last_seen_at).toLocaleString()}</td>
      <td>
        <input
          aria-label={`Name for ${d.mac}`}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="pc-01"
          maxLength={15}
        />
      </td>
      <td>
        <select aria-label={`Mode for ${d.mac}`} value={mode} onChange={(e) => setMode(e.target.value as MachineMode)}>
          <option value="disk">Disk</option>
          <option value="boot">Boot</option>
        </select>
      </td>
      <td>
        {mode === "boot" && (
          <select aria-label={`OS image for ${d.mac}`} value={osImageId} onChange={(e) => setOsImageId(e.target.value)}>
            <option value="">OS image…</option>
            {osImages.map((x) => (
              <option key={x.id} value={x.id}>
                {x.name}
              </option>
            ))}
          </select>
        )}
        <select aria-label={`Game image for ${d.mac}`} value={imageId} onChange={(e) => setImageId(e.target.value)}>
          <option value="">{mode === "boot" ? "Game image…" : "No game image"}</option>
          {gameImages.map((x) => (
            <option key={x.id} value={x.id}>
              {x.name}
            </option>
          ))}
        </select>
      </td>
      <td className="actions">
        <button type="button" onClick={claim} disabled={busy || !ready}>
          Add
        </button>
        <button type="button" className="secondary" onClick={ignore} disabled={busy}>
          Ignore
        </button>
      </td>
    </tr>
  );
}
