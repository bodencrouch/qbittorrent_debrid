import { useEffect, useMemo, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { eventsUrl, type EventEntry } from "@/api/backend"

type MatcherActivityPanelProps = {
  interceptor?: Record<string, unknown>
}

export function MatcherActivityPanel({ interceptor }: MatcherActivityPanelProps) {
  const [events, setEvents] = useState<EventEntry[]>([])

  useEffect(() => {
    let cancelled = false
    const es = new EventSource(eventsUrl())
    es.onmessage = (ev) => {
      if (cancelled) return
      try {
        const data = JSON.parse(ev.data) as EventEntry
        if (data.kind === "matcher.done" || data.kind?.startsWith("placement.")) {
          setEvents((prev) => [...prev.slice(-19), data])
        }
      } catch {
        // ignore malformed
      }
    }
    return () => {
      cancelled = true
      es.close()
    }
  }, [])

  const lastMatcher = useMemo(
    () => [...events].reverse().find((e) => e.kind === "matcher.done"),
    [events],
  )

  const placementMoves = Number(interceptor?.placement_moves ?? 0)

  return (
    <section className="space-y-2">
      <h2 className="text-sm font-semibold tracking-wide">Matcher activity</h2>
      <div className="flex flex-wrap gap-2 text-xs">
        {lastMatcher ? (
          <Badge variant="outline" className="font-normal">
            Last match: {lastMatcher.message}
          </Badge>
        ) : (
          <span className="text-muted-foreground">No matcher runs recorded this session.</span>
        )}
        {placementMoves > 0 && (
          <Badge variant="secondary" className="font-normal">
            Placement moves (interceptor): {placementMoves}
          </Badge>
        )}
      </div>
      {events.length > 0 && (
        <div className="max-h-28 overflow-y-auto rounded-md border border-border/50 divide-y divide-border/40 text-[11px]">
          {[...events].reverse().slice(0, 6).map((e) => (
            <div key={e.id} className="px-2 py-1 text-muted-foreground">
              <span className="text-foreground/80">{e.kind}</span> — {e.message}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
