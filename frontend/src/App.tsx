import { useCallback, useEffect, useState } from "react";
import { Clock, Database, Layers, LogOut, Monitor, RefreshCw, Server, Settings as SettingsIcon, UserRound } from "lucide-react";

import {
  api,
  type Discovered,
  type Image,
  type Health,
  type Machine,
  type MachineTraffic,
  type Storage,
  type User,
  SIGNED_OUT,
} from "./api";
import { isOnline } from "./components/AgentState";
import { SignIn } from "./components/SignIn";
import { TasksPanel } from "./components/TasksPanel";
import { ImagesPage } from "./pages/ImagesPage";
import { MachinesPage } from "./pages/MachinesPage";
import { ServerPage } from "./pages/ServerPage";
import { SettingsPage } from "./pages/SettingsPage";

type Tab = "machines" | "disks" | "server" | "settings";

// Machine status changes on the host (reset, errors) are picked up by polling.
const REFRESH_MS = 10_000;

/**
 * Sign-in first: the first admin (with the setup code) right after installing,
 * then everyone signs in. A session that ends on the server (expired, password
 * changed) brings the sign-in back.
 */
export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [setup, setSetup] = useState(false);
  const [checked, setChecked] = useState(false);

  const check = useCallback(() => {
    api
      .authState()
      .then((s) => {
        setSetup(s.setup_required);
        setUser(s.user);
      })
      .catch(() => setUser(null))
      .finally(() => setChecked(true));
  }, []);

  useEffect(() => {
    check();
    const signedOut = () => setUser(null);
    window.addEventListener(SIGNED_OUT, signedOut);
    return () => window.removeEventListener(SIGNED_OUT, signedOut);
  }, [check]);

  async function signOut() {
    await api.logout().catch(() => undefined);
    setUser(null);
  }

  if (!checked) return null;
  if (!user) {
    return (
      <SignIn
        setup={setup}
        onSignedIn={(u) => {
          setSetup(false);
          setUser(u);
        }}
      />
    );
  }
  return <Console user={user} onSignOut={signOut} />;
}

function Console({ user, onSignOut }: { user: User; onSignOut: () => void }) {
  const [tab, setTab] = useState<Tab>("machines");
  const [health, setHealth] = useState<Health | null>(null);
  const [disks, setDisks] = useState<Image[]>([]);
  const [machines, setMachines] = useState<Machine[]>([]);
  const [storage, setStorage] = useState<Storage | null>(null);
  const [traffic, setTraffic] = useState<Record<number, MachineTraffic>>({});
  const [discovered, setDiscovered] = useState<Discovered[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const reload = useCallback(async () => {
    setRefreshing(true);
    try {
      const [d, m] = await Promise.all([api.listDisks(), api.listMachines()]);
      setDisks(d);
      setMachines(m);
      setLastUpdated(new Date());
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setRefreshing(false);
    }
    // The pool can be unreadable while the rest works; it only drives a warning.
    api.getStorage().then(setStorage).catch(() => setStorage(null));
    api
      .machineTraffic()
      .then((t) => setTraffic(Object.fromEntries(t.map((x) => [x.machine_id, x]))))
      .catch(() => setTraffic({}));
    api.listDiscovered().then(setDiscovered).catch(() => setDiscovered([]));
  }, []);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    reload();
    const timer = window.setInterval(reload, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [reload]);

  const online = machines.filter((m) => isOnline(m)).length;
  const tabs: { id: Tab; label: string; icon: typeof Monitor; count?: number }[] = [
    { id: "machines", label: "Machines", icon: Monitor, count: machines.length },
    { id: "disks", label: "Images", icon: Layers, count: disks.length },
    { id: "server", label: "Server", icon: Database },
    { id: "settings", label: "Settings", icon: SettingsIcon },
  ];

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand-section">
          <div className="brand-logo">
            <div className="brand-icon">
              <Server size={18} />
            </div>
            <span>ggNet</span>
          </div>
        </div>

        <nav className="desktop-nav">
          {tabs.map(({ id, label, icon: Icon, count }) => (
            <button
              key={id}
              type="button"
              className={tab === id ? "tab active" : "tab"}
              onClick={() => setTab(id)}
            >
              <Icon size={15} />
              <span>{label}</span>
              {count !== undefined && <span className="tab-badge">{count}</span>}
            </button>
          ))}
        </nav>

        <div className="header-status">
          {storage && (
            <div className="storage-mini" title={`Pool ${storage.pool}: ${storage.used_percent} % used`}>
              <div className="storage-mini-header">
                <span>Storage</span>
                <span>{storage.used_percent}%</span>
              </div>
              <div className="storage-mini-bar">
                <div
                  className="storage-mini-fill"
                  style={{
                    width: `${Math.min(storage.used_percent, 100)}%`,
                    backgroundColor: storage.warning ? "var(--warn)" : "var(--accent)",
                  }}
                />
              </div>
            </div>
          )}

          <div className="status-chip" title={health ? `ggNet ${health.version} · pool ${health.pool}` : undefined}>
            <span className={`status-dot ${health ? "online" : "offline"}`} />
            <span>{health ? `${health.server_ip} · ${online}/${machines.length} online` : "API offline"}</span>
          </div>

          <div className="user-chip" title={`Signed in as ${user.username} (${user.role})`}>
            <UserRound size={14} />
            <span>{user.username}</span>
            {user.role !== "admin" && <span className="badge">{user.role}</span>}
            <button type="button" className="icon-btn" onClick={onSignOut} aria-label="Sign out" title="Sign out">
              <LogOut size={14} />
            </button>
          </div>

          <TasksPanel onFinished={reload} />

          <div className="header-refresh-wrap">
            {lastUpdated && (
              <span className="last-updated" title={lastUpdated.toLocaleString()}>
                <Clock size={12} className="last-updated-icon" />
                <span className="last-updated-time">{lastUpdated.toLocaleTimeString()}</span>
              </span>
            )}
            <button
              type="button"
              className="icon-btn"
              onClick={reload}
              disabled={refreshing}
              title="Refresh"
              aria-label="Refresh"
            >
              <RefreshCw size={14} className={refreshing ? "spin-animation" : ""} />
            </button>
          </div>
        </div>
      </header>

      <main>
        {loadError && (
          <div className="banner banner-error" role="alert">
            Cannot load data: {loadError}
          </div>
        )}
        {storage?.warning && (
          <div className="banner banner-warn" role="alert">
            Pool {storage.pool} is {storage.used_percent} % full (warning at {storage.warning_percent} %). Delete
            old versions or free space before writebacks fill it.
          </div>
        )}
        {tab === "machines" ? (
          <MachinesPage
            disks={disks}
            machines={machines}
            traffic={traffic}
            discovered={discovered}
            reload={reload}
          />
        ) : tab === "disks" ? (
          <ImagesPage disks={disks} machines={machines} reload={reload} />
        ) : tab === "server" ? (
          <ServerPage user={user} pcs={machines.length} />
        ) : (
          <SettingsPage storage={storage} reload={reload} user={user} onPasswordChanged={onSignOut} />
        )}
      </main>

      <nav className="mobile-bottom-bar" aria-label="Navigation">
        {tabs.map(({ id, label, icon: Icon, count }) => (
          <button
            key={id}
            type="button"
            className={`mobile-tab ${tab === id ? "active" : ""}`}
            onClick={() => setTab(id)}
          >
            <div className="mobile-tab-icon-wrap">
              <Icon size={18} />
              {count !== undefined && <span className="mobile-tab-badge">{count}</span>}
            </div>
            <span>{label}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}
