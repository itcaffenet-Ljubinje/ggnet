import type { MachineStatus } from "../api";

const LABELS: Record<MachineStatus, string> = {
  idle: "Idle",
  provisioned: "Ready",
  error: "Error",
};

export function StatusBadge({ status }: { status: MachineStatus }) {
  return <span className={`badge badge-${status}`}>{LABELS[status]}</span>;
}
