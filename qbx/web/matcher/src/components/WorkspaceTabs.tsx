import { useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import { MatchingPanel } from "@/components/MatchingPanel"
import { OverviewPanel } from "@/components/OverviewPanel"
import { DebridPanel } from "@/components/DebridPanel"
import type { TorrentInfo } from "@/api/backend"

export type TabId = "overview" | "match" | "debrid"

interface WorkspaceTabsProps {
  torrent: TorrentInfo | null
  initialTab?: TabId
  /** Controlled tab override (e.g. from grid context menu). */
  activeTab?: TabId
  onTabChange?: (tab: TabId) => void
  onActionDone?: () => void
}

export function WorkspaceTabs({
  torrent,
  initialTab = "overview",
  activeTab,
  onTabChange,
  onActionDone,
}: WorkspaceTabsProps) {
  const [tab, setTab] = useState<TabId>(activeTab || initialTab)

  useEffect(() => {
    if (activeTab) setTab(activeTab)
  }, [activeTab])

  useEffect(() => {
    if (!activeTab && initialTab) setTab(initialTab)
  }, [initialTab, torrent?.hash, activeTab])

  const selectTab = (next: TabId) => {
    setTab(next)
    onTabChange?.(next)
  }

  if (!torrent) {
    return (
      <div className="h-full flex items-center justify-center text-sm text-muted-foreground p-6 text-center">
        Select a torrent from the grid to inspect, match files, or force debrid.
        <br />
        <span className="text-xs mt-2 block">Right-click a row for Start / Stop / Remove / qbx actions.</span>
      </div>
    )
  }

  const tabs: { id: TabId; label: string }[] = [
    { id: "overview", label: "Overview" },
    { id: "match", label: "Files / Match" },
    { id: "debrid", label: "Debrid" },
  ]

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex gap-1 border-b border-border px-2 py-1.5">
        {tabs.map((t) => (
          <Button
            key={t.id}
            size="sm"
            variant={tab === t.id ? "default" : "ghost"}
            className="h-7 text-xs"
            onClick={() => selectTab(t.id)}
          >
            {t.label}
          </Button>
        ))}
      </div>
      <div className="flex-1 min-h-0 overflow-hidden">
        {tab === "overview" && <OverviewPanel torrent={torrent} onActionDone={onActionDone} />}
        {tab === "match" && (
          <div className="h-full p-2 overflow-auto">
            <MatchingPanel torrent={torrent} compact />
          </div>
        )}
        {tab === "debrid" && <DebridPanel torrent={torrent} onActionDone={onActionDone} />}
      </div>
    </div>
  )
}
