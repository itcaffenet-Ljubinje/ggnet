// Typed client for the ggNet backend API (/api/v1).

export interface Health {
  status: string;
  version: string;
  pool: string;
  server_ip: string;
  /** "development": runs from a source checkout (git); "installed": from a release in /opt/ggnet. */
  install?: "installed" | "development";
}

/** Prepare image's options beyond what every image gets. */
export interface PrepareOptions {
  defender_off: boolean;
  /** Background services a cafe PC does not need off (faster startup). */
  services_off: boolean;
}

export interface Image {
  id: number;
  name: string;
  zvol_path: string;
  size_gb: number;
  snapshot: string | null;
  published: boolean;
  editor_id: number | null;
  created_at: string;
  /** "os": a Boot Mode Windows image; "game": a game image. Older servers send none (game). */
  kind?: ImageKind;
  /** A copy in progress (or failed): the master@version it is copied from. */
  copy_source?: string | null;
  copy_percent?: number | null;
  copy_error?: string | null;
  /** OS image: the network card models Add new hardware installed; none: it needs hardware. */
  nic_models?: string[];
  /** New PCs get the default OS image and game image (Images → Make default). */
  is_default?: boolean;
}

/** An OS image no PC can boot yet: Add new hardware has not added any network card. */
export const needsHardware = (d: Image) => d.kind === "os" && d.published && (d.nic_models ?? []).length === 0;

export type ImageKind = "game" | "os";

/** Whether a disk's copy_source is an image file being imported (a path), not a version being copied. */
export const isImport = (source: string | null | undefined) => !!source && source.startsWith("/");
/** A new image being restored from a backup (Images → Backup / Restore). */
export const isRestore = (source: string | null | undefined) => !!source && source.startsWith("backup:");

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
  /** The hardware scan's Windows PE is set up (Settings → Hardware scan). */
  winpe_ready: boolean;
}

/** Settings → Hardware scan: Windows PE, the ISOs it can come from and its network drivers. */
export interface WinPeFile {
  name: string;
  present: boolean;
  size: number | null;
}

export interface IsoFile {
  path: string;
  name: string;
  size: number;
}

export interface WinPeDevice {
  /** 8086-15bc */
  chip: string;
  vendor: string;
  name: string;
}

export interface WinPePackage {
  name: string;
  files: number;
  size: number;
  devices: WinPeDevice[];
}

export interface WinPeChip {
  chip: string;
  vendor: string;
  /** From the driver package that covers it. */
  name: string | null;
  package: string | null;
  pcs: number;
}

export interface WinPe {
  folder: string;
  ready: boolean;
  files: WinPeFile[];
  scanner: boolean;
  isos: IsoFile[];
  iso_folders: string[];
  drivers_folder: string;
  packages: WinPePackage[];
  pc_chips: WinPeChip[];
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

/** Upload one file of a Windows PE driver package (the body is the file). */
async function putFile(path: string, file: Blob): Promise<void> {
  const res = await fetch(`/api${path}`, { method: "PUT", body: file });
  if (res.ok) return;
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    // Not JSON (a proxy error page).
  }
  if (res.status === 401) window.dispatchEvent(new Event(SIGNED_OUT));
  throw new ApiError(res.status, errorMessage(res.status, data));
}

/** Add a driver package: start, upload its files one by one, then check and add it. */
export async function uploadDriverPackage(
  name: string,
  files: File[],
  onProgress: (done: number, total: number) => void,
): Promise<WinPePackage> {
  const base = `/v1/winpe/drivers/${encodeURIComponent(name)}`;
  await request<void>("POST", `${base}/upload`);
  for (const [i, f] of files.entries()) {
    onProgress(i, files.length);
    await putFile(`${base}/files/${encodeURIComponent(f.name)}`, f);
  }
  onProgress(files.length, files.length);
  return request<WinPePackage>("POST", base);
}

