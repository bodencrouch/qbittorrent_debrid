import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { ControlApi, QBitService, type TorrentInfo } from "@/api/backend"
import { findAction, type ActionContext } from "@/lib/actions"
import { getErrorMessage } from "@/lib/utils"
import { toast } from "@/lib/toast"

export type ContextMenuAction =
  | "match"
  | "overview"
  | "debrid"
  | "done"

type MenuPos = { x: number; y: number }

interface TorrentContextMenuProps {
  torrent: TorrentInfo | null
  position: MenuPos | null
  onClose: () => void
  onActionDone?: () => void
  onNavigate?: (action: ContextMenuAction, torrent: TorrentInfo) => void
}

type Item =
  | { kind: "sep"; id: string }
  | {
      kind: "item"
      id: string
      label: string
      danger?: boolean
      disabled?: boolean
      run: () => void | Promise<void>
    }
  | {
      kind: "submenu"
      id: string
      label: string
      children: { id: string; label: string; run: () => void | Promise<void> }[]
    }

async function copyText(label: string, text: string) {
  if (!text) {
    toast.error(`Nothing to copy (${label})`)
    return
  }
  try {
    await navigator.clipboard.writeText(text)
    toast.success(`Copied ${label}`)
  } catch {
    toast.error(`Failed to copy ${label}`)
  }
}

