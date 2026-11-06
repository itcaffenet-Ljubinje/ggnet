import { useEffect, useState } from "react";
import { TriangleAlert } from "lucide-react";

import { api, type GameDisk, type ImageUpdate } from "../api";

const POLL_MS = 10_000;

/** What each sign of an unfinished Windows update means, for the badge's tooltip. */
const SIGNS: Record<string, string> = {
  "pending.xml": "Windows\\WinSxS\\pending.xml: update steps left for the next boot",
  RebootPending: "Component Based Servicing waits for a restart",
  PackagesPending: "update packages are not installed yet",
  RebootRequired: "Windows Update waits for a restart",
};

export function updateTitle(signs: string[]): string {
  return (
    "Windows has not finished an update in this version: every PC redoes it at every boot. " +
    "Boot it on one PC with Keep writeback, let Windows finish, shut down and apply.\n\n" +
    signs.map((s) => `• ${SIGNS[s] ?? s}`).join("\n")
  );
}

/** The "Update pending" badge of an OS image version. */
export function UpdateBadge({ signs }: { signs: string[] }) {
  return (
    <span className="badge badge-warn" title={updateTitle(signs)}>
      <TriangleAlert size={12} aria-hidden />
      Update pending
    </span>
  );
}

/**
 * The OS images' active versions with an unfinished Windows update, by disk
 * id; asked again when the images change, and while some are being checked.
 */
export function useImageUpdates(disks: GameDisk[]): Record<number, ImageUpdate> {
  const [updates, setUpdates] = useState<Record<number, ImageUpdate>>({});
  const key = disks
    .filter((d) => d.kind === "os" && d.published)
    .map((d) => `${d.id}@${d.snapshot}`)
    .join(",");

  useEffect(() => {
    if (!key) return;
    let stopped = false;
    let timer: number | undefined;
    async function load() {
      try {
        const list = await api.getImageUpdates();
        if (stopped) return;
        setUpdates(Object.fromEntries(list.map((u) => [u.disk_id, u])));
        if (list.some((u) => u.state === "checking")) timer = window.setTimeout(load, POLL_MS);
      } catch {
        // Only a hint: the list works without it.
      }
    }
    load();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [key]);

  return updates;
}
