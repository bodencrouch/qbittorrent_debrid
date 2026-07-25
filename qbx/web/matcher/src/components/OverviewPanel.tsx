import { ScrollArea } from "@/components/ui/scroll-area"
import { AttentionPanel } from "@/components/AttentionPanel"
import { InterceptorMonitorPanel } from "@/components/InterceptorMonitorPanel"
import { MatcherActivityPanel } from "@/components/MatcherActivityPanel"
import { IntegrationHealthPanel } from "@/components/IntegrationHealthPanel"
import type { HealthInfo } from "@/api/backend"
import type { SettingsSection } from "@/components/SettingsPanel"

type OverviewPanelProps = {
  health: HealthInfo | null
  onOpenSettings: (section?: SettingsSection) => void
  onOpenStorage: () => void
  onOpenTorrents: () => void
  onRefreshHealth: () => void
}

export function OverviewPanel({
  health,
  onOpenSettings,
  onOpenStorage,
  onOpenTorrents,
  onRefreshHealth,
}: OverviewPanelProps) {
  return (
    <ScrollArea className="h-full">
      <div className="p-4 max-w-3xl mx-auto space-y-8">
        <AttentionPanel
          onOpenSettings={onOpenSettings}
          onOpenStorage={onOpenStorage}
          onOpenTorrents={onOpenTorrents}
          onRefreshHealth={onRefreshHealth}
        />
        <InterceptorMonitorPanel
          interceptor={health?.interceptor}
          onRefreshHealth={onRefreshHealth}
        />
        <MatcherActivityPanel interceptor={health?.interceptor} />
        <IntegrationHealthPanel onOpenSettings={onOpenSettings} />
      </div>
    </ScrollArea>
  )
}
