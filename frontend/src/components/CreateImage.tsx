import { useEffect, useState, type FormEvent } from "react";
import { Plus, X } from "lucide-react";

import { api, type ImageKind } from "../api";
import { formatBytes } from "../format";
import { ErrorBanner } from "./ErrorBanner";

const GiB = 1024 ** 3;

interface Props {
  onClose: () => void;
  /** After the image was made: reload the list. */
  onCreated: () => Promise<void>;
}

/** Images → Create image: an empty image (a draft) to fill on a PC and publish. */
export function CreateImage({ onClose, onCreated }: Props) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<ImageKind>("game");
  const [sizeGb, setSizeGb] = useState("100");
  const [free, setFree] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .getStorage()
      .then((s) => setFree(typeof s?.available_bytes === "number" ? s.available_bytes : null))
      .catch(() => setFree(null));
  }, []);

  const tooBig = free !== null && Number(sizeGb) * GiB > free;

  async function create(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createDisk(name.trim(), Number(sizeGb), kind);
      await onCreated();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    setBusy(false);
  }

  return (
    <div className="modal-backdrop">
      <div className="modal-content" role="dialog" aria-label="Create image">
        <div className="modal-header">
          <div className="title-with-icon">
            <Plus size={20} aria-hidden />
            <h2>Create image</h2>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <form onSubmit={create}>
          <div className="modal-body">
            <ErrorBanner message={error} onClose={() => setError(null)} />
            <div className="settings-grid">
              <label>
                Name
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. steam-main"
                  required
                  maxLength={32}
                  autoFocus
                />
              </label>
              <label>
                Type
                <select value={kind} onChange={(e) => setKind(e.target.value as ImageKind)}>
                  <option value="game">Game image</option>
                  <option value="os">OS image (Boot Mode)</option>
                </select>
              </label>
              <label>
                Size (GB)
                <input
                  type="number"
                  min={1}
                  max={16384}
                  value={sizeGb}
                  onChange={(e) => setSizeGb(e.target.value)}
                  required
                  aria-invalid={tooBig}
                  aria-describedby="new-image-free"
                />
              </label>
            </div>
            {free !== null && (
              <p id="new-image-free" className={`small ${tooBig ? "error-text" : "muted"}`}>
                {tooBig ? "More than the " : ""}
                {formatBytes(free)} free on the pool
              </p>
            )}
            <p className="small muted">
              The image starts empty (a draft): fill it on a PC, then publish it. It can be grown later (Edit); it can
              never be made smaller. A game image needs room for every PC&apos;s writeback too.
            </p>
          </div>
          <div className="modal-footer">
            <div className="footer-right">
              <button type="button" className="secondary" onClick={onClose}>
                Cancel
              </button>
              <button type="submit" disabled={busy || tooBig || !name.trim()}>
                <Plus size={14} />
                {busy ? "Creating…" : "Create"}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
