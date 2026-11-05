import { useCallback, useEffect, useState } from "react";
import { Check, Cpu, Loader2, X } from "lucide-react";

import { api, type AddHardwareStatus, type AddHardwareWizard as Wizard, type GameDisk } from "../api";

const POLL_MS = 3000;

// The wizard's steps as the admin sees them; the server's steps map onto them.
const STEPS = [
  { key: "start", title: "Prepare the new PC" },
  { key: "waiting", title: "Power on the new PC" },
  { key: "scanning", title: "Reading its hardware" },
  { key: "adding", title: "Adding its network card to the image" },
  { key: "rebooting", title: "Restarting the PC" },
  { key: "done", title: "Done" },
] as const;

const STEP_INDEX: Record<Wizard["step"], number> = {
  waiting: 1,
  scanning: 2,
  collected: 2,
  adding: 3,
  rebooting: 4,
  done: 5,
  failed: -1,
};

const JOB_STEPS: Record<string, string> = {
  starting: "Starting…",
  job: "Copying the newest version and writing the job into it…",
  vm: "Windows installs the card in a VM on the server (about a minute)…",
  result: "Reading what Windows did…",
  prepare: "Preparing the new version for a network boot…",
  apply: "Making it the next version…",
  done: "New version made.",
};

interface Props {
  disk: GameDisk;
  onClose: () => void;
  /** After a new version was made: reload the image. */
  onChanged?: () => Promise<void>;
}

/**
 * Add new hardware, step by step: once started, the next PC that boots this
 * image and whose network card the image cannot start reads its hardware in
 * Windows PE; Windows installs the card in a VM on the server, the image gets
 * a new version and the PC boots it. Once per board type.
 */
export function AddHardwareWizard({ disk, onClose, onChanged }: Props) {
  const [status, setStatus] = useState<AddHardwareStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await api.getAddHardware(disk.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [disk.id]);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const wizard = status?.wizard ?? null;
  const done = wizard?.step === "done";
  useEffect(() => {
    if (done && onChanged) onChanged();
  }, [done, onChanged]);

  async function act(call: () => Promise<AddHardwareStatus>) {
    setBusy(true);
    setError(null);
    try {
      setStatus(await call());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  }

  async function close() {
    if (wizard?.active && !window.confirm("Cancel Add new hardware? A running install is stopped.")) return;
    if (wizard) await act(() => api.closeHardwareWizard(disk.id));
    onClose();
  }

  const current = wizard ? STEP_INDEX[wizard.step] : 0;
  const failed = wizard?.step === "failed";
  return (
    <div className="modal-backdrop">
      <div className="modal-content" role="dialog" aria-label={`Add new hardware to ${disk.name}`}>
        <div className="modal-header">
          <div className="title-with-icon">
            <Cpu size={20} aria-hidden />
            <h2>Add new hardware to {disk.name}</h2>
          </div>
          <button type="button" className="icon-btn" onClick={close} aria-label="Close">
            <X size={18} />
          </button>
        </div>

        <div className="modal-body">
          {error && <p className="error-text small">{error}</p>}
          <ol className="wizard-steps">
            {STEPS.map((s, i) => {
              const state = failed ? (i === 0 ? "done" : "") : i < current ? "done" : i === current ? "current" : "";
              return (
                <li key={s.key} className={`wizard-step ${state}`} aria-current={state === "current" ? "step" : undefined}>
                  <span className="wizard-mark">
                    {state === "done" ? (
                      <Check size={14} />
                    ) : state === "current" && i > 0 && i < 5 ? (
                      <Loader2 size={14} className="spin" />
                    ) : (
                      i + 1
                    )}
                  </span>
                  <span>{s.title}</span>
                </li>
              );
            })}
          </ol>

          {failed ? (
            <p className="error-text" role="status">
              Add new hardware stopped: {wizard?.error}
            </p>
          ) : current === 0 ? (
            <>
              <p>Once for each new board type; the other PCs with the same board boot right away after it.</p>
              <p className="warn-text small">
                After Start, the first PC that boots this image goes through Add new hardware, even if the image
                knows its card. Power on only the new PC until it is done.
              </p>
              <ul className="small">
                <li>The new PC is registered in Machines with {disk.name} as its OS image, and is powered off.</li>
                <li>In its BIOS: UEFI network boot (PXE) first, Secure Boot off for this run.</li>
                <li>
                  The image&apos;s newest version is active. PCs keep using it while the card is added; they get the
                  new version at their next boot.
                </li>
              </ul>
              {status && status.waiting.length > 0 && (
                <p className="small">
                  Waiting on screen for this: <span className="strong">{status.waiting.join(", ")}</span>. It goes on
                  by itself after Start.
                </p>
              )}
            </>
          ) : current === 1 ? (
            <p role="status">
              Power on the new PC now (network boot).
              {status && status.waiting.length > 0 && <> {status.waiting.join(", ")} is waiting and goes on by itself.</>}
            </p>
          ) : current === 2 ? (
            <p role="status">
              {wizard?.machine} is in Windows PE reading its hardware
              {wizard?.card ? (
                <>
                  : <span className="mono small">PCI\{wizard.card.nic}\{wizard.card.instance}</span>. It restarts by
                  itself.
                </>
              ) : (
                "…"
              )}
            </p>
          ) : current === 3 ? (
            <p role="status">
              {status?.job ? JOB_STEPS[status.job.step] ?? status.job.step : "Starting…"} {wizard?.machine} waits on
              screen meanwhile.
            </p>
          ) : current === 4 ? (
            <p role="status">The new version is active; {wizard?.machine} boots it now.</p>
          ) : (
            <p role="status">
              {wizard?.machine} booted the new version
              {status?.job?.snapshot ? ` @${status.job.snapshot}` : ""}. PCs with the same board boot this image too.
            </p>
          )}
        </div>

        <div className="modal-footer">
          <div className="footer-right">
            {current === 0 && !failed && (
              <>
                <button type="button" className="secondary" onClick={onClose}>
                  Close
                </button>
                <button
                  type="button"
                  className="btn-primary"
                  disabled={busy || wizard?.active === true}
                  onClick={() => act(() => api.openHardwareWizard(disk.id))}
                >
                  Start
                </button>
              </>
            )}
            {(done || failed) && (
              <button
                type="button"
                className="btn-primary"
                disabled={busy}
                onClick={async () => {
                  await act(() => api.closeHardwareWizard(disk.id));
                  onClose();
                }}
              >
                {done ? "Finish" : "Close"}
              </button>
            )}
            {current > 0 && !done && !failed && (
              <button type="button" className="secondary" disabled={busy} onClick={close}>
                Cancel
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
