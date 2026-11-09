import { useCallback, useEffect, useState, type FormEvent } from "react";
import { History, KeyRound, Trash2, UserPlus, Users as UsersIcon } from "lucide-react";

import { api, type Activity, type User, type UserRole } from "../api";
import { useAsyncAction } from "../useAsyncAction";
import { ErrorBanner } from "./ErrorBanner";

/** Settings → Users (admin): who may sign in, with which role. */
export function UsersCard({ me }: { me: User }) {
  const { busy, error, setError, run } = useAsyncAction();
  const [users, setUsers] = useState<User[]>([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<UserRole>("staff");

  const load = useCallback(() => run("load", async () => setUsers(await api.listUsers())), [run]);
  useEffect(() => {
    load();
  }, [load]);

  async function add(e: FormEvent) {
    e.preventDefault();
    const ok = await run("add", () => api.createUser(username.trim(), password, role));
    if (ok) {
      setUsername("");
      setPassword("");
      await load();
    }
  }

  async function change(u: User, body: Parameters<typeof api.updateUser>[1]) {
    await run(`user-${u.id}`, () => api.updateUser(u.id, body));
    await load();
  }

  async function resetPassword(u: User) {
    const password = window.prompt(`New password for ${u.username} (at least 8 characters). They are signed out.`);
    if (!password) return;
    await change(u, { password });
  }

  async function remove(u: User) {
    if (!window.confirm(`Delete user ${u.username}?`)) return;
    await run(`user-${u.id}`, () => api.deleteUser(u.id));
    await load();
  }

  return (
    <div className="card" aria-label="Users">
      <h3 className="card-title section-title">
        <UsersIcon size={15} /> Users
      </h3>
      <p className="muted small">
        Admins can do everything. Staff see everything and can turn PCs on, shut them down and restart them.
      </p>
      <ErrorBanner message={error} onClose={() => setError(null)} />
      <table>
        <thead>
          <tr>
            <th>User</th>
            <th>Role</th>
            <th>Last sign-in</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id} className={u.disabled ? "muted" : undefined}>
              <td className="strong">
                {u.username}
                {u.id === me.id && <span className="muted small"> (you)</span>}
                {u.disabled && <span className="badge">disabled</span>}
              </td>
              <td>
                <select
                  aria-label={`Role of ${u.username}`}
                  value={u.role}
                  onChange={(e) => change(u, { role: e.target.value as UserRole })}
                  disabled={busy !== null}
                >
                  <option value="admin">Admin</option>
                  <option value="staff">Staff</option>
                </select>
              </td>
              <td className="small">{u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "never"}</td>
              <td>
                <div className="actions-cell">
                  <button
                    type="button"
                    className="icon-btn"
                    onClick={() => resetPassword(u)}
                    disabled={busy !== null}
                    aria-label={`New password for ${u.username}`}
                    title="New password"
                  >
                    <KeyRound size={14} />
                  </button>
                  {u.id !== me.id && (
                    <button
                      type="button"
                      className="secondary small"
                      onClick={() => change(u, { disabled: !u.disabled })}
                      disabled={busy !== null}
                    >
                      {u.disabled ? "Enable" : "Disable"}
                    </button>
                  )}
                  {u.id !== me.id && (
                    <button
                      type="button"
                      className="icon-btn icon-btn-danger"
                      onClick={() => remove(u)}
                      disabled={busy !== null}
                      aria-label={`Delete ${u.username}`}
                      title="Delete"
                    >
                      <Trash2 size={14} />
                    </button>
                  )}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form className="form-row" onSubmit={add} aria-label="Add user">
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} required minLength={2} />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            required
            minLength={8}
          />
        </label>
        <label>
          Role
          <select value={role} onChange={(e) => setRole(e.target.value as UserRole)}>
            <option value="staff">Staff</option>
            <option value="admin">Admin</option>
          </select>
        </label>
        <button type="submit" disabled={busy !== null}>
          <UserPlus size={14} />
          {busy === "add" ? "Adding…" : "Add user"}
        </button>
      </form>
    </div>
  );
}

/** Settings → Activity (admin): who changed what, newest first. */
export function ActivityCard() {
  const [entries, setEntries] = useState<Activity[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listActivity(200)
      .then(setEntries)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  return (
    <div className="card" aria-label="Activity">
      <h3 className="card-title section-title">
        <History size={15} /> Activity
        <a className="link-btn small card-title-action" href="/api/v1/activity/export.csv" download>
          Download CSV
        </a>
      </h3>
      <ErrorBanner message={error} onClose={() => setError(null)} />
      {entries && entries.length === 0 && <p className="muted small">Nothing changed yet.</p>}
      {entries && entries.length > 0 && (
        <div className="table-wrapper activity-log">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Who</th>
                <th>What</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((a) => (
                <tr key={a.id}>
                  <td className="small">{new Date(a.at).toLocaleString()}</td>
                  <td>{a.username ?? <span className="muted">—</span>}</td>
                  <td className="mono small">{a.action}</td>
                  <td>
                    <span className={a.status_code < 400 ? "badge badge-provisioned" : "badge badge-error"}>
                      {a.status_code < 400 ? "OK" : a.status_code}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** Settings → Account: one's own password. Every session ends, so sign in again. */
export function AccountCard({ me, onPasswordChanged }: { me: User; onPasswordChanged: () => void }) {
  const { busy, error, setError, run } = useAsyncAction();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (next !== again) {
      setError("The new passwords differ.");
      return;
    }
    const ok = await run("password", () => api.changePassword(current, next));
    if (ok) onPasswordChanged();
  }

  return (
    <form className="card" onSubmit={submit} aria-label="Account">
      <h3 className="card-title section-title">
        <KeyRound size={15} /> Account: {me.username} ({me.role})
      </h3>
      <ErrorBanner message={error} onClose={() => setError(null)} />
      <div className="form-row">
        <label>
          Current password
          <input
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        <label>
          New password
          <input
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            autoComplete="new-password"
            minLength={8}
            required
          />
        </label>
        <label>
          New password again
          <input
            type="password"
            value={again}
            onChange={(e) => setAgain(e.target.value)}
            autoComplete="new-password"
            required
          />
        </label>
        <button type="submit" disabled={busy !== null}>
          {busy ? "Saving…" : "Change password"}
        </button>
      </div>
    </form>
  );
}
