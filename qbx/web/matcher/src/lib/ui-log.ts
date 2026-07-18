/** Push an immediate line into the Control Shell live log (bottom panel). */

export type UiLogDetail = {
  kind: string
  message: string
  hash?: string
  ts?: number
}

export function uiLog(kind: string, message: string, hash?: string): void {
  const detail: UiLogDetail = {
    kind,
    message,
    hash,
    ts: Date.now() / 1000,
  }
  try {
    window.dispatchEvent(new CustomEvent("qbx:ui-log", { detail }))
  } catch {
    /* ignore non-browser */
  }
}

export const UI_LOG_EVENT = "qbx:ui-log"
