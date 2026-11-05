import { useCallback, useEffect, useState, type FormEvent } from "react";
import { FolderOpen, HardDriveDownload, RefreshCw, Trash2, Upload, Usb, X } from "lucide-react";

import { api, uploadImage, type DiskKind, type ImageFile, type ImportSources } from "../api";
import { formatBytes } from "../format";
import { useAsyncAction } from "../useAsyncAction";
import { ErrorBanner } from "./ErrorBanner";

type Source = "folder" | "usb" | "upload";

const ACCEPT = ".vhd,.vhdx,.qcow2,.vmdk,.raw,.img";
const FORMAT_NAMES: Record<string, string> = { vpc: "VHD", vhdx: "VHDX", qcow2: "QCOW2", vmdk: "VMDK", raw: "RAW" };

interface Props {
  onClose: () => void;
  /** After the import started: reload the list (it shows the progress). */
  onStarted: () => Promise<void>;
}

const percent = (u: { sent: number; total: number }) => Math.round((100 * u.sent) / (u.total || 1));

/** A suggested image name from a file name: lower case, a-z 0-9 and -. */
export function nameFromFile(file: string): string {
  return file
    .replace(/\.[^.]+$/, "")
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 32);
}

/**
 * Import an image file (VHD, VHDX, QCOW2, VMDK, RAW) as a new OS image or
 * game disk: from the imports folder on the pool, a USB disk plugged into the
 * server (opened read-only) or an upload from this browser.
 */
