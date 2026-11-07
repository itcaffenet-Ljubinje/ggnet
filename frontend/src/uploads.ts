import { useSyncExternalStore } from "react";

import { uploadImage, type ImageFile } from "./api";

/** A file being sent from this browser into the server's imports folder. */
export interface Upload {
  id: number;
  name: string;
  sent: number;
  total: number;
  started: number;
  status: "running" | "done" | "failed";
  error: string | null;
}

let uploads: Upload[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function set(next: Upload[]) {
  uploads = next;
  listeners.forEach((l) => l());
}

function update(id: number, change: Partial<Upload>) {
  set(uploads.map((u) => (u.id === id ? { ...u, ...change } : u)));
}

/** A page closed or reloaded while files go up loses them: the browser asks first. */
function guard(e: BeforeUnloadEvent) {
  if (uploads.some((u) => u.status === "running")) {
    e.preventDefault();
    e.returnValue = "";
  }
}
if (typeof window !== "undefined") window.addEventListener("beforeunload", guard);

/**
 * Upload a file; it goes on while the web UI is used (the Import dialog can be
 * closed) and shows in Background tasks. Resolves with the file on the server.
 */
export async function startUpload(file: File): Promise<ImageFile> {
  const id = nextId++;
  set([...uploads, { id, name: file.name, sent: 0, total: file.size, started: Date.now(), status: "running", error: null }]);
  try {
    const done = await uploadImage(file, (sent, total) => update(id, { sent, total }));
    update(id, { status: "done", sent: file.size });
    return done;
  } catch (e) {
    update(id, { status: "failed", error: e instanceof Error ? e.message : String(e) });
    throw e;
  }
}

/** Forget the uploads that ended. */
export function clearUploads() {
  set(uploads.filter((u) => u.status === "running"));
}

export function useUploads(): Upload[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => uploads,
  );
}

/** Tests start from nothing. */
export function resetUploads() {
  set([]);
}
