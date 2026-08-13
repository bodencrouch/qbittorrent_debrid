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
import { toast } from "@/lib/toast"
import { cn } from "@/lib/utils"

interface CommandBarProps {
  ctx: ActionContext
  className?: string
}

export function CommandBar({ ctx, className }: CommandBarProps) {
  const [busyId, setBusyId] = useState<string | null>(null)
  const actions = barActions()
  const torrents = ctx.torrents && ctx.torrents.length > 0 ? ctx.torrents : ctx.torrent ? [ctx.torrent] : []
  const bulk = torrents.length > 1

  const runAction = useCallback(
    async (action: AppAction) => {
      if (busyId) return
      if (bulk) {
        const eligible = torrents.filter((t) => !action.disabledReason?.({ ...ctx, torrent: t }))
        if (eligible.length === 0) return
        setBusyId(action.id)
        const results = await Promise.allSettled(eligible.map((t) => action.run({ ...ctx, torrent: t })))
        const failed = results.filter((r) => r.status === "rejected").length
        const ok = results.length - failed
        if (ok > 0) toast.success(`${action.label}: queued for ${ok} torrent${ok === 1 ? "" : "s"}`)
        if (failed > 0) toast.error(`${action.label}: failed for ${failed} torrent${failed === 1 ? "" : "s"}`)
        ctx.onActionDone?.()
        setBusyId(null)
        return
      }
      const reason = action.disabledReason?.(ctx) ?? null
      if (reason) return
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
    [busyId, ctx, bulk, torrents],
  )

  if (torrents.length === 0) {
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
        {bulk ? (
          <>
            <span className="mr-1 font-mono text-[10px] text-muted-foreground">
              {torrents.length} selected
            </span>
            {ctx.onClearSelection && (
              <Button
                size="sm"
                variant="ghost"
                className="h-6 mr-2 px-2 text-[10px]"
                onClick={ctx.onClearSelection}
              >
                Clear
              </Button>
            )}
          </>
        ) : (
          <span
            className="mr-2 max-w-[12rem] truncate font-mono text-[10px] text-muted-foreground"
            title={torrents[0].name}
          >
            {torrents[0].name}
          </span>
        )}
        {actions.map((action) => {
          const reason = bulk ? null : (action.disabledReason?.(ctx) ?? null)
          const allDisabledInBulk =
            bulk && torrents.every((t) => !!action.disabledReason?.({ ...ctx, torrent: t }))
          const disabled = !!reason || !!busyId || allDisabledInBulk
          const working = busyId === action.id
          const tip = bulk
            ? allDisabledInBulk
              ? `${action.tip} (not applicable to any selected torrent)`
              : `${action.tip} Applies to all eligible torrents in the selection.`
            : reason || action.tip
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
                  aria-label={`${action.label}. ${tip}`}
                >
                  {working ? `${action.label}…` : action.label}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs text-left normal-case font-normal">
                {tip}
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
