import { useEffect, useState, type FormEvent } from "react";
import { FileOutput, X } from "lucide-react";

import { api, type BackupLocation, type Image, type Snapshot } from "../api";
import { formatBytes } from "../format";
import { ErrorBanner } from "./ErrorBanner";
import { Modal } from "./Modal";

const GiB = 1024 ** 3;
const VHD_MAX = 2040 * GiB;
const FAT32_MAX = 4 * GiB - 1;

interface Props {
  image: Image;
  /** The image's compression ratio: a version's data is its referenced bytes times this. */
  compressRatio?: number | null;
  onClose: () => void;
}

/**
 * Images → ⋮ → Export: one version into a dynamic VHDX or VHD file on a USB
 * disk, a backup folder or the imports folder. The image is only read; the
 * export runs in the background (Notifications).
 */
export function ExportImage({ image, compressRatio, onClose }: Props) {
  const [versions, setVersions] = useState<Snapshot[]>([]);
  const [snapshot, setSnapshot] = useState(image.snapshot ?? "");
  const [format, setFormat] = useState<"vhdx" | "vhd">("vhdx");
  const [locations, setLocations] = useState<BackupLocation[] | null>(null);
  const [location, setLocation] = useState("");
  const [free, setFree] = useState<number | null>(null);
  const [fileName, setFileName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.listSnapshots(image.id).then(setVersions).catch((e) => setError(String(e)));
    api
      .backupLocations(true)
      .then((list) => {
        setLocations(list);
        if (list.length === 1) setLocation(list[0].id);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [image.id]);

  // The file is named after the image and version, unless the admin typed a name.
  useEffect(() => {
    if (!nameTouched) setFileName(`${image.name}-${snapshot}.${format}`);
  }, [image.name, snapshot, format, nameTouched]);

  useEffect(() => {
    setFree(null);
    if (!location) return;
    api
      .openBackupLocation(location, false)
      .then((o) => setFree(o.free_bytes))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [location]);

  const chosen = locations?.find((l) => l.id === location);
  const version = versions.find((v) => v.name === snapshot);
  // The file holds the version's data, at most the disk's size.
  const size = Math.min(
    image.size_gb * GiB,
    version?.referenced_bytes ? version.referenced_bytes * (compressRatio ?? 1) : image.size_gb * GiB,
  );
  const vhdTooBig = format === "vhd" && image.size_gb * GiB > VHD_MAX;
  const fat32 = chosen?.fstype === "vfat" && size > FAT32_MAX;
  const noRoom = free !== null && size > free;
  const nameOk = new RegExp(`^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,120}\\.${format}$`, "i").test(fileName.trim());

  async function start(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.startExport({ image_id: image.id, snapshot, format, location, file_name: fileName.trim() });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    setBusy(false);
  }

  return (
    <Modal label={`Export ${image.name}`} onClose={onClose}>
      <div className="modal-header">
        <div className="title-with-icon">
          <FileOutput size={20} aria-hidden />
          <h2>Export {image.name}</h2>
        </div>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
          <X size={18} />
        </button>
      </div>
      <form onSubmit={start}>
        <div className="modal-body">
          <ErrorBanner message={error} onClose={() => setError(null)} />
          <div className="settings-grid">
            <label>
              Version
              <select value={snapshot} onChange={(e) => setSnapshot(e.target.value)}>
                {versions.length === 0 && <option value={snapshot}>@{snapshot}</option>}
                {versions.map((v) => (
                  <option key={v.name} value={v.name}>
                    @{v.name}
                    {v.active ? " (active)" : ""}
                    {v.comment ? ` · ${v.comment}` : ""}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Format
              <select value={format} onChange={(e) => setFormat(e.target.value as "vhdx" | "vhd")}>
                <option value="vhdx">VHDX (Windows, Hyper-V, Proxmox; up to 64 TB)</option>
                <option value="vhd">VHD (older tools; up to 2040 GB)</option>
              </select>
            </label>
            <label>
              Location
              <select value={location} onChange={(e) => setLocation(e.target.value)} disabled={!locations?.length}>
                {locations && locations.length !== 1 && (
                  <option value="">{locations.length === 0 ? "No USB disk or folder found" : "Choose where…"}</option>
                )}
                {locations?.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              File name
              <input
                value={fileName}
                onChange={(e) => {
                  setNameTouched(true);
                  setFileName(e.target.value);
                }}
                aria-invalid={!nameOk}
              />
            </label>
          </div>
          <p className="small muted">
            About {formatBytes(size)} of data
            {free !== null ? `, ${formatBytes(free)} free there` : ""}. The file is dynamic: it takes only the space
            of the data. The image is only read; PCs keep running.
          </p>
          {vhdTooBig && (
            <p className="small error-text">A VHD holds at most 2040 GB and this image is {image.size_gb} GB: choose VHDX.</p>
          )}
          {fat32 && (
            <p className="small error-text">
              This USB disk has FAT32, which takes no file over 4 GB. Use a disk with exFAT, NTFS or ext4, or the
              imports folder.
            </p>
          )}
          {noRoom && !fat32 && <p className="small error-text">Not enough free space there.</p>}
        </div>
        <div className="modal-footer">
          <div className="footer-right">
            <button type="button" className="secondary" onClick={onClose}>
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy || !location || !snapshot || !nameOk || vhdTooBig || fat32 || noRoom}
            >
              <FileOutput size={14} />
              {busy ? "Starting…" : "Export"}
            </button>
          </div>
        </div>
      </form>
    </Modal>
  );
}
