import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useVirtualizer } from "@tanstack/react-virtual"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { formatSize, getErrorMessage } from "@/lib/utils"
import { ControlApi, type TorrentInfo } from "@/api/backend"
import { toast } from "sonner"
import {
  TorrentContextMenu,
  type ContextMenuAction,
} from "@/components/TorrentContextMenu"

type SortKey = "name" | "progress" | "state" | "size" | "priority" | "debrid_status" | "dlspeed" | "ratio"

type ColId = "name" | "progress" | "state" | "size" | "priority" | "dlspeed" | "debrid_status"

type ColDef = {
  id: ColId
  sortKey: SortKey
  label: string
  min: number
  defaultWidth: number
}

const COLUMNS: ColDef[] = [
  { id: "name", sortKey: "name", label: "Name", min: 120, defaultWidth: 280 },
  { id: "progress", sortKey: "progress", label: "%", min: 56, defaultWidth: 70 },
  { id: "state", sortKey: "state", label: "State", min: 72, defaultWidth: 96 },
  { id: "size", sortKey: "size", label: "Size", min: 64, defaultWidth: 80 },
  { id: "priority", sortKey: "priority", label: "Queue", min: 52, defaultWidth: 64 },
  { id: "dlspeed", sortKey: "dlspeed", label: "↓", min: 64, defaultWidth: 88 },
  { id: "debrid_status", sortKey: "debrid_status", label: "Debrid", min: 72, defaultWidth: 100 },
]

const FILTERS = [
  { id: "downloading", label: "Active DL" },
  { id: "stalledDL", label: "Stalled" },
  { id: "queuedDL", label: "Queued" },
  { id: "completed", label: "Done" },
  { id: "all", label: "All" },
] as const

/** Stable sort ranks for debrid status (lower = earlier when ascending). */
const DEBRID_SORT_RANK: Record<string, number> = {
  debriding: 10,
  pending: 20,
  candidate: 30,
  deferred: 40,
  blocked: 50,
  webseed: 60,
  done: 70,
  failed: 80,
  skipped: 90,
  idle: 100,
  none: 110,
}

const WIDTH_STORAGE_KEY = "qbx_grid_col_widths"
const LIMIT_STORAGE_KEY = "qbx_grid_limit"
const LIMIT_PRESETS = [100, 250, 500, 1000, 2000, 5000, 0] as const

type WidthMap = Record<ColId, number>

function defaultWidths(): WidthMap {
  return Object.fromEntries(COLUMNS.map((c) => [c.id, c.defaultWidth])) as WidthMap
}

function readStoredWidths(): WidthMap {
  const base = defaultWidths()
  try {
    const raw = localStorage.getItem(WIDTH_STORAGE_KEY)
    if (!raw) return base
    const parsed = JSON.parse(raw) as Partial<Record<ColId, number>>
    for (const col of COLUMNS) {
      const n = Number(parsed[col.id])
      if (Number.isFinite(n) && n >= col.min) base[col.id] = Math.floor(n)
    }
  } catch {
    /* ignore */
  }
  return base
}

function readStoredLimit(): number {
  try {
    const raw = localStorage.getItem(LIMIT_STORAGE_KEY)
    if (raw == null || raw === "") return 500
    const n = Number(raw)
    if (!Number.isFinite(n) || n < 0) return 500
    return Math.floor(n)
  } catch {
    return 500
  }
}

