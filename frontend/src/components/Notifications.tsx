import { useCallback, useEffect, useRef, useState } from "react";
import { Bell, BellRing, CheckCircle2, Loader2, MinusCircle, X, XCircle } from "lucide-react";

import { api, type ServerTask } from "../api";
import { formatBytes, formatDuration } from "../format";
import { clearUploads, useUploads, type Upload } from "../uploads";

const POLL_BUSY_MS = 3000;
const POLL_IDLE_MS = 15000;
const TOAST_MS = 8000;
const KEEP = 50;
// What ended, kept in this browser until the admin clears it.
const STORE = "ggnet:notifications";
const CLEARED = "ggnet:notifications-cleared";
const SEEN = "ggnet:notifications-seen";

const running = (s: string) => s === "running" || s === "queued";

export interface Finished {
  id: string;
  title: string;
  status: "done" | "failed" | "cancelled";
  error: string | null;
  detail: string | null;
  at: string;
}

interface Toast {
  id: string;
  text: string;
  ok: boolean;
}

function read<T>(key: string, empty: T): T {
  try {
    const v = window.localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : empty;
  } catch {
    return empty;
  }
}

function write(key: string, value: unknown) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // private window or blocked storage: the list lives until the page reloads
  }
}

/** "19.4 MB/s · 6 min left" from what was done since the start. */
function speed(done: number | null, total: number | null, startedAt: number | null): string | null {
  if (!done || !total || !startedAt) return null;
  const seconds = (Date.now() - startedAt) / 1000;
  if (seconds < 5 || done <= 0) return null;
  const rate = done / seconds;
  return `${formatBytes(rate)}/s · ${formatDuration((Math.max(0, total - done) / rate) * 1000)} left`;
}

/**
 * The header's Notifications: what runs on the server (backups, restores,
 * copies, imports, Add new hardware) and the files this browser uploads,
 * with their progress, wherever one is in the web UI; below, what ended,
 * kept until Clear. A short notice says when a task ends.
 */
