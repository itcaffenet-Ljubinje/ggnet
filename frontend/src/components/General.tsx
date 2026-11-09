import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Power, RotateCw, Server, TriangleAlert, XCircle } from "lucide-react";

import { api, type General as GeneralInfo, type HostService } from "../api";
import { formatBytes, formatDuration } from "../format";
import { ErrorBanner } from "./ErrorBanner";
import { Modal } from "./Modal";

const REBOOT_WORD = "REBOOT";

/**
 * Settings → General: this server at a glance, ggNet's services with a
 * Restart each (never the iSCSI target), and a server reboot that says what
 * it interrupts and wants the word REBOOT.
 */
export function General({ canManage = true }: { canManage?: boolean }) {
  const [info, setInfo] = useState<GeneralInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [rebooting, setRebooting] = useState(false);

  const load = useCallback(async () => {
    try {
      setInfo(await api.getGeneral());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function restart(s: HostService) {
    if (!window.confirm(`Restart ${s.label} (${s.unit})?${s.label === "ggNet" ? "\n\nThe web UI is back in a few seconds; the PCs keep their disks." : ""}`))
      return;
    setBusy(s.unit);
    setNotice(null);
    try {
      const r = await api.restartService(s.unit);
      if (r.restarting_self) {
        setNotice("ggNet is restarting; this page reloads its data in a few seconds.");
        window.setTimeout(load, 8000);
      } else {
        setNotice(`${s.label} restarted.`);
        await load();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <div className="card" aria-label="Server">
        <h3 className="card-title section-title">
          <Server size={15} /> Server
        </h3>
        <ErrorBanner message={error} onClose={() => setError(null)} />
        {notice && (
          <p className="small" role="status">
            {notice}
          </p>
        )}
        {info && (
          <dl className="details">
            <dt>Name</dt>
            <dd>
              {info.hostname} <span className="mono muted">{info.server_ip}</span>
            </dd>
            <dt>ggNet</dt>
            <dd>{info.version}</dd>
            <dt>Proxmox VE</dt>
            <dd className="small">{info.proxmox ?? "—"}</dd>
            <dt>Kernel</dt>
            <dd className="mono small">{info.kernel}</dd>
            <dt>Up for</dt>
            <dd>{info.uptime_seconds !== null ? formatDuration(info.uptime_seconds * 1000) : "—"}</dd>
            <dt>Database</dt>
            <dd>
              {info.database.kind === "postgresql" ? "PostgreSQL" : "SQLite"} {info.database.version ?? ""}
              {info.database.size_bytes !== null && ` · ${formatBytes(info.database.size_bytes)}`}
              <div className="small muted">
                {info.database.backups.length > 0
                  ? `Last backup before an update: ${new Date(info.database.backups[0].at).toLocaleString()} ` +
                    `(${info.database.backups[0].name})`
                  : "No backup yet: one is taken before every update."}
              </div>
            </dd>
          </dl>
        )}
      </div>

      <div className="card" aria-label="Services">
        <h3 className="card-title section-title">
          <RotateCw size={15} /> Services
        </h3>
        {info && (
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Service</th>
                  <th>State</th>
                  <th>Since</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {info.services.map((s) => (
                  <tr key={s.unit}>
                    <td>
                      <div className="strong">{s.label}</div>
                      <div className="small muted">
                        <span className="mono">{s.unit}</span> · {s.purpose}
                      </div>
                    </td>
                    <td>
                      <ServiceState service={s} />
                    </td>
                    <td className="small">{s.since ? new Date(s.since).toLocaleString() : "—"}</td>
                    <td className="th-actions">
                      {s.restartable ? (
                        canManage && (
                          <button
                            type="button"
                            className="secondary small"
                            disabled={busy !== null}
                            onClick={() => restart(s)}
                            aria-label={`Restart ${s.label}`}
                          >
                            <RotateCw size={13} />
                            {busy === s.unit ? "Restarting…" : "Restart"}
                          </button>
                        )
                      ) : (
                        <span className="small muted" title={s.note ?? undefined}>
                          not from here
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {canManage && info && (
        <div className="card" aria-label="Reboot the server">
          <h3 className="card-title section-title">
            <Power size={15} /> Reboot the server
          </h3>
          <p className="small muted">
            This server is the Proxmox VE host: a reboot also stops its virtual machines and containers, and every
            PC that is on loses its disks until the server is back.
          </p>
          <button type="button" className="danger" onClick={() => setRebooting(true)}>
            <Power size={14} />
            Reboot…
          </button>
        </div>
      )}
      {rebooting && info && <RebootDialog info={info} onClose={() => setRebooting(false)} />}
    </>
  );
}

function ServiceState({ service: s }: { service: HostService }) {
  if (s.state === "active")
    return (
      <span className="badge badge-provisioned">
        <CheckCircle2 size={12} /> {s.sub_state === "exited" ? "done" : "running"}
      </span>
    );
  if (s.state === "failed")
    return (
      <span className="badge badge-error">
        <XCircle size={12} /> failed
      </span>
    );
  return <span className="badge">{s.state}</span>;
}

function RebootDialog({ info, onClose }: { info: GeneralInfo; onClose: () => void }) {
  const [word, setWord] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const blocked = info.running_tasks.length > 0;

  async function reboot() {
    setError(null);
    try {
      await api.rebootServer(word);
      setSent(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <Modal label="Reboot the server" onClose={onClose}>
      <div className="modal-header">
        <h2 className="title-with-icon">
          <TriangleAlert size={18} className="warn-text" /> Reboot the server
        </h2>
      </div>
      <div className="modal-body">
        <ErrorBanner message={error} onClose={() => setError(null)} />
        {sent ? (
          <p role="status">
            The server reboots in a few seconds. The web UI is back when it has started (usually 2–5 minutes).
          </p>
        ) : (
          <>
            {blocked && (
              <p className="error-text small">
                Running now: {info.running_tasks.join(", ")}. Wait for it to finish, or cancel it, first.
              </p>
            )}
            <p className="small">It stops:</p>
            <ul className="small">
              <li>
                {info.online_pcs.length > 0
                  ? `${info.online_pcs.length} PC(s) that are on lose their disks: ${info.online_pcs.join(", ")}`
                  : "No PC is on."}
              </li>
              <li>
                {info.guests.length > 0
                  ? `Virtual machines and containers: ${info.guests.join(", ")}`
                  : "No virtual machine or container is running."}
              </li>
            </ul>
            <label>
              Type {REBOOT_WORD} to confirm
              <input
                value={word}
                onChange={(e) => setWord(e.target.value)}
                aria-label="Confirmation word"
                autoComplete="off"
              />
            </label>
          </>
        )}
      </div>
      <div className="modal-footer">
        <button type="button" className="secondary" onClick={onClose}>
          {sent ? "Close" : "Cancel"}
        </button>
        {!sent && (
          <button type="button" className="danger" disabled={blocked || word !== REBOOT_WORD} onClick={reboot}>
            <Power size={14} />
            Reboot now
          </button>
        )}
      </div>
    </Modal>
  );
}
