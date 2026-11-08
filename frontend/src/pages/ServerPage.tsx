import { useEffect, useState } from "react";
import {
  AlertTriangle,
  CalendarClock,
  CheckCircle2,
  Cpu,
  Database,
  HardDrive,
  MemoryStick,
  Play,
  RefreshCw,
  Square,
} from "lucide-react";

import { api, type Pool, type PoolDisk, type ServerStatus, type User } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { MaintenanceSchedule } from "../components/MaintenanceSchedule";
import { SubNav, useSection, type SubNavItem } from "../components/SubNav";
import { formatBytes } from "../format";
import { useAsyncAction } from "../useAsyncAction";
import { useSingleFlight } from "../useSingleFlight";

const REFRESH_MS = 10_000;
const DAY_MS = 24 * 3600 * 1000;
// Scrub and TRIM are expected at least monthly.
const STALE_DAYS = 35;
const HOT_C = 60;
const WORN_PERCENT = 80;

const daysSince = (iso: string | null, now = Date.now()) =>
  iso ? Math.floor((now - Date.parse(iso)) / DAY_MS) : null;

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");

/** A disk that needs a look: not online, errors, failed SMART, hot or worn out. */
export function diskProblems(d: PoolDisk): string[] {
  const out: string[] = [];
  if (d.state !== "ONLINE") out.push(d.state);
  const errors = d.read_errors + d.write_errors + d.checksum_errors;
  if (errors) out.push(`${errors} errors`);
  if (d.smart_passed === false) out.push("SMART failed");
  if (d.temperature !== null && d.temperature >= HOT_C) out.push(`${d.temperature} °C`);
  if (d.wear_percent !== null && d.wear_percent >= WORN_PERCENT) out.push(`${d.wear_percent}% worn`);
  return out;
}

