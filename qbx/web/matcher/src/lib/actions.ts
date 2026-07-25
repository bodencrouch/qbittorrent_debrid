/**
 * Shared action catalog for command bar, palette, and context menu.
 * One definition per op — surfaces only choose which subset to render.
 */

import { ControlApi, QBitService, StorageService, type HealthInfo, type TorrentInfo } from "@/api/backend"
import { uiLog } from "@/lib/ui-log"

export type ActionGroup = "Daemon" | "Torrent" | "Nav" | "Settings" | "Storage"

export type ActionContext = {
  torrent: TorrentInfo | null
  health: HealthInfo | null
  /** Open Settings to a section id (connection, providers, …). */
  openSettings?: (section?: string) => void
  onNavigate?: (tab: "overview" | "match" | "debrid", torrent: TorrentInfo) => void
  /** Switch the shell to the Storage surface (duplicate & hardlink manager). */
  openStorage?: () => void
  onActionDone?: () => void
  refreshHealth?: () => void | Promise<void>
}

export type AppAction = {
  id: string
  label: string
  group: ActionGroup
  tip: string
  shortcut?: string
  /** Shown in the selection command bar. */
  bar?: boolean
  variant?: "default" | "outline" | "destructive"
  /** Return null if enabled; otherwise a short disable reason. */
  disabledReason?: (ctx: ActionContext) => string | null
  run: (ctx: ActionContext) => Promise<void> | void
}

function tagsOf(t: TorrentInfo | null): string[] {
  return (t?.tags || "")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean)
}

function needTorrent(ctx: ActionContext): string | null {
  return ctx.torrent ? null : "Select a torrent"
}

function isPaused(t: TorrentInfo): boolean {
  const s = (t.state || "").toLowerCase()
  return s.includes("paused") || s.includes("stopped")
}

