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
  created_at: string;
}

export type MachineMode = "disk" | "boot";
export type MachineStatus = "idle" | "provisioned" | "error";

export interface Machine {
  id: number;
  name: string;
  mode: MachineMode;
  initiator_iqn: string;
  mac: string | null;
  game_disk_id: number | null;
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
  deleteDisk: (id: number) => request<void>("DELETE", `/v1/game-disks/${id}`),

  listMachines: () => request<Machine[]>("GET", "/v1/machines"),
  createMachine: (body: MachineCreate) => request<Machine>("POST", "/v1/machines", body),
  updateMachine: (id: number, body: Partial<MachineCreate>) =>
    request<Machine>("PATCH", `/v1/machines/${id}`, body),
  assignDisk: (id: number, game_disk_id: number | null) =>
    request<Machine>("POST", `/v1/machines/${id}/assign`, { game_disk_id }),
  setKeepWriteback: (id: number, enabled: boolean) =>
    request<Machine>("PUT", `/v1/machines/${id}/keep-writeback`, { enabled }),
  applyWritebacks: (id: number) => request<Machine>("POST", `/v1/machines/${id}/apply-writebacks`),
  deleteMachine: (id: number) => request<void>("DELETE", `/v1/machines/${id}`),
};