/** Which kept writebacks Apply Writebacks turns into versions (Boot Mode has two). */
export interface ApplyChoice {
  os?: boolean;
  game?: boolean;
  /** Apply the OS writeback although Windows has not finished an update in it. */
  ignore_update?: boolean;
  /** Set on every version Apply Writebacks makes. */
  comment?: string;
}

/** One version of a game image (a ZFS snapshot of its master). */
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
  /** OS images: signs of an unfinished Windows update ([]: none); null when not checked. */
  update?: string[] | null;
  /** What automatic removal (Settings → Retention) does with it, in words. */
  retention?: string | null;
}

/** The disk a new PC gets from `list`: the default one, else the only one; "" when there is a choice. */
export function preselectDisk(list: Image[]): string {
  const pick = list.find((d) => d.is_default) ?? (list.length === 1 ? list[0] : undefined);
  return pick ? String(pick.id) : "";
}

/** Images → Backup / Restore: a USB disk or a backup folder of the server. */
export interface BackupLocation {
  id: string;
  kind: "usb" | "dir" | "imports";
  label: string;
  size: number | null;
  path: string | null;
  /** A USB disk's file system: vfat (FAT32) takes no file over 4 GB. */
  fstype?: string | null;
}

export interface FoundBackup {
  folder: string;
  image: string;
  kind: "os" | "game";
  created_at: string;
  versions: string[];
  active: string | null;
  bytes: number;
  complete: boolean;
  problem: string | null;
}

export interface BackupLocationOpen {
  id: string;
  path: string;
  free_bytes: number;
  backups: FoundBackup[];
}

export interface BackupJob {
  id: number;
  kind: "backup" | "restore" | "export";
  image_id: number | null;
  image_name: string;
  location: string;
  location_label: string;
  folder: string | null;
  snapshot?: string | null;
  format?: string | null;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  done_bytes: number;
  total_bytes: number | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

/** A job running on the server (Background tasks panel). */
export interface ServerTask {
  id: string;
  kind: "backup" | "restore" | "export" | "copy" | "import" | "hardware";
  title: string;
  detail: string | null;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  percent: number | null;
  done_bytes: number | null;
  total_bytes: number | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  /** POST here to stop it. */
  cancel: string | null;
  image_id: number | null;
}

/** An image's space on the pool, read live from ZFS (Images table). */
export interface ImageUsage {
  image_id: number;
  /** On the pool, with its versions. */
  used_bytes: number;
  /** The disk's size as the PCs see it. */
  volume_bytes: number;
  compress_ratio: number | null;
  versions: number;
}

/** Whether an OS image's active version has an unfinished Windows update. */
export interface ImageUpdate {
  image_id: number;
  snapshot: string;
  state: "none" | "pending" | "checking" | "unknown";
  signs: string[];
  error: string | null;
}

/** The signs of an unfinished Windows update in an Apply Writebacks refusal (409). */
export function updatePending(e: unknown): string[] | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  const detail = (e.data as { detail?: { update_pending?: unknown } } | null)?.detail;
  return Array.isArray(detail?.update_pending) ? (detail.update_pending as string[]) : null;
}

/** A PC's writeback (clone) of a game image. */
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
  /** New PC template: name with {n} or {nn} (the first free number), e.g. komp-{nn}. */
  new_pc_name: string;
  /** Off: every PC boots its own disk (server maintenance); Disk Mode PCs are not affected. */
  network_boot: boolean;
  /** Game image of a new PC; null: unknown PCs are not registered by Add new hardware. */
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
/** Settings → Updates: the installed and the newest ggNet. */
export interface UpdateStatus {
  current: string;
  latest: { tag: string; version: string; name: string; notes: string; url: string; published_at: string } | null;
  available: boolean;
  can_update: boolean;
  reason: string | null;
  error: string | null;
  job: {
    version: string | null;
    from_version: string | null;
    started_at: string | null;
    state: "running" | "done" | "failed";
    exit: number | null;
    log: string[];
  } | null;
}

