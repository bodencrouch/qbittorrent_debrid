import { useCallback, useEffect, useMemo, useState } from "react"
import { Group, Panel, Separator, useDefaultLayout } from "react-resizable-panels"
import { Toaster } from "@/components/ui/sonner"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { TorrentGrid } from "@/components/TorrentGrid"
import { WorkspaceTabs, type TabId } from "@/components/WorkspaceTabs"
import { LogPanel } from "@/components/LogPanel"
import { SettingsPanel } from "@/components/SettingsPanel"
import { ControlApi, type HealthInfo, type TorrentInfo } from "@/api/backend"
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

export default function App() {
  const query = useMemo(() => readQuery(), [])
  const [health, setHealth] = useState<HealthInfo | null>(null)
  const [selected, setSelected] = useState<TorrentInfo | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [highlight, setHighlight] = useState<Set<string>>(new Set())
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [workspaceTab, setWorkspaceTab] = useState<TabId>(
    query.view === "match" ? "match" : query.view === "debrid" ? "debrid" : "overview",
  )

  const verticalLayout = useDefaultLayout({ id: "qbx-shell-v" })
  const horizontalLayout = useDefaultLayout({ id: "qbx-shell-h" })

  const refreshHealth = useCallback(async () => {
    try {
      const h = await ControlApi.health()
      setHealth(h)
    } catch (err) {
      console.warn(err)
    }
  }, [])

  useEffect(() => {
    refreshHealth()
    const id = window.setInterval(refreshHealth, 10000)
    return () => window.clearInterval(id)
  }, [refreshHealth])

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

  const openWebUi = () => {
    window.open("/qbt/", "_blank", "noopener,noreferrer")
  }

  return (
    <div className="h-screen flex flex-col bg-background text-foreground dark">
      <header className="flex items-center gap-3 border-b border-border px-4 py-2 bg-card/50 shrink-0">
        <h1 className="text-sm font-bold tracking-[0.2em]">QBX</h1>
        <Badge variant={health?.ok ? "default" : "destructive"} className="text-[10px]">
          {health?.ok ? "online" : "…"}
        </Badge>
        <Badge variant="outline" className="text-[10px]">
          debrid {health?.debrid_enabled ? "on" : "off"}
        </Badge>
        <Badge variant="outline" className="text-[10px]">
          interceptor {health?.interceptor_running ? "running" : "stopped"}
        </Badge>
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" variant="outline" className="h-7 text-xs" onClick={toggleInterceptor}>
            {health?.interceptor_running ? "Stop interceptor" : "Start interceptor"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            onClick={async () => {
              try {
                await ControlApi.interceptorScan()
                toast.success("Policy scan started")
                setRefreshKey((k) => k + 1)
              } catch (err) {
                toast.error(getErrorMessage(err))
              }
            }}
          >
            Scan now
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            onClick={openWebUi}
            title="Open the full qBittorrent WebUI in a new browser window (proxied at /qbt/)"
          >
            Open WebUI
          </Button>
          <Button
            size="sm"
            variant={settingsOpen ? "default" : "outline"}
            className="h-7 text-xs"
            onClick={() => setSettingsOpen((v) => !v)}
          >
            Settings
          </Button>
        </div>
      </header>

      <SettingsPanel
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={() => {
          void refreshHealth()
          setRefreshKey((k) => k + 1)
        }}
      />

      <div className="flex-1 min-h-0">
        <Group orientation="vertical" className="h-full" {...verticalLayout}>
          <Panel id="main" defaultSize="72" minSize="35">
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