/** Server: the ZFS pool, its disks, scrub and TRIM, the RAM cache. */
export function ServerPage({
  user,
  pcs,
}: {
  user?: User | null;
  /** PCs registered, for the RAM cache advice. */
  pcs?: number;
}) {
  const { busy, error, setError, run } = useAsyncAction();
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const admin = !user || user.role === "admin";
  const [section, setSection] = useSection("server", SERVER_SECTIONS);

  // The polling keeps its own state: it never marks an action (scrub, TRIM, ARC) busy or done.
  const load = useSingleFlight(async () => {
    try {
      setStatus(await api.getServer());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  });
  useEffect(() => {
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  async function scrub(action: "start" | "stop") {
    const msg =
      action === "start"
        ? "Start a scrub of the pool?\n\nZFS reads every block and checks it. It takes about half an hour " +
          "to a few hours, and the PCs are slower while it runs: best when the cafe is closed."
        : "Stop the running scrub? It can be started again later; it starts over.";
    if (!window.confirm(msg)) return;
    await run(`scrub-${action}`, async () => setStatus(await api.scrub(action)));
  }

  async function trim(action: "start" | "stop") {
    const msg =
      action === "start"
        ? "Start a TRIM of the pool?\n\nZFS tells each SSD which blocks are free, so it can erase them ahead " +
          "and new writes stay fast. Nothing on the pool changes. It takes minutes to an hour and the disks are " +
          "a little busier meanwhile: best when the cafe is closed."
        : "Stop the running TRIM? It can be started again later.";
    if (!window.confirm(msg)) return;
    await run(`trim-${action}`, async () => setStatus(await api.trim(action)));
  }

  async function setArcMax(gb: number) {
    const msg =
      `Set the RAM cache to at most ${gb} GB?\n\n` +
      "It applies now (the cache grows or shrinks over the next minutes) and stays after a restart " +
      "(/etc/modprobe.d/zfs.conf).";
    if (!window.confirm(msg)) return;
    await run("arc", async () => setStatus(await api.setArcMax(gb)));
  }

  async function readSmart() {
    await run("smart", async () => setStatus(await api.readSmart()));
  }

  const pool = status?.pool ?? null;
  return (
    <section aria-label="Server">
      <ErrorBanner message={error} onClose={() => setError(null)} />
      {status && <ServerKpis status={status} />}
      <SubNav label="Server sections" items={SERVER_SECTIONS} active={section} onSelect={setSection} />
      {status && !pool && section !== "cache" && (
        <div className="card">
          <p className="error-text">The ZFS pool could not be read.</p>
        </div>
      )}
      {section === "pool" && pool && (
        <PoolCard pool={pool} admin={admin} busy={busy} onScrub={scrub} onTrim={trim} />
      )}
      {section === "maintenance" && pool && <MaintenanceSchedule admin={admin} />}
      {section === "disks" && pool && status && (
        <DisksCard pool={pool} readAt={status.smart_read_at} admin={admin} busy={busy} onRead={readSmart} />
      )}
      {section === "cache" &&
        (status?.arc ? (
          <CacheCard status={status} pcs={pcs} admin={admin} busy={busy} onSet={setArcMax} />
        ) : (
          status && <p className="empty">The RAM cache could not be read.</p>
        ))}
    </section>
  );
}

type ServerSection = "pool" | "maintenance" | "disks" | "cache";

const SERVER_SECTIONS: SubNavItem<ServerSection>[] = [
  { id: "pool", label: "Pool", icon: Database },
  { id: "maintenance", label: "Maintenance", icon: CalendarClock },
  { id: "disks", label: "Disks", icon: HardDrive },
  { id: "cache", label: "RAM cache", icon: MemoryStick },
];

function ServerKpis({ status }: { status: ServerStatus }) {
  const pool = status.pool;
  const problems = pool ? pool.disks.filter((d) => diskProblems(d).length).length : 0;
  const cards = [
    {
      title: "Pool",
      value: pool?.state ?? "unknown",
      sub: pool ? `${pool.disks.length} disks${problems ? `, ${problems} need a look` : ", all fine"}` : "not readable",
      icon: Database,
      tone: pool?.state === "ONLINE" && !problems ? "green" : "amber",
    },
    {
      title: "Used",
      value: pool?.capacity !== null && pool?.capacity !== undefined ? `${pool.capacity}%` : "—",
      sub: pool?.free_bytes ? `${formatBytes(pool.free_bytes)} free` : "",
      icon: HardDrive,
      tone: "blue",
    },
    {
      title: "RAM cache",
      value: status.arc ? formatBytes(status.arc.size_bytes) : "—",
      sub:
        status.arc?.hit_percent !== null && status.arc?.hit_percent !== undefined
          ? `${status.arc.hit_percent}% of reads from RAM`
          : "",
      icon: MemoryStick,
      tone: "purple",
    },
    {
      title: "Writebacks",
      value: formatBytes(status.writebacks_bytes),
      sub: `${status.writebacks_count} writebacks of all PCs`,
      icon: Cpu,
      tone: "amber",
    },
  ];
  return (
    <div className="kpi-grid">
      {cards.map(({ title, value, sub, icon: Icon, tone }) => (
        <div key={title} className="kpi-card">
          <div className="kpi-info">
            <span className="kpi-title">{title}</span>
            <span className="kpi-value">{value}</span>
            <span className="kpi-subtitle">{sub}</span>
          </div>
          <div className={`kpi-icon-box ${tone}`}>
            <Icon size={22} />
          </div>
        </div>
      ))}
    </div>
  );
}

function PoolCard({
  pool,
  admin,
  busy,
  onScrub,
  onTrim,
}: {
  pool: Pool;
  admin: boolean;
  busy: string | null;
  onScrub: (action: "start" | "stop") => void;
  onTrim: (action: "start" | "stop") => void;
}) {
  const scan = pool.scan;
  const scanning = scan?.state === "scanning";
  const scrubAge = scan?.function === "scrub" ? daysSince(scan.end_time) : null;
  const trimAge = daysSince(pool.last_trim);
  const trimming = pool.disks.filter((d) => d.trim_state === "ACTIVE");
  const trimPercent = trimming.length
    ? Math.round(trimming.reduce((sum, d) => sum + (d.trim_percent ?? 0), 0) / trimming.length)
    : null;
  const used = pool.capacity ?? 0;
  return (
    <div className="card" aria-label="Pool">
      <h3 className="card-title section-title">
        <Database size={15} /> Pool {pool.name}
        <span className={`badge ${pool.state === "ONLINE" ? "badge-provisioned" : "badge-error"}`}>{pool.state}</span>
      </h3>
      <div className={used >= 80 ? "meter meter-warn" : "meter"} role="meter" aria-valuenow={used} aria-label="Pool used">
        <div style={{ width: `${used}%` }} />
      </div>
      <p className="muted small">
        {pool.allocated_bytes !== null && pool.size_bytes !== null &&
          `${formatBytes(pool.allocated_bytes)} of ${formatBytes(pool.size_bytes)} used (${used}%)`}
        {pool.fragmentation !== null && ` · fragmentation ${pool.fragmentation}%`}
        {pool.errors > 0 && <span className="error-text"> · {pool.errors} data errors</span>}
      </p>
      <p className="muted small">
        Layout: {pool.groups.map((g) => `${g.name} (${g.disks.length} disks)`).join(", ")}
      </p>

      <div className="form-row">
        <div>
          <div className="strong">Scrub</div>
          {scanning ? (
            <div className="small">
              {scan?.function === "resilver" ? "Resilvering" : "Running"}: {scan?.percent ?? 0}%
              {scan?.paused && " (paused)"}
            </div>
          ) : scan?.function === "scrub" ? (
            <div className={scrubAge !== null && scrubAge > STALE_DAYS ? "small warn-text" : "small"}>
              Last: {when(scan.end_time)}, {scan.errors ? `${scan.errors} errors repaired` : "no errors"}
              {scan.state === "canceled" && " (stopped)"}
            </div>
          ) : (
            <div className="small warn-text">Never scrubbed</div>
          )}
          <div className="muted small">Reads every block to find and repair bit rot. Proxmox runs it monthly.</div>
        </div>
        {admin && (
          <div className="actions-cell">
            {scanning ? (
              <button type="button" className="secondary small" onClick={() => onScrub("stop")} disabled={busy !== null}>
                <Square size={13} /> Stop scrub
              </button>
            ) : (
              <button type="button" className="secondary small" onClick={() => onScrub("start")} disabled={busy !== null}>
                <Play size={13} /> Start scrub
              </button>
            )}
          </div>
        )}
      </div>

      <div className="form-row">
        <div>
          <div className="strong">TRIM</div>
          {trimming.length ? (
            <div className="small">Running: {trimPercent}%</div>
          ) : pool.autotrim ? (
            <div className="small">
              <CheckCircle2 size={12} /> Autotrim on: ZFS tells the SSDs about freed blocks as it goes.
              <span className="muted">
                {" "}
                Last full TRIM: {pool.last_trim ? when(pool.last_trim) : "never"} (only needed now and then).
              </span>
            </div>
          ) : (
            <div className={trimAge === null || trimAge > STALE_DAYS ? "small warn-text" : "small"}>
              {pool.last_trim ? `Last: ${when(pool.last_trim)} (${trimAge} days ago)` : "Never trimmed"}
              {(trimAge === null || trimAge > STALE_DAYS) && (
                <>
                  {" "}
                  <AlertTriangle size={12} /> Proxmox's monthly TRIM skips pools that are not all NVMe: start it here.
                </>
              )}
            </div>
          )}
          <div className="muted small">Tells the SSDs which blocks are free, so writes stay fast.</div>
        </div>
        {admin && (
          <div className="actions-cell">
            {trimming.length ? (
              <button type="button" className="secondary small" onClick={() => onTrim("stop")} disabled={busy !== null}>
                <Square size={13} /> Stop TRIM
              </button>
            ) : (
              <button type="button" className="secondary small" onClick={() => onTrim("start")} disabled={busy !== null}>
                <Play size={13} /> Start TRIM
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function DisksCard({
  pool,
  readAt,
  admin,
  busy,
  onRead,
}: {
  pool: Pool;
  readAt: string | null;
  admin: boolean;
  busy: string | null;
  onRead: () => void;
}) {
  return (
    <div className="card" aria-label="Disks">
      <h3 className="card-title section-title">
        <HardDrive size={15} /> Disks
      </h3>
      <div className="form-row">
        <span className="muted small">
          {readAt ? `SMART read ${when(readAt)}` : "SMART not read yet: temperature, wear and health show after reading."}
        </span>
        {admin && (
          <button type="button" className="secondary small" onClick={onRead} disabled={busy !== null}>
            <RefreshCw size={13} /> {busy === "smart" ? "Reading…" : "Read SMART"}
          </button>
        )}
      </div>
      <div className="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>Disk</th>
              <th>Mirror</th>
              <th>State</th>
              <th>Errors (R/W/C)</th>
              <th>Temp</th>
              <th>Wear</th>
              <th>Hours</th>
              <th>SMART</th>
            </tr>
          </thead>
          <tbody>
            {pool.disks.map((d) => {
              const problems = diskProblems(d);
              return (
                <tr key={d.name} className={problems.length ? "row-warn" : undefined}>
                  <td>
                    <div className="strong small">{d.model ?? d.name}</div>
                    <div className="mono muted small">
                      {d.serial ?? d.name}
                      {d.capacity_bytes ? ` · ${formatBytes(d.capacity_bytes)}` : ""}
                    </div>
                  </td>
                  <td className="small">{d.group}</td>
                  <td>
                    <span className={`badge ${d.state === "ONLINE" ? "badge-provisioned" : "badge-error"}`}>{d.state}</span>
                  </td>
                  <td className="mono small">
                    {d.read_errors}/{d.write_errors}/{d.checksum_errors}
                  </td>
                  <td className={d.temperature !== null && d.temperature >= HOT_C ? "warn-text small" : "small"}>
                    {d.temperature !== null ? `${d.temperature} °C` : "—"}
                  </td>
                  <td className={d.wear_percent !== null && d.wear_percent >= WORN_PERCENT ? "warn-text small" : "small"}>
                    {d.wear_percent !== null ? `${d.wear_percent}%` : "—"}
                  </td>
                  <td className="small">{d.power_on_hours !== null ? d.power_on_hours.toLocaleString() : "—"}</td>
                  <td>
                    {d.smart_passed === null ? (
                      <span className="muted small">—</span>
                    ) : d.smart_passed ? (
                      <span className="badge badge-provisioned" aria-label={`SMART passed for ${d.name}`}>
                        <CheckCircle2 size={12} /> OK
                      </span>
                    ) : (
                      <span className="badge badge-error">Failed</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="muted small">Wear is the SSD's endurance used; plan a replacement above {WORN_PERCENT}%.</p>
    </div>
  );
}

const GiB = 1024 ** 3;

/** RAM cache for this many PCs (20 → 32 GB, 40 → 64 GB, 100 → 128 GB, then +128 GB per 100). */
export function recommendedCacheGb(pcs: number): number {
  if (pcs <= 20) return 32;
  if (pcs <= 40) return 64;
  if (pcs <= 100) return 128;
  return 128 + Math.ceil((pcs - 100) / 100) * 128;
}

function CacheCard({
  status,
  pcs,
  admin,
  busy,
  onSet,
}: {
  status: ServerStatus;
  pcs?: number;
  admin: boolean;
  busy: string | null;
  onSet: (gb: number) => void;
}) {
  const arc = status.arc!;
  const mem = status.memory;
  const fill = arc.max_bytes ? Math.round((100 * arc.size_bytes) / arc.max_bytes) : 0;
  const current = Math.round(arc.max_bytes / GiB);
  const [gb, setGb] = useState(current);
  const low = arc.allowed_min_bytes ? Math.ceil(arc.allowed_min_bytes / GiB) : 1;
  const high = arc.allowed_max_bytes ? Math.floor(arc.allowed_max_bytes / GiB) : null;
  const advice = pcs !== undefined ? recommendedCacheGb(pcs) : null;
  return (
    <div className="card" aria-label="RAM cache">
      <h3 className="card-title section-title">
        <MemoryStick size={15} /> RAM cache (ZFS ARC)
      </h3>
      <div className="meter" role="meter" aria-valuenow={fill} aria-label="RAM cache filled">
        <div style={{ width: `${fill}%` }} />
      </div>
      <p className="muted small">
        {formatBytes(arc.size_bytes)} of at most {formatBytes(arc.max_bytes)} ({fill}%)
        {arc.hit_percent !== null && ` · ${arc.hit_percent}% of reads served from RAM since the server started`}
      </p>
      {mem && (
        <p className="muted small">
          Server RAM: {formatBytes(mem.total_bytes)}, {formatBytes(mem.available_bytes)} available
        </p>
      )}
      <p className="muted small">
        Every PC reads the same image blocks, so a block read once is served to the others from RAM. Writes of
        writebacks are gathered in RAM and written to the SSDs every few seconds.
      </p>
      {admin && high !== null && (
        <form
          className="form-row"
          aria-label="RAM cache size"
          onSubmit={(e) => {
            e.preventDefault();
            onSet(gb);
          }}
        >
          <label>
            Maximum (GB)
            <input
              type="number"
              min={low}
              max={high}
              value={gb}
              onChange={(e) => setGb(Number(e.target.value))}
              required
            />
          </label>
          <button type="submit" disabled={busy !== null || gb === current}>
            {busy === "arc" ? "Saving…" : "Save"}
          </button>
          <span className="muted small">
            {low}–{high} GB on this server; the rest stays for Proxmox and its VMs.
            {advice !== null && ` For ${pcs} PCs about ${Math.min(advice, high)} GB is enough.`}
          </span>
        </form>
      )}
    </div>
  );
}
