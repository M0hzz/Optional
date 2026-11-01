// Live updates from GET /api/v1/stream (server-sent events). New events and finished monitoring passes
// refresh the affected queries; warning and critical events raise a toast and, if the user switched them
// on, a browser notification. When the stream is down, views fall back to polling (useRefetchInterval).
import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import type { EventItem } from "../api/types";

export type LiveState = "connecting" | "live" | "offline";
const LiveCtx = createContext<LiveState>("connecting");
const KEY = "optlab.browserNotifications";

export function browserNotificationsOn(): boolean {
  try { return localStorage.getItem(KEY) === "on"; } catch { return false; }
}

export function setBrowserNotifications(on: boolean): void {
  try { localStorage.setItem(KEY, on ? "on" : "off"); } catch { /* private window: per-session only */ }
}

export function notificationSupport(): "unsupported" | "insecure" | NotificationPermission {
  if (typeof window === "undefined" || !("Notification" in window)) return "unsupported";
  if (!window.isSecureContext) return "insecure";
  return Notification.permission;
}

/** Refetch every 30 s while the live stream is down; never while it is up. */
export function useRefetchInterval(ms = 30_000): number | false {
  return useContext(LiveCtx) === "live" ? false : ms;
}

export const useLiveState = () => useContext(LiveCtx);

export function LiveProvider({ children, onEvent }: { children: ReactNode; onEvent?: (e: EventItem) => void }) {
  const qc = useQueryClient();
  const [state, setState] = useState<LiveState>(() => (typeof EventSource === "undefined" ? "offline" : "connecting"));
  const pending = useRef<Set<string>>(new Set());
  const timer = useRef<number | null>(null);
  const cb = useRef(onEvent);
  useEffect(() => { cb.current = onEvent; }, [onEvent]);

  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const flush = () => {
      timer.current = null;
      for (const k of pending.current) qc.invalidateQueries({ queryKey: [k] });
      pending.current.clear();
    };
    const touch = (...keys: string[]) => {
      keys.forEach((k) => pending.current.add(k));
      if (timer.current === null) timer.current = window.setTimeout(flush, 250);
    };
    const es = new EventSource("/api/v1/stream");
    es.addEventListener("hello", () => setState("live"));
    es.addEventListener("event", (m) => {
      const e = JSON.parse((m as MessageEvent).data) as EventItem;
      touch("events", "monitor-status", "overview", "timeline", "positions", "position", "position-monitor", "contracts",
            "contract-monitor", "alert-rules", "plans", "plan", "plan-timeline", "risk");
      cb.current?.(e);
      if (e.notify && browserNotificationsOn() && notificationSupport() === "granted") {
        try { new Notification(e.title, { body: e.message.slice(0, 300), tag: e.id }); } catch { /* ignore */ }
      }
    });
    es.addEventListener("pass", () => touch("positions", "position", "overview", "monitor-status", "snapshots",
                                            "position-monitor", "contract-monitor", "alert-rules", "plans", "plan", "risk"));
    es.onerror = () => setState(es.readyState === EventSource.CLOSED ? "offline" : "connecting");
    es.onopen = () => setState("live");
    return () => { es.close(); if (timer.current !== null) window.clearTimeout(timer.current); };
  }, [qc]);

  return <LiveCtx.Provider value={state}>{children}</LiveCtx.Provider>;
}
