import { useCallback, useEffect, useMemo, useState, type ComponentProps, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { formatSize, getErrorMessage } from "@/lib/utils"
import { uiLog } from "@/lib/ui-log"
import {
  ControlApi,
  eventsUrl,
  type EventEntry,
  type HealthInfo,
  type TorrentInfo,
} from "@/api/backend"
import { toast } from "sonner"

interface DebridPanelProps {
  torrent: TorrentInfo
  onActionDone?: () => void
}

type Detail = TorrentInfo & {
  properties?: Record<string, unknown>
  webseeds?: { url: string }[]
  magnet_uri?: string
}

const QBX_TAGS = [
  "qbx-debrid",
  "qbx-done",
  "qbx-failed",
  "qbx-candidate",
  "qbx-stalled",
  "qbx-webseed",
  "qbx-skip",
  "qbx-duplicate",
] as const

const STATUS_STYLE: Record<string, string> = {
  idle: "border-border text-muted-foreground",
  active: "border-amber-400/60 text-amber-300 bg-amber-500/10",
  candidate: "border-sky-400/60 text-sky-300 bg-sky-500/10",
  pending: "border-sky-400/60 text-sky-300 bg-sky-500/10",
  deferred: "border-violet-400/60 text-violet-300 bg-violet-500/10",
  blocked: "border-orange-400/60 text-orange-300 bg-orange-500/10",
  webseed: "border-emerald-400/60 text-emerald-300 bg-emerald-500/10",
  done: "border-emerald-500/70 text-emerald-200 bg-emerald-500/15",
  failed: "border-red-500/70 text-red-300 bg-red-500/15",
  skipped: "border-zinc-500/70 text-zinc-300 bg-zinc-500/10",
}

function TipButton({
  tip,
  children,
  ...btn
}: ComponentProps<typeof Button> & { tip: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button title={tip} {...btn}>
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs text-left normal-case font-normal">
        {tip}
      </TooltipContent>
    </Tooltip>
  )
}

function formatEta(seconds?: number): string {
  if (seconds == null || seconds < 0 || seconds >= 8640000) return "—"
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return `${h}h ${m}m`
}

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return "invalid"
  }
}

function parseUrls(raw: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const part of raw.split(/[\n\r,|;]+/)) {
    const url = part.trim()
    if (!url || seen.has(url)) continue
    seen.add(url)
    out.push(url)
  }
  return out
}

function copyText(label: string, text: string) {
  void navigator.clipboard.writeText(text).then(
    () => toast.success(`${label} copied`),
    () => toast.error(`Could not copy ${label}`),
  )
}

function Section({
  title,
  right,
  children,
}: {
  title: string
  right?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
          {title}
        </h4>
        {right}
      </div>
      {children}
    </section>
  )
}

function Stat({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={`text-xs truncate ${mono ? "font-mono" : ""}`} title={value}>
        {value}
      </div>
    </div>
  )
}

