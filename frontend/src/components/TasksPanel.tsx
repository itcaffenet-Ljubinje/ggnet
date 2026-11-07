import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, ListTodo, Loader2, X, XCircle } from "lucide-react";

import { api, type ServerTask } from "../api";
import { formatBytes, formatDuration } from "../format";
import { clearUploads, useUploads, type Upload } from "../uploads";

const POLL_BUSY_MS = 3000;
const POLL_IDLE_MS = 15000;
const TOAST_MS = 8000;

const running = (s: string) => s === "running" || s === "queued";

interface Toast {
  id: string;
  text: string;
  ok: boolean;
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
 * The header's Background tasks button: what runs on the server (backups,
 * restores, copies, imports, Add new hardware) and the files this browser
 * uploads, with their progress, wherever one is in the web UI. A short
 * notice says when one ends.
 */
export function TasksPanel({ onFinished }: { onFinished?: () => void }) {
  const [tasks, setTasks] = useState<ServerTask[]>([]);
  const [open, setOpen] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [cancelling, setCancelling] = useState<Set<string>>(new Set());
  const uploads = useUploads();
  const seen = useRef<Map<string, string>>(new Map());
  const first = useRef(true);

  const notify = useCallback((id: string, text: string, ok: boolean) => {
    setToasts((t) => [...t.filter((x) => x.id !== id), { id, text, ok }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), TOAST_MS);
  }, []);

  const load = useCallback(async () => {
    let list: ServerTask[];
    try {
      list = await api.getTasks();
    } catch {
      return; // the panel is only a view; the pages show errors
    }
    let ended = false;
    for (const t of list) {
      const before = seen.current.get(t.id);
      if (!first.current && before && running(before) && !running(t.status)) {
        ended = true;
        notify(t.id, `${t.title}: ${t.status === "done" ? "completed" : t.status}`, t.status === "done");
      }
      seen.current.set(t.id, t.status);
    }
    // A running task that is gone from the list ended too (an import or copy is published).
    for (const [id, status] of seen.current) {
      if (running(status) && !list.some((t) => t.id === id)) {
        ended = true;
        seen.current.delete(id);
      }
    }
    first.current = false;
    setTasks(list);
    if (ended) onFinished?.();
  }, [notify, onFinished]);

  const busy = tasks.some((t) => running(t.status)) || uploads.some((u) => u.status === "running");
  useEffect(() => {
    load();
    const timer = window.setInterval(load, busy ? POLL_BUSY_MS : POLL_IDLE_MS);
    return () => window.clearInterval(timer);
  }, [load, busy]);

  // An upload that ends says so too.
  const uploadSeen = useRef<Map<number, string>>(new Map());
  useEffect(() => {
    for (const u of uploads) {
      const before = uploadSeen.current.get(u.id);
      if (before === "running" && u.status !== "running")
        notify(`upload-${u.id}`, `Upload of ${u.name}: ${u.status === "done" ? "completed" : "failed"}`, u.status === "done");
      uploadSeen.current.set(u.id, u.status);
    }
  }, [uploads, notify]);

  async function cancel(t: ServerTask) {
    if (!t.cancel || !window.confirm(`Stop ${t.title}?`)) return;
    setCancelling(new Set(cancelling).add(t.id));
    try {
      await api.cancelTask(t.cancel);
    } catch (e) {
      notify(`${t.id}-cancel`, e instanceof Error ? e.message : String(e), false);
    }
    await load();
  }

  const count = tasks.filter((t) => running(t.status)).length + uploads.filter((u) => u.status === "running").length;
  return (
    <div className="tasks-wrap">
      <button
        type="button"
        className={`icon-btn tasks-btn ${count > 0 ? "busy" : ""}`}
        onClick={() => setOpen(!open)}
        aria-label={count > 0 ? `Background tasks: ${count} running` : "Background tasks"}
        aria-expanded={open}
        title="Background tasks"
      >
        {count > 0 ? <Loader2 size={15} className="spin" /> : <ListTodo size={15} />}
        {count > 0 && <span className="tab-badge">{count}</span>}
      </button>

      {open && (
        <div className="tasks-panel" role="dialog" aria-label="Background tasks">
          <div className="tasks-panel-head">
            <span className="strong">Background tasks</span>
            <button type="button" className="icon-btn" onClick={() => setOpen(false)} aria-label="Close">
              <X size={14} />
            </button>
          </div>
          {tasks.length === 0 && uploads.length === 0 ? (
            <p className="small muted">Nothing runs in the background.</p>
          ) : (
            <ul className="tasks-list">
              {uploads.map((u) => (
                <UploadRow key={`u${u.id}`} upload={u} />
              ))}
              {tasks.map((t) => (
                <li key={t.id} className={`task task-${t.status}`}>
                  <div className="task-line">
                    <span className="strong">{t.title}</span>
                    <span className="small muted">{label(t)}</span>
                  </div>
                  {t.detail && <div className="small muted">{t.detail}</div>}
                  {running(t.status) && (
                    <Bar percent={t.status === "queued" ? 0 : t.percent} indeterminate={t.percent === null} />
                  )}
                  {t.status === "running" && (
                    <div className="small muted">
                      {t.done_bytes !== null && t.total_bytes ? `${formatBytes(t.done_bytes)} of ${formatBytes(t.total_bytes)}` : ""}
                      {(() => {
                        const s = speed(t.done_bytes, t.total_bytes, t.started_at ? Date.parse(t.started_at) : null);
                        return s ? ` · ${s}` : "";
                      })()}
                    </div>
                  )}
                  {t.error && t.status === "failed" && <div className="small error-text">{t.error}</div>}
                  {t.cancel && (
                    <button
                      type="button"
                      className="secondary small"
                      disabled={cancelling.has(t.id)}
                      onClick={() => cancel(t)}
                    >
                      {cancelling.has(t.id) ? "Cancelling…" : "Cancel"}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
          {uploads.some((u) => u.status !== "running") && (
            <button type="button" className="link-btn small" onClick={clearUploads}>
              Clear finished uploads
            </button>
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

function label(t: ServerTask): string {
  if (t.status === "running") return t.percent !== null ? `${t.percent}%` : "running";
  return { queued: "queued", done: "completed", failed: "failed", cancelled: "cancelled" }[t.status] ?? t.status;
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
    <li className={`task task-${u.status}`}>
      <div className="task-line">
        <span className="strong">Upload of {u.name}</span>
        <span className="small muted">{u.status === "running" ? `${percent}%` : u.status === "done" ? "completed" : "failed"}</span>
      </div>
      <div className="small muted">from this browser into the imports folder</div>
      {u.status === "running" && (
        <>
          <Bar percent={percent} />
          <div className="small muted">
            {formatBytes(u.sent)} of {formatBytes(u.total)}
            {(() => {
              const s = speed(u.sent, u.total, u.started);
              return s ? ` · ${s}` : "";
            })()}
            {" · keep this tab open"}
          </div>
        </>
      )}
      {u.error && <div className="small error-text">{u.error}</div>}
    </li>
  );
}
