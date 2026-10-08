import { vi } from "vitest";

import type { GameDisk, Machine } from "../api";

export function disk(overrides: Partial<GameDisk> = {}): GameDisk {
  return {
    id: 1,
    name: "cs2",
    zvol_path: "tank/ggnet/images/cs2",
    size_gb: 100,
    snapshot: "base",
    published: true,
    editor_id: null,
    created_at: "2026-10-07T12:00:00Z",
    ...overrides,
  };
}

export function machine(overrides: Partial<Machine> = {}): Machine {
  return {
    id: 1,
    name: "pc01",
    mode: "disk",
    initiator_iqn: "iqn.1991-05.com.microsoft:pc01",
    mac: null,
    game_disk_id: null,
    editing_disk_id: null,
    drive_letter: "D",
    reported_drive_letter: null,
    status: "idle",
    last_error: null,
    clone_zvol: null,
    clone_snapshot: null,
    iscsi_target_iqn: null,
    outdated: false,
    last_seen_at: null,
    agent_version: null,
    reported_iqn: null,
    iscsi_connected: null,
    keep_writeback: false,
    writeback_dirty: false,
    session_active: null,
    session_changed_at: null,
    booted_at: null,
    created_at: "2026-10-07T12:00:00Z",
    updated_at: "2026-10-07T12:00:00Z",
    ...overrides,
  };
}

/** Stub fetch with one JSON response for every call and return the mock. */
export function stubFetch(status = 200, body: unknown = {}) {
  const mock = vi.fn().mockImplementation(async () =>
    status === 204 ? new Response(null, { status }) : new Response(JSON.stringify(body), { status }),
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

/** The [url, init] of every fetch call, with the JSON body parsed. */
export function calls(mock: ReturnType<typeof vi.fn>) {
  return mock.mock.calls.map(([url, init]) => ({
    url: url as string,
    method: (init as RequestInit | undefined)?.method,
    body: (init as RequestInit | undefined)?.body
      ? JSON.parse((init as RequestInit).body as string)
      : undefined,
  }));
}
