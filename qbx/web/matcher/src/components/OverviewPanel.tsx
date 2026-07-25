import { useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { formatSize, getErrorMessage } from "@/lib/utils"
import { ControlApi, type TorrentInfo } from "@/api/backend"
import { toast } from "sonner"

interface OverviewPanelProps {
  torrent: TorrentInfo
  onActionDone?: () => void
}

export function OverviewPanel({ torrent }: OverviewPanelProps) {
  const [detail, setDetail] = useState<(TorrentInfo & { properties?: any; webseeds?: { url: string }[] }) | null>(null)

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

  const t = detail || torrent
  const props = detail?.properties || {}

  return (
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

      <p className="text-xs text-muted-foreground">
        Use the action bar below (or ⌘K / Ctrl+K) for Force debrid, nudge, retry, and torrent controls.
      </p>

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
    </div>
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