export function DebridPanel({ torrent, onActionDone }: DebridPanelProps) {
  const [detail, setDetail] = useState<Detail | null>(null)
  const [webseeds, setWebseeds] = useState<{ url: string }[]>([])
  const [newUrl, setNewUrl] = useState("")
  const [busy, setBusy] = useState<string | null>(null)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [selectedSeeds, setSelectedSeeds] = useState<Set<string>>(new Set())
  const [hostFilter, setHostFilter] = useState("")
  const [health, setHealth] = useState<HealthInfo | null>(null)
  const [ixStatus, setIxStatus] = useState<Record<string, unknown> | null>(null)
  const [cfgBits, setCfgBits] = useState<{ delivery?: string; providers?: string }>({})
  const [activity, setActivity] = useState<{ id: string; ts: number; kind: string; message: string }[]>([])

  const t = detail || torrent
  const props = detail?.properties || {}
  const tags = useMemo(() => {
    const fromQbx = t.qbx_tags || []
    const fromTags = (t.tags || "")
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean)
    return Array.from(new Set([...fromQbx, ...fromTags])).sort()
  }, [t.qbx_tags, t.tags])

  const magnet =
    detail?.magnet_uri ||
    String(props.magnet_uri || "") ||
    (t.hash ? `magnet:?xt=urn:btih:${t.hash}` : "")

  const reload = useCallback(async () => {
    try {
      const [d, h, ix, cfg] = await Promise.all([
        ControlApi.getTorrent(torrent.hash),
        ControlApi.health().catch(() => null),
        ControlApi.interceptorStatus().catch(() => null),
        ControlApi.getConfig().catch(() => null),
      ])
      setDetail(d as Detail)
      setWebseeds(d.webseeds || [])
      if (h) setHealth(h)
      if (ix) setIxStatus(ix)
      if (cfg) {
        const interceptor = (cfg.interceptor || {}) as Record<string, unknown>
        const providers = Array.isArray(cfg.providers)
          ? (cfg.providers as { name?: string; enabled?: boolean }[])
              .filter((p) => p && p.enabled !== false)
              .map((p) => p.name || "?")
              .join(", ")
          : ""
        setCfgBits({
          delivery: String(interceptor.delivery_mode || "webseed"),
          providers: providers || (h?.debrid_enabled ? "configured" : "none"),
        })
      }
    } catch (err) {
      toast.error(getErrorMessage(err))
    }
  }, [torrent.hash])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    if (!autoRefresh) return
    const ms = t.qbx_inflight || t.qbx_status === "active" ? 2500 : 8000
    const id = window.setInterval(() => void reload(), ms)
    return () => window.clearInterval(id)
  }, [autoRefresh, reload, t.qbx_inflight, t.qbx_status])

  useEffect(() => {
    setSelectedSeeds(new Set())
    setActivity([])
    const es = new EventSource(eventsUrl(0))
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as EventEntry
        const h = String(data.hash || "").toLowerCase()
        if (h && h !== torrent.hash.toLowerCase()) return
        const kind = String(data.kind || "")
        if (
          !/^(ui\.|intercept\.|webseed\.|resolve\.|download\.|nudge|scan\.|qbt\.(pause|resume|recheck)|debrid)/i.test(
            kind,
          ) &&
          !/debrid|webseed|intercept|nudge|magnet/i.test(String(data.message || ""))
        ) {
          return
        }
        setActivity((prev) => {
          const next = [
            {
              id: `e-${data.id}`,
              ts: data.ts,
              kind,
              message: data.message,
            },
            ...prev,
          ]
          return next.slice(0, 40)
        })
      } catch {
        /* ignore */
      }
    }
    return () => es.close()
  }, [torrent.hash])

  const run = async (label: string, kind: string, fn: () => Promise<unknown>, okHint?: string) => {
    setBusy(label)
    uiLog(kind, `${label}: ${torrent.name}`, torrent.hash)
    try {
      await fn()
      uiLog(`${kind}.ok`, okHint || `${label} accepted`, torrent.hash)
      toast.success(label)
      onActionDone?.()
      await reload()
    } catch (err) {
      uiLog(`${kind}.failed`, `${label} failed: ${getErrorMessage(err)}`, torrent.hash)
      toast.error(getErrorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const addUrls = async () => {
    const urls = parseUrls(newUrl)
    if (!urls.length) return
    setBusy("Add webseeds")
    uiLog("ui.webseed.add", `Add ${urls.length} webseed(s)`, torrent.hash)
    try {
      const r = await ControlApi.mutateWebseeds(torrent.hash, "add", urls)
      setWebseeds(r.webseeds || [])
      setNewUrl("")
      uiLog("ui.webseed.add.ok", `Added ${urls.length} (${r.webseeds?.length ?? 0} total)`, torrent.hash)
      toast.success(urls.length === 1 ? "Webseed added" : `${urls.length} webseeds added`)
      onActionDone?.()
    } catch (err) {
      uiLog("ui.webseed.add.failed", getErrorMessage(err), torrent.hash)
      toast.error(getErrorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const removeUrls = async (urls: string[]) => {
    if (!urls.length) return
    setBusy("Remove webseeds")
    uiLog("ui.webseed.remove", `Remove ${urls.length} webseed(s)`, torrent.hash)
    try {
      const r = await ControlApi.mutateWebseeds(torrent.hash, "remove", urls)
      setWebseeds(r.webseeds || [])
      setSelectedSeeds(new Set())
      uiLog("ui.webseed.remove.ok", `Removed ${urls.length} (${r.webseeds?.length ?? 0} left)`, torrent.hash)
      toast.success(urls.length === 1 ? "Webseed removed" : `${urls.length} webseeds removed`)
    } catch (err) {
      uiLog("ui.webseed.remove.failed", getErrorMessage(err), torrent.hash)
      toast.error(getErrorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const toggleTag = async (tag: string) => {
    const has = tags.includes(tag)
    await run(
      has ? `Remove ${tag}` : `Add ${tag}`,
      "ui.tags",
      () => ControlApi.tags(torrent.hash, has ? [] : [tag], has ? [tag] : []),
      has ? `Removed ${tag}` : `Added ${tag}`,
    )
  }

  const hosts = useMemo(() => {
    const m = new Map<string, number>()
    for (const w of webseeds) {
      const h = hostOf(w.url)
      m.set(h, (m.get(h) || 0) + 1)
    }
    return [...m.entries()].sort((a, b) => b[1] - a[1])
  }, [webseeds])

  const filteredSeeds = useMemo(() => {
    if (!hostFilter) return webseeds
    return webseeds.filter((w) => hostOf(w.url) === hostFilter)
  }, [webseeds, hostFilter])

  const progressPct = Math.min(100, Math.max(0, (t.progress || 0) * 100))
  const status = (t.qbx_status || "idle").toLowerCase()
  const downloaded = Number(props.total_downloaded ?? props.downloaded ?? NaN)
  const uploaded = Number(props.total_uploaded ?? props.uploaded ?? NaN)
  const availability = Number(props.availability ?? NaN)
  const pieceSize = Number(props.piece_size ?? NaN)
  const piecesHave = Number(props.pieces_have ?? NaN)
  const piecesNum = Number(props.pieces_num ?? NaN)

  const pendingCount = Number(ixStatus?.pending_candidates_count ?? (ixStatus?.queue_pending as number) ?? NaN)
  const inflightCount = Number(ixStatus?.inflight_count ?? (Array.isArray(ixStatus?.inflight) ? (ixStatus?.inflight as unknown[]).length : NaN))

  return (
    <TooltipProvider delayDuration={220}>
      <div className="h-full overflow-auto p-3 space-y-4 text-sm">
        {/* Identity + status */}
        <div className="space-y-2">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <h3 className="font-semibold leading-tight break-all">{t.name}</h3>
              <button
                type="button"
                className="mt-1 font-mono text-[11px] text-muted-foreground hover:text-foreground truncate max-w-full text-left"
                title="Copy hash"
                onClick={() => copyText("Hash", t.hash)}
              >
                {t.hash}
              </button>
            </div>
            <div className="flex flex-col items-end gap-1 shrink-0">
              <Badge className={`text-[10px] ${STATUS_STYLE[status] || STATUS_STYLE.idle}`} variant="outline">
                {status}
                {t.qbx_inflight ? " · inflight" : ""}
              </Badge>
              <Badge variant="outline" className="text-[10px]">
                {t.state || "?"}
              </Badge>
            </div>
          </div>
          {t.qbx_reason && (
            <p className="text-xs text-amber-200/90 bg-amber-500/10 border border-amber-500/20 rounded px-2 py-1.5">
              <span className="text-amber-400/80 font-medium">Reason · </span>
              {t.qbx_reason}
            </p>
          )}
          <div className="flex flex-wrap gap-1.5 text-[10px]">
            <Badge variant="secondary">delivery {cfgBits.delivery || "…"}</Badge>
            <Badge variant="secondary">debrid {health?.debrid_enabled ? "on" : "off"}</Badge>
            <Badge variant="secondary">
              interceptor {health?.interceptor_running ? "running" : "stopped"}
            </Badge>
            {cfgBits.providers && <Badge variant="outline">{cfgBits.providers}</Badge>}
            {Number.isFinite(inflightCount) && (
              <Badge variant="outline">global inflight {inflightCount}</Badge>
            )}
            {Number.isFinite(pendingCount) && (
              <Badge variant="outline">pending {pendingCount}</Badge>
            )}
          </div>
        </div>

        {/* Live metrics */}
        <Section
          title="Torrent"
          right={
            <label className="flex items-center gap-1.5 text-[10px] text-muted-foreground cursor-pointer">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
                className="accent-sky-500"
              />
              Live refresh
            </label>
          }
        >
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Progress value={progressPct} className="h-1.5 flex-1" />
              <span className="font-mono text-[11px] w-12 text-right">{progressPct.toFixed(1)}%</span>
            </div>
            <div className="grid grid-cols-2 gap-x-3 gap-y-2">
              <Stat label="↓ / ↑" value={`${formatSize(t.dlspeed || 0)}/s · ${formatSize(t.upspeed || 0)}/s`} mono />
              <Stat label="ETA" value={formatEta(t.eta)} mono />
              <Stat label="Size" value={formatSize(t.size || 0)} mono />
              <Stat
                label="Downloaded"
                value={Number.isFinite(downloaded) ? formatSize(downloaded) : "—"}
                mono
              />
              <Stat label="Seeds / peers" value={`${t.num_seeds ?? 0} / ${t.num_leechs ?? 0}`} mono />
              <Stat label="Ratio" value={(t.ratio ?? 0).toFixed(3)} mono />
              <Stat label="Queue / prio" value={String(t.priority ?? "—")} mono />
              <Stat label="Category" value={t.category || "—"} />
              <Stat
                label="Availability"
                value={Number.isFinite(availability) ? availability.toFixed(3) : "—"}
                mono
              />
              <Stat
                label="Pieces"
                value={
                  Number.isFinite(piecesHave) && Number.isFinite(piecesNum)
                    ? `${piecesHave}/${piecesNum}${Number.isFinite(pieceSize) ? ` · ${formatSize(pieceSize)}` : ""}`
                    : "—"
                }
                mono
              />
              <Stat label="Save path" value={t.savePath || "—"} mono />
              <Stat
                label="Uploaded"
                value={Number.isFinite(uploaded) ? formatSize(uploaded) : "—"}
                mono
              />
            </div>
          </div>
        </Section>

        {/* Pipeline ops live on the shared command bar / ⌘K palette */}
        <Section title="Debrid pipeline">
          <p className="text-[11px] text-muted-foreground leading-relaxed">
            Force debrid, nudge, retry, skip/allow, pause/resume, and recheck run from the action bar
            under the header (or ⌘K / Ctrl+K). This panel tracks tags, webseeds, and status.
          </p>
        </Section>

        {/* Clipboard / refresh helpers (not duplicated on the bar) */}
        <Section title="Clipboard">
          <div className="flex flex-wrap gap-1.5">
            <TipButton
              tip="Copy magnet URI for this torrent."
              size="sm"
              variant="outline"
              disabled={!magnet}
              onClick={() => {
                uiLog("ui.copy_magnet", `Copy magnet: ${t.name}`, t.hash)
                copyText("Magnet", magnet)
              }}
            >
              Copy magnet
            </TipButton>
            <TipButton
              tip="Copy infohash."
              size="sm"
              variant="outline"
              onClick={() => copyText("Hash", t.hash)}
            >
              Copy hash
            </TipButton>
            <TipButton
              tip="Refresh torrent detail, webseeds, and interceptor snapshot now."
              size="sm"
              variant="ghost"
              disabled={!!busy}
              onClick={() => void reload()}
            >
              Refresh
            </TipButton>
          </div>
          {busy && <p className="text-[11px] text-muted-foreground">Working: {busy}…</p>}
        </Section>

        {/* Tags */}
        <Section title="qbx tags">
          <div className="flex flex-wrap gap-1">
            {QBX_TAGS.map((tag) => {
              const on = tags.includes(tag)
              return (
                <TipButton
                  key={tag}
                  tip={on ? `Remove ${tag}` : `Add ${tag}`}
                  size="sm"
                  variant={on ? "default" : "outline"}
                  className="h-6 text-[10px] font-mono px-2"
                  disabled={!!busy}
                  onClick={() => void toggleTag(tag)}
                >
                  {tag.replace(/^qbx-/, "")}
                </TipButton>
              )
            })}
          </div>
          {tags.filter((x) => !QBX_TAGS.includes(x as (typeof QBX_TAGS)[number])).length > 0 && (
            <div className="flex flex-wrap gap-1 pt-1">
              {tags
                .filter((x) => !QBX_TAGS.includes(x as (typeof QBX_TAGS)[number]))
                .map((tag) => (
                  <Badge key={tag} variant="secondary" className="text-[10px] font-mono">
                    {tag}
                  </Badge>
                ))}
            </div>
          )}
        </Section>

        {/* Webseeds */}
        <Section
          title={`Webseeds (${webseeds.length})`}
          right={
            <div className="flex gap-1">
              <Button
                size="sm"
                variant="ghost"
                className="h-6 text-[10px]"
                disabled={!webseeds.length}
                onClick={() => copyText("Webseeds", webseeds.map((w) => w.url).join("\n"))}
              >
                Copy all
              </Button>
              <Button size="sm" variant="ghost" className="h-6 text-[10px]" onClick={() => void reload()}>
                Refresh
              </Button>
            </div>
          }
        >
          {hosts.length > 0 && (
            <div className="flex flex-wrap gap-1">
              <Button
                size="sm"
                variant={!hostFilter ? "default" : "outline"}
                className="h-6 text-[10px]"
                onClick={() => setHostFilter("")}
              >
                all
              </Button>
              {hosts.map(([host, n]) => (
                <Button
                  key={host}
                  size="sm"
                  variant={hostFilter === host ? "default" : "outline"}
                  className="h-6 text-[10px] font-mono"
                  onClick={() => setHostFilter((h) => (h === host ? "" : host))}
                >
                  {host} ({n})
                </Button>
              ))}
            </div>
          )}

          <div className="space-y-1 max-h-52 overflow-auto border border-border rounded-md p-2">
            {filteredSeeds.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                {webseeds.length === 0
                  ? "No webseeds yet. Force debrid (delivery=webseed) or paste URLs below."
                  : "No webseeds match this host filter."}
              </p>
            ) : (
              filteredSeeds.map((w) => {
                const checked = selectedSeeds.has(w.url)
                return (
                  <div key={w.url} className="flex items-start gap-2 text-[11px] font-mono">
                    <input
                      type="checkbox"
                      className="mt-1 accent-sky-500"
                      checked={checked}
                      onChange={() => {
                        setSelectedSeeds((prev) => {
                          const next = new Set(prev)
                          if (next.has(w.url)) next.delete(w.url)
                          else next.add(w.url)
                          return next
                        })
                      }}
                    />
                    <div className="flex-1 min-w-0">
                      <div className="text-[10px] text-muted-foreground">{hostOf(w.url)}</div>
                      <div className="break-all leading-snug">{w.url}</div>
                    </div>
                    <div className="flex flex-col gap-0.5 shrink-0">
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-6 px-2"
                        onClick={() => copyText("URL", w.url)}
                      >
                        Copy
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-6 px-2"
                        disabled={!!busy}
                        onClick={() => void removeUrls([w.url])}
                      >
                        Remove
                      </Button>
                    </div>
                  </div>
                )
              })
            )}
          </div>

          <div className="flex flex-wrap gap-1.5">
            <TipButton
              tip="Select every visible webseed in the list."
              size="sm"
              variant="outline"
              className="h-7 text-[11px]"
              disabled={!filteredSeeds.length}
              onClick={() => setSelectedSeeds(new Set(filteredSeeds.map((w) => w.url)))}
            >
              Select visible
            </TipButton>
            <TipButton
              tip="Remove selected webseeds from qBittorrent."
              size="sm"
              variant="destructive"
              className="h-7 text-[11px]"
              disabled={!!busy || selectedSeeds.size === 0}
              onClick={() => void removeUrls([...selectedSeeds])}
            >
              Remove selected ({selectedSeeds.size})
            </TipButton>
            <TipButton
              tip="Remove every webseed on this torrent."
              size="sm"
              variant="outline"
              className="h-7 text-[11px]"
              disabled={!!busy || webseeds.length === 0}
              onClick={() => {
                if (!window.confirm(`Remove all ${webseeds.length} webseed(s)?`)) return
                void removeUrls(webseeds.map((w) => w.url))
              }}
            >
              Remove all
            </TipButton>
          </div>

          <div className="flex gap-2 items-end">
            <textarea
              value={newUrl}
              onChange={(e) => setNewUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault()
                  void addUrls()
                }
              }}
              placeholder={"https://… one URL per line\n(or separate with commas / | )"}
              rows={4}
              className="flex w-full min-h-[5.5rem] rounded-md border border-input bg-transparent px-3 py-2 text-xs font-mono shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 resize-y"
            />
            <TipButton
              tip="Add one or more HTTP URLs as qBittorrent webseeds. Ctrl/Cmd+Enter to submit."
              size="sm"
              disabled={!!busy || parseUrls(newUrl).length === 0}
              onClick={() => void addUrls()}
            >
              Add {parseUrls(newUrl).length || ""}
            </TipButton>
          </div>
        </Section>

        {/* Activity for this torrent */}
        <Section title="Activity (this torrent)">
          <div className="max-h-40 overflow-auto border border-border rounded-md p-2 font-mono text-[10px] leading-4 space-y-1">
            {activity.length === 0 ? (
              <p className="text-muted-foreground">
                Waiting for intercept / webseed / nudge events… Actions above also write to the live log.
              </p>
            ) : (
              activity.map((line) => (
                <div key={line.id} className="flex gap-2">
                  <span className="text-muted-foreground shrink-0 w-[58px]">
                    {new Date(line.ts * 1000).toLocaleTimeString()}
                  </span>
                  <span className="text-emerald-400/90 shrink-0 w-[100px] truncate">{line.kind}</span>
                  <span className="break-all text-foreground/90">{line.message}</span>
                </div>
              ))
            )}
          </div>
        </Section>
      </div>
    </TooltipProvider>
  )
}
