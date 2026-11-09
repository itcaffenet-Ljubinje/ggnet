import { useEffect, useRef, useState } from "react";
import { Keyboard, Maximize2, X } from "lucide-react";
import RFB from "@novnc/novnc";

import { api, type Machine } from "../api";
import { Modal } from "./Modal";

type State = "connecting" | "connected" | "closed" | "failed";

/** The address of the console websocket, on the server the page came from. */
export function consoleSocketUrl(path: string, loc: Pick<Location, "protocol" | "host"> = window.location): string {
  return `${loc.protocol === "https:" ? "wss" : "ws"}://${loc.host}${path}`;
}

/**
 * The VM's screen, keyboard and mouse in ggNet (noVNC). The server hands out
 * a one-time ticket and the VM's VNC password for this connection; no
 * Proxmox sign-in is needed.
 */
export function VmConsole({ machine: m, onClose }: { machine: Machine; onClose: () => void }) {
  const screenRef = useRef<HTMLDivElement>(null);
  const rfbRef = useRef<RFB | null>(null);
  const [state, setState] = useState<State>("connecting");
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let rfb: RFB | null = null;
    let gone = false;
    api
      .openVmConsole(m.id)
      .then((c) => {
        if (gone || !screenRef.current) return;
        rfb = new RFB(screenRef.current, consoleSocketUrl(c.path), {
          credentials: { password: c.password },
          wsProtocols: ["binary"],
        });
        rfb.scaleViewport = true;
        rfb.addEventListener("connect", () => {
          setState("connected");
          rfb?.focus();
        });
        rfb.addEventListener("disconnect", (e) => {
          const clean = (e as CustomEvent<{ clean: boolean }>).detail?.clean;
          setState(clean ? "closed" : "failed");
        });
        rfb.addEventListener("securityfailure", () => {
          setState("failed");
          setProblem("The VM refused the console password; open the console again.");
        });
        rfbRef.current = rfb;
      })
      .catch((e) => {
        setState("failed");
        setProblem(e instanceof Error ? e.message : String(e));
      });
    return () => {
      gone = true;
      rfb?.disconnect();
      rfbRef.current = null;
    };
  }, [m.id]);

  const label =
    state === "connecting"
      ? "Connecting…"
      : state === "connected"
        ? "Connected"
        : state === "closed"
          ? "Closed (the VM stopped?)"
          : "Not connected";

  return (
    <Modal label={`Console of ${m.name}`} onClose={onClose} className="vm-console-modal">
      <div className="modal-header">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <h2>{m.name}</h2>
          <span className={`badge ${state === "connected" ? "badge-provisioned" : "badge-warn"}`}>{label}</span>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button
            type="button"
            className="secondary small"
            onClick={() => rfbRef.current?.sendCtrlAltDel()}
            disabled={state !== "connected"}
            title="Send Ctrl+Alt+Del to Windows"
          >
            <Keyboard size={14} />
            Ctrl+Alt+Del
          </button>
          <button
            type="button"
            className="secondary small"
            onClick={() => screenRef.current?.requestFullscreen?.().catch(() => undefined)}
            disabled={state !== "connected"}
            title="Full screen (Esc leaves it)"
          >
            <Maximize2 size={14} />
            Full screen
          </button>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
      </div>
      {problem && (
        <div className="banner banner-error" role="alert">
          {problem}
        </div>
      )}
      <div className="vm-console-screen" ref={screenRef} />
    </Modal>
  );
}
