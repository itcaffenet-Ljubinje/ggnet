import { useEffect, useState, type FormEvent } from "react";
import { Download, Lock, RefreshCw, RotateCw, Save } from "lucide-react";

import { api, type BootSettings, type BootSettingsOut, type GameDisk, type PxeStatus } from "../api";
import { useAsyncAction } from "../useAsyncAction";
import { ErrorBanner } from "./ErrorBanner";

type GatewayChoice = "none" | "dhcp" | "ip";

const choiceOf = (gateway: string): GatewayChoice =>
  gateway === "none" || gateway === "dhcp" ? gateway : "ip";

/**
 * Settings → Network boot: the PXE service (proxyDHCP + TFTP) and what iPXE
 * gets. The router stays the DHCP server; ggNet only answers PXE.
 */
export function NetworkBoot() {
  const { busy, error, setError, run } = useAsyncAction();
  const [status, setStatus] = useState<PxeStatus | null>(null);
  const [saved, setSaved] = useState<BootSettingsOut | null>(null);
  const [choice, setChoice] = useState<GatewayChoice>("none");
  const [ip, setIp] = useState("");
  const [unregistered, setUnregistered] = useState<BootSettings["unregistered"]>("wait");
  const [mokEnroll, setMokEnroll] = useState(false);
  const [mokPassword, setMokPassword] = useState("ggnet");
  const [newPcName, setNewPcName] = useState("pc-{nn}");
  const [defaultGame, setDefaultGame] = useState<GameDisk | null>(null);
  const [done, setDone] = useState(false);

  function show(s: BootSettingsOut) {
    setSaved(s);
    setChoice(choiceOf(s.gateway));
    setIp(choiceOf(s.gateway) === "ip" ? s.gateway : "");
    setUnregistered(s.unregistered);
    setMokEnroll(s.mok_enroll);
    setMokPassword(s.mok_password);
    setNewPcName(s.new_pc_name ?? "pc-{nn}");
  }

  useEffect(() => {
    run("load", async () => {
      show(await api.getBootSettings());
      setStatus(await api.getPxe());
      setDefaultGame((await api.listDisks()).find((d) => d.published && d.kind !== "os" && d.is_default) ?? null);
    });
  }, [run]);

  async function refresh() {
    await run("refresh", async () => setStatus(await api.getPxe()));
  }

  async function restart() {
    const msg =
      `Restart ${status?.unit ?? "the PXE service"}?\n\n` +
      "PCs starting from the network right now try again; running PCs are not affected.";
    if (!window.confirm(msg)) return;
    await run("restart", async () => setStatus(await api.restartPxe()));
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    setDone(false);
    const gateway = choice === "ip" ? ip.trim() : choice;
    const body = {
      gateway,
      unregistered,
      mok_enroll: mokEnroll,
      mok_password: mokPassword,
      new_pc_name: newPcName.trim(),
    };
    const ok = await run("save", async () => show(await api.saveBootSettings(body)));
    if (ok) setDone(true);
  }

  const running = status?.state === "active";
  const missing = status ? Object.entries(status.files).filter(([, there]) => !there).map(([f]) => f) : [];

  return (
    <div className="card" aria-label="Network boot">
      <h3 className="card-title">Network boot (PXE)</h3>
      <ErrorBanner message={error} onClose={() => setError(null)} />

      {status && (
        <div className="pxe-status">
          <span
            className={`badge ${running ? "badge-provisioned" : status.state === "failed" ? "badge-error" : "badge-warn"}`}
          >
            {!status.installed ? "Not installed" : running ? "Running" : status.state === "failed" ? "Failed" : "Stopped"}
          </span>
          <span className="mono small">{status.unit}</span>
          {running && status.since && (
            <span className="muted small">since {new Date(status.since).toLocaleString()}</span>
          )}
          <span className="pxe-actions">
            <button type="button" className="icon-btn" onClick={refresh} disabled={busy !== null} aria-label="Refresh">
              <RefreshCw size={14} />
            </button>
            <button
              type="button"
              className="secondary small"
              onClick={restart}
              disabled={busy !== null || !status.installed}
            >
              <RotateCw size={13} />
              {busy === "restart" ? "Restarting…" : "Restart"}
            </button>
          </span>
        </div>
      )}
      {status && (
        <p className="muted small">
          TFTP {status.tftp_root}:{" "}
          {Object.entries(status.files).map(([f, there], i) => (
            <span key={f}>
              {i > 0 && ", "}
              <span className={there ? undefined : "error-text"}>
                {f} {there ? "✓" : "missing"}
              </span>
            </span>
          ))}
          {missing.length > 0 && " (run the installer again to put them back)"}
        </p>
      )}

      {status && <SecureBoot status={status} />}

      {saved && (
        <form onSubmit={save}>
          {!saved.saved && (
            <p className="small muted">Not saved in the web UI yet: these are the values from config.toml.</p>
          )}
          <div className="settings-grid">
            <label>
              Gateway for Windows (iBFT)
              <select value={choice} onChange={(e) => setChoice(e.target.value as GatewayChoice)}>
                <option value="none">None, 0.0.0.0 (recommended)</option>
                <option value="dhcp">The router's (from DHCP)</option>
                <option value="ip">Another address…</option>
              </select>
              <span className="muted small">
                With the router's gateway some PCs stopped at 0x7B; Windows gets its internet route anyway.
              </span>
            </label>
            {choice === "ip" && (
              <label>
                Gateway address
                <input value={ip} onChange={(e) => setIp(e.target.value)} placeholder="e.g. 192.168.0.40" required />
              </label>
            )}
            <label>
              Unregistered PCs
              <select
                value={unregistered}
                onChange={(e) => setUnregistered(e.target.value as BootSettings["unregistered"])}
              >
                <option value="wait">Wait on screen until added</option>
                <option value="local">Boot from their own disk</option>
              </select>
              <span className="muted small">A waiting PC boots as soon as it is added in Machines → New PCs.</span>
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={mokEnroll}
                onChange={(e) => setMokEnroll(e.target.checked)}
                disabled={!status?.secure_boot}
              />
              Prepare the Secure Boot key on PCs (no USB)
            </label>
            {mokEnroll && (
              <label>
                MokManager password
                <input
                  value={mokPassword}
                  onChange={(e) => setMokPassword(e.target.value)}
                  pattern="[a-z0-9]{4,16}"
                  title="4 to 16 lower-case letters or digits (MokManager's keyboard is US)"
                  required
                />
                <span className="muted small">
                  The agent of each Boot Mode PC without the key asks for it. At its next boot the blue MokManager
                  screen opens: Enroll MOK → Continue → Yes, type this password, Reboot. Until then MokManager waits
                  10 s on every boot, so turn this off when the PCs are done.
                </span>
              </label>
            )}
            <h3 className="card-title">New PCs (Add new hardware)</h3>
            <label>
              Name
              <input
                value={newPcName}
                onChange={(e) => setNewPcName(e.target.value)}
                pattern="[A-Za-z0-9-]*\{nn?\}[A-Za-z0-9-]*"
                title="With {nn} (01, 02 …) or {n} (1, 2 …) for the PC's number, e.g. komp-{nn}"
                required
              />
            </label>
            <p className="small">
              Game disk:{" "}
              {defaultGame ? (
                <span className="strong">{defaultGame.name}</span>
              ) : (
                <span className="warn-text">none, register new PCs by hand</span>
              )}{" "}
              <span className="muted">
                (the default game disk: Images → ⋮ → Make default). While Add new hardware is open on an image, a PC
                ggNet does not know yet that boots from the network is registered with this name (the first free
                number) and the default game disk, in Boot Mode on that image, and goes straight through the wizard.
              </span>
            </p>
          </div>
          <div className="actions-row">
            <button type="submit" disabled={busy !== null}>
              <Save size={14} />
              {busy === "save" ? "Saving…" : "Save"}
            </button>
            {done && <span className="save-success-tag">Saved: applies to the next PC that boots</span>}
          </div>
        </form>
      )}
    </div>
  );
}

