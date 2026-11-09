import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Machine } from "../api";
import { VmConsole, consoleSocketUrl } from "./VmConsole";

const made: { url: string; options: unknown; rfb: EventTarget }[] = [];

vi.mock("@novnc/novnc", () => ({
  default: class extends EventTarget {
    scaleViewport = false;
    constructor(_target: HTMLElement, url: string, options: unknown) {
      super();
      made.push({ url, options, rfb: this });
    }
    disconnect() {}
    focus() {}
    sendCtrlAltDel() {}
  },
}));

const VM = { id: 7, name: "work-vm", vm_id: 104 } as Machine;

describe("VmConsole", () => {
  it("connects over the page's own server with the one-time password", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async () =>
      new Response(JSON.stringify({ path: "/api/v1/vm-console/abc", password: "Xy3kPq7z", expires_in: 30 }),
        { status: 200 })));
    made.length = 0;
    render(<VmConsole machine={VM} onClose={() => undefined} />);
    await vi.waitFor(() => expect(made).toHaveLength(1));
    expect(made[0].url).toBe(consoleSocketUrl("/api/v1/vm-console/abc"));
    expect(made[0].options).toEqual({ credentials: { password: "Xy3kPq7z" }, wsProtocols: ["binary"] });
    made[0].rfb.dispatchEvent(new Event("connect"));
    expect(await screen.findByText("Connected")).toBeInTheDocument();
  });

  it("uses wss on an https page", () => {
    expect(consoleSocketUrl("/api/v1/vm-console/k", { protocol: "https:", host: "192.168.0.40:8443" }))
      .toBe("wss://192.168.0.40:8443/api/v1/vm-console/k");
    expect(consoleSocketUrl("/x", { protocol: "http:", host: "h:8088" })).toBe("ws://h:8088/x");
  });

  it("says why it could not connect", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async () =>
      new Response(JSON.stringify({ detail: { error: "'work-vm' is not running; start it first" } }), { status: 409 })));
    render(<VmConsole machine={VM} onClose={() => undefined} />);
    expect(await screen.findByText(/is not running/)).toBeInTheDocument();
  });
});
