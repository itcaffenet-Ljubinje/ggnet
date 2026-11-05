import { Cpu, Plus } from "lucide-react";

import type { GameDisk } from "../api";

interface Props {
  disk: GameDisk;
  /** Open the Add new hardware wizard. */
  onAddHardware?: () => void;
}

/** The network card models an OS image supports (Add new hardware adds one per board type). */
export function HardwarePanel({ disk, onAddHardware }: Props) {
  const models = disk.nic_models ?? [];
  return (
    <div className="card" aria-label="Supported network cards">
      <h3 className="card-title title-with-icon">
        <Cpu size={16} aria-hidden />
        Supported network cards
        {onAddHardware && (
          <button type="button" className="secondary card-title-action" onClick={onAddHardware}>
            <Plus size={14} />
            Add new hardware
          </button>
        )}
      </h3>
      {models.length === 0 ? (
        <p className="warn-text small">
          None yet: no PC can boot this image. Run Add new hardware once for each board type.
        </p>
      ) : (
        <ul className="nic-list">
          {models.map((name) => (
            <li key={name}>{name}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
