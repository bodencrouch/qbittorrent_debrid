import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import {
  AttentionService,
  ApiError,
  ControlApi,
  type AttentionItem,
  type AttentionPayload,
} from "@/api/backend"
import type { SettingsSection } from "@/components/SettingsPanel"
import { getErrorMessage } from "@/lib/utils"
import { toast } from "sonner"

type AttentionPanelProps = {
  onOpenSettings?: (section?: SettingsSection) => void
  onOpenStorage?: () => void
  onOpenTorrents?: () => void
  onRefreshHealth?: () => void
  onOpenMatcher?: () => void
}

function severityBadge(severity: AttentionItem["severity"]) {
  switch (severity) {
    case "critical":
      return { label: "critical", variant: "destructive" as const }
    case "warning":
      return { label: "warning", variant: "outline" as const }
    case "info":
      return { label: "info", variant: "secondary" as const }
  }
}

export function AttentionPanel({
  onOpenSettings,
  onOpenStorage,
  onOpenTorrents,
  onOpenMatcher,
  onRefreshHealth,
}: AttentionPanelProps) {
  const [payload, setPayload] = useState<AttentionPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [authRequired, setAuthRequired] = useState(false)

  const load = useCallback(async () => {
    try {
      const data = await AttentionService.get()
      setPayload(data)
      setAuthRequired(false)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setAuthRequired(true)
        setPayload(null)
      } else {
        toast.error(getErrorMessage(err))
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    const id = window.setInterval(() => void load(), 15000)
    return () => window.clearInterval(id)
  }, [load])

  const runAction = async (item: AttentionItem) => {
    const action = item.primary_action
    const type = String(action.type || "")
    try {
      switch (type) {
        case "open_settings":
          onOpenSettings?.(action.section as SettingsSection | undefined)
          break
        case "open_storage":
          onOpenStorage?.()
          break
        case "open_torrents":
          onOpenTorrents?.()
          break
        case "interceptor_scan":
          await ControlApi.interceptorScan()
          toast.success("Policy scan queued")
          onRefreshHealth?.()
          await load()
          break
        case "open_qbt":
          window.open("/qbt/", "_blank")
          break
        default:
          break
      }
    } catch (err) {
      toast.error(getErrorMessage(err))
    }
  }

  const items = payload?.items ?? []

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold tracking-wide">Needs attention</h2>
          {payload && payload.counts.critical > 0 && (
            <Badge variant="destructive" className="text-[9px]">
              {payload.counts.critical} critical
            </Badge>
          )}
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-7 text-[10px]"
          onClick={() => void load()}
        >
          Refresh
        </Button>
      </div>

      {loading && !payload && !authRequired ? (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Spinner />
          Loading attention queue…
        </div>
      ) : authRequired ? (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/5 p-4 text-sm">
          <p className="font-medium text-foreground">API token required</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Header counts come from <code className="text-[10px]">/api/health</code>. Open Settings,
            enter your qbx API token, and save to load the full attention queue here.
          </p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="mt-3 h-7 text-[10px]"
            onClick={() => onOpenSettings?.("application")}
          >
            Open Settings → Application
          </Button>
        </div>
      ) : items.length === 0 ? (
        <div className="rounded-md border border-dashed border-border/60 p-6 text-center text-sm text-muted-foreground">
          <p className="font-medium text-foreground/90">All clear</p>
          <p className="mt-1 text-xs">
            No actionable issues right now. Stalled torrents and new duplicate groups will appear here.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((item) => {
            const badge = severityBadge(item.severity)
            return (
              <div
                key={item.id}
                className="rounded-md border border-border/60 bg-card/40 px-3 py-2 text-xs space-y-1"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <Badge variant={badge.variant} className="text-[9px] uppercase">
                        {badge.label}
                      </Badge>
                      <span className="font-medium">{item.title}</span>
                    </div>
                    <p className="text-muted-foreground mt-1">{item.detail}</p>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    className="h-7 shrink-0 text-[10px]"
                    onClick={() => void runAction(item)}
                  >
                    Act
                  </Button>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
