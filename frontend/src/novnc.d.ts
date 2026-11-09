// noVNC ships CommonJS without type definitions; only what the VM console uses.
declare module "@novnc/novnc" {
  export interface RfbOptions {
    credentials?: { password?: string; username?: string; target?: string };
    wsProtocols?: string[];
    shared?: boolean;
  }

  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: RfbOptions);
    scaleViewport: boolean;
    resizeSession: boolean;
    focusOnClick: boolean;
    viewOnly: boolean;
    disconnect(): void;
    sendCtrlAltDel(): void;
    focus(): void;
  }
}
