import type { Machine } from "../api";
import { formatBytes, formatLink } from "../format";

/** ggRock's Hardware tab, plus the network the PC reaches the server on. */
export function MachineDetails({ machine: m }: { machine: Machine }) {
  const hw = m.hardware;
  const rows: [string, string | null][] = [
    ["IP address", m.reported_ip],
    ["MAC address", m.reported_mac],
    ["Network adapter", hw?.nic ?? null],
    ["Link speed", m.link_speed_mbps !== null ? formatLink(m.link_speed_mbps) : null],
    ["CPU", hw?.cpu ?? null],
    ["Graphics", hw && hw.gpus.length > 0 ? hw.gpus.join(", ") : null],
    ["Motherboard", hw?.motherboard ?? null],
    ["Memory", hw?.memory_bytes ? formatBytes(hw.memory_bytes) : null],
    ["Windows started", m.booted_at ? new Date(m.booted_at).toLocaleString() : null],
    ["Agent", m.agent_version],
  ];
  return (
    <div className="details">
      {m.last_seen_at === null ? (
        <p className="muted small">No agent has reported from this PC yet.</p>
      ) : (
        <dl className="facts" aria-label={`Details of ${m.name}`}>
          {rows.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value ?? <span className="muted">—</span>}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