/** The web UI's HTTPS (Settings → Network boot). */
export interface TlsStatus {
  enabled: boolean;
  url: string | null;
  subject: string | null;
  names: string[];
  not_after: string | null;
  sha256: string | null;
  self_signed: boolean;
  /** After a new certificate: ggNet restarts to use it. */
  restarting?: boolean;
}

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

/** Settings → General: one of ggNet's services. */
export interface HostService {
  unit: string;
  label: string;
  purpose: string;
  state: string;
  sub_state: string;
  since: string | null;
  restartable: boolean;
  note: string | null;
}

export interface General {
  hostname: string;
  version: string;
  install: "installed" | "development";
  app_dir: string;
  config_file: string;
  server_ip: string;
  proxmox: string | null;
  kernel: string;
  uptime_seconds: number | null;
  database: {
    kind: string;
    version: string | null;
    size_bytes: number | null;
    backups: { name: string; size_bytes: number; at: string }[];
  };
  services: HostService[];
  guests: string[];
  online_pcs: string[];
  running_tasks: string[];
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
  /** Disk Mode PCs update their agent from this package. */
  auto_update: boolean;
  /** Disk Mode machines whose agent is older than the package. */
  outdated: string[];
}

export type BulkKind =
  | "on"
  | "shutdown"
  | "reboot"
  | "assign"
  | "pin"
  | "drive_letter"
  | "stop_keep_writeback"
  | "hide"
  | "show"
  | "discard"
  | "mode"
  | "delete"
  | "assign_os";

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
  game_image_id: number | null;
  os_image_id?: number | null;      // Boot Mode: the OS image
  os_clone_snapshot?: string | null; // Boot Mode: the OS image version the PC runs
  editing_image_id: number | null;
  drive_letter: string;
  reported_drive_letter: string | null;
  pinned_snapshot: string | null;
  pending_command: "shutdown" | "reboot" | null;
  reported_ip: string | null;
  reported_mac: string | null;
  link_speed_mbps: number | null;
  /** Kept out of the Machines list until Show hidden. */
  hidden: boolean;
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
  game_image_id?: number | null;
  os_image_id?: number | null;     // Boot Mode: the OS image
}

export interface MachineCreate {
  name: string;
  mode?: MachineMode;
  /** Identifies the PC; the server derives the initiator IQN from it. */
  mac?: string | null;
  game_image_id?: number | null;
  os_image_id?: number | null;     // Boot Mode: the OS image
  drive_letter?: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** The response body, e.g. {detail: {error, update_pending}}. */
    readonly data: unknown = null,
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
  if (!res.ok) throw new ApiError(res.status, errorMessage(res.status, data), data);
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

