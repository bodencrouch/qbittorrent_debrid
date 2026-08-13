import { useCallback, useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import { OverviewPanel } from "@/components/OverviewPanel"
import { StoragePanel } from "@/components/StoragePanel"
import { LogPanel } from "@/components/LogPanel"
import { SettingsPanel, type SettingsSection } from "@/components/SettingsPanel"
import { MatchingPanel } from "@/components/MatchingPanel"
import { DebridPanel } from "@/components/DebridPanel"
import { ControlApi, type HealthInfo } from "@/api/backend"
import { bridge } from "@/embed/bridge"
import { useHostSelection } from "@/embed/useHostSelection"
import { cn } from "@/lib/utils"

export type EmbedPanel = "overview" | "storage" | "log" | "settings" | "match" | "debrid"

const NAV_PANELS: { id: EmbedPanel; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "storage", label: "Storage" },
  { id: "log", label: "Live log" },
]

function normalizePanel(value: string | null): EmbedPanel {
  switch (value) {
    case "storage":
    case "log":
    case "settings":
    case "match":
    case "debrid":
      return value
    default:
      return "overview"
  }
}

function applyTheme(theme: "light" | "dark") {
  document.documentElement.classList.toggle("dark", theme === "dark")
}

interface EmbedHostProps {
  initialPanel: string | null
  initialHash?: string | null
  initialTheme?: string | null
  initialSection?: string | null
}

/**
 * Renders a single qbx panel with no shell chrome (no header, command bar,
 * palette, or resizable log splitter) so it reads as native content inside a
 * qBittorrent WebUI tab or MochaUI window. Shares every panel component with
 * the standalone App — only the surrounding frame differs, so there is
 * exactly one implementation of each surface to keep in sync.
 */
export function EmbedHost({ initialPanel, initialHash, initialTheme, initialSection }: EmbedHostProps) {
  const [panel, setPanel] = useState<EmbedPanel>(normalizePanel(initialPanel))
  const [section, setSection] = useState<SettingsSection | undefined>(
    (initialSection as SettingsSection) || undefined,
  )
  const [health, setHealth] = useState<HealthInfo | null>(null)
  const [active, setActive] = useState(true)
  const { activeHash, torrent } = useHostSelection(initialHash)

  useEffect(() => {
    applyTheme(initialTheme === "light" ? "light" : "dark")
  }, [initialTheme])

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await ControlApi.health())
    } catch {
      setHealth(null)
    }
  }, [])

  useEffect(() => {
    void refreshHealth()
    // Throttle to 60s while the host says our tab isn't visible — no point
    // polling a hidden iframe every 10s.
    const id = window.setInterval(() => void refreshHealth(), active ? 10000 : 60000)
    return () => window.clearInterval(id)
  }, [refreshHealth, active])

  useEffect(() => {
    return bridge.onHost((msg) => {
      switch (msg.type) {
        case "qbx.host.theme":
          applyTheme(msg.theme)
          break
        case "qbx.host.panel":
          setPanel(normalizePanel(msg.panel))
          if (msg.section) setSection(msg.section as SettingsSection)
          break
        case "qbx.host.activated":
          setActive(true)
          void refreshHealth()
          break
        case "qbx.host.deactivated":
          setActive(false)
          break
        default:
          break
      }
    })
  }, [refreshHealth])

  useEffect(() => {
    bridge.toHost({ v: 1, type: "qbx.ready", panel })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const openSettings = useCallback((s?: string) => {
    bridge.toHost({ v: 1, type: "qbx.openWindow", window: "settings", section: s })
  }, [])

  const openStorage = useCallback(() => setPanel("storage"), [])
  const openTorrents = useCallback(() => {
    bridge.toHost({ v: 1, type: "qbx.switchTab", tab: "transfers" })
  }, [])

  const isNavPanel = panel === "overview" || panel === "storage" || panel === "log"

  return (
    <div className="h-screen w-full flex flex-col bg-background text-foreground overflow-hidden">
      {isNavPanel && (
        <nav aria-label="qbx" className="flex items-center gap-1 border-b border-border px-2 py-1.5 shrink-0">
          {NAV_PANELS.map((p) => (
            <Button
              key={p.id}
              size="sm"
              variant={panel === p.id ? "default" : "ghost"}
              className={cn("h-7 text-xs")}
              onClick={() => setPanel(p.id)}
            >
              {p.label}
            </Button>
          ))}
          <Button size="sm" variant="ghost" className="h-7 text-xs ml-auto" onClick={() => openSettings()}>
            Settings
          </Button>
        </nav>
      )}
      <div className="flex-1 min-h-0">
        {panel === "overview" && (
          <OverviewPanel
            health={health}
            onOpenSettings={openSettings}
            onOpenStorage={openStorage}
            onOpenTorrents={openTorrents}
            onRefreshHealth={() => void refreshHealth()}
          />
        )}
        {panel === "storage" && <StoragePanel />}
        {panel === "log" && (
          <LogPanel
            filterHash={activeHash}
            onHashClick={(hash) => bridge.toHost({ v: 1, type: "qbx.selectTorrent", hash })}
          />
        )}
        {panel === "settings" && (
          <SettingsPanel
            open
            embedded
            initialSection={section}
            onClose={() => bridge.toHost({ v: 1, type: "qbx.closeWindow" })}
            onSaved={() => void refreshHealth()}
          />
        )}
        {panel === "match" &&
          (torrent ? (
            <div className="h-full p-2 overflow-auto">
              <MatchingPanel torrent={torrent} />
            </div>
          ) : (
            <EmptySelection />
          ))}
        {panel === "debrid" &&
          (torrent ? (
            <DebridPanel torrent={torrent} onActionDone={() => void refreshHealth()} />
          ) : (
            <EmptySelection />
          ))}
      </div>
    </div>
  )
}

function EmptySelection() {
  return (
    <div className="h-full flex items-center justify-center text-sm text-muted-foreground p-6 text-center">
      Select a torrent in qBittorrent, then reopen this window.
    </div>
  )
}
