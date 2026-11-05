import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AddHardwareStatus, AddHardwareWizard as Wizard } from "../api";
import { disk } from "../test/fixtures";
import { AddHardwareWizard } from "./AddHardwareWizard";

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
const wizard = (step: Wizard["step"], more: Partial<Wizard> = {}): Wizard => ({
  step,
  active: !["done", "failed"].includes(step),
  machine: step === "waiting" ? null : "komp-11",
  card: null,
  error: null,
  started: 0,
  finished: null,
  ...more,
});

let status: AddHardwareStatus;
let calls: { method: string; url: string }[];

beforeEach(() => {
  status = { wizard: null, job: null, waiting: ["komp-11"] };
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ method, url });
      if (method === "POST") status = { ...status, wizard: wizard("waiting") };
      if (method === "DELETE") status = { ...status, wizard: null };
      return json(status);
    }),
  );
});

afterEach(() => vi.useRealTimers());

describe("AddHardwareWizard", () => {
  it("starts, follows the new PC and finishes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onClose = vi.fn();
    render(<AddHardwareWizard disk={disk({ id: 8, kind: "os", name: "scan-test" })} onClose={onClose} />);
    expect(await screen.findByText(/komp-11/)).toBeInTheDocument();          // waiting on screen already
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(await screen.findByText(/Power on the new PC now/)).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/v1/game-disks/8/add-hardware/wizard"))).toBe(true);

    status = { ...status, wizard: wizard("adding"), job: null };
    await act(() => vi.advanceTimersByTimeAsync(3100));
    expect(await screen.findByText(/komp-11 waits on screen meanwhile/)).toBeInTheDocument();

    status = { ...status, wizard: wizard("done") };
    await act(() => vi.advanceTimersByTimeAsync(3100));
    fireEvent.click(await screen.findByRole("button", { name: "Finish" }));
    await vi.waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(calls.some((c) => c.method === "DELETE")).toBe(true);
  });

  it("shows why it stopped", async () => {
    status = { wizard: wizard("failed", { error: "Windows could not install the hardware" }), job: null, waiting: [] };
    render(<AddHardwareWizard disk={disk({ id: 8, kind: "os" })} onClose={() => {}} />);
    expect(await screen.findByText(/Windows could not install the hardware/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Close" })).toHaveLength(2);   // header × and footer
  });
});
