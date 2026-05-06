import type { WSMessage } from "./types";

type Listener = (m: WSMessage) => void;

export class DashboardSocket {
  private url: string;
  private listeners: Listener[] = [];
  private ws: WebSocket | null = null;
  private retryMs = 1000;
  private maxRetryMs = 15_000;
  private closed = false;
  private statusListeners: ((s: "connecting" | "open" | "closed") => void)[] =
    [];

  constructor() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    // Vite dev proxy forwards /api/v1/events ws upgrades to :7957.
    this.url = `${proto}//${location.host}/api/v1/events`;
  }

  on(fn: Listener): () => void {
    this.listeners.push(fn);
    return () => {
      this.listeners = this.listeners.filter((l) => l !== fn);
    };
  }

  onStatus(fn: (s: "connecting" | "open" | "closed") => void) {
    this.statusListeners.push(fn);
    return () => {
      this.statusListeners = this.statusListeners.filter((l) => l !== fn);
    };
  }

  private emitStatus(s: "connecting" | "open" | "closed") {
    for (const l of this.statusListeners) l(s);
  }

  connect(token: string | null = null) {
    if (this.ws) return;
    this.closed = false;
    const url = token
      ? `${this.url}?token=${encodeURIComponent(token)}`
      : this.url;
    this.emitStatus("connecting");
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.addEventListener("open", () => {
      this.retryMs = 1000;
      this.emitStatus("open");
    });
    ws.addEventListener("message", (ev) => {
      try {
        const msg = JSON.parse(ev.data) as WSMessage;
        for (const l of this.listeners) l(msg);
      } catch {
        /* swallow */
      }
    });
    ws.addEventListener("close", () => {
      this.ws = null;
      this.emitStatus("closed");
      if (!this.closed) {
        const wait = this.retryMs;
        this.retryMs = Math.min(this.retryMs * 2, this.maxRetryMs);
        setTimeout(() => this.connect(token), wait);
      }
    });
    ws.addEventListener("error", () => {
      try {
        ws.close();
      } catch {
        /* swallow */
      }
    });
  }

  close() {
    this.closed = true;
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* swallow */
      }
      this.ws = null;
    }
  }
}

export const dashboardSocket = new DashboardSocket();
