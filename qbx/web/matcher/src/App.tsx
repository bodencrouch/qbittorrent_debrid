import { useCallback, useEffect, useMemo, useState } from "react"
import { Group, Panel, Separator, useDefaultLayout } from "react-resizable-panels"
import { Toaster } from "@/components/ui/sonner"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { TorrentGrid } from "@/components/TorrentGrid"
import { WorkspaceTabs, type TabId } from "@/components/WorkspaceTabs"
import { LogPanel } from "@/components/LogPanel"
import { SettingsPanel, type SettingsSection } from "@/components/SettingsPanel"
import { StoragePanel } from "@/components/StoragePanel"
import { OverviewPanel } from "@/components/OverviewPanel"
import { MatcherSurface } from "@/components/MatcherSurface"
import { CommandBar } from "@/components/CommandBar"
import { CommandPalette } from "@/components/CommandPalette"
import { ControlApi, type HealthInfo, type TorrentInfo } from "@/api/backend"
import type { ActionContext } from "@/lib/actions"
import { getErrorMessage } from "@/lib/utils"
import { toast } from "sonner"
import type { ContextMenuAction } from "@/components/TorrentContextMenu"

function readQuery(): { view?: string; hash?: string } {
  const q = new URLSearchParams(window.location.search)
  return {
    view: q.get("view") || undefined,
    hash: q.get("hash") || undefined,
  }
}

type HealthState = "loading" | "online" | "offline" | "partial"

// Shell-level surface. Storage and Matcher are torrent-independent, so they cannot live in
// * WorkspaceTabs (those require a selected torrent).
type Surface = "overview" | "torrents" | "storage" | "matcher";

function initialSurface(view?: string): Surface {
  if (view === "storage") return "storage"
  if (view === "matcher") return "matcher"
  if (view === "torrents" || view === "match" || view === "debrid") return "torrents"
  return "overview"
}

function healthState(health: HealthInfo | null, everLoaded: boolean): HealthState {
  if (!everLoaded && !health) return "loading"
  if (!health) return "offline"
  if (health.ok) return "online"
  // Reachable API but unhealthy stack (qbt/debrid/interceptor issues).
  return "partial"
}

function healthBadge(state: HealthState): { label: string; variant: "default" | "destructive" | "outline" | "secondary" } {
  switch (state) {
    case "loading":
      return { label: "loading", variant: "secondary" }
    case "online":
      return { label: "online", variant: "default" }
    case "partial":
      return { label: "partial", variant: "outline" }
    case "offline":
      return { label: "offline", variant: "destructive" }
  }
}

