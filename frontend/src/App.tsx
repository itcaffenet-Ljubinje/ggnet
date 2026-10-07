import { useCallback, useEffect, useState } from "react";

import { api, type GameDisk, type Health, type Machine } from "./api";
import { GameDisksPage } from "./pages/GameDisksPage";
import { MachinesPage } from "./pages/MachinesPage";

type Tab = "machines" | "disks";

// Machine status changes on the host (reset, errors) are picked up by polling.
const REFRESH_MS = 10_000;

export function App() {
  const [tab, setTab] = useState<Tab>("machines");
  const [health, setHealth] = useState<Health | null>(null);
  const [disks, setDisks] = useState<GameDisk[]>([]);
  const [machines, setMachines] = useState<Machine[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const [d, m] = await Promise.all([api.listDisks(), api.listMachines()]);
      setDisks(d);
      setMachines(m);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    reload();
    const timer = window.setInterval(reload, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [reload]);

  return (
    <div className="app">
      <header>
        <h1>ggNet</h1>
        <nav>
          <button
            type="button"
            className={tab === "machines" ? "tab active" : "tab"}
            onClick={() => setTab("machines")}
          >
            Machines ({machines.length})
          </button>
          <button
            type="button"
            className={tab === "disks" ? "tab active" : "tab"}
            onClick={() => setTab("disks")}
          >
            Game disks ({disks.length})
          </button>
        </nav>
        <div className="muted small">
          {health ? `v${health.version} · pool ${health.pool} · ${health.server_ip}` : "API offline"}
        </div>
      </header>

      <main>
        {loadError && (
          <div className="banner banner-error" role="alert">
            Cannot load data: {loadError}
          </div>
        )}
        {tab === "machines" ? (
          <MachinesPage disks={disks} machines={machines} reload={reload} />
        ) : (
          <GameDisksPage disks={disks} machines={machines} reload={reload} />
        )}
      </main>
    </div>
  );
}
