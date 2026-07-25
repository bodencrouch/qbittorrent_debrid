import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { ControlApi } from "@/api/backend"
import { getErrorMessage } from "@/lib/utils"
import { toast } from "sonner"

export const INTERCEPTOR_PRESETS = {
  conservative: {
    label: "Conservative",
    patch: {
      interceptor: {
        stalled_min_minutes: 60,
        min_stalled_seeds: 2,
        max_stalled_download_speed: 512,
      },
    },
  },
  balanced: {
    label: "Balanced",
    patch: {
      interceptor: {
        stalled_min_minutes: 30,
        min_stalled_seeds: 1,
        max_stalled_download_speed: 1024,
      },
    },
  },
  aggressive: {
    label: "Aggressive",
    patch: {
      interceptor: {
        stalled_min_minutes: 15,
        min_stalled_seeds: 0,
        max_stalled_download_speed: 2048,
      },
    },
  },
} as const

type PresetKey = keyof typeof INTERCEPTOR_PRESETS

type InterceptorMonitorPanelProps = {
  interceptor?: Record<string, unknown>
  onRefreshHealth?: () => void
}

export function InterceptorMonitorPanel({
  interceptor,
  onRefreshHealth,
}: InterceptorMonitorPanelProps) {
  const [status, setStatus] = useState<Record<string, unknown> | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const s = await ControlApi.interceptorStatus()
      setStatus(s)
    } catch {
      setStatus(interceptor ? { ...interceptor } : null)
    }
  }, [interceptor])

  useEffect(() => {
    void load()
    const id = window.setInterval(() => void load(), 10000)
    return () => window.clearInterval(id)
  }, [load])

  const applyPreset = async (key: PresetKey) => {
    setBusy(true)
    try {
      await ControlApi.updateConfig(INTERCEPTOR_PRESETS[key].patch)
      toast.success(`Applied ${INTERCEPTOR_PRESETS[key].label} preset`)
      onRefreshHealth?.()
      await load()
    } catch (err) {
      toast.error(getErrorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const stats = status || interceptor || {}
  const decisions = Array.isArray(stats.recent_decisions)
    ? (stats.recent_decisions as Record<string, unknown>[])
    : []

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold tracking-wide">Interceptor</h2>
        <div className="flex gap-1">
          {(Object.keys(INTERCEPTOR_PRESETS) as PresetKey[]).map((key) => (
            <Button
              key={key}
              type="button"
              size="sm"
              variant="outline"
              className="h-7 text-[10px]"
              disabled={busy}
              onClick={() => void applyPreset(key)}
            >
              {INTERCEPTOR_PRESETS[key].label}
            </Button>
          ))}
        </div>
      </div>

      <p className="text-[11px] text-muted-foreground">
        A torrent becomes a debrid candidate when stalled long enough, seeds are below the threshold,
        and download speed stays under the cap (when stalled-only mode is on).
      </p>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
        {[
          ["Observed", stats.observed],
          ["Candidates", stats.candidates],
          ["Pending", stats.pending_count],
          ["Deferred", stats.deferred_count],
        ].map(([label, value]) => (
          <div key={String(label)} className="rounded-md border border-border/50 px-2 py-1.5">
            <div className="text-[10px] uppercase text-muted-foreground">{label}</div>
            <div className="font-mono font-medium">{String(value ?? "—")}</div>
          </div>
        ))}
      </div>

      {stats.last_policy_pass && typeof stats.last_policy_pass === "object" ? (
        <div className="text-[11px] text-muted-foreground">
          Last policy pass:{" "}
          <span className="font-mono text-foreground/90">
            {JSON.stringify(stats.last_policy_pass)}
          </span>
        </div>
      ) : null}

      {decisions.length > 0 ? (
        <div className="space-y-1">
          <h3 className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Recent decisions
          </h3>
          <div className="max-h-40 overflow-y-auto rounded-md border border-border/50 divide-y divide-border/40">
            {decisions.slice(-8).reverse().map((d, i) => (
              <div key={`${d.hash}-${i}`} className="px-2 py-1.5 text-[11px] font-mono">
                <span className="text-muted-foreground">{String(d.reason || d.status || "—")}</span>
                {d.hash ? (
                  <span className="ml-2 text-foreground/80">{String(d.hash).slice(0, 12)}…</span>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">No recent interceptor decisions yet.</p>
      )}

      {busy && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Spinner />
          Applying preset…
        </div>
      )}
    </section>
  )
}