export function Notifications({ onFinished }: { onFinished?: () => void }) {
  const [tasks, setTasks] = useState<ServerTask[]>([]);
  const [finished, setFinished] = useState<Finished[]>(() => read<Finished[]>(STORE, []));
  const [seenAt, setSeenAt] = useState<string>(() => read<string>(SEEN, ""));
  const [open, setOpen] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [cancelling, setCancelling] = useState<Set<string>>(new Set());
  const uploads = useUploads();
  const statuses = useRef<Map<string, string>>(new Map());
  const first = useRef(true);

  const toast = useCallback((id: string, text: string, ok: boolean) => {
    setToasts((t) => [...t.filter((x) => x.id !== id), { id, text, ok }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), TOAST_MS);
  }, []);

  const remember = useCallback((items: Finished[]) => {
    if (items.length === 0) return;
    const cleared = new Set(read<string[]>(CLEARED, []));
    setFinished((list) => {
      const known = new Set(list.map((f) => f.id));
      const next = [...items.filter((f) => !known.has(f.id) && !cleared.has(f.id)), ...list].slice(0, KEEP);
      write(STORE, next);
      return next;
    });
  }, []);

  const load = useCallback(async () => {
    let list: ServerTask[];
    try {
      list = await api.getTasks();
    } catch {
      return; // only a view: the pages show errors
    }
    let ended = false;
    const done: Finished[] = [];
    for (const t of list) {
      const before = statuses.current.get(t.id);
      if (!running(t.status)) {
        done.push({
          id: t.id, title: t.title, status: t.status as Finished["status"], error: t.error, detail: t.detail,
          at: t.finished_at ?? new Date().toISOString(),
        });
        if (!first.current && before && running(before)) {
          ended = true;
          toast(t.id, `${t.title}: ${t.status === "done" ? "completed" : t.status}`, t.status === "done");
        }
      }
      statuses.current.set(t.id, t.status);
    }
    // A copy or an import leaves the list when the image is published: it is done.
    for (const [id, status] of statuses.current) {
      if (running(status) && !list.some((t) => t.id === id)) {
        ended = true;
        statuses.current.delete(id);
      }
    }
    first.current = false;
    remember(done);
    setTasks(list);
    if (ended) onFinished?.();
  }, [toast, remember, onFinished]);

  const busy = tasks.some((t) => running(t.status)) || uploads.some((u) => u.status === "running");
  useEffect(() => {
    load();
    const timer = window.setInterval(load, busy ? POLL_BUSY_MS : POLL_IDLE_MS);
    return () => window.clearInterval(timer);
  }, [load, busy]);

  // An upload that ends is a notification too.
  const uploadStatus = useRef<Map<number, string>>(new Map());
  useEffect(() => {
    for (const u of uploads) {
      const before = uploadStatus.current.get(u.id);
      if (before === "running" && u.status !== "running") {
        const ok = u.status === "done";
        toast(`upload-${u.id}`, `Upload of ${u.name}: ${ok ? "completed" : "failed"}`, ok);
        remember([{ id: `upload-${u.started}-${u.name}`, title: `Upload of ${u.name}`, status: ok ? "done" : "failed",
                    error: u.error, detail: "from this browser into the imports folder", at: new Date().toISOString() }]);
      }
      uploadStatus.current.set(u.id, u.status);
    }
  }, [uploads, toast, remember]);

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next) {
      const now = new Date().toISOString();
      setSeenAt(now);
      write(SEEN, now);
    }
  }

  function clear() {
    write(CLEARED, [...read<string[]>(CLEARED, []), ...finished.map((f) => f.id)].slice(-500));
    write(STORE, []);
    setFinished([]);
    clearUploads();
  }

  async function cancel(t: ServerTask) {
    if (!t.cancel || !window.confirm(`Stop ${t.title}?`)) return;
    setCancelling(new Set(cancelling).add(t.id));
    try {
      await api.cancelTask(t.cancel);
    } catch (e) {
      toast(`${t.id}-cancel`, e instanceof Error ? e.message : String(e), false);
    }
    await load();
  }

  const active = tasks.filter((t) => running(t.status));
  const activeUploads = uploads.filter((u) => u.status === "running");
  const count = active.length + activeUploads.length;
  const unread = finished.some((f) => f.at > seenAt);
  const label = count > 0 ? `Notifications: ${count} running` : unread ? "Notifications: new" : "Notifications";
  return (
    <div className="tasks-wrap">
      <button type="button" className={`icon-btn tasks-btn ${count > 0 ? "busy" : ""}`} onClick={toggle}
              aria-label={label} aria-expanded={open} title="Notifications">
        {count > 0 ? <Loader2 size={15} className="spin" /> : unread ? <BellRing size={15} /> : <Bell size={15} />}
        {count > 0 && <span className="tab-badge">{count}</span>}
        {count === 0 && unread && <span className="notify-dot" aria-hidden />}
      </button>

      {open && (
        <div className="tasks-panel" role="dialog" aria-label="Notifications">
          <div className="tasks-panel-head">
            <span className="strong">Notifications</span>
            <button type="button" className="icon-btn" onClick={() => setOpen(false)} aria-label="Close">
              <X size={14} />
            </button>
          </div>

          {count === 0 && finished.length === 0 && <p className="small muted">Nothing yet.</p>}

          {count > 0 && (
            <>
              <div className="tasks-section">Running</div>
              <ul className="tasks-list">
                {activeUploads.map((u) => (
                  <UploadRow key={`u${u.id}`} upload={u} />
                ))}
                {active.map((t) => (
                  <li key={t.id} className="task">
                    <div className="task-line">
                      <span className="strong">{t.title}</span>
                      <span className="small muted">
                        {t.status === "queued" ? "queued" : t.percent !== null ? `${t.percent}%` : "running"}
                      </span>
                    </div>
                    {t.detail && <div className="small muted">{t.detail}</div>}
                    <Bar percent={t.status === "queued" ? 0 : t.percent} indeterminate={t.percent === null} />
                    {t.status === "running" && t.done_bytes !== null && t.total_bytes ? (
                      <div className="small muted">
                        {formatBytes(t.done_bytes)} of {formatBytes(t.total_bytes)}
                        {(() => {
                          const s = speed(t.done_bytes, t.total_bytes, t.started_at ? Date.parse(t.started_at) : null);
                          return s ? ` · ${s}` : "";
                        })()}
                      </div>
                    ) : null}
                    {t.cancel && (
                      <button type="button" className="secondary small" disabled={cancelling.has(t.id)}
                              onClick={() => cancel(t)}>
                        {cancelling.has(t.id) ? "Cancelling…" : "Cancel"}
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}

          {finished.length > 0 && (
            <>
              <div className="tasks-section tasks-section-row">
                <span>Finished</span>
                <button type="button" className="link-btn small" onClick={clear}>
                  Clear
                </button>
              </div>
              <ul className="tasks-list">
                {finished.map((f) => (
                  <li key={f.id} className={`task task-${f.status} ${f.at > seenAt ? "task-new" : ""}`}>
                    <div className="task-line">
                      <span className="title-with-icon strong">
                        {f.status === "done" ? (
                          <CheckCircle2 size={14} className="ok-icon" aria-label="Completed" />
                        ) : f.status === "failed" ? (
                          <XCircle size={14} className="error-icon" aria-label="Failed" />
                        ) : (
                          <MinusCircle size={14} className="muted" aria-label="Cancelled" />
                        )}
                        {f.title}
                      </span>
                      <span className="small muted">{new Date(f.at).toLocaleString()}</span>
                    </div>
                    {f.detail && <div className="small muted">{f.detail}</div>}
                    {f.error && f.status === "failed" && <div className="small error-text">{f.error}</div>}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.ok ? "toast-ok" : "toast-error"}`} role="status">
            {t.ok ? <CheckCircle2 size={15} /> : <XCircle size={15} />}
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Bar({ percent, indeterminate }: { percent: number | null; indeterminate?: boolean }) {
  return (
    <div className={`task-bar ${indeterminate ? "indeterminate" : ""}`} role="progressbar"
         aria-valuenow={percent ?? undefined} aria-valuemin={0} aria-valuemax={100}>
      <div className="task-bar-fill" style={{ width: indeterminate ? undefined : `${percent ?? 0}%` }} />
    </div>
  );
}

function UploadRow({ upload: u }: { upload: Upload }) {
  const percent = u.total ? Math.floor((100 * u.sent) / u.total) : 0;
  return (
    <li className="task">
      <div className="task-line">
        <span className="strong">Upload of {u.name}</span>
        <span className="small muted">{percent}%</span>
      </div>
      <div className="small muted">from this browser into the imports folder</div>
      <Bar percent={percent} />
      <div className="small muted">
        {formatBytes(u.sent)} of {formatBytes(u.total)}
        {(() => {
          const s = speed(u.sent, u.total, u.started);
          return s ? ` · ${s}` : "";
        })()}
        {" · keep this tab open"}
      </div>
    </li>
  );
}