export const APP_ACTIONS: AppAction[] = [
  {
    id: "force-debrid",
    label: "Force debrid",
    group: "Torrent",
    bar: true,
    tip: "Bypass the queue and send this magnet to debrid now. Log: intercept.* / webseed.*",
    shortcut: "F",
    disabledReason: needTorrent,
    async run(ctx) {
      const t = ctx.torrent!
      uiLog("ui.force_debrid", `Force debrid: ${t.name}`, t.hash)
      await ControlApi.intercept(t.hash)
      uiLog("ui.force_debrid.ok", "accepted — follow intercept.* / webseed.*", t.hash)
    },
  },
  {
    id: "nudge",
    label: "Nudge policy",
    group: "Torrent",
    bar: true,
    variant: "outline",
    tip: "Wake a queue-ordered policy pass (does not jump this torrent ahead of higher slots).",
    shortcut: "N",
    disabledReason: needTorrent,
    async run(ctx) {
      const t = ctx.torrent!
      uiLog("ui.nudge", `Nudge: ${t.name}`, t.hash)
      await ControlApi.nudge(t.hash)
      uiLog("ui.nudge.ok", "accepted — follow scan.manual.*", t.hash)
    },
  },
  {
    id: "retry",
    label: "Retry failed",
    group: "Torrent",
    bar: true,
    variant: "outline",
    tip: "Clear qbx-failed / qbx-skip / qbx-done, tag candidate, queue another policy scan.",
    shortcut: "R",
    disabledReason: needTorrent,
    async run(ctx) {
      const t = ctx.torrent!
      uiLog("ui.retry", `Retry: ${t.name}`, t.hash)
      await ControlApi.retry(t.hash)
      uiLog("ui.retry.ok", "accepted — tags cleared; follow scan.manual.*", t.hash)
    },
  },
  {
    id: "skip-auto",
    label: "Skip auto",
    group: "Torrent",
    bar: true,
    variant: "destructive",
    tip: "Tag qbx-skip so auto-debrid never picks this torrent. Force debrid still works.",
    disabledReason: (ctx) => {
      const miss = needTorrent(ctx)
      if (miss) return miss
      if (tagsOf(ctx.torrent).includes("qbx-skip")) return "Already skipped (use Allow auto)"
      return null
    },
    async run(ctx) {
      const t = ctx.torrent!
      uiLog("ui.skip_auto", `Skip auto: ${t.name}`, t.hash)
      await ControlApi.skipAuto(t.hash)
      uiLog("ui.skip_auto.ok", "tagged qbx-skip", t.hash)
    },
  },
  {
    id: "allow-auto",
    label: "Allow auto",
    group: "Torrent",
    bar: true,
    variant: "outline",
    tip: "Remove qbx-skip so the interceptor may auto-debrid again.",
    disabledReason: (ctx) => {
      const miss = needTorrent(ctx)
      if (miss) return miss
      if (!tagsOf(ctx.torrent).includes("qbx-skip")) return "Not skipped"
      return null
    },
    async run(ctx) {
      const t = ctx.torrent!
      uiLog("ui.unskip", `Allow auto: ${t.name}`, t.hash)
      await ControlApi.tags(t.hash, [], ["qbx-skip"])
      uiLog("ui.unskip.ok", "removed qbx-skip", t.hash)
    },
  },
  {
    id: "recheck",
    label: "Recheck",
    group: "Torrent",
    bar: true,
    variant: "outline",
    tip: "Ask qBittorrent to recheck on-disk pieces (useful after rematch).",
    disabledReason: needTorrent,
    async run(ctx) {
      const t = ctx.torrent!
      uiLog("ui.recheck", `Recheck: ${t.name}`, t.hash)
      await QBitService.RecheckTorrent(t.hash)
      uiLog("ui.recheck.ok", "recheck requested", t.hash)
    },
  },
  {
    id: "pause",
    label: "Pause",
    group: "Torrent",
    bar: true,
    variant: "outline",
    tip: "Pause this torrent in qBittorrent.",
    disabledReason: (ctx) => {
      const miss = needTorrent(ctx)
      if (miss) return miss
      if (isPaused(ctx.torrent!)) return "Already paused"
      return null
    },
    async run(ctx) {
      const t = ctx.torrent!
      uiLog("ui.pause", `Pause: ${t.name}`, t.hash)
      await ControlApi.pause(t.hash)
    },
  },
  {
    id: "resume",
    label: "Resume",
    group: "Torrent",
    bar: true,
    variant: "outline",
    tip: "Resume this torrent in qBittorrent.",
    disabledReason: (ctx) => {
      const miss = needTorrent(ctx)
      if (miss) return miss
      if (!isPaused(ctx.torrent!)) return "Already running"
      return null
    },
    async run(ctx) {
      const t = ctx.torrent!
      uiLog("ui.resume", `Resume: ${t.name}`, t.hash)
      await ControlApi.resume(t.hash)
    },
  },
  {
    id: "open-webui",
    label: "Open WebUI",
    group: "Nav",
    variant: "outline",
    tip: "Open the full qBittorrent WebUI (proxied at /qbt/).",
    shortcut: "W",
    run(ctx) {
      const hash = ctx.torrent?.hash
      uiLog("ui.open_webui", hash ? `Open WebUI: ${ctx.torrent!.name}` : "Open WebUI")
      window.open(hash ? `/qbt/#/transfer|${hash}` : "/qbt/", "_blank", "noopener,noreferrer")
    },
  },
  {
    id: "match-files",
    label: "Match files",
    group: "Torrent",
    tip: "Open the Match workspace for this torrent.",
    disabledReason: needTorrent,
    run(ctx) {
      ctx.onNavigate?.("match", ctx.torrent!)
    },
  },
  {
    id: "show-overview",
    label: "Show overview",
    group: "Torrent",
    tip: "Open the Overview workspace for this torrent.",
    disabledReason: needTorrent,
    run(ctx) {
      ctx.onNavigate?.("overview", ctx.torrent!)
    },
  },
  {
    id: "show-debrid",
    label: "Show debrid",
    group: "Torrent",
    tip: "Open the Debrid workspace for this torrent.",
    disabledReason: needTorrent,
    run(ctx) {
      ctx.onNavigate?.("debrid", ctx.torrent!)
    },
  },
  {
    id: "interceptor-toggle",
    label: "Toggle interceptor",
    group: "Daemon",
    tip: "Start or stop the background interceptor.",
    shortcut: "I",
    async run(ctx) {
      if (ctx.health?.interceptor_running) {
        await ControlApi.interceptorStop()
      } else {
        await ControlApi.interceptorStart()
      }
      await ctx.refreshHealth?.()
    },
  },
  {
    id: "scan-now",
    label: "Scan now",
    group: "Daemon",
    tip: "Run a full policy scan now.",
    shortcut: "S",
    async run(ctx) {
      await ControlApi.interceptorScan()
      ctx.onActionDone?.()
    },
  },
  {
    id: "storage-open",
    label: "Go to Storage",
    group: "Storage",
    tip: "Open the exact-content duplicate & hardlink manager.",
    run(ctx) {
      ctx.openStorage?.()
    },
  },
  {
    id: "storage-scan",
    label: "Scan storage for duplicates",
    group: "Storage",
    tip: "Hash size collisions under the configured roots. Log: storage.scan.*",
    async run(ctx) {
      ctx.openStorage?.()
      await StorageService.scan()
    },
  },
  {
    id: "settings-connection",
    label: "Settings: Connection",
    group: "Settings",
    tip: "Open Settings → Connection",
    run(ctx) {
      ctx.openSettings?.("connection")
    },
  },
  {
    id: "settings-providers",
    label: "Settings: Providers",
    group: "Settings",
    tip: "Open Settings → Providers",
    run(ctx) {
      ctx.openSettings?.("providers")
    },
  },
  {
    id: "settings-anonymity",
    label: "Settings: Anonymity",
    group: "Settings",
    tip: "Open Settings → Anonymity",
    run(ctx) {
      ctx.openSettings?.("anonymity")
    },
  },
  {
    id: "settings-interceptor",
    label: "Settings: Interceptor",
    group: "Settings",
    tip: "Open Settings → Interceptor",
    run(ctx) {
      ctx.openSettings?.("interceptor")
    },
  },
  {
    id: "settings-matcher",
    label: "Settings: Matcher",
    group: "Settings",
    tip: "Open Settings → Matcher",
    run(ctx) {
      ctx.openSettings?.("matcher")
    },
  },
  {
    id: "settings-application",
    label: "Settings: Application",
    group: "Settings",
    tip: "Open Settings → Application",
    run(ctx) {
      ctx.openSettings?.("application")
    },
  },
]

export function barActions(): AppAction[] {
  return APP_ACTIONS.filter((a) => a.bar)
}

export function actionsByGroup(group: ActionGroup): AppAction[] {
  return APP_ACTIONS.filter((a) => a.group === group)
}

export function findAction(id: string): AppAction | undefined {
  return APP_ACTIONS.find((a) => a.id === id)
}
