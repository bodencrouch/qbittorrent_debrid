import { useCallback, useEffect, useMemo, useState } from "react"
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from "@/components/ui/command"
import { APP_ACTIONS, type ActionContext, type ActionGroup, type AppAction } from "@/lib/actions"
import { getErrorMessage } from "@/lib/utils"
import { toast } from "sonner"

interface CommandPaletteProps {
  ctx: ActionContext
  open: boolean
  onOpenChange: (open: boolean) => void
}

const GROUP_ORDER: ActionGroup[] = ["Torrent", "Daemon", "Nav", "Settings"]

export function CommandPalette({ ctx, open, onOpenChange }: CommandPaletteProps) {
  const [busyId, setBusyId] = useState<string | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault()
        onOpenChange(!open)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, onOpenChange])

  const grouped = useMemo(() => {
    const map = new Map<ActionGroup, AppAction[]>()
    for (const g of GROUP_ORDER) map.set(g, [])
    for (const action of APP_ACTIONS) {
      map.get(action.group)?.push(action)
    }
    // Promote torrent group when a selection exists.
    const order = ctx.torrent
      ? GROUP_ORDER
      : (["Daemon", "Nav", "Settings", "Torrent"] as ActionGroup[])
    return order.map((g) => ({ group: g, actions: map.get(g) || [] })).filter((x) => x.actions.length)
  }, [ctx.torrent])

  const runAction = useCallback(
    async (action: AppAction) => {
      const reason = action.disabledReason?.(ctx) ?? null
      if (reason) {
        toast.message(reason)
        return
      }
      setBusyId(action.id)
      try {
        await action.run(ctx)
        if (!action.id.startsWith("settings-") && !action.id.startsWith("show-") && action.id !== "match-files") {
          toast.success(`${action.label} queued`)
        }
        ctx.onActionDone?.()
        onOpenChange(false)
      } catch (err) {
        toast.error(getErrorMessage(err))
      } finally {
        setBusyId(null)
      }
    },
    [ctx, onOpenChange],
  )

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange}>
      <CommandInput placeholder="Type a command… (nudge, scan, settings…)" />
      <CommandList>
        <CommandEmpty>No matching command.</CommandEmpty>
        {grouped.map(({ group, actions }) => (
          <CommandGroup key={group} heading={group}>
            {actions.map((action) => {
              const reason = action.disabledReason?.(ctx) ?? null
              const disabled = !!reason || !!busyId
              return (
                <CommandItem
                  key={action.id}
                  value={`${action.label} ${action.id} ${action.tip}`}
                  disabled={disabled}
                  onSelect={() => void runAction(action)}
                >
                  <span className="flex-1 truncate">{action.label}</span>
                  {reason ? (
                    <span className="ml-2 text-[10px] text-muted-foreground truncate max-w-[10rem]">
                      {reason}
                    </span>
                  ) : action.shortcut ? (
                    <CommandShortcut>{action.shortcut}</CommandShortcut>
                  ) : null}
                </CommandItem>
              )
            })}
          </CommandGroup>
        ))}
      </CommandList>
    </CommandDialog>
  )
}
