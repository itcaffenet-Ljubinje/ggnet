import { useEffect, useState, type FormEvent } from "react";
import { Cpu, HardDrive, Monitor, Network, Save, Settings, X } from "lucide-react";

import { api, type Image, type Machine, type MachineMode } from "../api";
import { formatBytes, formatLink, formatNic } from "../format";
import { useAsyncAction } from "../useAsyncAction";
import { isOnline } from "./AgentState";
import { ErrorBanner } from "./ErrorBanner";

interface Props {
  machine: Machine;
  disks: Image[];
  onClose: () => void;
  reload: () => Promise<void>;
}

type Tab = "general" | "disks" | "network" | "hardware";

// Game image letters; A-C are floppy and system drives.
const LETTERS = "DEFGHIJKLMNOPQRSTUVWXYZ".split("");

/**
 * Everything about one PC in one place (machine settings). Save
 * sends only what changed: name (a PC with disks is renamed on the server
 * too, so it asks first) and drive letter; the mode (with disks: they are
 * taken away and given again, after a confirmation); the disks (which
 * re-clones the writebacks, so it asks first); Keep Writeback; the pinned
 * version. The MAC is the PC's identity and only shown.
 */
export function MachineSettings({ machine: m, disks, onClose, reload }: Props) {
  const { busy, error, setError, run } = useAsyncAction();
  const [tab, setTab] = useState<Tab>("general");
  const [name, setName] = useState(m.name);
  const [mode, setMode] = useState<MachineMode>(m.mode);
  const [osImageId, setOsImageId] = useState(m.os_image_id ? String(m.os_image_id) : "");
  const [imageId, setImageId] = useState(m.game_image_id ? String(m.game_image_id) : "");
  const [letter, setLetter] = useState(m.drive_letter);
  const [keep, setKeep] = useState(m.keep_writeback);
  const [pin, setPin] = useState(m.pinned_snapshot ?? "");
  const [versions, setVersions] = useState<string[]>([]);
  const [saved, setSaved] = useState(false);

  const osImages = disks.filter((d) => d.published && d.kind === "os");
  const gameImages = disks.filter((d) => d.published && d.kind !== "os");
  const hasDisk = m.game_image_id !== null || m.status !== "idle";
  const online = isOnline(m);

  useEffect(() => {
    if (!m.game_image_id) return;
    api
      .listSnapshots(m.game_image_id)
      .then((s) => setVersions(s.map((v) => v.name)))
      .catch(() => setVersions([]));
  }, [m.game_image_id]);

  const disksChanged =
    imageId !== (m.game_image_id ? String(m.game_image_id) : "") ||
    (mode === "boot" && osImageId !== (m.os_image_id ? String(m.os_image_id) : ""));

  async function save(e: FormEvent) {
    e.preventDefault();
    setSaved(false);
    const patch: Record<string, string | null> = {};
    if (name.trim() !== m.name) patch.name = name.trim();
    const modeChanged = mode !== m.mode;
    if (modeChanged && !hasDisk) patch.mode = mode;
    if (mode === "disk" && letter !== m.drive_letter) patch.drive_letter = letter;
    if (patch.name && hasDisk) {
      const msg =
        `Rename ${m.name} to ${patch.name}? Its writebacks are renamed on the server (nothing is lost) and ` +
        "Windows gets the new name at its next boot.\n\nThe PC must be powered off.";
      if (!window.confirm(msg)) return;
    }
    if (modeChanged && hasDisk) {
      if (mode === "boot" && (!osImageId || !imageId)) {
        setError("A Boot Mode PC needs an OS image and a game image: pick both on the Disks tab.");
        return;
      }
      const msg =
        `Switch ${m.name} to ${mode === "boot" ? "Boot Mode (diskless)" : "Disk Mode (local Windows)"}? ` +
        "Its writebacks are removed and made again for the new mode, so everything the PC wrote is lost." +
        "\n\nThe PC must be powered off.";
      if (!window.confirm(msg)) return;
    } else if (disksChanged && m.game_image_id !== null) {
      const msg =
        `Change the disks of ${m.name}? Its writebacks are cloned again from the new disks, ` +
        "so everything the PC wrote is lost.\n\nThe PC must be powered off or disconnected.";
      if (!window.confirm(msg)) return;
    }
    const ok = await run("save", async () => {
      if (Object.keys(patch).length > 0) await api.updateMachine(m.id, patch as Parameters<typeof api.updateMachine>[1]);
      if (modeChanged && hasDisk) {
        // The mode can only change without disks: take them away, switch, give the new ones.
        await api.assignDisk(m.id, null);
        await api.updateMachine(m.id, { mode });
        if (imageId) {
          await api.assignDisk(
            m.id,
            Number(imageId),
            mode === "boot" ? Number(osImageId) : undefined,
          );
        }
      } else if (disksChanged) {
        await api.assignDisk(
          m.id,
          imageId ? Number(imageId) : null,
          mode === "boot" ? (osImageId ? Number(osImageId) : null) : undefined,
        );
      }
      if (keep !== m.keep_writeback) await api.setKeepWriteback(m.id, keep);
      if (!disksChanged && !modeChanged && (pin || null) !== m.pinned_snapshot) {
        await api.pinSnapshot(m.id, pin || null);
      }
    });
    await reload();
    if (ok) setSaved(true);
  }

  const tabs: { id: Tab; label: string; icon: typeof Settings }[] = [
    { id: "general", label: "General", icon: Settings },
    { id: "disks", label: "Disks", icon: HardDrive },
    { id: "network", label: "Network", icon: Network },
    { id: "hardware", label: "Hardware", icon: Cpu },
  ];
  const hw = m.hardware;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-content machine-settings-modal"
        role="dialog"
        aria-label={`Settings of ${m.name}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <div className={`machine-status-avatar ${online ? "online" : "offline"}`}>
              <Monitor size={20} />
            </div>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <h2>{m.name}</h2>
                <span className={`badge ${online ? "badge-provisioned" : "badge-warn"}`}>
                  {online ? "Online" : "Offline"}
                </span>
                <span className={`mode-pill ${m.mode === "boot" ? "mode-pill-boot" : "mode-pill-disk"}`}>
                  {m.mode === "boot" ? "Boot Mode" : "Disk Mode"}
                </span>
              </div>
              <p className="modal-subheading">
                {m.reported_ip ?? "No IP reported"} · MAC {m.mac ?? "not set"}
              </p>
            </div>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>

        <div className="settings-tabs-bar" role="tablist">
          {tabs.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={tab === id}
              className={`settings-tab-btn ${tab === id ? "active" : ""}`}
              onClick={() => setTab(id)}
            >
              <Icon size={14} />
              <span>{label}</span>
            </button>
          ))}
        </div>

        <form onSubmit={save}>
          <div className="settings-modal-body">
            <ErrorBanner message={error} onClose={() => setError(null)} />

            {tab === "general" && (
              <div className="form-grid-2">
                <label>
                  Name
                  <input
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    maxLength={15}
                    required
                    title={hasDisk ? "The PC must be powered off: its writebacks are renamed too" : undefined}
                  />
                </label>
                <label className="check">
                  <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
                  Keep writeback (changes survive reboots; for installing and updating)
                </label>
              </div>
            )}

            {tab === "disks" && (
              <div className="form-grid-2">
                <label>
                  Mode
                  <select
                    value={mode}
                    onChange={(e) => setMode(e.target.value as MachineMode)}
                    title={hasDisk ? "Changing it makes the writebacks again; the PC must be powered off" : undefined}
                  >
                    <option value="disk">Disk (local Windows)</option>
                    <option value="boot">Boot (diskless)</option>
                  </select>
                </label>
                {mode === "boot" && (
                  <label>
                    OS image
                    <select value={osImageId} onChange={(e) => setOsImageId(e.target.value)}>
                      <option value="">None</option>
                      {osImages.map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <label>
                  Game image
                  <select value={imageId} onChange={(e) => setImageId(e.target.value)}>
                    <option value="">None</option>
                    {gameImages.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.name}
                      </option>
                    ))}
                  </select>
                </label>
                {mode === "disk" && (
                  <label>
                    Game image letter
                    <select value={letter} onChange={(e) => setLetter(e.target.value)}>
                      {LETTERS.map((l) => (
                        <option key={l} value={l}>
                          {l}:
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                {m.game_image_id !== null && !disksChanged && mode === m.mode && (
                  <label>
                    Game image version
                    <select value={pin} onChange={(e) => setPin(e.target.value)}>
                      <option value="">Follow the active version</option>
                      {versions.map((v) => (
                        <option key={v} value={v}>
                          Pin @{v}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
              </div>
            )}

            {tab === "network" && (
              <div className="form-grid-2">
                <div>
                  <div className="muted small">MAC (the PC's identity)</div>
                  <div className="mono">{m.mac ?? "—"}</div>
                  <div className="muted small">
                    Fixed. A PC with a new network card is deleted and added again from New PCs.
                  </div>
                </div>
                <div>
                  <div className="muted small">Reported by the agent</div>
                  <div>IP {m.reported_ip ?? "—"}</div>
                  <div>MAC {m.reported_mac ?? "—"}</div>
                  <div>Link {m.link_speed_mbps ? formatLink(m.link_speed_mbps) : "—"}</div>
                </div>
              </div>
            )}

            {tab === "hardware" && (
              <dl className="details">
                <dt>CPU</dt>
                <dd>{hw?.cpu ?? "—"}</dd>
                <dt>GPU</dt>
                <dd>{hw?.gpus?.length ? hw.gpus.join(", ") : "—"}</dd>
                <dt>Motherboard</dt>
                <dd>{hw?.motherboard ?? "—"}</dd>
                <dt>Memory</dt>
                <dd>{hw?.memory_bytes ? formatBytes(hw.memory_bytes) : "—"}</dd>
                <dt>Network card</dt>
                <dd>{hw?.nic ?? "—"}</dd>
                <dt>Secure Boot</dt>
                <dd>{hw?.secure_boot === true ? "On" : hw?.secure_boot === false ? "Off" : "—"}</dd>
                <dt>ggNet Secure Boot key</dt>
                <dd>
                  {m.mok_state === "enrolled"
                    ? "Enrolled"
                    : m.mok_state === "requested"
                      ? "Asked for: confirm Enroll MOK in MokManager at the next boot"
                      : m.mok_state === "failed"
                        ? "The agent could not ask for it (see its log)"
                        : "—"}
                </dd>
                <dt>Boot network card</dt>
                <dd title={m.boot_nic ? `PCI\\${m.boot_nic}` : undefined}>
                  {m.boot_nic ? (
                    <>
                      {formatNic(m.boot_nic)} <span className="mono muted small">{m.boot_nic}</span>
                    </>
                  ) : (
                    "— (read by iPXE at the next network boot)"
                  )}
                </dd>
                <dt>Windows started</dt>
                <dd>{m.booted_at ? new Date(m.booted_at).toLocaleString() : "—"}</dd>
                <dt>Agent</dt>
                <dd>{m.agent_version ?? "not installed"}</dd>
              </dl>
            )}
          </div>

          <div className="modal-footer">
            <div className="footer-left" />
            <div className="footer-right">
              {saved && <span className="save-success-tag">Saved</span>}
              <button type="button" className="secondary" onClick={onClose} disabled={busy !== null}>
                Close
              </button>
              <button type="submit" className="btn-primary" disabled={busy !== null || !name.trim()}>
                <Save size={14} />
                {busy === "save" ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
