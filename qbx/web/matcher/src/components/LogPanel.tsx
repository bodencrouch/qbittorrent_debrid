import { useEffect, useMemo, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { eventsUrl, logsUrl, type EventEntry, type LogEntry } from "@/api/backend"
import { UI_LOG_EVENT, type UiLogDetail } from "@/lib/ui-log"

type StreamMode = "all" | "events" | "server" | "debrid"

type UnifiedLine = {
  id: string
  ts: number
  kind: string
  level?: string
  source?: string
  message: string
  hash?: string
  stream: "event" | "log" | "ui"
}

interface LogPanelProps {
  filterHash?: string | null
  onHashClick?: (hash: string) => void
}

const DEBRID_KIND_RE =
  /^(ui\.(force_debrid|nudge|retry|skip_auto|unskip|webseed|recheck|pause|resume|tags|copy_magnet|open_webui)|qbt\.(recheck|pause|resume)|intercept\.|webseed\.|resolve\.|download\.|scan\.|nudge|debrid)/i

function isDebridLine(line: UnifiedLine): boolean {
  if (DEBRID_KIND_RE.test(line.kind)) return true
  if (line.source?.includes("debrid") || line.source?.includes("interceptor")) {
    return /debrid|webseed|intercept|resolve|magnet|nudge|policy scan/i.test(line.message)
  }
  return /debrid|webseed|intercept|unrestrict|magnet|nudge|policy scan/i.test(line.message)
}

function kindColor(line: UnifiedLine): string {
  const k = line.kind.toLowerCase()
  if (k.includes("failed") || k.includes("error") || line.level === "ERROR") return "text-red-400"
  if (k.startsWith("ui.")) return "text-violet-300"
  if (k.startsWith("intercept.") || k.startsWith("webseed.") || k.startsWith("resolve.") || k.startsWith("scan."))
    return "text-emerald-400"
  if (k === "nudge" || k.startsWith("nudge")) return "text-amber-300"
  if (k.startsWith("download.")) return "text-sky-300"
  return "text-sky-400/90"
}

export function LogPanel({ filterHash, onHashClick }: LogPanelProps) {
  const [mode, setMode] = useState<StreamMode>("all")
  const [lines, setLines] = useState<UnifiedLine[]>([])
  const [paused, setPaused] = useState(false)
  const [grep, setGrep] = useState("")
  const [level, setLevel] = useState("")
  const [uiSeq, setUiSeq] = useState(0)
  const [streamError, setStreamError] = useState<string | null>(null)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const pausedRef = useRef(paused)
  pausedRef.current = paused

  useEffect(() => {
    const onUi = (ev: Event) => {
      const detail = (ev as CustomEvent<UiLogDetail>).detail
      if (!detail?.message) return
      setUiSeq((n) => n + 1)
      setLines((prev) => {
        const next = [
          ...prev,
          {
            id: `ui-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
            ts: detail.ts || Date.now() / 1000,
            kind: detail.kind || "ui.action",
            source: "ui",
            message: detail.message,
            hash: detail.hash,
            stream: "ui" as const,
          },
        ]
        return next.length > 800 ? next.slice(-800) : next
      })
    }
    window.addEventListener(UI_LOG_EVENT, onUi)
    return () => window.removeEventListener(UI_LOG_EVENT, onUi)
  }, [])

  useEffect(() => {
    const controllers: { abort: () => void }[] = []
    const push = (line: UnifiedLine) => {
      setLines((prev) => {
        const next = [...prev, line]
        return next.length > 800 ? next.slice(-800) : next
      })
    }

    // EventSource retries silently and indefinitely by default. A bad/missing
    // API token closes the connection outright (readyState CLOSED, no further
    // retry) rather than erroring transiently — that's the case worth telling
    // the user about, since otherwise the log just stays empty forever with
    // no visible reason why.
    const onStreamError = (label: string) => (ev: Event) => {
      const es = ev.currentTarget as EventSource
      if (es.readyState === EventSource.CLOSED) {
        setStreamError(`${label} stream unauthorized or unreachable — check the qbx API token in Settings.`)
      }
    }

    if (mode === "all" || mode === "events" || mode === "debrid") {
      const es = new EventSource(eventsUrl(0))
      es.onmessage = (ev) => {
        setStreamError(null)
        try {
          const data = JSON.parse(ev.data) as EventEntry
          push({
            id: `e-${data.id}`,
            ts: data.ts,
            kind: data.kind,
            message: data.message,
            hash: typeof data.hash === "string" ? data.hash : undefined,
            stream: "event",
            source: "event",
          })
        } catch {
          /* ignore */
        }
      }
      es.onerror = onStreamError("Event")
      controllers.push({ abort: () => es.close() })
    }

    if (mode === "all" || mode === "server" || mode === "debrid") {
      const es = new EventSource(logsUrl(0, level))
      es.onmessage = (ev) => {
        setStreamError(null)
        try {
          const data = JSON.parse(ev.data) as LogEntry
          push({
            id: `l-${data.id}`,
            ts: data.ts,
            kind: "log",
            level: data.level,
            source: data.source,
            message: data.message,
            stream: "log",
          })
        } catch {
          /* ignore */
        }
      }
      es.onerror = onStreamError("Log")
      controllers.push({ abort: () => es.close() })
    }

    return () => controllers.forEach((c) => c.abort())
  }, [mode, level])

  useEffect(() => {
    if (pausedRef.current) return
    const el = scrollerRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [lines, uiSeq])

  const visible = useMemo(() => {
    let list = lines
    if (mode === "debrid") {
      list = list.filter(isDebridLine)
    }
    if (filterHash) {
      const h = filterHash.toLowerCase()
      list = list.filter(
        (l) =>
          (l.hash || "").toLowerCase() === h ||
          l.message.toLowerCase().includes(h),
      )
    }
    if (grep.trim()) {
      const q = grep.toLowerCase()
      list = list.filter(
        (l) =>
          l.message.toLowerCase().includes(q) ||
          (l.kind || "").toLowerCase().includes(q) ||
          (l.source || "").toLowerCase().includes(q),
      )
    }
    return list
  }, [lines, filterHash, grep, mode])

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-1.5">
        <span className="text-xs font-semibold tracking-wide">Live log</span>
        {(["all", "debrid", "events", "server"] as StreamMode[]).map((m) => (
          <Button
            key={m}
            size="sm"
            variant={mode === m ? "default" : "outline"}
            className="h-6 text-[11px] capitalize"
            onClick={() => {
              setMode(m)
              setLines([])
            }}
            title={
              m === "debrid"
                ? "UI actions + scan/intercept/webseed/resolve/download (policy + debrid)"
                : undefined
            }
          >
            {m}
          </Button>
        ))}
        <select
          className="h-6 rounded border border-border bg-card px-1 text-[11px]"
          value={level}
          onChange={(e) => setLevel(e.target.value)}
          title="Minimum log level (server stream)"
        >
          <option value="">Level: any</option>
          <option value="DEBUG">DEBUG+</option>
          <option value="INFO">INFO+</option>
          <option value="WARNING">WARN+</option>
          <option value="ERROR">ERROR+</option>
        </select>
        <Input
          value={grep}
          onChange={(e) => setGrep(e.target.value)}
          placeholder="Filter…"
          className="h-6 max-w-[160px] text-[11px]"
        />
        {filterHash && (
          <Badge variant="outline" className="text-[10px]">
            hash:{filterHash.slice(0, 8)}…
          </Badge>
        )}
        <Button
          size="sm"
          variant={paused ? "default" : "ghost"}
          className="h-6 text-[11px] ml-auto"
          onClick={() => setPaused((p) => !p)}
        >
          {paused ? "Resume" : "Pause"}
        </Button>
        <Button size="sm" variant="ghost" className="h-6 text-[11px]" onClick={() => setLines([])}>
          Clear
        </Button>
      </div>
      {streamError && (
        <div className="border-b border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-[11px] text-amber-400">
          {streamError}
        </div>
      )}
      <div ref={scrollerRef} className="flex-1 min-h-0 overflow-auto font-mono text-[11px] leading-5 px-2 py-1">
        {visible.length === 0 ? (
          <div className="text-muted-foreground p-2">
            {mode === "debrid"
              ? "Waiting for debrid activity… Try Force debrid or Nudge policy."
              : "Waiting for activity…"}
          </div>
        ) : (
          visible.map((line) => (
            <div key={line.id} className="flex gap-2 hover:bg-accent/30 px-1 rounded">
              <span className="text-muted-foreground shrink-0 w-[70px]">
                {new Date(line.ts * 1000).toLocaleTimeString()}
              </span>
              <span className={`shrink-0 w-[120px] truncate ${kindColor(line)}`}>
                {line.stream === "log" ? line.level || "LOG" : line.kind}
              </span>
              <span className="shrink-0 w-[70px] truncate text-muted-foreground">
                {line.stream === "ui" ? "ui" : line.source || "event"}
              </span>
              <span className="min-w-0 break-all">
                {line.message}
                {line.hash && (
                  <button
                    type="button"
                    className="ml-2 text-amber-400/90 underline-offset-2 hover:underline"
                    onClick={() => onHashClick?.(line.hash!)}
                  >
                    {line.hash.slice(0, 10)}
                  </button>
                )}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
