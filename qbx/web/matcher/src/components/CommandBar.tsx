import { useCallback, useState } from "react"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { barActions, type ActionContext, type AppAction } from "@/lib/actions"
import { getErrorMessage } from "@/lib/utils"
import { toast } from "sonner"
import { cn } from "@/lib/utils"

interface CommandBarProps {
  ctx: ActionContext
  className?: string
}

export function CommandBar({ ctx, className }: CommandBarProps) {
  const [busyId, setBusyId] = useState<string | null>(null)
  const actions = barActions()

  const runAction = useCallback(
    async (action: AppAction) => {
      const reason = action.disabledReason?.(ctx) ?? null
      if (reason || busyId) return
      setBusyId(action.id)
      try {
        await action.run(ctx)
        toast.success(`${action.label} queued`)
        ctx.onActionDone?.()
      } catch (err) {
        toast.error(getErrorMessage(err))
      } finally {
        setBusyId(null)
      }
    },
    [busyId, ctx],
  )

  if (!ctx.torrent) {
    return (
      <div
        className={cn(
          "flex items-center gap-2 border-t border-border bg-card/40 px-3 py-1.5 text-xs text-muted-foreground",
          className,
        )}
      >
        <span className="font-medium tracking-wide text-muted-foreground/80">ACTIONS</span>
        <span>Select a torrent to enable ops</span>
      </div>
    )
  }

  return (
    <TooltipProvider delayDuration={200}>
      <div
        className={cn(
          "flex flex-wrap items-center gap-1.5 border-t border-border bg-card/40 px-3 py-1.5",
          busyId && "opacity-90",
          className,
        )}
        role="toolbar"
        aria-label="Torrent actions"
      >
        <span className="mr-1 text-[10px] font-semibold tracking-[0.14em] text-muted-foreground">
          ACTIONS
        </span>
        <span className="mr-2 max-w-[12rem] truncate font-mono text-[10px] text-muted-foreground" title={ctx.torrent.name}>
          {ctx.torrent.name}
        </span>
        {actions.map((action) => {
          const reason = action.disabledReason?.(ctx) ?? null
          const disabled = !!reason || !!busyId
          const working = busyId === action.id
          return (
            <Tooltip key={action.id}>
              <TooltipTrigger asChild>
                <Button
                  size="sm"
                  variant={action.variant || "default"}
                  className={cn(
                    "h-7 text-xs transition-opacity",
                    working && "animate-pulse",
                  )}
                  disabled={disabled}
                  onClick={() => void runAction(action)}
                  aria-label={reason ? `${action.label}. ${reason}` : `${action.label}. ${action.tip}`}
                >
                  {working ? `${action.label}…` : action.label}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs text-left normal-case font-normal">
                {reason || action.tip}
              </TooltipContent>
            </Tooltip>
          )
        })}
        {busyId && (
          <span className="ml-1 text-[10px] text-muted-foreground animate-in fade-in">Working…</span>
        )}
      </div>
    </TooltipProvider>
  )
}
