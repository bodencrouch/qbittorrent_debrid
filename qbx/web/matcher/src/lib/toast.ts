/**
 * Toast facade: sonner when this app renders on its own, relayed to the host
 * page when framed inside the qBittorrent WebUI.
 *
 * The embedded bundle deliberately renders no <Toaster/> — a toast inside a
 * tab column or a modal iframe could be scrolled out of view or clipped by
 * the frame, and the host page can show it over the whole window instead.
 */

import { toast as sonnerToast, type ExternalToast } from "sonner"
import { bridge, isEmbedded } from "@/embed/bridge"

type Level = "success" | "error" | "info" | "warning"

function relay(level: Level, message: string, opts?: ExternalToast) {
  const description = opts && typeof opts.description === "string" ? opts.description : undefined
  bridge.toHost({ v: 1, type: "qbx.toast", level, message, detail: description })
}

export const toast = isEmbedded()
  ? {
      success: (message: string, opts?: ExternalToast) => relay("success", message, opts),
      error: (message: string, opts?: ExternalToast) => relay("error", message, opts),
      info: (message: string, opts?: ExternalToast) => relay("info", message, opts),
      warning: (message: string, opts?: ExternalToast) => relay("warning", message, opts),
      message: (message: string, opts?: ExternalToast) => relay("info", message, opts),
    }
  : sonnerToast
