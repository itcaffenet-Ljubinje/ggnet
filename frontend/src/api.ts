// Typed client for the ggNet backend API (/api/v1).

export interface Health {
  status: string;
  version: string;
  pool: string;
  server_ip: string;
}

/** Prepare image's options beyond what every image gets. */
export interface PrepareOptions {
  defender_off: boolean;
  /** Background services a cafe PC does not need off (faster startup). */
  services_off: boolean;
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
  /** "os": a Boot Mode Windows image; "game": a game disk. Older servers send none (game). */
  kind?: DiskKind;
  /** A copy in progress (or failed): the master@version it is copied from. */
  copy_source?: string | null;
  copy_percent?: number | null;
  copy_error?: string | null;
  /** OS image: the network card models Add new hardware installed; none: it needs hardware. */
  nic_models?: string[];
}

/** An OS image no PC can boot yet: Add new hardware has not added any network card. */
export const needsHardware = (d: GameDisk) => d.kind === "os" && d.published && (d.nic_models ?? []).length === 0;

export type DiskKind = "game" | "os";

/** Whether a disk's copy_source is an image file being imported (a path), not a version being copied. */
export const isImport = (source: string | null | undefined) => !!source && source.startsWith("/");

/** An image file that can be imported (Images → Import). */
export interface ImageFile {
  path: string;
  name: string;
  size: number;
  /** qemu-img's name: vpc (VHD), vhdx, qcow2, vmdk, raw */
  format: string;
}

/** A file system on a USB disk plugged into the server. */
export interface UsbPart {
  name: string;
  path: string;
  size: number;
  fstype: string;
  label: string | null;
  model: string | null;
  /** Where ggNet opened it read-only, or null. */
  mounted: string | null;
}

export interface HardwareCard {
  /** PCI hardware ID: VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10 */
  nic: string;
  /** Its instance ID on the PC: 3&11583659&0&FE */
  instance: string;
}

/** An Add new hardware run: Windows installs the cards in a VM, then a new version. */
export interface AddHardwareJob {
  cards: HardwareCard[];
  step: "starting" | "job" | "vm" | "result" | "prepare" | "apply" | "done";
  running: boolean;
  error: string | null;
  snapshot: string | null;
  devices: { instance_id: string; ok: boolean; driver?: string | null; error?: string | null }[];
  started: number;
  finished: number | null;
}

/** The Add new hardware wizard of an image (step by step, as the new PC boots). */
export interface AddHardwareWizard {
  step: "waiting" | "scanning" | "collected" | "adding" | "rebooting" | "done" | "failed";
  active: boolean;
  machine: string | null;
  card: HardwareCard | null;
  error: string | null;
  started: number;
  finished: number | null;
}

export interface AddHardwareStatus {
  wizard: AddHardwareWizard | null;
  job: AddHardwareJob | null;
  /** PCs on screen waiting for Add new hardware for this image. */
  waiting: string[];
}

export interface ImportSources {
  /** The imports folder on the pool; null when its dataset is missing. */
  folder: string | null;
  files: ImageFile[];
  usb: UsbPart[];
}