  listDisks: () => request<Image[]>("GET", "/v1/images"),
  createDisk: (name: string, size_gb: number, kind: ImageKind = "game") =>
    request<Image>("POST", "/v1/images", { name, size_gb, kind }),
  publishDisk: (id: number) => request<Image>("POST", `/v1/images/${id}/publish`),
  startEdit: (id: number, machine_id: number) =>
    request<Image>("POST", `/v1/images/${id}/edit`, { machine_id }),
  finishEdit: (id: number) => request<Image>("POST", `/v1/images/${id}/finish-edit`),
  deleteDisk: (id: number) => request<void>("DELETE", `/v1/images/${id}`),
  renameDisk: (id: number, name: string) => request<Image>("PATCH", `/v1/images/${id}`, { name }),
  growDisk: (id: number, size_gb: number, extend = true) =>
    request<{ disk: Image; version: string | null; extended: boolean; note: string }>(
      "POST",
      `/v1/images/${id}/grow`,
      { size_gb, extend },
    ),
  importSources: () => request<ImportSources>("GET", "/v1/imports"),
  openUsb: (part: string) =>
    request<{ part: string; folder: string; files: ImageFile[] }>("POST", `/v1/imports/usb/${part}/open`),
  ejectUsb: (part: string) => request<void>("POST", `/v1/imports/usb/${part}/eject`),
  deleteImportFile: (path: string) =>
    request<void>("DELETE", `/v1/imports/file?path=${encodeURIComponent(path)}`),
  startImport: (name: string, kind: ImageKind, path: string, options: { prepare: boolean } & PrepareOptions) =>
    request<Image>("POST", "/v1/imports", { name, kind, path, ...options }),
  cancelImport: (id: number) => request<void>("POST", `/v1/imports/${id}/cancel`),
  getAddHardware: (id: number) => request<AddHardwareStatus>("GET", `/v1/images/${id}/add-hardware`),
  checkHardware: (id: number) => request<Image>("POST", `/v1/images/${id}/check-hardware`),
  openHardwareWizard: (id: number) =>
    request<AddHardwareStatus>("POST", `/v1/images/${id}/add-hardware/wizard`),
  closeHardwareWizard: (id: number) =>
    request<AddHardwareStatus>("DELETE", `/v1/images/${id}/add-hardware/wizard`),
  addHardware: (id: number, cards: HardwareCard[]) =>
    request<AddHardwareJob>("POST", `/v1/images/${id}/add-hardware`, { cards }),
  cancelAddHardware: (id: number) =>
    request<AddHardwareJob | null>("POST", `/v1/images/${id}/add-hardware/cancel`),
  /** A copy of one version (default: the active one), or of all versions. */
  copyDisk: (id: number, name: string, snapshot?: string, allVersions = false) =>
    request<Image>("POST", `/v1/images/${id}/copy`, {
      name,
      ...(snapshot ? { snapshot } : {}),
      ...(allVersions ? { all_versions: true } : {}),
    }),
  listSnapshots: (id: number) => request<Snapshot[]>("GET", `/v1/images/${id}/snapshots`),
  setActiveSnapshot: (id: number, snapshot: string) =>
    request<Image>("PUT", `/v1/images/${id}/active-snapshot`, { snapshot }),
  deleteSnapshot: (id: number, name: string) =>
    request<void>("DELETE", `/v1/images/${id}/snapshots/${encodeURIComponent(name)}`),
  setSnapshotComment: (id: number, name: string, comment: string) =>
    request<Snapshot>("PUT", `/v1/images/${id}/snapshots/${encodeURIComponent(name)}/comment`, { comment }),
  setSnapshotLock: (id: number, name: string, locked: boolean) =>
    request<Snapshot>("PUT", `/v1/images/${id}/snapshots/${encodeURIComponent(name)}/lock`, { locked }),
  prepareImage: (id: number, options?: PrepareOptions) =>
    request<Image>("POST", `/v1/images/${id}/prepare`, options),
  /** Make a version active and delete every newer one. */
  deleteNewerSnapshots: (id: number, name: string) =>
    request<Image>("POST", `/v1/images/${id}/snapshots/${encodeURIComponent(name)}/delete-newer`),
  mergeIntoBase: (id: number, name: string) =>
    request<Image>("POST", `/v1/images/${id}/snapshots/${encodeURIComponent(name)}/merge-into-base`),
  listWritebacks: (id: number) => request<Writeback[]>("GET", `/v1/images/${id}/writebacks`),

