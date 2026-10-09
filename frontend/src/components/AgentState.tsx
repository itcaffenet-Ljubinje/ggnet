import type { Machine } from "../api";
import { formatDuration, formatLink, SLOW_LINK_MBPS } from "../format";

// The agent sends a heartbeat every 30 s; after 90 s without one it counts as offline.
export const ONLINE_WINDOW_MS = 90_000;

export function ago(iso: string, now: number): string {
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} d ago`;
}

interface Props {
  machine: Machine;
  now?: number;
}

/** Whether the agent sent a heartbeat recently. */
export function isOnline(m: Machine, now = Date.now()): boolean {
  return m.last_seen_at !== null && now - Date.parse(m.last_seen_at) < ONLINE_WINDOW_MS;
}

export function AgentState({ machine: m, now = Date.now() }: Props) {
  if (!m.last_seen_at) return <span className="muted small">No agent yet</span>;

  const online = isOnline(m, now);
  const iqnMismatch = m.reported_iqn !== null && m.reported_iqn !== m.initiator_iqn;

  return (
    <div className="small">
      <span className={online ? "badge badge-provisioned" : "badge"}>
        {online ? "Online" : "Offline"}
      </span>
      {online && m.iscsi_connected !== null && (
        <span className={m.iscsi_connected ? "badge badge-provisioned" : "badge badge-warn"}>
          {m.iscsi_connected ? "Disk connected" : "Disk not connected"}
        </span>
      )}
      {m.pending_command && (
        <span className="badge badge-warn" title="Sent with the agent's next heartbeat">
          {m.pending_command === "shutdown" ? "Shutting down…" : "Restarting…"}
        </span>
      )}
      {online && m.link_speed_mbps !== null && m.link_speed_mbps < SLOW_LINK_MBPS && (
        <span className="badge badge-warn" title="Below 1 Gbit/s: games load slowly from the game disk">
          Link {formatLink(m.link_speed_mbps)}
        </span>
      )}
      <div className="muted">
        {ago(m.last_seen_at, now)}
        {m.agent_version && ` · agent ${m.agent_version}`}
      </div>
      {online && (m.reported_ip || m.booted_at) && (
        <div className="muted">
          {m.reported_ip}
          {m.reported_ip && m.link_speed_mbps !== null && ` · ${formatLink(m.link_speed_mbps)}`}
          {m.booted_at && `${m.reported_ip ? " · " : ""}up ${formatDuration(now - Date.parse(m.booted_at))}`}
        </div>
      )}
      {iqnMismatch && (
        <div className="error-text" title="The iSCSI ACL only allows the expected IQN">
          PC reports IQN {m.reported_iqn}
        </div>
      )}
    </div>
  );
}