/**
 * Secure Boot: shim (signed by Microsoft) starts iPXE signed with the ggNet
 * key. Each PC with Secure Boot on accepts that key once in MokManager.
 */
function SecureBoot({ status }: { status: PxeStatus }) {
  const key = status.secure_boot;
  if (!key) {
    return (
      <div className="secure-boot" aria-label="Secure Boot">
        <h4 className="section-title">
          <Lock size={14} /> Secure Boot
        </h4>
        <p className="muted small">
          No Secure Boot key on this server yet: run the installer again to make it and sign iPXE.
        </p>
      </div>
    );
  }
  return (
    <div className="secure-boot" aria-label="Secure Boot">
      <h4 className="section-title">
        <Lock size={14} /> Secure Boot
      </h4>
      <dl className="details">
        <dt>Key</dt>
        <dd>{key.subject}</dd>
        <dt>Fingerprint (SHA-256)</dt>
        <dd className="mono small">{key.fingerprint}</dd>
        <dt>Expires</dt>
        <dd>{key.expires ? new Date(key.expires).toLocaleDateString() : "—"}</dd>
        <dt>iPXE for shim</dt>
        <dd>
          {key.ipxe_signed === true ? (
            <span className="badge badge-provisioned">Signed with this key</span>
          ) : key.ipxe_signed === false ? (
            <span className="badge badge-error">Not signed with this key: run the installer again</span>
          ) : (
            <span className="badge badge-warn">Missing</span>
          )}
        </dd>
      </dl>
      <p className="small">
        <a className="button-link" href="/api/v1/boot/secureboot.cer" download="ggnet-sb.cer">
          <Download size={14} /> Download certificate (ggnet-sb.cer)
        </a>
      </p>
      <p className="muted small">
        Once per PC: copy the certificate to a FAT32 USB stick, turn Secure Boot on in the BIOS and boot
        from the network. In the blue MokManager screen choose Enroll key from disk, pick ggnet-sb.cer,
        check that the fingerprint matches, confirm and reboot.
      </p>
    </div>
  );
}
