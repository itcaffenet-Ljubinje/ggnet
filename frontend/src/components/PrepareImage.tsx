import { useState } from "react";
import { Monitor, Wand2, X } from "lucide-react";

import type { GameDisk, PrepareOptions } from "../api";

interface Props {
  disk: GameDisk;
  busy: boolean;
  onClose: () => void;
  onPrepare: (options: PrepareOptions) => void;
}

/** What Prepare image does to an OS image, and its options. */
export function PrepareImage({ disk, busy, onClose, onPrepare }: Props) {
  const [defenderOff, setDefenderOff] = useState(false);
  const [servicesOff, setServicesOff] = useState(true);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-content"
        role="dialog"
        aria-label={`Prepare ${disk.name}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <div className="title-with-icon">
            <Monitor size={20} className="kind-icon kind-os" aria-hidden />
            <h2>Prepare {disk.name} for all PCs</h2>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <div className="modal-body">
          <p className="small">
            A new version is made from the newest one, and PCs move to it at their next reboot. It gets:
          </p>
          <ul className="small">
            <li>the server&apos;s agent (Settings → Client app) and its service</li>
            <li>Fast Startup off, fixed IP addresses switched to DHCP, old drive letters forgotten</li>
            <li>
              High performance, no sleep or hibernation (the display stays on), no PCIe, USB or network card power
              saving
            </li>
            <li>a fixed 4 GB page file, no crash dumps</li>
            <li>no automatic Windows Update, Store updates, maintenance, search index or SysMain</li>
            <li>the iSCSI driver loaded after the network (about 25 s faster starts)</li>
          </ul>
          <label className="check">
            <input type="checkbox" checked={servicesOff} onChange={(e) => setServicesOff(e.target.checked)} />
            Turn off background services a cafe PC does not need (faster startup)
          </label>
          <p className="muted small">
            Delivery Optimization, Update Medic, telemetry, error reporting, diagnostics, Optimize drives, maps,
            location, sensors, BitLocker, scanners and the browsers&apos; updaters; no System Restore, no apps Windows
            installs by itself, no NTFS last-access writes. What games, launchers, anti-cheat and Microsoft/Xbox
            sign-in need stays on, and so does Windows Update for updating the image by hand.
          </p>
          <label className="check">
            <input type="checkbox" checked={defenderOff} onChange={(e) => setDefenderOff(e.target.checked)} />
            Turn off Microsoft Defender
          </label>
          <p className="muted small">
            Defender rebuilds its scan cache and downloads definitions at every start of a diskless PC (about 250 MB
            of writes and a slower start). Its services and drivers are disabled in the image; a restart wipes the PC
            anyway. Leaving this off keeps Defender as it is in the image.
          </p>
        </div>
        <div className="modal-footer">
          <div className="footer-left" />
          <div className="footer-right">
            <button type="button" className="secondary" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={busy}
              onClick={() => onPrepare({ defender_off: defenderOff, services_off: servicesOff })}
            >
              <Wand2 size={14} />
              {busy ? "Preparing…" : "Prepare image"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
