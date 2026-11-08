import { useState, type FormEvent } from "react";
import { Copy, X } from "lucide-react";

import { api, type Image } from "../api";
import { ErrorBanner } from "./ErrorBanner";

interface Props {
  image: Image;
  onClose: () => void;
  /** After the copy started: reload the list (it shows the progress). */
  onStarted: () => Promise<void>;
}

/** Images → ⋮ → Copy: a new, independent image from the active version or from all versions. */
export function CopyImage({ image, onClose, onStarted }: Props) {
  const [name, setName] = useState(`${image.name}-copy`.slice(0, 32));
  const [all, setAll] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const what = image.kind === "os" ? "OS image" : "game image";

  async function start(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.copyDisk(image.id, name.trim().toLowerCase(), undefined, all);
      await onStarted();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    setBusy(false);
  }

  return (
    <div className="modal-backdrop">
      <div className="modal-content" role="dialog" aria-label={`Copy ${image.name}`}>
        <div className="modal-header">
          <div className="title-with-icon">
            <Copy size={20} aria-hidden />
            <h2>Copy {image.name}</h2>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <form onSubmit={start}>
          <div className="modal-body">
            <ErrorBanner message={error} onClose={() => setError(null)} />
            <label>
              Name of the copy
              <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={32} autoFocus />
            </label>
            <fieldset className="choice-list">
              <label className="check">
                <input type="radio" name="what" checked={!all} onChange={() => setAll(false)} />
                <span>
                  Only the active version @{image.snapshot}
                  <span className="muted small"> · the copy starts at @base, without the history</span>
                </span>
              </label>
              <label className="check">
                <input type="radio" name="what" checked={all} onChange={() => setAll(true)} />
                <span>
                  All versions
                  <span className="muted small"> · the same versions, comments and active version (more space)</span>
                </span>
              </label>
            </fieldset>
            <p className="small muted">
              The copy is a new {what} with its own data: either can be deleted later. It runs in the background (a few
              minutes per 100 GB); Notifications shows its progress.
            </p>
          </div>
          <div className="modal-footer">
            <div className="footer-right">
              <button type="button" className="secondary" onClick={onClose}>
                Cancel
              </button>
              <button type="submit" disabled={busy || !name.trim()}>
                <Copy size={14} />
                {busy ? "Starting…" : "Copy"}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
