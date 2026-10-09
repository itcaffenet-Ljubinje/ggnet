import type { GameDisk, Machine } from "../api";

interface Props {
  machine: Machine;
  disk: GameDisk | undefined;
}

/** The version part of "pool/ggnet/images/cs2@v2". */
export function versionOf(snapshotPath: string | null): string | null {
  return snapshotPath?.split("@")[1] ?? null;
}

/**
 * ggRock's image status: whether the PC runs the version it should, and
 * where it goes at its next reboot.
 *   green  runs the active version and follows it
 *   red    custom: pinned to a version, or keeps its writeback
 *   arrow  moves to another version at its next reboot
 */
export function ImageStatus({ machine: m, disk }: Props) {
  const current = versionOf(m.clone_snapshot);
  if (m.status !== "provisioned" || !current || !disk?.snapshot) return null;
  const active = disk.snapshot;

  if (m.keep_writeback) {
    const where = current === active ? "" : ` · @${current}`;
    return (
      <span
        className="badge badge-error"
        title={`Keeps its writeback, so it stays on @${current} and does not follow the active version (@${active})`}
      >
        Keep writeback{where}
      </span>
    );
  }

  const target = m.pinned_snapshot ?? active;
  if (current !== target) {
    return (
      <span
        className="badge badge-warn"
        title={
          m.pinned_snapshot
            ? `Pinned to @${target}; moves from @${current} at its next reboot`
            : `Moves from @${current} to the active version @${target} at its next reboot`
        }
      >
        → @{target} at reboot
      </span>
    );
  }
  if (m.pinned_snapshot) {
    return (
      <span
        className="badge badge-error"
        title={`Pinned to @${target}; does not follow the active version (@${active})`}
      >
        Pinned @{target}
      </span>
    );
  }
  return (
    <span className="badge badge-provisioned" title={`Runs the active version @${active}`}>
      @{current}
    </span>
  );
}
