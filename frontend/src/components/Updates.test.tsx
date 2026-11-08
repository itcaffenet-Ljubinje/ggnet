import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { UpdateStatus } from "../api";
import { UpdateBanner, Updates } from "./Updates";

const LATEST = { tag: "v0.2.0", version: "0.2.0", name: "ggNet 0.2.0", notes: "- Backup / Restore", url: "https://x/v0.2.0", published_at: "2026-11-08T10:00:00Z" };
const STATUS: UpdateStatus = { current: "0.1.0", latest: LATEST, available: true, can_update: true, reason: null, error: null, job: null };

function stub(status: UpdateStatus) {
  const calls: { method: string; url: string; body?: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ method: init?.method ?? "GET", url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      const running = init?.method === "POST";
      return new Response(JSON.stringify(running ? { ...status, can_update: false, job: { version: "v0.2.0", from_version: "0.1.0", started_at: null, state: "running", exit: null, log: ["==> Downloading ggNet v0.2.0..."] } } : status));
    }),
  );
  return calls;
}

describe("Updates", () => {
  it("shows the newest release with its notes and installs it after confirmation", async () => {
    const calls = stub(STATUS);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Updates />);
    expect(await screen.findByText("ggNet 0.2.0")).toBeInTheDocument();
    expect(screen.getByText("- Backup / Restore")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Install v0.2.0" }));
    expect(calls.find((c) => c.method === "POST")).toMatchObject({ url: "/api/v1/server/update", body: { version: "v0.2.0" } });
    expect(await screen.findByText(/Updating to v0.2.0/)).toBeInTheDocument();
    expect(screen.getByText("==> Downloading ggNet v0.2.0...")).toBeInTheDocument();
  });

  it("says why a development checkout is not updated from here", async () => {
    stub({ ...STATUS, can_update: false, reason: "this ggNet runs from /root/ggnet … updated with git" });
    render(<Updates />);
    expect(await screen.findByText(/updated with git/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Install v0.2.0" })).toBeDisabled();
  });

  it("shows a banner to admins when a newer ggNet is out", async () => {
    stub(STATUS);
    const onOpen = vi.fn();
    render(<UpdateBanner onOpen={onOpen} />);
    await userEvent.click(await screen.findByRole("button", { name: "Settings → Updates" }));
    expect(screen.getByText(/ggNet v0.2.0 is available/)).toBeInTheDocument();
    expect(onOpen).toHaveBeenCalled();
  });
});
