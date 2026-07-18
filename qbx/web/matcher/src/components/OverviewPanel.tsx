import { useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { formatSize, getErrorMessage } from "@/lib/utils"
import { uiLog } from "@/lib/ui-log"
import { ControlApi, QBitService, type TorrentInfo } from "@/api/backend"
import { toast } from "sonner"

interface OverviewPanelProps {
  torrent: TorrentInfo
  onActionDone?: () => void
}

const OK_HINT: Record<string, string> = {
  "ui.force_debrid": "accepted — follow intercept.* / webseed.* below",
  "ui.nudge": "accepted — follow scan.manual.* then intercept.* if this torrent is eligible",
  "ui.retry": "accepted — tags cleared; follow scan.manual.* / intercept.* below",
  "ui.skip_auto": "accepted — tagged qbx-skip (auto-debrid off; Force debrid still works)",
  "ui.recheck": "accepted — qBittorrent recheck requested (qbt.recheck)",
}

function ActionButton({
  label,
  tip,
  disabled,
  variant = "default",
  onClick,
}: {
  label: string
  tip: string
  disabled?: boolean
  variant?: "default" | "outline" | "destructive"
  onClick: () => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          size="sm"
          variant={variant}
          disabled={disabled}
          onClick={onClick}
          title={tip}
          aria-label={`${label}. ${tip}`}
        >
          {label}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs text-left normal-case font-normal">
        {tip}
      </TooltipContent>
    </Tooltip>
  )
}

export function OverviewPanel({ torrent, onActionDone }: OverviewPanelProps) {
  const [detail, setDetail] = useState<(TorrentInfo & { properties?: any; webseeds?: { url: string }[] }) | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    ControlApi.getTorrent(torrent.hash)
      .then((d) => {
        if (!cancelled) setDetail(d)
      })
      .catch((err) => toast.error(getErrorMessage(err)))
    return () => {
      cancelled = true
    }
  }, [torrent.hash])

  const run = async (label: string, kind: string, fn: () => Promise<unknown>) => {
    setBusy(label)
    uiLog(kind, `${label}: ${torrent.name}`, torrent.hash)
    try {
      await fn()
      uiLog(`${kind}.ok`, `${label} ${OK_HINT[kind] || "accepted"}`, torrent.hash)
      toast.success(`${label} queued`)
      onActionDone?.()
      const d = await ControlApi.getTorrent(torrent.hash)
      setDetail(d)
    } catch (err) {
      uiLog(`${kind}.failed`, `${label} failed: ${getErrorMessage(err)}`, torrent.hash)
      toast.error(getErrorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const t = detail || torrent
  const props = detail?.properties || {}

  return (
    <TooltipProvider delayDuration={250}>
      <div className="h-full overflow-auto p-3 space-y-4 text-sm">
        <div>
          <h2 className="text-base font-semibold leading-tight break-all">{t.name}</h2>
          <p className="text-xs text-muted-foreground font-mono mt-1">{t.hash}</p>
          <div className="flex flex-wrap gap-2 mt-2">
            <Badge variant="outline">{t.state}</Badge>
            {t.qbx_status && <Badge>{t.qbx_status}</Badge>}
            {(t.tags || "")
              .split(",")
              .map((x) => x.trim())
              .filter(Boolean)
              .map((tag) => (
                <Badge key={tag} variant="secondary" className="text-[10px]">
                  {tag}
                </Badge>
              ))}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <Stat label="Progress" value={`${(t.progress * 100).toFixed(2)}%`} />
          <Stat label="Size" value={formatSize(t.size)} />
          <Stat label="↓ / ↑" value={`${formatSize(t.dlspeed || 0)}/s · ${formatSize(t.upspeed || 0)}/s`} />
          <Stat label="Ratio" value={(t.ratio ?? 0).toFixed(3)} />
          <Stat label="Save path" value={t.savePath || "—"} />
          <Stat label="Category" value={t.category || "—"} />
          <Stat label="Seeds / peers" value={`${t.num_seeds ?? 0} / ${t.num_leechs ?? 0}`} />
          <Stat label="Webseeds" value={String(detail?.webseeds?.length ?? "…")} />
          {t.qbx_reason && <Stat label="qbx reason" value={t.qbx_reason} />}
          {props.total_downloaded != null && (
            <Stat label="Downloaded" value={formatSize(Number(props.total_downloaded))} />
          )}
        </div>

        <div className="flex flex-wrap gap-2">
          <ActionButton
            label="Force debrid"
            tip="Bypass the queue and send this torrent’s magnet to your debrid provider now. On success, unrestricted HTTP links are injected as qBittorrent webseeds (or downloaded, depending on delivery mode). Progress appears in the live log as intercept.* and webseed.* events."
            disabled={!!busy}
            onClick={() => run("Force debrid", "ui.force_debrid", () => ControlApi.intercept(t.hash))}
          />
          <ActionButton
            label="Nudge policy"
            tip="Wake a queue-ordered policy pass (same as qbx nudge). Does not jump this torrent ahead of higher queue slots — only eligible stalled items are debrided. Live log: ui.nudge → nudge → scan.manual.start/complete, then intercept.* if something runs."
            disabled={!!busy}
            variant="outline"
            onClick={() => run("Nudge policy", "ui.nudge", () => ControlApi.nudge(t.hash))}
          />
          <ActionButton
            label="Retry failed"
            tip="Clear qbx-failed / qbx-skip / qbx-done tags, mark as candidate, and queue another policy scan so debrid can try again."
            disabled={!!busy}
            variant="outline"
            onClick={() => run("Retry failed", "ui.retry", () => ControlApi.retry(t.hash))}
          />
          <ActionButton
            label="Skip auto"
            tip="Tag this torrent qbx-skip so the interceptor will never auto-debrid it. Manual Force debrid still works."
            disabled={!!busy}
            variant="destructive"
            onClick={() => run("Skip auto", "ui.skip_auto", () => ControlApi.skipAuto(t.hash))}
          />
          <ActionButton
            label="Recheck"
            tip="Ask qBittorrent to recheck on-disk pieces for this torrent (useful after file rematch / rename)."
            disabled={!!busy}
            variant="outline"
            onClick={() => run("Recheck", "ui.recheck", () => QBitService.RecheckTorrent(t.hash))}
          />
          <ActionButton
            label="Open WebUI"
            tip="Open the full qBittorrent WebUI in a new browser window (proxied at /qbt/). The Control Shell no longer embeds WebUI."
            disabled={!!busy}
            variant="outline"
            onClick={() => {
              uiLog("ui.open_webui", `Open WebUI: ${torrent.name}`, torrent.hash)
              window.open("/qbt/", "_blank", "noopener,noreferrer")
            }}
          />
        </div>
        {busy && <p className="text-xs text-muted-foreground">Working: {busy}…</p>}
      </div>
    </TooltipProvider>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <>
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono break-all">{value}</span>
    </>
  )
}