export function ImportImage({ onClose, onStarted }: Props) {
  const { busy, error, setError, run } = useAsyncAction();
  const [source, setSource] = useState<Source>("folder");
  const [sources, setSources] = useState<ImportSources | null>(null);
  const [usbFiles, setUsbFiles] = useState<Record<string, ImageFile[]>>({});
  const [chosen, setChosen] = useState<ImageFile | null>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<DiskKind>("os");
  const [prepare, setPrepare] = useState(true);
  const [defenderOff, setDefenderOff] = useState(false);
  const [servicesOff, setServicesOff] = useState(true);
  const [upload, setUpload] = useState<{ name: string; sent: number; total: number } | null>(null);

  const load = useCallback(async () => {
    await run("load", async () => setSources(await api.importSources()));
  }, [run]);

  useEffect(() => {
    load();
  }, [load]);

  function choose(file: ImageFile) {
    setChosen(file);
    if (!name) setName(nameFromFile(file.name));
  }

  async function openUsb(part: string) {
    await run(`open-${part}`, async () => {
      const opened = await api.openUsb(part);
      setUsbFiles({ ...usbFiles, [part]: opened.files });
    });
    await load();
  }

  async function ejectUsb(part: string) {
    await run(`eject-${part}`, () => api.ejectUsb(part));
    const { [part]: _gone, ...rest } = usbFiles;
    setUsbFiles(rest);
    if (chosen && chosen.path.includes(`/usb/${part}/`)) setChosen(null);
    await load();
  }

  async function removeFile(file: ImageFile) {
    if (!window.confirm(`Delete ${file.name} from the imports folder?`)) return;
    await run(`delete-${file.path}`, () => api.deleteImportFile(file.path));
    if (chosen?.path === file.path) setChosen(null);
    await load();
  }

  async function send(file: File) {
    setUpload({ name: file.name, sent: 0, total: file.size });
    const ok = await run("upload", async () => {
      const done = await uploadImage(file, (sent, total) => setUpload({ name: file.name, sent, total }));
      choose(done);
    });
    setUpload(null);
    if (ok) {
      setSource("folder");
      await load();
    }
  }

  async function start(e: FormEvent) {
    e.preventDefault();
    if (!chosen) return;
    const what = kind === "os" ? "OS image" : "game disk";
    const msg =
      `Import ${chosen.name} (${formatBytes(chosen.size)}) as the ${what} "${name}"?\n\n` +
      "It is copied into a new zvol in the background and published when done; the file stays where it is.";
    if (!window.confirm(msg)) return;
    const options = {
      prepare: kind === "os" && prepare,
      defender_off: kind === "os" && prepare && defenderOff,
      services_off: kind === "os" && prepare && servicesOff,
    };
    const ok = await run("import", () => api.startImport(name.trim().toLowerCase(), kind, chosen.path, options));
    if (ok) await onStarted();
  }

  function fileList(files: ImageFile[], deletable: boolean) {
    if (files.length === 0) {
      return <p className="muted small">No image files (.vhd, .vhdx, .qcow2, .vmdk, .raw, .img).</p>;
    }
    return (
      <ul className="file-list" role="radiogroup" aria-label="Image files">
        {files.map((f) => (
          <li key={f.path} className={chosen?.path === f.path ? "chosen" : undefined}>
            <label className="check">
              <input
                type="radio"
                name="image-file"
                checked={chosen?.path === f.path}
                onChange={() => choose(f)}
              />
              <span className="strong">{f.name}</span>
              <span className="muted small">
                {FORMAT_NAMES[f.format] ?? f.format} · {formatBytes(f.size)}
              </span>
            </label>
            {deletable && (
              <button
                type="button"
                className="icon-btn icon-btn-danger"
                onClick={() => removeFile(f)}
                disabled={busy !== null}
                aria-label={`Delete ${f.name}`}
                title="Delete the file"
              >
                <Trash2 size={14} />
              </button>
            )}
          </li>
        ))}
      </ul>
    );
  }

  const tabs: { id: Source; label: string; icon: typeof Usb }[] = [
    { id: "folder", label: "Imports folder", icon: FolderOpen },
    { id: "usb", label: "USB disk", icon: Usb },
    { id: "upload", label: "Upload", icon: Upload },
  ];

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-content"
        role="dialog"
        aria-label="Import image"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <div className="title-with-icon">
            <HardDriveDownload size={20} aria-hidden />
            <h2>Import image</h2>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={start}>
          <div className="modal-body">
            <ErrorBanner message={error} onClose={() => setError(null)} />
            <div className="subnav" role="tablist" aria-label="Import from">
              {tabs.map(({ id, label, icon: Icon }) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={source === id}
                  className={`subnav-btn ${source === id ? "active" : ""}`}
                  onClick={() => setSource(id)}
                >
                  <Icon size={14} />
                  {label}
                </button>
              ))}
            </div>

            {sources === null ? (
              <p className="muted small">Loading…</p>
            ) : source === "folder" ? (
              sources.folder ? (
                <>
                  <p className="muted small">
                    Files in <span className="mono">{sources.folder}</span> (copy them there over the network, e.g.
                    a Samba share or scp).{" "}
                    <button type="button" className="link-btn small" onClick={load} disabled={busy !== null}>
                      <RefreshCw size={12} /> Refresh
                    </button>
                  </p>
                  {fileList(sources.files, true)}
                </>
              ) : (
                <p className="warn-text small">The imports dataset does not exist on the host yet.</p>
              )
            ) : source === "usb" ? (
              sources.usb.length === 0 ? (
                <p className="muted small">
                  No USB disk with a readable file system (NTFS, exFAT, FAT, ext4, XFS). Plug one into the server
                  and refresh.{" "}
                  <button type="button" className="link-btn small" onClick={load} disabled={busy !== null}>
                    <RefreshCw size={12} /> Refresh
                  </button>
                </p>
              ) : (
                sources.usb.map((p) => (
                  <div key={p.name} className="usb-part">
                    <div className="usb-part-head">
                      <span className="strong">{p.label ?? p.name}</span>
                      <span className="muted small">
                        {p.model ?? "USB disk"} · {p.path} · {p.fstype} · {formatBytes(p.size)}
                      </span>
                      {p.mounted ? (
                        <button
                          type="button"
                          className="secondary"
                          onClick={() => ejectUsb(p.name)}
                          disabled={busy !== null}
                        >
                          {busy === `eject-${p.name}` ? "Ejecting…" : "Eject"}
                        </button>
                      ) : (
                        <button type="button" onClick={() => openUsb(p.name)} disabled={busy !== null}>
                          {busy === `open-${p.name}` ? "Opening…" : "Open (read-only)"}
                        </button>
                      )}
                    </div>
                    {p.mounted && usbFiles[p.name] && fileList(usbFiles[p.name], false)}
                  </div>
                ))
              )
            ) : (
              <>
                <p className="muted small">
                  The file goes to the imports folder on the pool and can be imported from there. Large files
                  (50 GB and more) are faster over a Samba share or scp.
                </p>
                <input
                  type="file"
                  accept={ACCEPT}
                  aria-label="Image file to upload"
                  disabled={upload !== null || !sources.folder}
                  onChange={(e) => e.target.files?.[0] && send(e.target.files[0])}
                />
                {upload && (
                  <>
                    <div className="meter" role="meter" aria-label="Upload" aria-valuenow={percent(upload)}>
                      <div style={{ width: `${percent(upload)}%` }} />
                    </div>
                    <p className="muted small">
                      {upload.name}: {formatBytes(upload.sent)} of {formatBytes(upload.total)}
                    </p>
                  </>
                )}
              </>
            )}

            {chosen && (
              <div className="form-row">
                <label>
                  Name
                  <input value={name} onChange={(e) => setName(e.target.value)} maxLength={32} required />
                </label>
                <label>
                  Kind
                  <select value={kind} onChange={(e) => setKind(e.target.value as DiskKind)}>
                    <option value="os">OS image (Boot Mode)</option>
                    <option value="game">Game disk</option>
                  </select>
                </label>
              </div>
            )}
            {chosen && kind === "os" && (
              <>
                <label className="check">
                  <input type="checkbox" checked={prepare} onChange={(e) => setPrepare(e.target.checked)} />
                  Prepare image for all PCs after the import (agent, diskless settings)
                </label>
                {prepare && (
                  <>
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={servicesOff}
                        onChange={(e) => setServicesOff(e.target.checked)}
                      />
                      Turn off background services a cafe PC does not need (faster startup)
                    </label>
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={defenderOff}
                        onChange={(e) => setDefenderOff(e.target.checked)}
                      />
                      Turn off Microsoft Defender
                    </label>
                  </>
                )}
                <p className="muted small">
                  After the import, run Add new hardware once for each board type: until then no PC can boot it.
                </p>
              </>
            )}
          </div>

          <div className="modal-footer">
            <div className="footer-left muted small">{chosen ? `Selected: ${chosen.name}` : "Choose a file"}</div>
            <div className="footer-right">
              <button type="button" className="secondary" onClick={onClose} disabled={busy === "import"}>
                Close
              </button>
              <button type="submit" className="btn-primary" disabled={!chosen || !name.trim() || busy !== null}>
                <HardDriveDownload size={14} />
                {busy === "import" ? "Starting…" : "Import"}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