  getTasks: () => request<ServerTask[]>("GET", "/v1/tasks"),
  /** Stop a task by the cancel path the server gave (/api/v1/...). */
  cancelTask: (path: string) => request<unknown>("POST", path.replace(/^\/api/, "")),
  backupLocations: (exports = false) =>
    request<BackupLocation[]>("GET", `/v1/backups/locations${exports ? "?exports=true" : ""}`),
  startExport: (body: { image_id: number; snapshot: string; format: "vhdx" | "vhd"; location: string; file_name: string }) =>
    request<BackupJob>("POST", "/v1/backups/export", body),
  openBackupLocation: (id: string, writable: boolean) =>
    request<BackupLocationOpen>("POST", `/v1/backups/locations/${encodeURIComponent(id)}/open?writable=${writable}`),
  ejectBackupLocation: (id: string) =>
    request<void>("POST", `/v1/backups/locations/${encodeURIComponent(id)}/eject`),
  backupEstimate: (imageId: number) =>
    request<{ image_id: number; bytes: number | null }>("GET", `/v1/backups/estimate?image_id=${imageId}`),
  startBackup: (imageIds: number[], location: string) =>
    request<BackupJob[]>("POST", "/v1/backups", { image_ids: imageIds, location }),
  startRestore: (location: string, folder: string, name: string) =>
    request<BackupJob>("POST", "/v1/backups/restore", { location, folder, name }),
  backupJobs: () => request<BackupJob[]>("GET", "/v1/backups/jobs"),
  cancelBackupJob: (id: number) => request<BackupJob>("POST", `/v1/backups/jobs/${id}/cancel`),
  getImageUsage: () => request<ImageUsage[]>("GET", "/v1/images/usage"),
  setDefaultDisk: (id: number, isDefault: boolean) =>
    request<Image>("PUT", `/v1/images/${id}/default`, { default: isDefault }),
  getImageUpdates: () => request<ImageUpdate[]>("GET", "/v1/images/updates"),
  getAgentPackage: () => request<AgentPackage>("GET", "/v1/agent/package"),
  setAgentAutoUpdate: (auto_update: boolean) =>
    request<{ auto_update: boolean }>("PUT", "/v1/settings/agent", { auto_update }),
  getWinPe: () => request<WinPe>("GET", "/v1/winpe"),
  winPeFromIso: (path: string) => request<WinPe>("POST", "/v1/winpe/from-iso", { path }),
  removeDriverPackage: (name: string) => request<void>("DELETE", `/v1/winpe/drivers/${encodeURIComponent(name)}`),
  getStorage: () => request<Storage>("GET", "/v1/storage"),
  getServer: () => request<ServerStatus>("GET", "/v1/server"),
  getGeneral: () => request<General>("GET", "/v1/server/general"),
  restartService: (unit: string) =>
    request<{ restarting_self: boolean; services: HostService[] }>(
      "POST",
      `/v1/server/services/${encodeURIComponent(unit)}/restart`,
    ),
  rebootServer: (confirm: string) => request<{ rebooting_in_seconds: number }>("POST", "/v1/server/reboot", { confirm }),
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
  getUpdate: (check = false) => request<UpdateStatus>("GET", `/v1/server/update${check ? "?check=true" : ""}`),
  startUpdate: (version: string) => request<UpdateStatus>("POST", "/v1/server/update", { version }),
  getTls: () => request<TlsStatus>("GET", "/v1/settings/tls"),
  newCertificate: (names: string[]) => request<TlsStatus>("PUT", "/v1/settings/tls", { names }),
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
  /** Game image, and in Boot Mode the OS image (not sent keeps the current one). */
  assignDisk: (id: number, game_image_id: number | null, os_image_id?: number | null) =>
    request<Machine>(
      "POST",
      `/v1/machines/${id}/assign`,
      os_image_id === undefined ? { game_image_id } : { game_image_id, os_image_id },
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
    extra: {
      game_image_id?: number | null;
      os_image_id?: number;
      snapshot?: string | null;
      drive_letter?: string;
      mode?: MachineMode;
    } = {},
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

/**
 * Apply Writebacks; when Windows has not finished an update in the OS
 * writeback, the server refuses and the admin decides: restart the PC once
 * more (recommended) or apply anyway. Null when the admin did not apply.
 */
export async function applyWritebacksChecked(id: number, choice?: ApplyChoice): Promise<Machine | null> {
  try {
    return await api.applyWritebacks(id, choice);
  } catch (e) {
    if (updatePending(e) === null) throw e;
    const msg = `${(e as Error).message}\n\nApply anyway? Not recommended.`;
    if (!window.confirm(msg)) return null;
    return api.applyWritebacks(id, { ...choice, ignore_update: true });
  }
}
