import { lazy, Suspense, useEffect, useState, type FormEvent } from "react";
import { MonitorPlay, Play, Power, RotateCw, Save, Server, Square, X } from "lucide-react";

import {
  api,
  preselectDisk,
  type Image,
  type Machine,
  type VmActionKind,
  type VmHost,
  type VmInfo,
} from "../api";
import { useAsyncAction } from "../useAsyncAction";
import { ErrorBanner } from "./ErrorBanner";
import { Modal } from "./Modal";

// noVNC is loaded only when a console is opened.
const VmConsole = lazy(() => import("./VmConsole").then((m) => ({ default: m.VmConsole })));

const GB = 1024;

/** Memory a new VM may get: the host's, less what Settings keeps for the host. */
export function vmMemoryLimit(host: VmHost | null): number | null {
  if (!host?.available || !host.memory_mb || !host.settings) return null;
  return Math.max(host.memory_mb - host.settings.reserved_memory_gb * GB, 0);
}

interface CreateVmProps {
  disks: Image[];
  reload: () => Promise<void>;
  onClose: () => void;
}

/**
 * Make a VM on this host for working on images: it starts the OS image with
 * the game image, both as writebacks on its own drives, like a PC in Boot Mode.
 */
export function CreateVm({ disks, reload, onClose }: CreateVmProps) {
  const { busy, error, setError, run } = useAsyncAction();
  const [host, setHost] = useState<VmHost | null>(null);
  const [name, setName] = useState("");
  const gameImages = disks.filter((d) => d.published && d.kind !== "os");
  const osImages = disks.filter((d) => d.published && d.kind === "os");
  const [osImageId, setOsImageId] = useState(preselectDisk(osImages));
  const [imageId, setImageId] = useState(preselectDisk(gameImages));
  const [cores, setCores] = useState(4);
  const [memoryGb, setMemoryGb] = useState(8);
  const [keep, setKeep] = useState(true);

  useEffect(() => {
    api
      .vmHost()
      .then(setHost)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [setError]);

  const limit = vmMemoryLimit(host);
  const cpus = host?.cpus ?? 0;

  async function create(e: FormEvent) {
    e.preventDefault();
    const ok = await run("create", () =>
      api.createVm({
        name: name.trim(),
        os_image_id: Number(osImageId),
        game_image_id: Number(imageId),
        cores,
        memory_mb: memoryGb * GB,
        keep_writeback: keep,
      }),
    );
    await reload();
    if (ok) onClose();
  }

  return (
    <Modal label="Create VM" onClose={onClose} closeOnBackdrop>
      <div className="modal-sheet-grabber" />
      <div className="modal-header">
        <h2>Create VM</h2>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
          <X size={16} />
        </button>
      </div>
      <form onSubmit={create}>
        <div className="modal-body">
          <ErrorBanner message={error} onClose={() => setError(null)} />
          {host && !host.available ? (
            <div className="banner banner-warn" role="alert">
              Virtual machines need a Proxmox VE host: {host.problem}
            </div>
          ) : (
            <p className="muted small">
              A virtual machine on this server for installing and updating images without a PC. It starts the OS
              image with the game image, as a PC in Boot Mode does; Apply writebacks makes its changes new versions.
              {host?.available && (
                <>
                  {" "}
                  Network: bridge <strong>{host.bridge ?? "none"}</strong>, EFI and TPM state on{" "}
                  <strong>{host.storage ?? "none"}</strong> (Settings → Virtual machines).
                </>
              )}
            </p>
          )}
          <div className="form-group">
            <label htmlFor="vm-name">Name</label>
            <input
              id="vm-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. image-vm"
              required
              maxLength={15}
              autoFocus
            />
            <div className="form-hint">Windows gets this computer name.</div>
          </div>
          <div className="form-grid-2">
            <div className="form-group">
              <label htmlFor="vm-os">OS image</label>
              <select id="vm-os" value={osImageId} onChange={(e) => setOsImageId(e.target.value)} required>
                <option value="">Choose…</option>
                {osImages.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <label htmlFor="vm-game">Game image</label>
              <select id="vm-game" value={imageId} onChange={(e) => setImageId(e.target.value)} required>
                <option value="">Choose…</option>
                {gameImages.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <label htmlFor="vm-cores">CPU cores</label>
              <select id="vm-cores" value={cores} onChange={(e) => setCores(Number(e.target.value))}>
                {Array.from({ length: Math.max(cpus, cores) }, (_, i) => i + 1).map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
              {cpus > 0 && <div className="form-hint">The host has {cpus} threads.</div>}
            </div>
            <div className="form-group">
              <label htmlFor="vm-memory">Memory (GB)</label>
              <input
                id="vm-memory"
                type="number"
                min={2}
                max={limit !== null ? Math.floor(limit / GB) : undefined}
                value={memoryGb}
                onChange={(e) => setMemoryGb(Number(e.target.value))}
                required
              />
              {limit !== null && <div className="form-hint">At most {Math.floor(limit / GB)} GB.</div>}
            </div>
          </div>
          <label className="check">
            <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
            Keep writeback (changes survive shutdowns until you apply or discard them)
          </label>
        </div>
        <div className="modal-footer">
          <div className="footer-left" />
          <div className="footer-right">
            <button type="button" className="secondary" onClick={onClose} disabled={busy !== null}>
              Cancel
            </button>
            <button type="submit" disabled={busy !== null || host === null || !host.available}>
              <Server size={14} />
              {busy === "create" ? "Creating…" : "Create"}
            </button>
          </div>
        </div>
      </form>
    </Modal>
  );
}

const ASK: Partial<Record<VmActionKind, string>> = {
  shutdown: "Shut down {name}? Windows shuts down as from its Start menu.",
  reboot: "Restart {name}? Windows restarts as from its Start menu.",
  stop: "Power off {name} now? Like pulling the plug: Windows does not shut down and unsaved work is lost.",
  reset: "Reset {name} now? Like the reset button: unsaved work is lost.",
};

interface VmControlsProps {
  machine: Machine;
  vm: VmInfo | undefined;
  busy: boolean;
  run: ReturnType<typeof useAsyncAction>["run"];
  reload: () => Promise<void>;
}

/** Start, console and power buttons of a VM in the Machines list (in place of Wake-on-LAN and the agent's). */
export function VmControls({ machine: m, vm, busy, run, reload }: VmControlsProps) {
  const running = vm?.status === "running";
  const [console, setConsole] = useState(false);

  async function act(action: VmActionKind) {
    const ask = ASK[action];
    if (ask && !window.confirm(ask.replace("{name}", m.name))) return;
    await run(`vm-${m.id}`, () => api.vmAction(m.id, action));
    await reload();
  }

  if (!vm || vm.status === null) {
    return (
      <span className="badge badge-warn" title="Proxmox does not show this VM; it may have been removed there">
        VM missing
      </span>
    );
  }
  if (!running) {
    return (
      <button
        type="button"
        className="icon-btn icon-btn-ok"
        onClick={() => act("start")}
        disabled={busy || m.status !== "provisioned"}
        aria-label={`Start ${m.name}`}
        title={m.status !== "provisioned" ? "It needs its OS and game image first" : "Start the VM"}
      >
        <Play size={14} />
      </button>
    );
  }
  return (
    <>
      {console && (
        <Suspense fallback={null}>
          <VmConsole machine={m} onClose={() => setConsole(false)} />
        </Suspense>
      )}
      <button
        type="button"
        className="icon-btn"
        onClick={() => setConsole(true)}
        aria-label={`Console of ${m.name}`}
        title="Console: the VM's screen, keyboard and mouse"
      >
        <MonitorPlay size={14} />
      </button>
      <button
        type="button"
        className="icon-btn"
        onClick={() => act("reboot")}
        disabled={busy}
        aria-label={`Restart ${m.name}`}
        title="Restart"
      >
        <RotateCw size={14} />
      </button>
      <button
        type="button"
        className="icon-btn icon-btn-danger"
        onClick={() => act("shutdown")}
        disabled={busy}
        aria-label={`Shut down ${m.name}`}
        title="Shut down"
      >
        <Power size={14} />
      </button>
      <button
        type="button"
        className="icon-btn icon-btn-danger"
        onClick={() => act("stop")}
        disabled={busy}
        aria-label={`Power off ${m.name}`}
        title="Power off (no shutdown)"
      >
        <Square size={14} />
      </button>
    </>
  );
}

/**
 * Settings → Virtual machines: the bridge and storage new VMs get (the host's
 * own; ggNet never makes or changes them) and the memory VMs never take.
 */
export function VmSettingsCard() {
  const { busy, error, setError, run } = useAsyncAction();
  const [host, setHost] = useState<VmHost | null>(null);
  const [bridge, setBridge] = useState("");
  const [storage, setStorage] = useState("");
  const [reserved, setReserved] = useState(16);
  const [saved, setSaved] = useState(false);

  function load() {
    api
      .vmHost()
      .then((h) => {
        setHost(h);
        setBridge(h.settings?.bridge ?? "");
        setStorage(h.settings?.storage ?? "");
        setReserved(h.settings?.reserved_memory_gb ?? 16);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }

  useEffect(load, [setError]);

  async function save(e: FormEvent) {
    e.preventDefault();
    setSaved(false);
    const ok = await run("save", () =>
      api.saveVmSettings({ bridge: bridge || null, storage: storage || null, reserved_memory_gb: reserved }),
    );
    if (ok) {
      setSaved(true);
      load();
    }
  }

  if (host && !host.available) {
    return (
      <div className="card">
        <h3 className="card-title">Virtual machines</h3>
        <p className="muted">Virtual machines need a Proxmox VE host: {host.problem}</p>
      </div>
    );
  }

  const auto = (what: string | null | undefined) => `Automatic${what ? ` (${what})` : ""}`;
  const limit = vmMemoryLimit(host);

  return (
    <form className="card" onSubmit={save}>
      <h3 className="card-title">Virtual machines</h3>
      <p className="muted small">
        VMs for working on images (Machines → Create VM) run on this Proxmox host. They use a network bridge and a
        storage this host already has; ggNet never creates or changes them. A change applies to VMs created after
        it.
      </p>
      <ErrorBanner message={error} onClose={() => setError(null)} />
      <div className="form-grid-2">
        <div className="form-group">
          <label htmlFor="vm-bridge">Network bridge</label>
          <select id="vm-bridge" value={bridge} onChange={(e) => setBridge(e.target.value)} disabled={!host}>
            <option value="">{auto(!bridge ? host?.bridge : null)}</option>
            {(host?.bridges ?? []).map((b) => (
              <option key={b.name} value={b.name}>
                {b.name}
                {b.cidr ? ` · ${b.cidr}` : ""}
                {b.comments ? ` · ${b.comments}` : ""}
              </option>
            ))}
          </select>
          <div className="form-hint">
            Automatic is ggNet's own interface ({host?.service_interface ?? "—"}) when it is a bridge, else the
            host's first bridge.
          </div>
        </div>
        <div className="form-group">
          <label htmlFor="vm-storage">Storage for the EFI and TPM state</label>
          <select id="vm-storage" value={storage} onChange={(e) => setStorage(e.target.value)} disabled={!host}>
            <option value="">{auto(!storage ? host?.storage : null)}</option>
            {(host?.storages ?? []).map((s) => (
              <option key={s.name} value={s.name}>
                {s.name}
                {s.type ? ` · ${s.type}` : ""}
                {s.pool ? ` · ${s.pool}` : ""}
              </option>
            ))}
          </select>
          <div className="form-hint">A few MB per VM. The drives themselves are ggNet's writebacks.</div>
        </div>
        <div className="form-group">
          <label htmlFor="vm-reserved">Memory kept for the host (GB)</label>
          <input
            id="vm-reserved"
            type="number"
            min={0}
            value={reserved}
            onChange={(e) => setReserved(Number(e.target.value))}
          />
          <div className="form-hint">
            For Proxmox, ZFS and ggNet; a VM never gets more than the rest
            {limit !== null && host?.memory_mb ? ` (now ${Math.floor(limit / GB)} of ${Math.round(host.memory_mb / GB)} GB)` : ""}.
          </div>
        </div>
      </div>
      <div className="actions-row">
        <button type="submit" disabled={busy !== null || !host}>
          <Save size={14} />
          {busy === "save" ? "Saving…" : "Save"}
        </button>
        {saved && <span className="save-success-tag">Saved</span>}
      </div>
    </form>
  );
}
