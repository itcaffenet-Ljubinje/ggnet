import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Download, History, RefreshCw, Undo2, XCircle } from "lucide-react";

import { api, type ReleaseInfo, type UpdateHistoryEntry, type UpdateStatus } from "../api";
import { ErrorBanner } from "./ErrorBanner";

const POLL_MS = 3000;

/**
 * Settings → Updates: the installed ggNet, the newest release on GitHub and
 * its notes, and Install. The update runs on the server as its own service
 * and restarts ggNet; the PCs keep running.
 */
export function Updates() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (check = false) => {
    try {
      setStatus(await api.getUpdate(check));
      setError(null);
    } catch (e) {
      // While ggNet restarts during an update, the server does not answer for a moment.
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const running = status?.job?.state === "running";
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [running, load]);

  async function check() {
    setBusy(true);
    await load(true);
    setBusy(false);
  }

  async function downgrade(r: ReleaseInfo) {
    const msg =
      `Go back to ggNet ${r.tag}?\n\n` +
      `The database is put back as it was before this server updated away from ${r.version}: whatever was ` +
      "changed in the web UI since then (machines, images, settings) is lost. Images and writebacks on the " +
      "pool stay as they are. ggNet restarts once; the PCs keep running.";
    if (!window.confirm(msg)) return;
    setBusy(true);
    setError(null);
    try {
      setStatus(await api.startDowngrade(r.tag));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  }

  async function install() {
    const tag = status?.latest?.tag;
    if (!tag) return;
    const msg =
      `Update ggNet ${status.current} to ${tag}?\n\n` +
      "The server downloads it, checks it and installs it; ggNet restarts once (the web UI is away for a " +
      "moment). The PCs keep running: their disks stay connected. Your settings (config.toml) are kept.";
    if (!window.confirm(msg)) return;
    setBusy(true);
    setError(null);
    try {
      setStatus(await api.startUpdate(tag));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  }

  return (
    <div className="card" aria-label="Updates">
      <h3 className="card-title">ggNet updates</h3>
      <ErrorBanner message={running ? null : error} onClose={() => setError(null)} />
      {!status ? (
        <p className="muted small">Loading…</p>
      ) : (
        <>
          <p>
            Installed: <span className="strong">{status.current}</span>
            {status.latest ? (
              <>
                {" · "}newest: <span className="strong">{status.latest.tag}</span>
                {status.latest.published_at && (
                  <span className="muted small"> ({new Date(status.latest.published_at).toLocaleDateString()})</span>
                )}
              </>
            ) : (
              <span className="muted small"> · no release on GitHub yet</span>
            )}
          </p>
          {status.error && <p className="small warn-text">{status.error}</p>}
          {!status.available && status.latest && !running && (
            <p className="small">
              <CheckCircle2 size={13} className="ok-icon" /> This is the newest version.
            </p>
          )}
          {status.available && status.latest && (
            <div className="update-notes">
              <p className="strong">{status.latest.name}</p>
              {status.latest.notes && <pre className="small">{status.latest.notes}</pre>}
              {status.latest.url && (
                <a href={status.latest.url} target="_blank" rel="noreferrer" className="small">
                  Release page
                </a>
              )}
            </div>
          )}
          {status.reason && <p className="small muted">{status.reason}</p>}
          <div className="actions-row">
            <button type="button" className="secondary" onClick={check} disabled={busy || running}>
              <RefreshCw size={14} />
              Check now
            </button>
            {status.available && (
              <button type="button" onClick={install} disabled={busy || !status.can_update}>
                <Download size={14} />
                {running ? "Updating…" : `Install ${status.latest?.tag}`}
              </button>
            )}
          </div>
          {status.job && (
            <div className="update-job" aria-label="Last update">
              <p className="small">
                {status.job.state === "running" ? (
                  <>
                    {status.job.kind === "downgrade" ? "Going back" : "Updating"} to {status.job.version}… the web UI
                    comes back by itself.
                  </>
                ) : status.job.state === "done" ? (
                  <>
                    <CheckCircle2 size={13} className="ok-icon" />{" "}
                    {status.job.kind === "downgrade" ? "Went back" : "Updated"} from {status.job.from_version} to{" "}
                    {status.job.version}
                    {status.job.started_at ? ` (${new Date(status.job.started_at).toLocaleString()})` : ""}.
                  </>
                ) : (
                  <>
                    <XCircle size={13} className="error-icon" /> The update to {status.job.version} failed
                    {status.job.exit !== null ? ` (exit code ${status.job.exit})` : ""}; ggNet keeps running the old
                    version. See the log below.
                  </>
                )}
              </p>
              {status.job.log.length > 0 && <pre className="small update-log">{status.job.log.join("\n")}</pre>}
            </div>
          )}
          {(status.releases?.length ?? 0) > 0 && (
            <Releases releases={status.releases!} busy={busy || running} onDowngrade={downgrade} />
          )}
          {(status.history?.length ?? 0) > 0 && <UpdateHistory history={status.history!} />}
        </>
      )}
    </div>
  );
}

/** Every release with its notes; an older one this server ran can be gone back to. */
function Releases({ releases, busy, onDowngrade }: {
  releases: ReleaseInfo[];
  busy: boolean;
  onDowngrade: (r: ReleaseInfo) => void;
}) {
  return (
    <div aria-label="Releases">
      <h4 className="section-title">Releases</h4>
      <ul className="release-list">
        {releases.map((r) => (
          <li key={r.tag}>
            <details>
              <summary>
                <span className="strong">{r.tag}</span>
                {r.published_at && (
                  <span className="muted small"> · {new Date(r.published_at).toLocaleDateString()}</span>
                )}
                {r.installed && <span className="badge badge-provisioned">installed</span>}
                {r.can_downgrade && (
                  <button
                    type="button"
                    className="secondary small"
                    disabled={busy}
                    onClick={(e) => {
                      e.preventDefault();
                      onDowngrade(r);
                    }}
                    aria-label={`Go back to ${r.tag}`}
                  >
                    <Undo2 size={13} /> Go back to {r.tag}
                  </button>
                )}
              </summary>
              {r.notes ? <pre className="small">{r.notes}</pre> : <p className="small muted">No notes.</p>}
              {r.url && (
                <a href={r.url} target="_blank" rel="noreferrer" className="small">
                  Release page
                </a>
              )}
            </details>
          </li>
        ))}
      </ul>
      <p className="small muted">
        Going back is offered for versions this server ran: before each update the database is saved
        (/var/lib/ggnet/db-backups, the last 5), and going back puts that copy back.
      </p>
    </div>
  );
}

/** Updates and downgrades done here, newest first, each with its log. */
function UpdateHistory({ history }: { history: UpdateHistoryEntry[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const [log, setLog] = useState<string[] | null>(null);

  async function show(id: string) {
    if (open === id) {
      setOpen(null);
      return;
    }
    setOpen(id);
    setLog(null);
    setLog(await api.updateLog(id).catch(() => ["(the log cannot be read)"]));
  }

  return (
    <div aria-label="Previous updates">
      <h4 className="section-title title-with-icon">
        <History size={14} aria-hidden /> Previous updates
      </h4>
      <div className="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>What</th>
              <th>By</th>
              <th>Result</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {history.map((h) => (
              <tr key={h.id}>
                <td className="small">{h.started_at ? new Date(h.started_at).toLocaleString() : "—"}</td>
                <td>
                  {h.kind === "downgrade" ? "Back" : "Update"} {h.from_version} → {h.version}
                </td>
                <td className="small">{h.by ?? "—"}</td>
                <td>
                  {h.state === "done" ? (
                    <span className="badge badge-provisioned">done</span>
                  ) : h.state === "running" ? (
                    <span className="badge badge-blue">running</span>
                  ) : h.state === "unknown" ? (
                    <span className="badge" title="It ended under an older ggNet, which did not record how: see the log">
                      ended
                    </span>
                  ) : (
                    <span className="badge badge-error">failed{h.exit !== null ? ` (${h.exit})` : ""}</span>
                  )}
                </td>
                <td>
                  <button type="button" className="link-btn small" onClick={() => show(h.id)}>
                    {open === h.id ? "Hide log" : "Log"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {open && log && <pre className="small update-log">{log.join("\n")}</pre>}
    </div>
  );
}

/** For admins, a line under the header when a newer ggNet is out (checked by the server hourly). */
export function UpdateBanner({ onOpen }: { onOpen: () => void }) {
  const [tag, setTag] = useState<string | null>(null);
  useEffect(() => {
    api
      .getUpdate()
      .then((s) => setTag(s.available && s.latest ? s.latest.tag : null))
      .catch(() => setTag(null));
  }, []);
  if (!tag) return null;
  return (
    <div className="banner banner-info" role="status">
      ggNet {tag} is available.{" "}
      <button type="button" className="link-btn" onClick={onOpen}>
        Settings → Updates
      </button>
    </div>
  );
}