function debridStatusOf(t: TorrentInfo): string {
  if (t.qbx_inflight) return "debriding"
  const tags = new Set(
    [
      ...(t.qbx_tags || []),
      ...String(t.tags || "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    ].map((s) => s.toLowerCase()),
  )
  const raw = (t.qbx_status || "").toLowerCase()
  if (raw === "active" || tags.has("qbx-debrid")) return "debriding"
  if (raw === "failed" || tags.has("qbx-failed")) return "failed"
  if (raw === "skipped" || tags.has("qbx-skip")) return "skipped"
  if (raw === "webseed" || tags.has("qbx-webseed")) return "webseed"
  if (raw === "done" || tags.has("qbx-done")) return "done"
  if (raw === "candidate" || tags.has("qbx-candidate") || tags.has("qbx-stalled")) return "candidate"
  if (raw === "pending") return "pending"
  if (raw === "deferred") return "deferred"
  if (raw === "blocked") return "blocked"
  if (raw === "idle" || !raw) return "idle"
  return raw
}

function debridLabel(status: string): string {
  switch (status) {
    case "debriding":
      return "debriding"
    case "webseed":
      return "webseed"
    case "candidate":
      return "candidate"
    case "pending":
      return "pending"
    case "deferred":
      return "deferred"
    case "blocked":
      return "blocked"
    case "failed":
      return "failed"
    case "skipped":
      return "skipped"
    case "done":
      return "done"
    case "idle":
      return "—"
    default:
      return status || "—"
  }
}

function debridBadgeVariant(status: string): "default" | "secondary" | "destructive" | "outline" {
  switch (status) {
    case "debriding":
    case "webseed":
    case "done":
      return "default"
    case "failed":
      return "destructive"
    case "candidate":
    case "pending":
      return "secondary"
    default:
      return "outline"
  }
}

interface TorrentGridProps {
  selectedHash: string | null
  onSelect: (t: TorrentInfo) => void
  highlightHashes?: Set<string>
  refreshKey?: number
  onNavigate?: (action: ContextMenuAction, torrent: TorrentInfo) => void
  onActionDone?: () => void
}

export function TorrentGrid({
  selectedHash,
  onSelect,
  highlightHashes,
  refreshKey = 0,
  onNavigate,
  onActionDone,
}: TorrentGridProps) {
  const [torrents, setTorrents] = useState<TorrentInfo[]>([])
  const [filter, setFilter] = useState<string>("downloading")
  const [search, setSearch] = useState("")
  const [sortKey, setSortKey] = useState<SortKey>("priority")
  const [sortAsc, setSortAsc] = useState(true)
  const [loading, setLoading] = useState(true)
  const [limit, setLimit] = useState<number>(() => readStoredLimit())
  const [customOpen, setCustomOpen] = useState(false)
  const [customLimitDraft, setCustomLimitDraft] = useState("")
  const [widths, setWidths] = useState<WidthMap>(() => readStoredWidths())
  const [menu, setMenu] = useState<{ torrent: TorrentInfo; x: number; y: number } | null>(null)
  const parentRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{
    id: ColId
    startX: number
    startW: number
  } | null>(null)

  const setLimitPersistent = useCallback((next: number) => {
    const value = Number.isFinite(next) && next >= 0 ? Math.floor(next) : 500
    setLimit(value)
    try {
      localStorage.setItem(LIMIT_STORAGE_KEY, String(value))
    } catch {
      /* ignore */
    }
  }, [])

  const persistWidths = useCallback((next: WidthMap) => {
    setWidths(next)
    try {
      localStorage.setItem(WIDTH_STORAGE_KEY, JSON.stringify(next))
    } catch {
      /* ignore */
    }
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await ControlApi.listTorrents({
        filter: filter === "all" ? undefined : filter,
        sort: sortKey === "debrid_status" ? "priority" : sortKey,
        limit,
        offset: 0,
      })
      setTorrents(res.torrents)
    } catch (err) {
      toast.error(`Failed to load torrents: ${getErrorMessage(err)}`)
    } finally {
      setLoading(false)
    }
  }, [filter, sortKey, limit])

  useEffect(() => {
    load()
    const id = window.setInterval(load, 8000)
    return () => window.clearInterval(id)
  }, [load, refreshKey])

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const drag = dragRef.current
      if (!drag) return
      const col = COLUMNS.find((c) => c.id === drag.id)
      if (!col) return
      const nextW = Math.max(col.min, Math.floor(drag.startW + (e.clientX - drag.startX)))
      setWidths((prev) => ({ ...prev, [drag.id]: nextW }))
    }
    const onUp = () => {
      if (!dragRef.current) return
      dragRef.current = null
      setWidths((prev) => {
        try {
          localStorage.setItem(WIDTH_STORAGE_KEY, JSON.stringify(prev))
        } catch {
          /* ignore */
        }
        return prev
      })
      document.body.style.cursor = ""
      document.body.style.userSelect = ""
    }
    window.addEventListener("mousemove", onMove)
    window.addEventListener("mouseup", onUp)
    return () => {
      window.removeEventListener("mousemove", onMove)
      window.removeEventListener("mouseup", onUp)
    }
  }, [])

  const rows = useMemo(() => {
    let list = torrents.map((t) => ({
      ...t,
      debrid_status: debridStatusOf(t),
    }))
    if (search.trim()) {
      const q = search.toLowerCase()
      list = list.filter(
        (t) =>
          t.name.toLowerCase().includes(q) ||
          t.hash.toLowerCase().includes(q) ||
          (t.tags || "").toLowerCase().includes(q) ||
          (t.qbx_status || "").toLowerCase().includes(q) ||
          t.debrid_status.toLowerCase().includes(q),
      )
    }
    const dir = sortAsc ? 1 : -1
    list = [...list].sort((a, b) => {
      if (sortKey === "debrid_status") {
        const ar = DEBRID_SORT_RANK[a.debrid_status] ?? 200
        const br = DEBRID_SORT_RANK[b.debrid_status] ?? 200
        if (ar !== br) return (ar - br) * dir
        return a.name.localeCompare(b.name) * dir
      }
      const av = (a as any)[sortKey]
      const bv = (b as any)[sortKey]
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * dir
      return String(av ?? "").localeCompare(String(bv ?? "")) * dir
    })
    return list
  }, [torrents, search, sortKey, sortAsc])

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 36,
    overscan: 12,
  })

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) setSortAsc((v) => !v)
    else {
      setSortKey(key)
      setSortAsc(true)
    }
  }

  const applyCustomLimit = () => {
    const n = Number(customLimitDraft.trim())
    if (!Number.isFinite(n) || n < 0) {
      toast.error("Enter a page size ≥ 0 (0 = all)")
      return
    }
    setLimitPersistent(Math.floor(n))
    setCustomLimitDraft("")
  }

  const startResize = (id: ColId, clientX: number) => {
    dragRef.current = { id, startX: clientX, startW: widths[id] }
    document.body.style.cursor = "col-resize"
    document.body.style.userSelect = "none"
  }

  const gridTemplate = useMemo(
    () => COLUMNS.map((c) => `${widths[c.id]}px`).join(" "),
    [widths],
  )
  const totalWidth = useMemo(
    () => COLUMNS.reduce((sum, c) => sum + widths[c.id], 0) + 24,
    [widths],
  )

  const limitLabel = limit === 0 ? "all" : String(limit)
  const cappedHint =
    limit > 0 && torrents.length >= limit
      ? ` (capped at ${limit})`
      : ""

  const renderCell = (t: (typeof rows)[number], col: ColDef) => {
    switch (col.id) {
      case "name":
        return (
          <span className="truncate block" title={t.name}>
            {t.name}
          </span>
        )
      case "progress":
        return <span>{(t.progress * 100).toFixed(1)}%</span>
      case "state":
        return <span className="truncate block text-muted-foreground">{t.state}</span>
      case "size":
        return <span>{formatSize(t.size)}</span>
      case "priority":
        return <span>{t.priority ?? "—"}</span>
      case "dlspeed":
        return <span>{formatSpeed(t.dlspeed || 0)}</span>
      case "debrid_status": {
        const debrid = t.debrid_status
        const label = debridLabel(debrid)
        return (
          <span title={t.qbx_reason || debrid}>
            {label === "—" ? (
              <span className="text-muted-foreground">—</span>
            ) : (
              <Badge variant={debridBadgeVariant(debrid)} className="text-[10px] px-1 py-0">
                {label}
              </Badge>
            )}
          </span>
        )
      }
      default:
        return null
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <div className="flex gap-1">
          {FILTERS.map((f) => (
            <Button
              key={f.id}
              size="sm"
              variant={filter === f.id ? "default" : "outline"}
              className="h-7 text-xs"
              onClick={() => setFilter(f.id)}
            >
              {f.label}
            </Button>
          ))}
        </div>
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search name, hash, tag…"
          className="h-7 max-w-xs text-xs"
        />
        <label className="flex items-center gap-1 text-xs text-muted-foreground">
          <span>Show</span>
          <select
            className="h-7 rounded border border-border bg-card px-1 text-xs text-foreground"
            value={
              customOpen
                ? "custom"
                : LIMIT_PRESETS.includes(limit as (typeof LIMIT_PRESETS)[number])
                  ? String(limit)
                  : "custom"
            }
            onChange={(e) => {
              const v = e.target.value
              if (v === "custom") {
                setCustomOpen(true)
                setCustomLimitDraft(limit > 0 ? String(limit) : "")
                return
              }
              setCustomOpen(false)
              setLimitPersistent(Number(v))
            }}
            title="Page size (how many torrents to fetch)"
          >
            {LIMIT_PRESETS.map((n) => (
              <option key={n} value={n}>
                {n === 0 ? "All" : n}
              </option>
            ))}
            <option value="custom">Custom…</option>
          </select>
        </label>
        {(customOpen || !LIMIT_PRESETS.includes(limit as (typeof LIMIT_PRESETS)[number])) && (
          <div className="flex items-center gap-1">
            <Input
              value={customLimitDraft}
              onChange={(e) => setCustomLimitDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  applyCustomLimit()
                  setCustomOpen(false)
                }
              }}
              placeholder="e.g. 8000"
              className="h-7 w-24 text-xs"
              inputMode="numeric"
            />
            <Button
              size="sm"
              variant="outline"
              className="h-7 text-xs"
              onClick={() => {
                applyCustomLimit()
                setCustomOpen(false)
              }}
            >
              Apply
            </Button>
          </div>
        )}
        <Button
          size="sm"
          variant="ghost"
          className="h-7 text-xs"
          title="Reset column widths"
          onClick={() => persistWidths(defaultWidths())}
        >
          Reset cols
        </Button>
        <Button size="sm" variant="ghost" className="h-7 text-xs ml-auto" onClick={load} disabled={loading}>
          Refresh
        </Button>
        <span className="text-xs text-muted-foreground" title={`Fetch limit: ${limitLabel}`}>
          {rows.length} shown{cappedHint}
        </span>
      </div>

      <div className="overflow-x-auto border-b border-border bg-card/40">
        <div
          className="grid gap-0 px-3 py-1.5 min-w-full"
          style={{ gridTemplateColumns: gridTemplate, width: Math.max(totalWidth, 640) }}
        >
          {COLUMNS.map((col, idx) => (
            <div key={col.id} className="relative min-w-0 pr-2">
              <button
                type="button"
                className="text-left text-[11px] uppercase tracking-wide text-muted-foreground hover:text-foreground w-full truncate"
                onClick={() => toggleSort(col.sortKey)}
              >
                {col.label}
                {sortKey === col.sortKey ? (sortAsc ? " ↑" : " ↓") : ""}
              </button>
              {idx < COLUMNS.length - 1 && (
                <div
                  role="separator"
                  aria-orientation="vertical"
                  aria-label={`Resize ${col.label}`}
                  className="absolute right-0 top-0 z-10 h-full w-1.5 cursor-col-resize hover:bg-sky-500/50 active:bg-sky-500/70"
                  onMouseDown={(e) => {
                    e.preventDefault()
                    e.stopPropagation()
                    startResize(col.id, e.clientX)
                  }}
                  onDoubleClick={(e) => {
                    e.preventDefault()
                    persistWidths({ ...widths, [col.id]: col.defaultWidth })
                  }}
                />
              )}
            </div>
          ))}
        </div>
      </div>

      <div ref={parentRef} className="flex-1 min-h-0 overflow-auto font-mono text-xs">
        {loading && rows.length === 0 ? (
          <div className="p-4 text-muted-foreground">Loading torrents…</div>
        ) : rows.length === 0 ? (
          <div className="p-4 text-muted-foreground">No torrents in this filter.</div>
        ) : (
          <div
            style={{
              height: virtualizer.getTotalSize(),
              position: "relative",
              minWidth: Math.max(totalWidth, 640),
            }}
          >
            {virtualizer.getVirtualItems().map((item) => {
              const t = rows[item.index]
              const selected = selectedHash === t.hash
              const flash = highlightHashes?.has(t.hash)
              return (
                <div
                  key={t.hash}
                  role="row"
                  tabIndex={0}
                  onClick={() => onSelect(t)}
                  onContextMenu={(e) => {
                    e.preventDefault()
                    onSelect(t)
                    setMenu({ torrent: t, x: e.clientX, y: e.clientY })
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault()
                      onSelect(t)
                    }
                  }}
                  className={`absolute left-0 grid gap-0 px-3 py-1.5 text-left border-b border-border/40 hover:bg-accent/40 cursor-default outline-none ${
                    selected ? "bg-accent/60" : flash ? "bg-warning/20" : ""
                  }`}
                  style={{
                    height: item.size,
                    transform: `translateY(${item.start}px)`,
                    gridTemplateColumns: gridTemplate,
                    width: Math.max(totalWidth, 640),
                  }}
                >
                  {COLUMNS.map((col) => (
                    <div key={col.id} className="min-w-0 pr-2 flex items-center overflow-hidden">
                      {renderCell(t, col)}
                    </div>
                  ))}
                </div>
              )
            })}
          </div>
        )}
      </div>

      <TorrentContextMenu
        torrent={menu?.torrent ?? null}
        position={menu ? { x: menu.x, y: menu.y } : null}
        onClose={() => setMenu(null)}
        onActionDone={() => {
          void load()
          onActionDone?.()
        }}
        onNavigate={onNavigate}
      />
    </div>
  )
}

function formatSpeed(bps: number): string {
  if (!bps || bps < 0) return "—"
  return `${formatSize(bps)}/s`
}
