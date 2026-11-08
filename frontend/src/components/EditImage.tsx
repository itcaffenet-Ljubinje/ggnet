import { useState, type FormEvent } from "react";
import { Gamepad2, Monitor, Save, X } from "lucide-react";

import { api, type Image } from "../api";
import { useAsyncAction } from "../useAsyncAction";
import { ErrorBanner } from "./ErrorBanner";
import { Modal } from "./Modal";

const STEPS = [50, 100, 200];
const MAX_GB = 16384;

interface Props {
  disk: Image;
  onClose: () => void;
  /** After a change: reload, and the note to show (what to do in Windows). */
  onSaved: (note: string | null) => Promise<void>;
}

/**
 * Edit an image: its name and its size. A disk only grows: a published one
 * gets a new version with the bigger size and, unless turned off, its data
 * partition (a game image's, or an OS image's C:) grown on the server.
 */
export function EditImage({ disk, onClose, onSaved }: Props) {
  const { busy, error, setError, run } = useAsyncAction();
  const [name, setName] = useState(disk.name);
  const [size, setSize] = useState(disk.size_gb);
  const [extend, setExtend] = useState(true);
  const Icon = disk.kind === "os" ? Monitor : Gamepad2;
  const newName = name.trim().toLowerCase();
  const grows = size > disk.size_gb;
  const changed = (newName !== "" && newName !== disk.name) || grows;

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!changed) return onClose();
    if (grows) {
      const how = disk.published
        ? `A new version @… with ${size} GB is made from the newest one and becomes active; ` +
          "PCs get it at their next reboot. Older versions keep their size."
        : "The draft grows in place.";
      if (!window.confirm(`Grow "${disk.name}" from ${disk.size_gb} GB to ${size} GB?\n\n${how}\n\nA disk cannot be made smaller again.`))
        return;
    }
    let note: string | null = null;
    const ok = await run("save", async () => {
      if (newName && newName !== disk.name) await api.renameDisk(disk.id, newName);
      if (grows) note = (await api.growDisk(disk.id, size, extend)).note;
    });
    if (ok) await onSaved(note);
  }

  return (
    <Modal label={`Edit ${disk.name}`} onClose={onClose} closeOnBackdrop>
      <div className="modal-header">
        <div className="title-with-icon">
          <Icon size={20} className={`kind-icon ${disk.kind === "os" ? "kind-os" : "kind-game"}`} aria-hidden />
          <h2>Edit {disk.name}</h2>
        </div>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
          <X size={18} />
        </button>
      </div>

      <form onSubmit={save}>
        <div className="modal-body">
          <ErrorBanner message={error} onClose={() => setError(null)} />
          <label>
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={32} required />
            <span className="muted small">Only the name in ggNet changes; the zvol and the PCs stay as they are.</span>
          </label>

          <label>
            Size (GB)
            <input
              type="number"
              min={disk.size_gb}
              max={MAX_GB}
              value={size}
              onChange={(e) => setSize(Number(e.target.value))}
              aria-label="Size (GB)"
            />
          </label>
          <div className="actions-row">
            {STEPS.map((step) => (
              <button
                key={step}
                type="button"
                className="secondary"
                onClick={() => setSize(Math.min(MAX_GB, size + step))}
              >
                +{step} GB
              </button>
            ))}
            {grows && (
              <button type="button" className="secondary" onClick={() => setSize(disk.size_gb)}>
                Reset
              </button>
            )}
          </div>
          <p className="muted small">
            Now {disk.size_gb} GB{grows ? ` → ${size} GB (+${size - disk.size_gb} GB)` : ""}. A disk only grows.
          </p>
          {grows && disk.published && (
            <label className="check">
              <input type="checkbox" checked={extend} onChange={(e) => setExtend(e.target.checked)} />
              Extend the data partition on the server too, so Windows needs no work
            </label>
          )}
          {grows && disk.published && extend && disk.kind === "os" && (
            <p className="muted small">
              C: is grown on the server: its Recovery partition is first moved to the end of the disk
              (copied and checked), so Windows still finds it.
            </p>
          )}
        </div>

        <div className="modal-footer">
          <div className="footer-left" />
          <div className="footer-right">
            <button type="button" className="secondary" onClick={onClose} disabled={busy !== null}>
              Close
            </button>
            <button type="submit" className="btn-primary" disabled={busy !== null || !newName}>
              <Save size={14} />
              {busy === "save" ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      </form>
    </Modal>
  );
}
