import { useEffect, useState, type FormEvent } from "react";

import {
  api,
  type Retention,
  type RetentionAction,
  type RetentionSettings,
  type Storage,
} from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { formatBytes } from "../format";
import { useAsyncAction } from "../useAsyncAction";

interface Props {
  storage: Storage | null;
  reload: () => Promise<void>;
}

const NUMBERS: { key: keyof RetentionSettings; label: string; min: number; max: number; hint: string }[] = [
  {
    key: "reserved_percent",
    label: "Reserved disk space (%)",
    min: 0,
    max: 50,
    hint: "Kept free on the whole pool, so SSDs never run full (ggRock recommends at least 15 %).",
  },
  {
    key: "warning_percent",
    label: "Warning threshold (%)",
    min: 50,
    max: 99,
    hint: "A warning is shown when the pool is fuller than this.",
  },
  {
    key: "unused_snapshot_days",
    label: "Unused versions (days)",
    min: 1,
    max: 3650,
    hint: "A version that is not active, not pinned and no PC runs on is deleted after this many days…",
  },
  {
    key: "keep_newest_snapshots",
    label: "Always keep newest versions",
    min: 1,
    max: 100,
    hint: "…except the newest N versions of each game disk.",
  },
  {
    key: "inactive_writeback_hours",
    label: "Inactive writebacks (hours)",
    min: 1,
    max: 8760,
    hint: "A PC with Keep Writeback that has been off this long loses its writeback.",
  },
];

function settingsOf(r: Retention): RetentionSettings {
  const { saved: _saved, last_run: _last, ...s } = r;
  return s;
}

export function SettingsPage({ storage, reload }: Props) {
  const { busy, error, setError, run } = useAsyncAction();
  const [retention, setRetention] = useState<Retention | null>(null);
  const [form, setForm] = useState<RetentionSettings | null>(null);
  const [preview, setPreview] = useState<RetentionAction[] | null>(null);

  useEffect(() => {
    api
      .getRetention()
      .then((r) => {
        setRetention(r);
        setForm(settingsOf(r));
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [setError]);

  if (!form || !retention) {
    return (
      <section>
        <ErrorBanner message={error} onClose={() => setError(null)} />
        <p className="empty">Loading…</p>
      </section>
    );
  }

  const set = (key: keyof RetentionSettings, value: number | boolean) =>
    setForm({ ...form, [key]: value });

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!form) return;
    const reservedChanges = !retention?.saved || form.reserved_percent !== retention.reserved_percent;
    if (reservedChanges && storage) {
      const bytes = (storage.total_bytes * form.reserved_percent) / 100;
      const msg =
        form.reserved_percent === 0
          ? `Remove the reserved space on pool ${storage.pool}?`
          : `Reserve ${form.reserved_percent} % (${formatBytes(bytes)}) of pool ${storage.pool}?\n\n` +
            "Nothing on the pool can use that space, including the Proxmox VMs and containers on it.";
      if (!window.confirm(msg)) return;
    }
    await run("save", async () => {
      const r = await api.saveRetention(form);
      setRetention(r);
      setForm(settingsOf(r));
    });
    await reload();
  }

  async function showPreview() {
    if (!form) return;
    await run("preview", async () => setPreview(await api.previewRetention(form)));
  }

  async function runNow() {
    const msg =
      "Delete now what the saved settings allow?\n\n" +
      "Old versions and inactive kept writebacks are removed for good. Use Preview first.";
    if (!window.confirm(msg)) return;
    await run("run", async () => {
      await api.runRetention();
      setRetention(await api.getRetention());
      setPreview(null);
    });
    await reload();
  }

  const last = retention.last_run;

  return (
    <section>
      <ErrorBanner message={error} onClose={() => setError(null)} />

      {storage && (
        <div className="card" aria-label="Storage">
          <h3 className="card-title">Pool {storage.pool}</h3>
          <div
            className={storage.warning ? "meter meter-warn" : "meter"}
            role="meter"
            aria-label="Pool used"
            aria-valuenow={storage.used_percent}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <div style={{ width: `${Math.min(storage.used_percent, 100)}%` }} />
          </div>
          <p className="small">
            {storage.used_percent} % used: {formatBytes(storage.used_bytes)} of {formatBytes(storage.total_bytes)},{" "}
            {formatBytes(storage.available_bytes)} free · reserved {formatBytes(storage.reserved_bytes)} · warning
            at {storage.warning_percent} %
          </p>
        </div>
      )}

      <form className="card" onSubmit={save} aria-label="Retention">
        <h3 className="card-title">Snapshots and writebacks retention</h3>
        {!retention.saved && (
          <p className="small warn-text">
            Not saved yet: these are the defaults and nothing has been applied to the host.
          </p>
        )}
        <label className="check">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
          Run automatically every hour
        </label>
        <label className="check">
          <input type="checkbox" checked={form.dry_run} onChange={(e) => set("dry_run", e.target.checked)} />
          Dry run: only record what would be deleted
        </label>
        <div className="settings-grid">
          {NUMBERS.map((n) => (
            <label key={n.key}>
              {n.label}
              <input
                type="number"
                min={n.min}
                max={n.max}
                value={form[n.key] as number}
                onChange={(e) => set(n.key, Number(e.target.value))}
                required
              />
              <span className="muted small">{n.hint}</span>
            </label>
          ))}
        </div>
        <div className="actions-row">
          <button type="submit" disabled={busy !== null}>
            {busy === "save" ? "Saving…" : "Save"}
          </button>
          <button type="button" className="secondary" onClick={showPreview} disabled={busy !== null}>
            {busy === "preview" ? "Checking…" : "Preview"}
          </button>
          <button
            type="button"
            className="danger"
            onClick={runNow}
            disabled={busy !== null || !retention.saved}
            title={retention.saved ? undefined : "Save the settings first"}
          >
            {busy === "run" ? "Running…" : "Run now"}
          </button>
        </div>
      </form>

      {preview && (
        <div className="card" aria-label="Preview">
          <h3 className="card-title">Preview: these settings would delete now</h3>
          <ActionList actions={preview} />
        </div>
      )}

      {last && (
        <div className="card" aria-label="Last run">
          <h3 className="card-title">
            Last run {new Date(last.at).toLocaleString()}
            {last.dry_run && " (dry run)"}
          </h3>
          <ActionList actions={last.actions} />
        </div>
      )}
    </section>
  );
}

function ActionList({ actions }: { actions: RetentionAction[] }) {
  if (actions.length === 0) return <p className="muted small">Nothing to delete.</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>What</th>
          <th>Why</th>
          <th>Result</th>
        </tr>
      </thead>
      <tbody>
        {actions.map((a) => (
          <tr key={`${a.kind}-${a.target}`}>
            <td className="strong">
              {a.kind === "snapshot" ? "Version " : a.kind === "writeback" ? "Writeback of " : ""}
              {a.target}
            </td>
            <td>{a.reason}</td>
            <td>
              {a.error ? (
                <span className="error-text small">{a.error}</span>
              ) : a.done ? (
                <span className="badge badge-provisioned">Deleted</span>
              ) : (
                <span className="muted">—</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
