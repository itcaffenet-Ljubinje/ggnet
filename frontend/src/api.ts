// Typed client for the ggNet backend API (/api/v1).

export interface Health {
  status: string;
  version: string;
  pool: string;
  server_ip: string;
}

export interface GameDisk {
  id: number;
  name: string;
  zvol_path: string;
  size_gb: number;
  snapshot: string | null;
  published: boolean;
  editor_id: number | null;
  created_at: string;
}

/** One version of a game disk (a ZFS snapshot of its master). */
export interface Snapshot {
  name: string;
  created_at: string;
  used_bytes: number;
  referenced_bytes: number;
  active: boolean;
  machines: string[];
  pinned: string[];
}

/** A PC's writeback (clone) of a game disk. */
export interface Writeback {
  machine_id: number | null;
  machine_name: string;
  zvol: string;
  snapshot: string;
  used_bytes: number;
  keep_writeback: boolean;
  pinned_snapshot: string | null;
  session_active: boolean | null;
  outdated: boolean;
}

/** Automated snapshot and writeback removal (Settings → Retention). */
export interface RetentionSettings {
  enabled: boolean;
  dry_run: boolean;
  reserved_percent: number;
  warning_percent: number;
  unused_snapshot_days: number;
  keep_newest_snapshots: number;
  inactive_writeback_hours: number;
}

export interface RetentionAction {
  kind: "snapshot" | "writeback" | "error";
  target: string;
  reason: string;
  done: boolean;
  error: string | null;
}

export interface RetentionReport {
  at: string;
  dry_run: boolean;
  actions: RetentionAction[];
}

export interface Retention extends RetentionSettings {
  saved: boolean;
  last_run: RetentionReport | null;
}

export interface Storage {
  pool: string;
  total_bytes: number;
  used_bytes: number;
  available_bytes: number;
  used_percent: number;
  reserved_bytes: number;
  warning_percent: number;
  warning: boolean;
}

/** Hardware the agent reports (ggRock's Hardware tab). */
export interface MachineHardware {
  nic: string | null;
  cpu: string | null;
  gpus: string[];
  motherboard: string | null;
  memory_bytes: number | null;
}

export type MachineMode = "disk" | "boot";
export type MachineStatus = "idle" | "provisioned" | "editing" | "error";

export interface Machine {
  id: number;
  name: string;
  mode: MachineMode;
  initiator_iqn: string;
  mac: string | null;
  game_disk_id: number | null;
  editing_disk_id: number | null;
  drive_letter: string;
  reported_drive_letter: string | null;
  pinned_snapshot: string | null;
  reported_ip: string | null;
  reported_mac: string | null;
  link_speed_mbps: number | null;
  hardware: MachineHardware | null;
  status: MachineStatus;
  last_error: string | null;
  clone_zvol: string | null;
  clone_snapshot: string | null;
  iscsi_target_iqn: string | null;
  outdated: boolean;
  last_seen_at: string | null;
  agent_version: string | null;
  reported_iqn: string | null;
  iscsi_connected: boolean | null;
  keep_writeback: boolean;
  writeback_dirty: boolean;
  session_active: boolean | null;
  session_changed_at: string | null;
  booted_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface MachineCreate {
  name: string;
  mode?: MachineMode;
  initiator_iqn?: string | null;
  mac?: string | null;
  game_disk_id?: number | null;
  drive_letter?: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Turn a FastAPI error body into one readable message. */
export function errorMessage(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && "error" in detail) {
    return String((detail as { error: unknown }).error);
  }
  if (Array.isArray(detail)) {
    // 422 validation errors: [{loc: [...], msg: "..."}]
    return detail
      .map((d: { loc?: unknown[]; msg?: string }) => {
        const field = (d.loc ?? []).filter((p) => p !== "body").join(".");
        const msg = (d.msg ?? "invalid value").replace(/^Value error, /, "");
        return field ? `${field}: ${msg}` : msg;
      })
      .join("; ");
  }
  if (typeof detail === "string") return detail;
  return `Request failed (HTTP ${status})`;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 204) return undefined as T;
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    // Non-JSON body (e.g. a proxy error page).
  }
  if (!res.ok) throw new ApiError(res.status, errorMessage(res.status, data));
  return data as T;
}

export const api = {
  health: () => request<Health>("GET", "/health"),

  listDisks: () => request<GameDisk[]>("GET", "/v1/game-disks"),
  createDisk: (name: string, size_gb: number) =>
    request<GameDisk>("POST", "/v1/game-disks", { name, size_gb }),
  publishDisk: (id: number) => request<GameDisk>("POST", `/v1/game-disks/${id}/publish`),
  startEdit: (id: number, machine_id: number) =>
    request<GameDisk>("POST", `/v1/game-disks/${id}/edit`, { machine_id }),
  finishEdit: (id: number) => request<GameDisk>("POST", `/v1/game-disks/${id}/finish-edit`),
  deleteDisk: (id: number) => request<void>("DELETE", `/v1/game-disks/${id}`),
  listSnapshots: (id: number) => request<Snapshot[]>("GET", `/v1/game-disks/${id}/snapshots`),
  setActiveSnapshot: (id: number, snapshot: string) =>
    request<GameDisk>("PUT", `/v1/game-disks/${id}/active-snapshot`, { snapshot }),
  deleteSnapshot: (id: number, name: string) =>
    request<void>("DELETE", `/v1/game-disks/${id}/snapshots/${encodeURIComponent(name)}`),
  listWritebacks: (id: number) => request<Writeback[]>("GET", `/v1/game-disks/${id}/writebacks`),

  getStorage: () => request<Storage>("GET", "/v1/storage"),
  getRetention: () => request<Retention>("GET", "/v1/settings/retention"),
  saveRetention: (body: RetentionSettings) => request<Retention>("PUT", "/v1/settings/retention", body),
  previewRetention: (body: RetentionSettings) =>
    request<RetentionAction[]>("POST", "/v1/settings/retention/preview", body),
  runRetention: () => request<RetentionReport>("POST", "/v1/settings/retention/run"),

  listMachines: () => request<Machine[]>("GET", "/v1/machines"),
  createMachine: (body: MachineCreate) => request<Machine>("POST", "/v1/machines", body),
  updateMachine: (id: number, body: Partial<MachineCreate>) =>
    request<Machine>("PATCH", `/v1/machines/${id}`, body),
  assignDisk: (id: number, game_disk_id: number | null) =>
    request<Machine>("POST", `/v1/machines/${id}/assign`, { game_disk_id }),
  setDriveLetterAll: (drive_letter: string) =>
    request<Machine[]>("PUT", "/v1/machines/drive-letter", { drive_letter }),
  setKeepWriteback: (id: number, enabled: boolean) =>
    request<Machine>("PUT", `/v1/machines/${id}/keep-writeback`, { enabled }),
  applyWritebacks: (id: number) => request<Machine>("POST", `/v1/machines/${id}/apply-writebacks`),
  pinSnapshot: (id: number, snapshot: string | null) =>
    request<Machine>("PUT", `/v1/machines/${id}/pin`, { snapshot }),
    discardWriteback: (id: number) => request<Machine>("POST", `/v1/machines/${id}/discard-writeback`),
  deleteMachine: (id: number) => request<void>("DELETE", `/v1/machines/${id}`),
};