export function TorrentContextMenu({
  torrent,
  position,
  onClose,
  onActionDone,
  onNavigate,
}: TorrentContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null)
  const [coords, setCoords] = useState<MenuPos>({ x: 0, y: 0 })
  const [openSub, setOpenSub] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useLayoutEffect(() => {
    if (!position || !ref.current) return
    const el = ref.current
    const pad = 8
    const w = el.offsetWidth
    const h = el.offsetHeight
    const x = Math.min(position.x, window.innerWidth - w - pad)
    const y = Math.min(position.y, window.innerHeight - h - pad)
    setCoords({ x: Math.max(pad, x), y: Math.max(pad, y) })
  }, [position, torrent, openSub])

  useEffect(() => {
    if (!position) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onScroll = () => onClose()
    window.addEventListener("keydown", onKey)
    window.addEventListener("mousedown", onDown)
    window.addEventListener("scroll", onScroll, true)
    return () => {
      window.removeEventListener("keydown", onKey)
      window.removeEventListener("mousedown", onDown)
      window.removeEventListener("scroll", onScroll, true)
    }
  }, [position, onClose])

  if (!torrent || !position) return null

  const run = async (fn: () => void | Promise<void>, okMsg?: string) => {
    if (busy) return
    setBusy(true)
    try {
      await fn()
      if (okMsg) toast.success(okMsg)
      onActionDone?.()
      onClose()
    } catch (err) {
      const msg = getErrorMessage(err)
      if (msg !== "Cancelled") toast.error(msg)
    } finally {
      setBusy(false)
    }
  }

  const actionCtx: ActionContext = {
    torrent,
    health: null,
    onNavigate: (tab, t) => onNavigate?.(tab === "match" ? "match" : tab === "debrid" ? "debrid" : "overview", t),
    onActionDone,
  }

  const catalogIds = ["force-debrid", "nudge", "retry", "skip-auto", "allow-auto", "match-files", "show-overview", "open-webui"] as const
  const catalogItems: Item[] = catalogIds.flatMap((id) => {
    const action = findAction(id)
    if (!action) return []
    const reason = action.disabledReason?.(actionCtx) ?? null
    // Hide Allow auto when not skipped (and Skip when already skipped) for a thinner menu.
    if (reason && (id === "allow-auto" || id === "skip-auto")) return []
    return [
      {
        kind: "item" as const,
        id: action.id,
        label: `qbx: ${action.label}`,
        disabled: !!reason,
        run: () =>
          run(async () => {
            if (reason) throw new Error(reason)
            await action.run(actionCtx)
          }, `${action.label} queued`),
      },
    ]
  })

  const items: Item[] = [
    {
      kind: "item",
      id: "start",
      label: "Start",
      run: () => run(() => ControlApi.resume(torrent.hash), "Started"),
    },
    {
      kind: "item",
      id: "stop",
      label: "Stop",
      run: () => run(() => ControlApi.pause(torrent.hash), "Stopped"),
    },
    {
      kind: "item",
      id: "forceStart",
      label: "Force Start",
      run: () => run(() => ControlApi.forceStart(torrent.hash, true), "Force start enabled"),
    },
    { kind: "sep", id: "sep-remove" },
    {
      kind: "item",
      id: "delete",
      label: "Remove…",
      danger: true,
      run: () =>
        run(async () => {
          const ok = window.confirm(`Remove "${torrent.name}" from qBittorrent?\n(Files on disk are kept.)`)
          if (!ok) throw new Error("Cancelled")
          await ControlApi.delete(torrent.hash, false)
        }, "Removed torrent"),
    },
    {
      kind: "item",
      id: "deleteFiles",
      label: "Remove and delete files…",
      danger: true,
      run: () =>
        run(async () => {
          const ok = window.confirm(
            `Permanently delete files for "${torrent.name}"?\nThis cannot be undone.`,
          )
          if (!ok) throw new Error("Cancelled")
          await ControlApi.delete(torrent.hash, true)
        }, "Removed torrent + files"),
    },
    { kind: "sep", id: "sep-check" },
    {
      kind: "item",
      id: "recheck",
      label: "Force recheck",
      run: () => run(() => QBitService.RecheckTorrent(torrent.hash), "Recheck requested"),
    },
    {
      kind: "item",
      id: "reannounce",
      label: "Force reannounce",
      run: () => run(() => ControlApi.reannounce(torrent.hash), "Reannounce requested"),
    },
    {
      kind: "submenu",
      id: "queue",
      label: "Queue",
      children: [
        {
          id: "queueTop",
          label: "Move to top",
          run: () => run(() => ControlApi.queue(torrent.hash, "top"), "Moved to top"),
        },
        {
          id: "queueUp",
          label: "Move up",
          run: () => run(() => ControlApi.queue(torrent.hash, "up"), "Moved up"),
        },
        {
          id: "queueDown",
          label: "Move down",
          run: () => run(() => ControlApi.queue(torrent.hash, "down"), "Moved down"),
        },
        {
          id: "queueBottom",
          label: "Move to bottom",
          run: () => run(() => ControlApi.queue(torrent.hash, "bottom"), "Moved to bottom"),
        },
      ],
    },
    {
      kind: "submenu",
      id: "copy",
      label: "Copy",
      children: [
        {
          id: "copyName",
          label: "Name",
          run: () => copyText("name", torrent.name).then(onClose),
        },
        {
          id: "copyHash",
          label: "Info hash",
          run: () => copyText("hash", torrent.hash).then(onClose),
        },
        {
          id: "copyMagnet",
          label: "Magnet link",
          run: async () => {
            const detail = await ControlApi.getTorrent(torrent.hash)
            const magnet =
              detail.magnet_uri || `magnet:?xt=urn:btih:${torrent.hash}&dn=${encodeURIComponent(torrent.name)}`
            await copyText("magnet", magnet)
            onClose()
          },
        },
        {
          id: "copyPath",
          label: "Content path",
          run: () => copyText("path", torrent.contentPath || torrent.savePath).then(onClose),
        },
      ],
    },
    { kind: "sep", id: "sep-qbx" },
    ...catalogItems,
  ]

  return (
    <div
      ref={ref}
      role="menu"
      className="fixed z-[100] min-w-[220px] rounded-md border border-border bg-popover text-popover-foreground shadow-lg py-1 text-xs"
      style={{ left: coords.x, top: coords.y }}
      onContextMenu={(e) => e.preventDefault()}
    >
      <div className="px-3 py-1.5 text-[10px] uppercase tracking-wide text-muted-foreground truncate max-w-[280px] border-b border-border/60 mb-1">
        {torrent.name}
      </div>
      {items.map((item) => {
        if (item.kind === "sep") {
          return <div key={item.id} className="my-1 border-t border-border/70" role="separator" />
        }
        if (item.kind === "submenu") {
          return (
            <div
              key={item.id}
              className="relative"
              onMouseEnter={() => setOpenSub(item.id)}
              onMouseLeave={() => setOpenSub((cur) => (cur === item.id ? null : cur))}
            >
              <button
                type="button"
                role="menuitem"
                disabled={busy}
                className="flex w-full items-center justify-between gap-4 px-3 py-1.5 text-left hover:bg-accent disabled:opacity-50"
              >
                <span>{item.label}</span>
                <span className="text-muted-foreground">›</span>
              </button>
              {openSub === item.id && (
                <div className="absolute left-full top-0 ml-0.5 min-w-[160px] rounded-md border border-border bg-popover shadow-lg py-1">
                  {item.children.map((child) => (
                    <button
                      key={child.id}
                      type="button"
                      role="menuitem"
                      disabled={busy}
                      className="block w-full px-3 py-1.5 text-left hover:bg-accent disabled:opacity-50"
                      onClick={() => void child.run()}
                    >
                      {child.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )
        }
        return (
          <button
            key={item.id}
            type="button"
            role="menuitem"
            disabled={busy || item.disabled}
            className={`block w-full px-3 py-1.5 text-left hover:bg-accent disabled:opacity-50 ${
              item.danger ? "text-red-400 hover:text-red-300" : ""
            }`}
            onClick={() => void item.run()}
          >
            {item.label}
          </button>
        )
      })}
    </div>
  )
}