/** Upload a file into the imports folder, reporting the bytes sent. */
export function uploadImage(file: File, onProgress: (sent: number, total: number) => void): Promise<ImageFile> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/v1/imports/upload/${encodeURIComponent(file.name)}`);
    xhr.upload.onprogress = (e) => onProgress(e.loaded, e.lengthComputable ? e.total : file.size);
    xhr.onload = () => {
      let data: unknown = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        // Not JSON (a proxy error page).
      }
      if (xhr.status === 401) window.dispatchEvent(new Event(SIGNED_OUT));
      if (xhr.status >= 200 && xhr.status < 300) resolve(data as ImageFile);
      else reject(new ApiError(xhr.status, errorMessage(xhr.status, data)));
    };
    xhr.onerror = () => reject(new ApiError(0, "The upload was interrupted"));
    xhr.send(file);
  });
}

/** Which kept writebacks Apply Writebacks turns into versions (Boot Mode has two). */
export interface ApplyChoice {
  os?: boolean;
  game?: boolean;
  /** Set on every version Apply Writebacks makes. */
  comment?: string;
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
  /** Who, when, what changed; null when unset. */
  comment?: string | null;
  /** Never deleted (automatically or by hand) until unlocked. */
  locked?: boolean;
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
  writeback_warning_gb: number;
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

/** Settings → Network boot: what iPXE gets; config.toml's until saved. */
export interface BootSettings {
  /** "none" (0.0.0.0), "dhcp" (the router's) or an IPv4 address. */
  gateway: string;
  unregistered: "wait" | "local";
  /** Boot Mode agents ask shim to enroll the ggNet Secure Boot key (confirmed in MokManager). */
  mok_enroll: boolean;
  /** Typed in MokManager to confirm: a-z and 0-9, 4 to 16 characters. */
  mok_password: string;
}

export interface BootSettingsOut extends BootSettings {
  saved: boolean;
}

export interface SecureBootKey {
  subject: string;
  /** SHA-256, as MokManager shows it. */
  fingerprint: string;
  expires: string | null;
  /** Whether sb/grubx64.efi is signed with this key; null: not there. */
  ipxe_signed: boolean | null;
}

/** The PXE service (proxyDHCP + TFTP) as systemd sees it. */
export interface PxeStatus {
  unit: string;
  installed: boolean;
  state: string;
  sub_state: string;
  since: string | null;
  tftp_root: string;
  files: Record<string, boolean>;
  /** The ggNet Secure Boot key the PCs enroll; null until the installer made it. */
  secure_boot?: SecureBootKey | null;
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

/** Server page: the ZFS pool, its disks (with SMART), the RAM cache. */
export interface PoolScan {
  function: string | null;
  state: string | null;
  start_time: string | null;
  end_time: string | null;
  errors: number;
  percent: number | null;
  paused: boolean;
}

export interface PoolDisk {
  name: string;
  group: string;
  state: string;
  read_errors: number;
  write_errors: number;
  checksum_errors: number;
  trim_state: string | null;
  trim_percent: number | null;
  trim_time: string | null;
  trim_supported: boolean;
  smart_passed: boolean | null;
  model: string | null;
  serial: string | null;
  capacity_bytes: number | null;
  temperature: number | null;
  wear_percent: number | null;
  power_on_hours: number | null;
}

export interface Pool {
  name: string;
  state: string;
  size_bytes: number | null;
  allocated_bytes: number | null;
  free_bytes: number | null;
  fragmentation: number | null;
  capacity: number | null;
  errors: number;
  scan: PoolScan | null;
  last_trim: string | null;
  autotrim: boolean | null;
  groups: { name: string; type: string; state: string; disks: string[] }[];
  disks: PoolDisk[];
}

export interface ServerStatus {
  pool: Pool | null;
  arc: {
    size_bytes: number;
    max_bytes: number;
    min_bytes: number;
    hit_percent: number | null;
    /** What the maximum may be set to on this server. */
    allowed_min_bytes?: number | null;
    allowed_max_bytes?: number | null;
  } | null;
  memory: { total_bytes: number; available_bytes: number } | null;
  writebacks_bytes: number;
  writebacks_count: number;
  smart_read_at: string | null;
}

export interface MaintenanceSettings {
  enabled: boolean;
  weekday: number;
  start: string;
  end: string;
  trim: boolean;
  scrub: boolean;
  order: "trim_first" | "scrub_first";
}

export interface MaintenanceStep {
  name: string;
  status: "waiting" | "running" | "paused" | "done" | "stopped" | "failed";
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface Maintenance extends MaintenanceSettings {
  next_window: string | null;
  last_run: { window_start: string; window_end: string; closed: boolean; steps: MaintenanceStep[] } | null;
}

/** Hardware the agent reports (the Hardware tab). */
export interface MachineHardware {
  nic: string | null;
  cpu: string | null;
  gpus: string[];
  motherboard: string | null;
  memory_bytes: number | null;
  /** Windows booted with Secure Boot on; null or missing: unknown (not UEFI, or an older agent). */
  secure_boot?: boolean | null;
}

/** iSCSI traffic of a mapped machine since its writeback was last cloned. */
export interface MachineTraffic {
  machine_id: number;
  sent_bytes: number;
  received_bytes: number;
  sent_bps: number | null;
  received_bps: number | null;
}

/** Settings → Client app: the ggnet-agent package on the server. */
export interface AgentPackage {
  available: boolean;
  directory: string;
  version: string | null;
  server_url: string;
  install_command: string;
  files: { name: string; size_bytes: number; sha256: string }[];
}

export type BulkKind = "on" | "shutdown" | "reboot" | "assign" | "pin" | "drive_letter" | "stop_keep_writeback";

/** What happened to one machine in a bulk operation. */
export interface BulkResult {
  machine_id: number;
  name: string | null;
  ok: boolean;
  error: string | null;
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
  os_disk_id?: number | null;      // Boot Mode: the OS image
  os_clone_snapshot?: string | null; // Boot Mode: the OS image version the PC runs
  editing_disk_id: number | null;
  drive_letter: string;
  reported_drive_letter: string | null;
  pinned_snapshot: string | null;
  pending_command: "shutdown" | "reboot" | null;
  reported_ip: string | null;
  reported_mac: string | null;
  link_speed_mbps: number | null;
  hardware: MachineHardware | null;
  /** Boot Mode: the boot network card's PCI ID as iPXE read it (VEN_10EC&DEV_8168&SUBSYS_…&REV_16). */
  boot_nic?: string | null;
  /** The ggNet Secure Boot key on this PC, as its agent reports it. */
  mok_state?: "enrolled" | "requested" | "failed" | null;
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
  agent_stopped: boolean;
  keep_writeback: boolean;
  writeback_dirty: boolean;
  writeback_bytes: number | null;
  writeback_large: boolean;
  ipxe_at: string | null;
  session_active: boolean | null;
  session_changed_at: string | null;
  booted_at: string | null;
  created_at: string;
  updated_at: string;
}

/** A PC that knocked (iPXE or its agent) but is not registered yet. */
export interface Discovered {
  id: number;
  mac: string;
  source: "pxe" | "agent";
  hostname: string | null;
  ip_address: string | null;
  agent_version: string | null;
  boot_nic?: string | null;
  seen_count: number;
  ignored: boolean;
  first_seen_at: string;
  last_seen_at: string;
}

export interface DiscoveryClaim {
  name: string;
  mode?: MachineMode;
  game_disk_id?: number | null;
  os_disk_id?: number | null;     // Boot Mode: the OS image
}

export interface MachineCreate {
  name: string;
  mode?: MachineMode;
  /** Identifies the PC; the server derives the initiator IQN from it. */
  mac?: string | null;
  game_disk_id?: number | null;
  os_disk_id?: number | null;     // Boot Mode: the OS image
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
  if (res.status === 401 && !path.startsWith("/v1/auth/")) {
    // The session ended (expired, password changed, signed out elsewhere): show the sign-in.
    window.dispatchEvent(new Event(SIGNED_OUT));
  }
  if (!res.ok) throw new ApiError(res.status, errorMessage(res.status, data));
  return data as T;
}

/** Fired when the server no longer knows this browser's session. */
export const SIGNED_OUT = "ggnet:signed-out";

export type UserRole = "admin" | "staff";

export interface User {
  id: number;
  username: string;
  role: UserRole;
  disabled: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface AuthState {
  /** No user yet: create the first admin with the setup code from the server. */
  setup_required: boolean;
  user: User | null;
}

export interface Activity {
  id: number;
  at: string;
  username: string | null;
  action: string;
  status_code: number;
  ip_address: string | null;
}

export const api = {
  health: () => request<Health>("GET", "/health"),

  authState: () => request<AuthState>("GET", "/v1/auth/state"),
  setup: (setup_code: string, username: string, password: string) =>
    request<AuthState>("POST", "/v1/auth/setup", { setup_code, username, password }),
  login: (username: string, password: string) =>
    request<AuthState>("POST", "/v1/auth/login", { username, password }),
  logout: () => request<void>("POST", "/v1/auth/logout"),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("POST", "/v1/auth/password", { current_password, new_password }),
  listUsers: () => request<User[]>("GET", "/v1/users"),
  createUser: (username: string, password: string, role: UserRole) =>
    request<User>("POST", "/v1/users", { username, password, role }),
  updateUser: (id: number, body: { role?: UserRole; password?: string; disabled?: boolean }) =>
    request<User>("PATCH", `/v1/users/${id}`, body),
  deleteUser: (id: number) => request<void>("DELETE", `/v1/users/${id}`),
  listActivity: (limit = 200) => request<Activity[]>("GET", `/v1/activity?limit=${limit}`),

  listDisks: () => request<GameDisk[]>("GET", "/v1/game-disks"),
  createDisk: (name: string, size_gb: number, kind: DiskKind = "game") =>
    request<GameDisk>("POST", "/v1/game-disks", { name, size_gb, kind }),
  publishDisk: (id: number) => request<GameDisk>("POST", `/v1/game-disks/${id}/publish`),
  startEdit: (id: number, machine_id: number) =>
    request<GameDisk>("POST", `/v1/game-disks/${id}/edit`, { machine_id }),
  finishEdit: (id: number) => request<GameDisk>("POST", `/v1/game-disks/${id}/finish-edit`),
  deleteDisk: (id: number) => request<void>("DELETE", `/v1/game-disks/${id}`),
  renameDisk: (id: number, name: string) => request<GameDisk>("PATCH", `/v1/game-disks/${id}`, { name }),
  growDisk: (id: number, size_gb: number, extend = true) =>
    request<{ disk: GameDisk; version: string | null; extended: boolean; note: string }>(
      "POST",
      `/v1/game-disks/${id}/grow`,
      { size_gb, extend },
    ),
  importSources: () => request<ImportSources>("GET", "/v1/imports"),
  openUsb: (part: string) =>
    request<{ part: string; folder: string; files: ImageFile[] }>("POST", `/v1/imports/usb/${part}/open`),
  ejectUsb: (part: string) => request<void>("POST", `/v1/imports/usb/${part}/eject`),
  deleteImportFile: (path: string) =>
    request<void>("DELETE", `/v1/imports/file?path=${encodeURIComponent(path)}`),
  startImport: (name: string, kind: DiskKind, path: string, options: { prepare: boolean } & PrepareOptions) =>
    request<GameDisk>("POST", "/v1/imports", { name, kind, path, ...options }),
  cancelImport: (id: number) => request<void>("POST", `/v1/imports/${id}/cancel`),
  getAddHardware: (id: number) => request<AddHardwareStatus>("GET", `/v1/game-disks/${id}/add-hardware`),
  openHardwareWizard: (id: number) =>
    request<AddHardwareStatus>("POST", `/v1/game-disks/${id}/add-hardware/wizard`),
  closeHardwareWizard: (id: number) =>
    request<AddHardwareStatus>("DELETE", `/v1/game-disks/${id}/add-hardware/wizard`),
  addHardware: (id: number, cards: HardwareCard[]) =>
    request<AddHardwareJob>("POST", `/v1/game-disks/${id}/add-hardware`, { cards }),
  cancelAddHardware: (id: number) =>
    request<AddHardwareJob | null>("POST", `/v1/game-disks/${id}/add-hardware/cancel`),
  copyDisk: (id: number, name: string, snapshot?: string) =>
    request<GameDisk>("POST", `/v1/game-disks/${id}/copy`, snapshot ? { name, snapshot } : { name }),
  listSnapshots: (id: number) => request<Snapshot[]>("GET", `/v1/game-disks/${id}/snapshots`),
  setActiveSnapshot: (id: number, snapshot: string) =>
    request<GameDisk>("PUT", `/v1/game-disks/${id}/active-snapshot`, { snapshot }),
  deleteSnapshot: (id: number, name: string) =>
    request<void>("DELETE", `/v1/game-disks/${id}/snapshots/${encodeURIComponent(name)}`),
  setSnapshotComment: (id: number, name: string, comment: string) =>
    request<Snapshot>("PUT", `/v1/game-disks/${id}/snapshots/${encodeURIComponent(name)}/comment`, { comment }),
  setSnapshotLock: (id: number, name: string, locked: boolean) =>
    request<Snapshot>("PUT", `/v1/game-disks/${id}/snapshots/${encodeURIComponent(name)}/lock`, { locked }),
  prepareImage: (id: number, options?: PrepareOptions) =>
    request<GameDisk>("POST", `/v1/game-disks/${id}/prepare`, options),
  mergeIntoBase: (id: number, name: string) =>
    request<GameDisk>("POST", `/v1/game-disks/${id}/snapshots/${encodeURIComponent(name)}/merge-into-base`),
  listWritebacks: (id: number) => request<Writeback[]>("GET", `/v1/game-disks/${id}/writebacks`),

  getAgentPackage: () => request<AgentPackage>("GET", "/v1/agent/package"),
  getStorage: () => request<Storage>("GET", "/v1/storage"),
  getServer: () => request<ServerStatus>("GET", "/v1/server"),
  scrub: (action: "start" | "stop") => request<ServerStatus>("POST", "/v1/server/scrub", { action }),
  trim: (action: "start" | "stop") => request<ServerStatus>("POST", "/v1/server/trim", { action }),
  readSmart: () => request<ServerStatus>("POST", "/v1/server/smart"),
  setArcMax: (max_gb: number) => request<ServerStatus>("PUT", "/v1/server/arc", { max_gb }),
  getSchedule: () => request<Maintenance>("GET", "/v1/server/schedule"),
  saveSchedule: (body: MaintenanceSettings) => request<Maintenance>("PUT", "/v1/server/schedule", body),
  getRetention: () => request<Retention>("GET", "/v1/settings/retention"),
  getBootSettings: () => request<BootSettingsOut>("GET", "/v1/settings/boot"),
  saveBootSettings: (body: BootSettings) => request<BootSettingsOut>("PUT", "/v1/settings/boot", body),
  getPxe: () => request<PxeStatus>("GET", "/v1/pxe"),
  restartPxe: () => request<PxeStatus>("POST", "/v1/pxe/restart"),
  saveRetention: (body: RetentionSettings) => request<Retention>("PUT", "/v1/settings/retention", body),
  previewRetention: (body: RetentionSettings) =>
    request<RetentionAction[]>("POST", "/v1/settings/retention/preview", body),
  runRetention: () => request<RetentionReport>("POST", "/v1/settings/retention/run"),

  machineTraffic: () => request<MachineTraffic[]>("GET", "/v1/machines/traffic"),
  listMachines: () => request<Machine[]>("GET", "/v1/machines"),
  createMachine: (body: MachineCreate) => request<Machine>("POST", "/v1/machines", body),
  updateMachine: (id: number, body: Partial<MachineCreate>) =>
    request<Machine>("PATCH", `/v1/machines/${id}`, body),
  /** Game disk, and in Boot Mode the OS image (not sent keeps the current one). */
  assignDisk: (id: number, game_disk_id: number | null, os_disk_id?: number | null) =>
    request<Machine>(
      "POST",
      `/v1/machines/${id}/assign`,
      os_disk_id === undefined ? { game_disk_id } : { game_disk_id, os_disk_id },
    ),
  setDriveLetterAll: (drive_letter: string) =>
    request<Machine[]>("PUT", "/v1/machines/drive-letter", { drive_letter }),
  setKeepWriteback: (id: number, enabled: boolean) =>
    request<Machine>("PUT", `/v1/machines/${id}/keep-writeback`, { enabled }),
  applyWritebacks: (id: number, choice?: ApplyChoice) =>
    request<Machine>("POST", `/v1/machines/${id}/apply-writebacks`, choice),
  pinSnapshot: (id: number, snapshot: string | null) =>
    request<Machine>("PUT", `/v1/machines/${id}/pin`, { snapshot }),
  bulk: (
    machine_ids: number[],
    action: BulkKind,
    extra: { game_disk_id?: number | null; snapshot?: string | null; drive_letter?: string } = {},
  ) => request<BulkResult[]>("POST", "/v1/machines/bulk", { machine_ids, action, ...extra }),
  power: (id: number, action: "on" | "shutdown" | "reboot") =>
    request<Machine>("POST", `/v1/machines/${id}/power`, { action }),
  discardWriteback: (id: number) => request<Machine>("POST", `/v1/machines/${id}/discard-writeback`),
  deleteMachine: (id: number) => request<void>("DELETE", `/v1/machines/${id}`),

  listDiscovered: () => request<Discovered[]>("GET", "/v1/discovery"),
  claimDiscovered: (id: number, body: DiscoveryClaim) =>
    request<Machine>("POST", `/v1/discovery/${id}/claim`, body),
  ignoreDiscovered: (id: number) => request<Discovered>("POST", `/v1/discovery/${id}/ignore`),
};
