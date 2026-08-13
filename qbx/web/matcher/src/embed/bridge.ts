/**
 * postMessage protocol between the qBittorrent WebUI (host, in qbx-inject.js)
 * and this app when it's framed as an iframe (?panel=...).
 *
 * Every message carries an explicit target origin — never "*" — and every
 * handler checks ev.origin. The host side additionally checks ev.source,
 * because the WebUI hosts several same-origin iframes (Add Torrent,
 * Preferences, ...) and an origin check alone would let any of them spoof us.
 */

export const BRIDGE_VERSION = 1 as const

export type HostToShellMessage =
  | { v: 1; type: "qbx.host.hello"; theme: "light" | "dark"; selection: string[]; activeHash: string | null }
  | { v: 1; type: "qbx.host.selection"; selection: string[]; activeHash: string | null }
  | { v: 1; type: "qbx.host.theme"; theme: "light" | "dark" }
  | { v: 1; type: "qbx.host.panel"; panel: string; hash?: string; section?: string }
  | { v: 1; type: "qbx.host.activated" }
  | { v: 1; type: "qbx.host.deactivated" }
  // Tolerate the pre-bridge shape for one release; App.tsx (standalone) still
  // posts/consumes it directly.
  | { type: "qbx.selectTorrent"; hash: string }

export type ShellToHostMessage =
  | { v: 1; type: "qbx.ready"; panel: string }
  | { v: 1; type: "qbx.toast"; level: "success" | "error" | "info" | "warning"; message: string; detail?: string }
  | { v: 1; type: "qbx.selectTorrent"; hash: string }
  | { v: 1; type: "qbx.openWindow"; window: "settings" | "match" | "debrid"; hash?: string; section?: string }
  | { v: 1; type: "qbx.closeWindow" }
  | { v: 1; type: "qbx.switchTab"; tab: "transfers" | "search" | "rss" | "log" | "qbx" }
  | { v: 1; type: "qbx.error"; status: number; message: string }

export function isEmbedded(): boolean {
  try {
    return window.parent !== window
  } catch {
    return true
  }
}

function hostOrigin(): string {
  return window.location.origin
}

function post(message: ShellToHostMessage): void {
  if (!isEmbedded()) return
  window.parent.postMessage(message, hostOrigin())
}

type Listener = (msg: HostToShellMessage) => void

const listeners = new Set<Listener>()
let wired = false

function ensureWired(): void {
  if (wired) return
  wired = true
  window.addEventListener("message", (ev: MessageEvent) => {
    if (ev.origin !== hostOrigin()) return
    if (ev.source !== window.parent) return
    const data = ev.data as HostToShellMessage | undefined
    if (!data || typeof data.type !== "string") return
    listeners.forEach((fn) => fn(data))
  })
}

/** Subscribe to every message from the host. Returns an unsubscribe function. */
function onHost(fn: Listener): () => void {
  ensureWired()
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** Subscribe to one message type only. */
function onHostType<T extends HostToShellMessage["type"]>(
  type: T,
  fn: (msg: Extract<HostToShellMessage, { type: T }>) => void,
): () => void {
  return onHost((msg) => {
    if (msg.type === type) fn(msg as Extract<HostToShellMessage, { type: T }>)
  })
}

export const bridge = {
  toHost: post,
  onHost,
  onHostType,
}
