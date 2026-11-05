import type { ReactNode } from "react";
import { ArrowLeft, Gamepad2, GitBranch, HardDrive, Layers, Monitor } from "lucide-react";

import { isImport, needsHardware, type GameDisk, type Machine, type Snapshot, type Writeback } from "../api";
import { DiskDetails } from "../components/DiskDetails";
import { HardwarePanel } from "../components/HardwarePanel";
import { formatBytes } from "../format";

interface Props {
  disk: GameDisk;
  machines: Machine[];
  reload: () => Promise<void>;
  onBack: () => void;
  /** The image's ⋮ menu, the same as on the list. */
  menu?: ReactNode;
  /** Open the Add new hardware wizard (OS images). */
  onAddHardware?: () => void;
}

/** One image on its own page: what it is, its snapshots and the PCs' writebacks. */
export function ImageDetailsPage({ disk, machines, reload, onBack, menu, onAddHardware }: Props) {
  const KindIcon = disk.kind === "os" ? Monitor : Gamepad2;
  const editor = machines.find((m) => m.id === disk.editor_id);
  return (
    <section aria-label={`Image ${disk.name}`}>
      <button type="button" className="secondary page-back" onClick={onBack}>
        <ArrowLeft size={14} />
        Images
      </button>

      <div className="details-header">
        <KindIcon size={22} className={`kind-icon ${disk.kind === "os" ? "kind-os" : "kind-game"}`} aria-hidden />
        <h2>{disk.name}</h2>
        <span className="badge">{disk.kind === "os" ? "OS image" : "Game disk"}</span>
        {disk.copy_source && !disk.copy_error ? (
          <span className="badge badge-editing">
            {isImport(disk.copy_source) ? "Importing" : "Copying"} {disk.copy_percent ?? 0}%
          </span>
        ) : disk.copy_error ? (
          <span className="badge badge-error">{isImport(disk.copy_source) ? "Import failed" : "Copy failed"}</span>
        ) : needsHardware(disk) ? (
          <span className="badge badge-warn">Needs hardware</span>
        ) : disk.published ? (
          <span className="badge badge-provisioned">Active @{disk.snapshot}</span>
        ) : editor ? (
          <span className="badge badge-editing">Editing on {editor.name}</span>
        ) : (
          <span className="badge badge-idle">Draft</span>
        )}
        <span className="mono muted small">{disk.zvol_path}</span>
        {menu}
      </div>

      {disk.copy_source ? (
        <p className={disk.copy_error ? "error-text" : "empty"}>
          {disk.copy_error
            ? `${isImport(disk.copy_source) ? "The import" : "The copy"} from ${disk.copy_source} failed: ${disk.copy_error}`
            : `${isImport(disk.copy_source) ? "Importing" : "Copying"} from ${disk.copy_source}: ` +
              `${disk.copy_percent ?? 0}% done. It is published by itself when it ends.`}
        </p>
      ) : disk.published ? (
        <>
          {disk.kind === "os" && <HardwarePanel disk={disk} onAddHardware={onAddHardware} />}
          <DiskDetails
            disk={disk}
            onChanged={reload}
            refreshKey={machines}
            summary={(snapshots, writebacks) => <Summary disk={disk} snapshots={snapshots} writebacks={writebacks} />}
          />
        </>
      ) : (
        <p className="empty">
          A draft has no snapshots or writebacks yet: fill it {disk.kind === "os" ? "" : "on a PC "}and publish it.
        </p>
      )}
    </section>
  );
}

function Summary({ disk, snapshots, writebacks }: { disk: GameDisk; snapshots: Snapshot[]; writebacks: Writeback[] }) {
  const latest = snapshots[snapshots.length - 1];
  const versionsBytes = snapshots.reduce((sum, s) => sum + s.used_bytes, 0);
  const writtenBytes = writebacks.reduce((sum, w) => sum + w.used_bytes, 0);
  const kept = writebacks.filter((w) => w.keep_writeback).length;
  const running = writebacks.filter((w) => w.session_active).length;
  const cards = [
    { title: "Size", value: `${disk.size_gb} GB`, sub: latest ? `${formatBytes(latest.referenced_bytes)} of data` : "", icon: Layers, tone: "blue" },
    {
      title: "Snapshots",
      value: snapshots.length,
      sub: `active @${disk.snapshot}${latest && latest.name !== disk.snapshot ? `, latest @${latest.name}` : ""} · ${formatBytes(versionsBytes)} held`,
      icon: GitBranch,
      tone: "purple",
    },
    {
      title: "Writebacks",
      value: writebacks.length,
      sub: `${running} running · ${kept} kept · ${formatBytes(writtenBytes)} written`,
      icon: HardDrive,
      tone: "amber",
    },
  ];
  return (
    <div className="kpi-grid">
      {cards.map(({ title, value, sub, icon: Icon, tone }) => (
        <div key={title} className="kpi-card">
          <div className="kpi-info">
            <span className="kpi-title">{title}</span>
            <span className="kpi-value">{value}</span>
            <span className="kpi-subtitle">{sub}</span>
          </div>
          <div className={`kpi-icon-box ${tone}`}>
            <Icon size={22} />
          </div>
        </div>
      ))}
    </div>
  );
}