export default function App() {
  const query = useMemo(() => readQuery(), [])
  const [health, setHealth] = useState<HealthInfo | null>(null)
  const [healthEverLoaded, setHealthEverLoaded] = useState(false)
  const [selected, setSelected] = useState<TorrentInfo | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [highlight, setHighlight] = useState<Set<string>>(new Set())
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsSection, setSettingsSection] = useState<SettingsSection | undefined>()
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [surface, setSurface] = useState<Surface>(initialSurface(query.view))
  const [workspaceTab, setWorkspaceTab] = useState<TabId>(
    query.view === "match" ? "match" : query.view === "debrid" ? "debrid" : "overview",
  )

  const verticalLayout = useDefaultLayout({ id: "qbx-shell-v" })
  const horizontalLayout = useDefaultLayout({ id: "qbx-shell-h" })

  const refreshHealth = useCallback(async () => {
    try {
      const h = await ControlApi.health()
      setHealth(h)
      setHealthEverLoaded(true)
    } catch (err) {
      console.warn(err)
      setHealth(null)
      setHealthEverLoaded(true)
    }
  }, [])

  useEffect(() => {
    refreshHealth()
    const id = window.setInterval(refreshHealth, 10000)
    return () => window.clearInterval(id)
  }, [refreshHealth])

  useEffect(() => {
    if (sessionStorage.getItem("qbx_update_checked")) return
    let cancelled = false
    ControlApi.version()
      .then((v) => {
        if (cancelled || !v.check_on_startup || !v.source.owner || !v.source.repo) return
        sessionStorage.setItem("qbx_update_checked", "1")
        return ControlApi.updateCheck().then((res) => {
          if (cancelled || !res.ok || !res.update_available) return
          toast.info(`qbx ${res.latest} is available`, {
            action: res.release?.html_url
              ? {
                  label: "Release notes",
                  onClick: () => window.open(res.release!.html_url, "_blank", "noopener,noreferrer"),
                }
              : undefined,
          })
        })
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const onMsg = (ev: MessageEvent) => {
      if (ev.origin !== window.location.origin) return
      const data = ev.data
      if (data?.type === "qbx.selectTorrent" && data.hash) {
        ControlApi.getTorrent(data.hash)
          .then((t) => setSelected(t))
          .catch(() => undefined)
      }
    }
    window.addEventListener("message", onMsg)
    return () => window.removeEventListener("message", onMsg)
  }, [])

  useEffect(() => {
    if (!query.hash) return
    ControlApi.getTorrent(query.hash)
      .then((t) => setSelected(t))
      .catch(() => toast.error("Deep-link torrent not found"))
  }, [query.hash])

  const onSelect = (t: TorrentInfo) => {
    setSelected(t)
    const url = new URL(window.location.href)
    url.searchParams.set("hash", t.hash)
    window.history.replaceState({}, "", url.toString())
  }

  const onGridNavigate = (action: ContextMenuAction, torrent: TorrentInfo) => {
    onSelect(torrent)
    if (action === "match") setWorkspaceTab("match")
    else if (action === "debrid") setWorkspaceTab("debrid")
    else setWorkspaceTab("overview")
  }

  const openSettings = useCallback((section?: string) => {
    if (section) setSettingsSection(section as SettingsSection)
    else setSettingsSection(undefined)
    setSettingsOpen(true)
  }, [])

  const actionCtx: ActionContext = useMemo(
    () => ({
      torrent: selected,
      health,
      openSettings,
      onNavigate: (tab, torrent) => {
        onSelect(torrent)
        setSurface("torrents")
        setWorkspaceTab(tab)
      },
      openStorage: () => setSurface("storage"),
      onActionDone: () => {
        setRefreshKey((k) => k + 1)
        void refreshHealth()
      },
      refreshHealth,
    }),
    [selected, health, openSettings, refreshHealth],
  )

  const toggleInterceptor = async () => {
    try {
      if (health?.interceptor_running) {
        await ControlApi.interceptorStop()
        toast.success("Interceptor stopped")
      } else {
        await ControlApi.interceptorStart()
        toast.success("Interceptor started")
      }
      await refreshHealth()
    } catch (err) {
      toast.error(getErrorMessage(err))
    }
  }

  const hState = healthState(health, healthEverLoaded)
  const hBadge = healthBadge(hState)
  const contractStatus = health?.contract?.status
  const contractBadge =
    contractStatus && contractStatus !== "ok"
      ? {
          label: `contract ${contractStatus}`,
          variant: contractStatus === "blocked" ? ("destructive" as const) : ("outline" as const),
        }
      : null
  const attentionCritical = health?.attention?.critical_count ?? 0

  return (
    <div className="h-screen flex flex-col bg-background text-foreground dark">
      <header className="flex items-center gap-3 border-b border-border px-4 py-2 bg-card/50 shrink-0">
        <h1 className="text-sm font-bold tracking-[0.2em]">QBX</h1>
        <Badge variant={hBadge.variant} className="text-[10px]">
          {hBadge.label}
        </Badge>
        {contractBadge && (
          <Badge variant={contractBadge.variant} className="text-[10px]">
            {contractBadge.label}
          </Badge>
        )}
        {attentionCritical > 0 && (
          <Badge variant="destructive" className="text-[10px]">
            {attentionCritical} need attention
          </Badge>
        )}
        <Badge variant="outline" className="text-[10px]">
          debrid {health?.debrid_enabled ? "on" : "off"}
        </Badge>
        <Badge variant="outline" className="text-[10px]">
          interceptor {health?.interceptor_running ? "running" : "stopped"}
        </Badge>
        <nav aria-label="Primary" className="flex items-center gap-1 ml-2">
          <Button
            size="sm"
            variant={surface === "overview" ? "default" : "outline"}
            className="h-7 text-xs"
            aria-current={surface === "overview" ? "page" : undefined}
            onClick={() => setSurface("overview")}
          >
            Overview
          </Button>
          <Button
            size="sm"
            variant={surface === "torrents" ? "default" : "outline"}
            className="h-7 text-xs"
            aria-current={surface === "torrents" ? "page" : undefined}
            onClick={() => setSurface("torrents")}
          >
            Torrents
          </Button>
          <Button
            size="sm"
            variant={surface === "storage" ? "default" : "outline"}
            className="h-7 text-xs"
            aria-current={surface === "storage" ? "page" : undefined}
            onClick={() => setSurface("storage")}
          >
            Storage
          </Button>
          <Button
            size="sm"
            variant={surface === "matcher" ? "default" : "outline"}
            className="h-7 text-xs"
            aria-current={surface === "matcher" ? "page" : undefined}
            onClick={() => setSurface("matcher")}
          >
            Matcher
          </Button>
        </nav>
        {health?.server_info && (
          <Badge variant="outline" className="text-[10px] font-mono ml-2">
            {health.server_info.host}:{health.server_info.port}
          </Badge>
        )}
        {health?.links?.qbittorrent_webui && (
          <a
            href={health.links.qbittorrent_webui}
            target="_blank"
            rel="noopener noreferrer"
            className="ml-2"
          >
            <Button size="sm" variant="outline" className="h-7 text-xs">
              Open qBittorrent
            </Button>
          </a>
        )}
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" variant="outline" className="h-7 text-xs" onClick={toggleInterceptor}>
            {health?.interceptor_running ? "Stop interceptor" : "Start interceptor"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs font-mono"
            onClick={() => setPaletteOpen(true)}
            title="Command palette (⌘K / Ctrl+K)"
          >
            ⌘K
          </Button>
          {!health?.configured && (
            <Button
              size="sm"
              variant="destructive"
              className="h-7 text-xs"
              onClick={() => {
                toast.error("qbx is not configured yet. Run: qbx setup");
              }}
            >
              Setup Required
            </Button>
          )}
          <Button
            size="sm"
            variant={settingsOpen ? "default" : "outline"}
            className="h-7 text-xs"
            onClick={() => openSettings()}
          >
            Settings
          </Button>
        </div>
      </header>

      <CommandBar ctx={actionCtx} />

      <SettingsPanel
        open={settingsOpen}
        initialSection={settingsSection}
        onClose={() => {
          setSettingsOpen(false)
          setSettingsSection(undefined)
        }}
        onSaved={() => {
          void refreshHealth()
          setRefreshKey((k) => k + 1)
        }}
      />

      <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} ctx={actionCtx} />

      <div className="flex-1 min-h-0">
        <Group orientation="vertical" className="h-full" {...verticalLayout}>
          <Panel id="main" defaultSize="72" minSize="35">
            {surface === "overview" ? (
              <OverviewPanel
                health={health}
                onOpenSettings={openSettings}
                onOpenStorage={() => setSurface("storage")}
                onOpenTorrents={() => setSurface("torrents")}
                onOpenMatcher={() => setSurface("matcher")}
                onRefreshHealth={() => void refreshHealth()}
              />
            ) : surface === "storage" ? (
              <StoragePanel />
            ) : surface === "matcher" ? (
              <MatcherSurface health={health} />
            ) : (
            <Group orientation="horizontal" className="h-full" {...horizontalLayout}>
              <Panel id="grid" defaultSize="55" minSize="30">
                <TorrentGrid
                  selectedHash={selected?.hash || null}
                  onSelect={onSelect}
                  highlightHashes={highlight}
                  refreshKey={refreshKey}
                  onNavigate={onGridNavigate}
                  onActionDone={() => {
                    setRefreshKey((k) => k + 1)
                    refreshHealth()
                  }}
                />
              </Panel>
              <Separator className="w-1.5 bg-border/60 hover:bg-sky-500/50 transition-colors" />
              <Panel id="workspace" defaultSize="45" minSize="25">
                <WorkspaceTabs
                  torrent={selected}
                  activeTab={workspaceTab}
                  onTabChange={setWorkspaceTab}
                  onActionDone={() => {
                    setRefreshKey((k) => k + 1)
                    refreshHealth()
                  }}
                />
              </Panel>
            </Group>
            )}
          </Panel>
          <Separator className="h-1.5 bg-border/60 hover:bg-sky-500/50 transition-colors" />
          <Panel id="logs" defaultSize="28" minSize="15">
            <LogPanel
              filterHash={selected?.hash || null}
              onHashClick={(hash) => {
                setHighlight(new Set([hash]))
                ControlApi.getTorrent(hash)
                  .then((t) => setSelected(t))
                  .catch(() => undefined)
              }}
            />
          </Panel>
        </Group>
      </div>
      <Toaster position="bottom-right" />
    </div>
  )
}
