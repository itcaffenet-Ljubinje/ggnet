import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ServerTask } from "../api";
import { resetUploads, startUpload } from "../uploads";
import { TasksPanel } from "./TasksPanel";

const BACKUP: ServerTask = {
  id: "backup-2", kind: "backup", title: "Backup of hw-test", detail: "USB Ultra USB 3.0 (vfat)", status: "running",
  percent: 25, done_bytes: 3 * 1024 ** 3, total_bytes: 12 * 1024 ** 3, started_at: null, finished_at: null,
  error: null, cancel: "/api/v1/backups/jobs/2/cancel", image_id: 3,
};

let tasks: ServerTask[];
let calls: { method: string; url: string }[];

beforeEach(() => {
  tasks = [BACKUP];
  calls = [];
  resetUploads();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ method: init?.method ?? "GET", url });
      return new Response(JSON.stringify(url.endsWith("/tasks") ? tasks : {}));
    }),
  );
});

afterEach(() => vi.useRealTimers());

describe("TasksPanel", () => {
  it("counts what runs and lists it with its progress, and can cancel it", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<TasksPanel />);
    const button = await screen.findByRole("button", { name: "Background tasks: 1 running" });
    await userEvent.click(button);
    const panel = screen.getByRole("dialog", { name: "Background tasks" });
    expect(within(panel).getByText("Backup of hw-test")).toBeInTheDocument();
    expect(within(panel).getByText("25%")).toBeInTheDocument();
    expect(within(panel).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "25");
    expect(within(panel).getByText(/3.00 GB of 12.0 GB/)).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "Cancel" }));
    expect(calls).toContainEqual({ method: "POST", url: "/api/v1/backups/jobs/2/cancel" });
  });

  it("says when a task ends and reloads the page's data", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onFinished = vi.fn();
    render(<TasksPanel onFinished={onFinished} />);
    await screen.findByRole("button", { name: "Background tasks: 1 running" });
    tasks = [{ ...BACKUP, status: "done", percent: 100, cancel: null }];
    await act(() => vi.advanceTimersByTimeAsync(3100));
    expect(await screen.findByText("Backup of hw-test: completed")).toBeInTheDocument();
    expect(onFinished).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Background tasks" })).toBeInTheDocument();
  });

  it("shows a browser upload that goes on while the import dialog is closed", async () => {
    let finish: (v: unknown) => void = () => {};
    class FakeXhr {
      upload: { onprogress?: (e: { loaded: number; lengthComputable: boolean; total: number }) => void } = {};
      onload?: () => void;
      onerror?: () => void;
      status = 201;
      responseText = JSON.stringify({ path: "/imports/win11.vhdx", name: "win11.vhdx", size: 100, format: "vhdx" });
      open() {}
      send() {
        this.upload.onprogress?.({ loaded: 40, lengthComputable: true, total: 100 });
        finish = () => this.onload?.();
      }
    }
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    tasks = [];
    render(<TasksPanel />);
    let done: Promise<unknown>;
    act(() => {
      done = startUpload(new File([new Uint8Array(100)], "win11.vhdx"));
    });
    await userEvent.click(await screen.findByRole("button", { name: "Background tasks: 1 running" }));
    expect(screen.getByText("Upload of win11.vhdx")).toBeInTheDocument();
    expect(screen.getByText("40%")).toBeInTheDocument();
    await act(async () => {
      finish(undefined);
      await done;
    });
    expect(await screen.findByText("Upload of win11.vhdx: completed")).toBeInTheDocument();
  });
});
