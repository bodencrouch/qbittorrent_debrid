import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import {
  IntegrationService,
  type ContractCheck,
  type ContractReport,
} from "@/api/backend"
import type { SettingsSection } from "@/components/SettingsPanel"
import { getErrorMessage } from "@/lib/utils"
import { toast } from "@/lib/toast"

type IntegrationHealthPanelProps = {
  onOpenSettings?: (section?: SettingsSection) => void
}

function statusBadge(status: ContractReport["status"]) {
  switch (status) {
    case "ok":
      return { label: "OK", variant: "default" as const }
    case "degraded":
      return { label: "degraded", variant: "outline" as const }
    case "blocked":
      return { label: "blocked", variant: "destructive" as const }
  }
}

function mapSection(section: string): SettingsSection {
  if (section === "connection" || section === "interceptor" || section === "matcher") {
    return section
  }
  if (section === "content_dupes") return "matcher"
  return "application"
}

function CheckRow({
  check,
  onFix,
  onSnooze,
}: {
  check: ContractCheck
  onFix: (section: SettingsSection) => void
  onSnooze?: (checkId: string) => void
}) {
  const isHard = check.severity === "hard"
  return (
    <div className="rounded-md border border-border/60 bg-muted/20 px-3 py-2 text-xs space-y-1">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Badge variant={isHard ? "destructive" : "outline"} className="text-[9px] uppercase">
              {isHard ? "fail" : "warn"}
            </Badge>
            <span className="font-medium">{check.title}</span>
          </div>
          <p className="text-muted-foreground mt-1">{check.detail}</p>
          {check.remediation && (
            <p className="text-muted-foreground/90 mt-1">{check.remediation}</p>
          )}
        </div>
        <div className="flex shrink-0 gap-1">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-7 text-[10px]"
            onClick={() => onFix(mapSection(check.settings_section))}
          >
            Settings
          </Button>
          {check.severity === "soft" && onSnooze && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-7 text-[10px]"
              onClick={() => onSnooze(check.id)}
            >
              Snooze 7d
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}

export function IntegrationHealthPanel({ onOpenSettings }: IntegrationHealthPanelProps) {
  const [report, setReport] = useState<ContractReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState(false)

  const load = useCallback(async () => {
    try {
      const r = await IntegrationService.get()
      setReport(r)
    } catch (err) {
      toast.error(getErrorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const snoozeCheck = async (checkId: string) => {
    try {
      await IntegrationService.snooze(checkId, Date.now() / 1000 + 7 * 86400)
      toast.success("Warning snoozed for 7 days")
      await load()
    } catch (err) {
      toast.error(getErrorMessage(err))
    }
  }

  const runChecks = async () => {
    setRunning(true)
    try {
      const r = await IntegrationService.run()
      setReport(r)
      if (r.status === "blocked") {
        toast.error("Integration contract blocked — fix hard failures before running automation")
      } else if (r.status === "degraded") {
        toast.warning("Integration contract has warnings")
      } else {
        toast.success("Integration contract OK")
      }
    } catch (err) {
      toast.error(getErrorMessage(err))
    } finally {
      setRunning(false)
    }
  }

  const badge = report ? statusBadge(report.status) : null
  const checks = report?.checks ?? []
  const hard = checks.filter((c) => c.severity === "hard")
  const soft = checks.filter((c) => c.severity === "soft")

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h3 className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
            Integration health
          </h3>
          {badge && (
            <Badge variant={badge.variant} className="text-[9px] uppercase">
              {badge.label}
            </Badge>
          )}
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-7 text-[10px]"
          disabled={running}
          onClick={() => void runChecks()}
        >
          {running ? (
            <>
              <Spinner className="mr-1.5" />
              Running…
            </>
          ) : (
            "Run checks"
          )}
        </Button>
      </div>
      <p className="text-[11px] text-muted-foreground">
        Validates configured paths, writability, and qBittorrent alignment before matcher and storage
        automation runs.
      </p>
      {loading && !report ? (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Spinner />
          Loading contract status…
        </div>
      ) : checks.length === 0 ? (
        <p className="text-xs text-muted-foreground">No issues reported.</p>
      ) : (
        <div className="space-y-2">
          {hard.length > 0 && (
            <div className="space-y-2">
              <p className="text-[10px] uppercase tracking-wide text-destructive">Hard failures</p>
              {hard.map((c) => (
                <CheckRow
                  key={c.id}
                  check={c}
                  onFix={(s) => onOpenSettings?.(s)}
                  onSnooze={(id) => void snoozeCheck(id)}
                />
              ))}
            </div>
          )}
          {soft.length > 0 && (
            <div className="space-y-2">
              <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Warnings</p>
              {soft.map((c) => (
                <CheckRow
                  key={c.id}
                  check={c}
                  onFix={(s) => onOpenSettings?.(s)}
                  onSnooze={(id) => void snoozeCheck(id)}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}
