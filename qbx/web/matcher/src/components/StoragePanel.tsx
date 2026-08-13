/**
 * Storage surface: exact-content duplicate and hardlink manager.
 *
 * Grouping is byte-identical content, never filename. Selection is not action —
 * a confirm step states exactly what will happen, deletions go to a recoverable
 * quarantine, and protected roots can never be selected for removal.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react"
import { toast } from "@/lib/toast"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Progress } from "@/components/ui/progress"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import {
  StorageService,
  eventsUrl,
  type DuplicateGroup,
  type DuplicateMember,
  type QuarantineEntry,
  type ReclaimAction,
  type StorageGroups,
  type SuppressedEntry,
} from "@/api/backend"
import { cn, formatSize, getErrorMessage } from "@/lib/utils"

type KeeperRule = "newest" | "oldest" | "shortest_path" | "under_root"

const KEEPER_RULES: { value: KeeperRule; label: string; tip: string }[] = [
  { value: "newest", label: "Keep newest", tip: "Highest modified time wins." },
  { value: "oldest", label: "Keep oldest", tip: "Lowest modified time wins — the original import." },
  { value: "shortest_path", label: "Keep shortest path", tip: "Prefer the copy nearest a root." },
  { value: "under_root", label: "Keep under protected root", tip: "Prefer a copy inside a protected root." },
]

/** What will happen to each member once the user commits. */
type Outcome = "keep" | "link" | "delete" | "review"

const OUTCOME_STYLES: Record<Outcome, string> = {
  keep: "text-emerald-400",
  link: "text-sky-400",
  delete: "text-rose-400",
  review: "text-amber-400",
}

const OUTCOME_LABELS: Record<Outcome, string> = {
  keep: "KEEP",
  link: "LINK-AWAY",
  delete: "DELETE",
  review: "REVIEW",
}

type GroupDecision = {
  keeper: string
  /** Only members the user marked for removal; everything else is KEEP. */
  actions: Record<string, "link" | "delete">
}

type GroupFilter = "all" | "unreviewed" | "partial" | "full"

type FocusRow =
  | { kind: "header"; digest: string }
  | { kind: "member"; digest: string; member: DuplicateMember }

function eligibleLosers(group: DuplicateGroup, keeper: string): DuplicateMember[] {
  const keeperMember = group.members.find((m) => m.path === keeper)
  return group.members.filter((member) => {
    if (member.path === keeper) return false
    if (member.protected) return false
    if (keeperMember && keeperMember.ino === member.ino) return false
    return true
  })
}

function groupReviewState(
  group: DuplicateGroup,
  decision: GroupDecision | undefined,
  keeper: string,
): "unreviewed" | "partial" | "full" {
  const eligible = eligibleLosers(group, keeper)
  if (!eligible.length) return "full"
  if (!decision) return "unreviewed"
  const selected = eligible.filter((m) => decision.actions[m.path])
  if (!selected.length) return "unreviewed"
  if (selected.length === eligible.length) return "full"
  return "partial"
}

function buildFocusOrder(
  groups: DuplicateGroup[],
  expanded: Set<string>,
  dupesOnly: boolean,
  decisions: Record<string, GroupDecision>,
  rule: KeeperRule,
): FocusRow[] {
  const rows: FocusRow[] = []
  for (const group of groups) {
    rows.push({ kind: "header", digest: group.digest })
    if (!expanded.has(group.digest)) continue
    const keeper = decisions[group.digest]?.keeper || pickKeeper(group, rule) || ""
    for (const member of group.members) {
      if (dupesOnly && member.path === keeper) continue
      rows.push({ kind: "member", digest: group.digest, member })
    }
  }
  return rows
}

function sameVolumeAsKeeper(group: DuplicateGroup, keeperPath: string, member: DuplicateMember): boolean {
  const keeper = group.members.find((m) => m.path === keeperPath)
  return !!keeper && keeper.dev === member.dev
}

export function StoragePanel() {
  const [data, setData] = useState<StorageGroups | null>(null)
  const [loading, setLoading] = useState(false)
  const [rule, setRule] = useState<KeeperRule>("newest")
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [decisions, setDecisions] = useState<Record<string, GroupDecision>>({})
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [applying, setApplying] = useState(false)
  const [quarantine, setQuarantine] = useState<QuarantineEntry[]>([])
  const [showQuarantine, setShowQuarantine] = useState(false)
  const [suppressed, setSuppressed] = useState<SuppressedEntry[]>([])
  const [showSuppressed, setShowSuppressed] = useState(false)
  const [sessionSuppressed, setSessionSuppressed] = useState<Set<string>>(new Set())
  const [dupesOnly, setDupesOnly] = useState(false)
  const [groupFilter, setGroupFilter] = useState<GroupFilter>("all")
  const [focusId, setFocusId] = useState<string | null>(null)
  const esRef = useRef<EventSource | null>(null)
  const tableRef = useRef<HTMLDivElement | null>(null)

  const reload = useCallback(async () => {
    try {
      const groups = await StorageService.groups()
      setData(groups)
    } catch (err) {
      console.warn(err)
    }
  }, [])

  const reloadQuarantine = useCallback(async () => {
    try {
      const q = await StorageService.quarantine()
      setQuarantine(q.items)
    } catch (err) {
      console.warn(err)
    }
  }, [])

  const reloadSuppressed = useCallback(async () => {
    try {
      const s = await StorageService.listSuppressed()
      setSuppressed(s.items)
    } catch (err) {
      console.warn(err)
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadQuarantine()
    void reloadSuppressed()
  }, [reload, reloadQuarantine, reloadSuppressed])

  // Live scan progress over the shared SSE stream.
  useEffect(() => {
    const es = new EventSource(eventsUrl(0))
    esRef.current = es
    es.onmessage = (ev) => {
      let payload: { kind?: string } = {}
      try {
        payload = JSON.parse(ev.data)
      } catch {
        return
      }
      const kind = payload.kind || ""
      if (!kind.startsWith("storage.")) return
      if (kind === "storage.scan.progress" || kind === "storage.scan.start") {
        void StorageService.status().then((s) =>
          setData((prev) => (prev ? { ...prev, ...s } : ({ ...s, truncated: false, items: [] } as StorageGroups))),
        )
        return
      }
      if (kind === "storage.scan.done" || kind === "storage.scan.failed") {
        void reload()
        return
      }
      if (kind.startsWith("storage.quarantine") || kind === "storage.apply.done") {
        void reloadQuarantine()
      }
      if (kind.startsWith("storage.suppress")) {
        void reloadSuppressed()
        void reload()
      }
    }
    return () => {
      es.close()
      esRef.current = null
    }
  }, [reload, reloadQuarantine, reloadSuppressed])

  const running = !!data?.running
  const groups = useMemo(() => data?.items || [], [data])

  const visibleGroups = useMemo(() => {
    return groups
      .filter((g) => !sessionSuppressed.has(g.digest))
      .filter((g) => {
        if (groupFilter === "all") return true
        const keeper = decisions[g.digest]?.keeper || pickKeeper(g, rule) || ""
        const state = groupReviewState(g, decisions[g.digest], keeper)
        if (groupFilter === "unreviewed") return state === "unreviewed"
        if (groupFilter === "partial") return state === "partial"
        return state === "full"
      })
  }, [groups, sessionSuppressed, groupFilter, decisions, rule])

  const focusOrder = useMemo(
    () => buildFocusOrder(visibleGroups, expanded, dupesOnly, decisions, rule),
    [visibleGroups, expanded, dupesOnly, decisions, rule],
  )

  const applyRuleToGroups = useCallback(
    (targets: DuplicateGroup[]) => {
      const next: Record<string, GroupDecision> = { ...decisions }
      for (const group of targets) {
        const keeper = pickKeeper(group, rule)
        if (!keeper) continue
        const actions: Record<string, "link" | "delete"> = {}
        for (const member of group.members) {
          if (member.path === keeper) continue
          if (member.protected) continue
          if (member.ino === group.members.find((m) => m.path === keeper)?.ino) continue
          actions[member.path] = sameVolumeAsKeeper(group, keeper, member) ? "link" : "delete"
        }
        if (Object.keys(actions).length) next[group.digest] = { keeper, actions }
        else delete next[group.digest]
      }
      setDecisions(next)
      const added = Object.entries(next).filter(([digest, d]) => {
        const prev = decisions[digest]
        return !prev || Object.keys(d.actions).length > Object.keys(prev.actions).length
      })
      const count = added.reduce((n, [, d]) => n + Object.keys(d.actions).length, 0)
      toast.success(
        `Selected ${count} redundant cop${count === 1 ? "y" : "ies"} across ${added.length} group(s)`,
      )
    },
    [decisions, rule],
  )

  const startScan = async () => {
    setLoading(true)
    try {
      await StorageService.scan()
      setDecisions({})
      toast.success("Duplicate scan started")
    } catch (err) {
      toast.error(getErrorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  const cancelScan = async () => {
    try {
      await StorageService.cancelScan()
      toast.info("Cancelling scan…")
    } catch (err) {
      toast.error(getErrorMessage(err))
    }
  }

  const toggleExpanded = (digest: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(digest)) next.delete(digest)
      else next.add(digest)
      return next
    })
  }

  /** Apply the active keeper rule to expanded groups only, selecting the losers. */
  const applyRuleToAll = () => {
    const targets = groups.filter((g) => expanded.has(g.digest))
    if (!targets.length) {
      toast.info("Expand groups first, or use Expand all + select", {
        action: {
          label: "Expand all + select",
          onClick: () => {
            const all = new Set(groups.map((g) => g.digest))
            setExpanded(all)
            applyRuleToGroups(groups)
          },
        },
      })
      return
    }
    applyRuleToGroups(targets)
  }

  const expandAll = () => setExpanded(new Set(visibleGroups.map((g) => g.digest)))
  const collapseAll = () => setExpanded(new Set())

  const suppressSession = (digest: string) => {
    setSessionSuppressed((prev) => new Set(prev).add(digest))
    setExpanded((prev) => {
      const next = new Set(prev)
      next.delete(digest)
      return next
    })
    toast.success("Group hidden for this session")
  }

  const suppressPermanent = async (digest: string) => {
    try {
      await StorageService.suppress(digest, true)
      setSessionSuppressed((prev) => {
        const next = new Set(prev)
        next.delete(digest)
        return next
      })
      await reload()
      await reloadSuppressed()
      toast.success("Group hidden on future scans")
    } catch (err) {
      toast.error(getErrorMessage(err))
    }
  }

  const revealPath = async (path: string) => {
    try {
      await StorageService.reveal(path)
      toast.success("Opened folder in file manager")
    } catch (err) {
      toast.error(getErrorMessage(err))
    }
  }

  const focusRowId = (row: FocusRow) =>
    row.kind === "header" ? `header:${row.digest}` : `member:${row.digest}:${row.member.path}`

  const moveFocus = (delta: number) => {
    if (!focusOrder.length) return
    const current = focusId ? focusOrder.findIndex((r) => focusRowId(r) === focusId) : -1
    const next = current < 0 ? (delta > 0 ? 0 : focusOrder.length - 1) : (current + delta + focusOrder.length) % focusOrder.length
    setFocusId(focusRowId(focusOrder[next]))
  }

  const clearSelection = () => setDecisions({})

  const setMemberAction = (group: DuplicateGroup, member: DuplicateMember, action: "link" | "delete" | null) => {
    setDecisions((prev) => {
      const current = prev[group.digest]
      const keeper = current?.keeper || pickKeeper(group, rule) || group.members[0].path
      const actions = { ...(current?.actions || {}) }
      if (action === null) delete actions[member.path]
      else actions[member.path] = action
      const next = { ...prev }
      if (Object.keys(actions).length === 0) delete next[group.digest]
      else next[group.digest] = { keeper, actions }
      return next
    })
  }

  const setKeeper = (group: DuplicateGroup, path: string) => {
    setDecisions((prev) => {
      const current = prev[group.digest]
      const actions = { ...(current?.actions || {}) }
      // The keeper can never also be a removal target.
      delete actions[path]
      return { ...prev, [group.digest]: { keeper: path, actions } }
    })
  }

  const outcomeFor = (group: DuplicateGroup, member: DuplicateMember): Outcome => {
    const decision = decisions[group.digest]
    if (!decision) return "keep"
    if (decision.keeper === member.path) return "keep"
    const action = decision.actions[member.path]
    if (action === "link") return "link"
    if (action === "delete") return "delete"
    return "keep"
  }

  const handleTableKeyDown = (ev: KeyboardEvent) => {
    if (!focusOrder.length) return
    const row = focusId ? focusOrder.find((r) => focusRowId(r) === focusId) : focusOrder[0]
    if (!row) return

    if (ev.key === "ArrowDown") {
      ev.preventDefault()
      moveFocus(1)
      return
    }
    if (ev.key === "ArrowUp") {
      ev.preventDefault()
      moveFocus(-1)
      return
    }
    if (ev.key === "Enter") {
      ev.preventDefault()
      if (row.kind === "header") {
        toggleExpanded(row.digest)
        return
      }
      void revealPath(row.member.path)
      return
    }
    if (ev.key === " " && row.kind === "member") {
      ev.preventDefault()
      const group = visibleGroups.find((g) => g.digest === row.digest)
      if (!group) return
      const keeper = decisions[group.digest]?.keeper || pickKeeper(group, rule) || ""
      const member = row.member
      const isKeeper = member.path === keeper
      const keeperMember = group.members.find((m) => m.path === keeper)
      const sharesInode = !!keeperMember && keeperMember.ino === member.ino
      const selectable = !member.protected && !isKeeper && !sharesInode
      if (!selectable) return
      const outcome = outcomeFor(group, member)
      const selected = outcome === "link" || outcome === "delete"
      const sameVolume = !!keeperMember && keeperMember.dev === member.dev
      setMemberAction(group, member, selected ? null : sameVolume ? "link" : "delete")
    }
  }

  const selection = useMemo(() => {
    let linked = 0
    let deleted = 0
    let bytesFreed = 0
    let bytesQuarantined = 0
    for (const group of groups) {
      const decision = decisions[group.digest]
      if (!decision) continue
      for (const [path, action] of Object.entries(decision.actions)) {
        const member = group.members.find((m) => m.path === path)
        if (!member) continue
        if (action === "link") {
          linked += 1
          bytesFreed += group.size
        } else {
          deleted += 1
          bytesQuarantined += group.size
        }
      }
    }
    return { linked, deleted, total: linked + deleted, bytesFreed, bytesQuarantined }
  }, [groups, decisions])

  const commit = async () => {
    setApplying(true)
    try {
      const items = Object.entries(decisions).map(([digest, decision]) => ({
        digest,
        keeper_path: decision.keeper,
        actions: Object.entries(decision.actions).map(([path, action]) => ({
          path,
          action: action as ReclaimAction,
        })),
      }))
      const result = await StorageService.apply(items)
      const quarantinedIds = result.outcomes.filter((o) => o.quarantine_id).map((o) => o.quarantine_id)
      toast.success(
        `${result.linked} linked, ${result.deleted} quarantined, ${result.skipped} skipped`,
        quarantinedIds.length
          ? {
              duration: 12000,
              action: {
                label: "Undo",
                onClick: () => {
                  void StorageService.restore(quarantinedIds)
                    .then((r) => toast.success(`Restored ${r.restored} file(s)`))
                    .catch((err) => toast.error(getErrorMessage(err)))
                    .finally(() => void reloadQuarantine())
                },
              },
            }
          : undefined,
      )
      setDecisions({})
      setConfirmOpen(false)
      await reload()
      await reloadQuarantine()
    } catch (err) {
      toast.error(getErrorMessage(err))
    } finally {
      setApplying(false)
    }
  }

  const purgeAll = async () => {
    if (!quarantine.length) return
    try {
      const res = await StorageService.purge(quarantine.map((q) => q.id))
      toast.success(`Purged ${res.purged} file(s), ${formatSize(res.bytes_freed)} freed`)
      await reloadQuarantine()
    } catch (err) {
      toast.error(getErrorMessage(err))
    }
  }

  const progress = data?.progress
  const hashPercent =
    progress && progress.candidates > 0
      ? Math.min(100, Math.round((progress.hashed / progress.candidates) * 100))
      : 0

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2 shrink-0">
        <h2 className="text-xs font-bold tracking-[0.15em] text-muted-foreground">STORAGE</h2>
        <Badge variant="outline" className="text-[10px]">
          {data?.groups || 0} group(s)
        </Badge>
        <Badge variant="outline" className="text-[10px]">
          {formatSize(data?.reclaimable_bytes || 0)} reclaimable
        </Badge>
        {running ? (
          <Button size="sm" variant="destructive" className="h-7 text-xs" onClick={cancelScan}>
            Cancel scan
          </Button>
        ) : (
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            onClick={startScan}
            disabled={loading}
          >
            Scan for duplicates
          </Button>
        )}
        <div className="ml-auto flex items-center gap-2">
          <label htmlFor="keeper-rule" className="text-[11px] text-muted-foreground">
            Keeper rule
          </label>
          <select
            id="keeper-rule"
            aria-label="Keeper rule"
            className="h-7 rounded-md border border-input bg-background px-2 text-xs"
            value={rule}
            onChange={(e) => setRule(e.target.value as KeeperRule)}
          >
            {KEEPER_RULES.map((r) => (
              <option key={r.value} value={r.value} title={r.tip}>
                {r.label}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            onClick={applyRuleToAll}
            disabled={!groups.length || running}
          >
            Select redundant copies
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            onClick={() => setShowQuarantine((v) => !v)}
          >
            Quarantine ({quarantine.length})
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            onClick={() => setShowSuppressed((v) => !v)}
          >
            Suppressed ({suppressed.length + sessionSuppressed.size})
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-1.5 shrink-0">
        <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <Checkbox checked={dupesOnly} onCheckedChange={(v) => setDupesOnly(v === true)} />
          Dupes only
        </label>
        <label htmlFor="group-filter" className="text-[11px] text-muted-foreground">
          Show
        </label>
        <select
          id="group-filter"
          aria-label="Group filter"
          className="h-7 rounded-md border border-input bg-background px-2 text-xs"
          value={groupFilter}
          onChange={(e) => setGroupFilter(e.target.value as GroupFilter)}
        >
          <option value="all">All groups</option>
          <option value="unreviewed">Unreviewed</option>
          <option value="partial">Partially reviewed</option>
          <option value="full">Fully selected</option>
        </select>
        <Button size="sm" variant="outline" className="h-7 text-xs" onClick={expandAll} disabled={!visibleGroups.length}>
          Expand all
        </Button>
        <Button size="sm" variant="outline" className="h-7 text-xs" onClick={collapseAll} disabled={!expanded.size}>
          Collapse all
        </Button>
        <span className="text-[10px] text-muted-foreground ml-auto">
          ↑↓ navigate · Space select · Enter expand/reveal
        </span>
      </div>

      {running && (
        <div className="border-b border-border px-3 py-2 shrink-0 space-y-1">
          <Progress value={hashPercent} className="h-1.5" />
          <p className="text-[11px] text-muted-foreground font-mono">
            {progress?.stage} · {progress?.files_seen || 0} seen · {progress?.hashed || 0}/
            {progress?.candidates || 0} hashed · {progress?.groups_found || 0} groups ·{" "}
            {Math.round(progress?.elapsed || 0)}s
          </p>
        </div>
      )}

      {showQuarantine && (
        <div className="border-b border-border px-3 py-2 shrink-0 max-h-40 overflow-auto">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="text-[11px] font-semibold">Quarantine</h3>
            <span className="text-[11px] text-muted-foreground">
              {formatSize(quarantine.reduce((n, q) => n + q.size, 0))} recoverable
            </span>
            <div className="ml-auto flex gap-2">
              <Button
                size="sm"
                variant="outline"
                className="h-6 text-[11px]"
                disabled={!quarantine.length}
                onClick={() => {
                  void StorageService.restore(quarantine.map((q) => q.id))
                    .then((r) => toast.success(`Restored ${r.restored} file(s)`))
                    .catch((err) => toast.error(getErrorMessage(err)))
                    .finally(() => void reloadQuarantine())
                }}
              >
                Restore all
              </Button>
              <Button
                size="sm"
                variant="destructive"
                className="h-6 text-[11px]"
                disabled={!quarantine.length}
                onClick={purgeAll}
              >
                Purge all permanently
              </Button>
            </div>
          </div>
          {quarantine.length === 0 ? (
            <p className="text-[11px] text-muted-foreground">
              Nothing quarantined. Deleted duplicates land here first and stay recoverable until purged.
            </p>
          ) : (
            <table className="w-full text-[11px] font-mono">
              <thead className="text-muted-foreground">
                <tr>
                  <th scope="col" className="text-left font-normal">
                    Original path
                  </th>
                  <th scope="col" className="text-right font-normal">
                    Size
                  </th>
                </tr>
              </thead>
              <tbody>
                {quarantine.map((q) => (
                  <tr key={q.id}>
                    <th scope="row" className="text-left font-normal truncate max-w-0 w-full">
                      {q.original}
                    </th>
                    <td className="text-right tabular-nums">{formatSize(q.size)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {showSuppressed && (
        <div className="border-b border-border px-3 py-2 shrink-0 max-h-40 overflow-auto">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="text-[11px] font-semibold">Suppressed groups</h3>
            <span className="text-[11px] text-muted-foreground">
              {sessionSuppressed.size} this session · {suppressed.length} permanent
            </span>
            <div className="ml-auto flex gap-2">
              <Button
                size="sm"
                variant="outline"
                className="h-6 text-[11px]"
                disabled={!sessionSuppressed.size}
                onClick={() => {
                  setSessionSuppressed(new Set())
                  toast.success("Session suppressions cleared")
                }}
              >
                Clear session
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-6 text-[11px]"
                disabled={!suppressed.length}
                onClick={() => {
                  void StorageService.restoreSuppressed(suppressed.map((s) => s.id))
                    .then((r) => toast.success(`Restored ${r.restored} group(s)`))
                    .catch((err) => toast.error(getErrorMessage(err)))
                    .finally(() => {
                      void reloadSuppressed()
                      void reload()
                    })
                }}
              >
                Restore all permanent
              </Button>
            </div>
          </div>
          {sessionSuppressed.size === 0 && suppressed.length === 0 ? (
            <p className="text-[11px] text-muted-foreground">
              Hide false positives per group. Session hides clear on reload; permanent hides survive scans.
            </p>
          ) : (
            <table className="w-full text-[11px] font-mono">
              <thead className="text-muted-foreground">
                <tr>
                  <th scope="col" className="text-left font-normal">
                    Digest
                  </th>
                  <th scope="col" className="text-right font-normal">
                    Scope
                  </th>
                </tr>
              </thead>
              <tbody>
                {[...sessionSuppressed].map((digest) => (
                  <tr key={`session-${digest}`}>
                    <th scope="row" className="text-left font-normal truncate max-w-0 w-full">
                      {digest.slice(0, 16)}…
                    </th>
                    <td className="text-right">session</td>
                  </tr>
                ))}
                {suppressed.map((s) => (
                  <tr key={s.id}>
                    <th scope="row" className="text-left font-normal truncate max-w-0 w-full">
                      {s.digest.slice(0, 16)}…
                    </th>
                    <td className="text-right">
                      <button
                        type="button"
                        className="text-[10px] underline-offset-2 hover:underline"
                        onClick={() => {
                          void StorageService.restoreSuppressed([s.id])
                            .then((r) => toast.success(`Restored ${r.restored} group(s)`))
                            .catch((err) => toast.error(getErrorMessage(err)))
                            .finally(() => {
                              void reloadSuppressed()
                              void reload()
                            })
                        }}
                      >
                        restore
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <div
        ref={tableRef}
        tabIndex={0}
        className="flex-1 min-h-0 overflow-auto outline-none focus-visible:ring-1 focus-visible:ring-ring"
        onKeyDown={handleTableKeyDown}
        onFocus={() => {
          if (!focusId && focusOrder.length) setFocusId(focusRowId(focusOrder[0]))
        }}
      >
        {!data?.roots?.length ? (
          <EmptyState
            title="No storage roots configured"
            body="Add folders under Settings → Matcher (or content_dupes.roots) and qbx will find byte-identical copies you can safely hardlink or reclaim."
          />
        ) : groups.length === 0 ? (
          <EmptyState
            title={running ? "Scanning…" : "No duplicate content found"}
            body={
              running
                ? "Hashing size collisions. Groups appear as soon as the pass completes."
                : "Run a scan to look for byte-identical copies across your configured roots."
            }
          />
        ) : visibleGroups.length === 0 ? (
          <EmptyState
            title="No groups match the current filters"
            body="Try a different group filter or restore suppressed groups."
          />
        ) : (
          <table className="w-full text-xs">
            <caption className="sr-only">Exact-content duplicate groups</caption>
            <thead className="sticky top-0 bg-card/95 text-muted-foreground">
              <tr>
                <th scope="col" className="w-8" />
                <th scope="col" className="text-left font-normal px-2 py-1">
                  Group
                </th>
                <th scope="col" className="text-right font-normal px-2 py-1">
                  Copies
                </th>
                <th scope="col" className="text-right font-normal px-2 py-1">
                  Size
                </th>
                <th scope="col" className="text-right font-normal px-2 py-1">
                  Reclaimable
                </th>
                <th scope="col" className="w-24" />
              </tr>
            </thead>
            <tbody>
              {visibleGroups.map((group) => {
                const open = expanded.has(group.digest)
                const decision = decisions[group.digest]
                const keeper = decision?.keeper || pickKeeper(group, rule) || ""
                const marked = decision ? Object.keys(decision.actions).length : 0
                const headerFocused = focusId === `header:${group.digest}`
                return (
                  <GroupRows
                    key={group.digest}
                    group={group}
                    open={open}
                    keeper={keeper}
                    marked={marked}
                    dupesOnly={dupesOnly}
                    headerFocused={headerFocused}
                    focusId={focusId}
                    onToggle={() => toggleExpanded(group.digest)}
                    onSetKeeper={(path) => setKeeper(group, path)}
                    onSetAction={(member, action) => setMemberAction(group, member, action)}
                    outcomeFor={(member) => outcomeFor(group, member)}
                    onSuppressSession={() => suppressSession(group.digest)}
                    onSuppressPermanent={() => void suppressPermanent(group.digest)}
                    onReveal={(path) => void revealPath(path)}
                    onFocusHeader={() => setFocusId(`header:${group.digest}`)}
                    onFocusMember={(member) => setFocusId(`member:${group.digest}:${member.path}`)}
                  />
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="border-t border-border px-3 py-2 shrink-0 flex items-center gap-2">
        <span className="text-[11px] text-muted-foreground">
          {selection.total === 0
            ? "Nothing selected — selection never acts on its own."
            : `${selection.linked} to hardlink, ${selection.deleted} to quarantine`}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            onClick={clearSelection}
            disabled={selection.total === 0}
          >
            Clear selection
          </Button>
          <Button
            size="sm"
            variant="destructive"
            className="h-7 text-xs"
            onClick={() => setConfirmOpen(true)}
            disabled={selection.total === 0 || running}
          >
            Reclaim {selection.total > 0 ? `${selection.total} cop${selection.total === 1 ? "y" : "ies"}` : ""}
          </Button>
        </div>
      </div>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Reclaim space from duplicate copies?</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2 text-left">
                <p>
                  {selection.linked} cop{selection.linked === 1 ? "y" : "ies"} will be replaced with a
                  hardlink to the keeper, freeing {formatSize(selection.bytesFreed)} immediately.
                </p>
                <p>
                  {selection.deleted} cop{selection.deleted === 1 ? "y" : "ies"} will move to quarantine
                  ({formatSize(selection.bytesQuarantined)}), recoverable until you purge.
                </p>
                <p className="text-amber-400">
                  Every group keeps at least one copy. Protected roots are never touched.
                </p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={applying}>Keep all</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                void commit()
              }}
              disabled={applying}
            >
              {applying
                ? "Working…"
                : `Reclaim ${selection.total} cop${selection.total === 1 ? "y" : "ies"}`}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

function pickKeeper(group: DuplicateGroup, rule: KeeperRule): string {
  if (!group.members.length) return ""
  const protectedMembers = group.members.filter((m) => m.protected)
  const pool = protectedMembers.length ? protectedMembers : group.members
  if (rule === "under_root" && protectedMembers.length) {
    return [...protectedMembers].sort((a, b) => a.path.length - b.path.length)[0].path
  }
  if (rule === "oldest") return [...pool].sort((a, b) => a.mtime - b.mtime)[0].path
  if (rule === "shortest_path") return [...pool].sort((a, b) => a.path.length - b.path.length)[0].path
  return [...pool].sort((a, b) => b.mtime - a.mtime)[0].path
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="h-full flex flex-col items-center justify-center gap-2 p-6 text-center">
      <p className="text-sm font-medium">{title}</p>
      <p className="text-xs text-muted-foreground max-w-md">{body}</p>
    </div>
  )
}

function GroupRows({
  group,
  open,
  keeper,
  marked,
  dupesOnly,
  headerFocused,
  focusId,
  onToggle,
  onSetKeeper,
  onSetAction,
  outcomeFor,
  onSuppressSession,
  onSuppressPermanent,
  onReveal,
  onFocusHeader,
  onFocusMember,
}: {
  group: DuplicateGroup
  open: boolean
  keeper: string
  marked: number
  dupesOnly: boolean
  headerFocused: boolean
  focusId: string | null
  onToggle: () => void
  onSetKeeper: (path: string) => void
  onSetAction: (member: DuplicateMember, action: "link" | "delete" | null) => void
  outcomeFor: (member: DuplicateMember) => Outcome
  onSuppressSession: () => void
  onSuppressPermanent: () => void
  onReveal: (path: string) => void
  onFocusHeader: () => void
  onFocusMember: (member: DuplicateMember) => void
}) {
  const label = group.members[0]?.path.split("/").pop() || group.digest.slice(0, 12)
  const keeperPath = keeper || group.suggested_keeper || ""
  return (
    <>
      <tr
        className={cn(
          "border-b border-border/40 hover:bg-accent/30",
          headerFocused && "ring-1 ring-inset ring-ring bg-accent/20",
        )}
        onClick={onFocusHeader}
      >
        <td className="px-1 py-1">
          <button
            type="button"
            aria-expanded={open}
            aria-label={open ? `Collapse group ${label}` : `Expand group ${label}`}
            className="h-6 w-6 text-muted-foreground hover:text-foreground"
            onClick={(e) => {
              e.stopPropagation()
              onToggle()
            }}
          >
            {open ? "▾" : "▸"}
          </button>
        </td>
        <th scope="row" className="text-left font-normal px-2 py-1">
          <span className="font-mono truncate">{label}</span>
          {keeperPath && (
            <span className="block text-[10px] text-muted-foreground truncate" title={keeperPath}>
              keeper: {keeperPath}
            </span>
          )}
          {group.has_existing_hardlinks && (
            <Badge variant="secondary" className="ml-2 text-[10px]">
              has hardlinks
            </Badge>
          )}
          {marked > 0 && (
            <Badge variant="outline" className="ml-2 text-[10px]">
              {marked} selected
            </Badge>
          )}
        </th>
        <td className="text-right px-2 py-1 tabular-nums">
          {group.members.length}
          <span className="text-muted-foreground"> / {group.distinct_inodes} inode(s)</span>
        </td>
        <td className="text-right px-2 py-1 tabular-nums">{formatSize(group.size)}</td>
        <td className="text-right px-2 py-1 tabular-nums">{formatSize(group.reclaimable_bytes)}</td>
        <td className="px-2 py-1 text-right">
          <div className="flex justify-end gap-1">
            <button
              type="button"
              className="text-[10px] text-muted-foreground hover:underline"
              onClick={(e) => {
                e.stopPropagation()
                onSuppressSession()
              }}
            >
              hide
            </button>
            <button
              type="button"
              className="text-[10px] text-muted-foreground hover:underline"
              onClick={(e) => {
                e.stopPropagation()
                onSuppressPermanent()
              }}
            >
              hide always
            </button>
          </div>
        </td>
      </tr>
      {open &&
        group.members.map((member) => {
          if (dupesOnly && member.path === keeper) return null
          const outcome = outcomeFor(member)
          const isKeeper = member.path === keeper
          const keeperMember = group.members.find((m) => m.path === keeper)
          const sameVolume = !!keeperMember && keeperMember.dev === member.dev
          const sharesInode = !!keeperMember && keeperMember.ino === member.ino
          const selectable = !member.protected && !isKeeper && !sharesInode
          const memberFocused = focusId === `member:${group.digest}:${member.path}`
          return (
            <tr
              key={member.path}
              className={cn(
                "border-b border-border/20 bg-background/40",
                memberFocused && "ring-1 ring-inset ring-ring bg-accent/10",
              )}
              onClick={() => onFocusMember(member)}
              onDoubleClick={() => onReveal(member.path)}
            >
              <td className="px-1 py-1 align-top">
                <Checkbox
                  aria-label={`Select ${member.path} for reclaim`}
                  checked={outcome === "link" || outcome === "delete"}
                  disabled={!selectable}
                  onCheckedChange={(checked) =>
                    onSetAction(member, checked ? (sameVolume ? "link" : "delete") : null)
                  }
                />
              </td>
              <th scope="row" className="text-left font-normal px-2 py-1 max-w-0 w-full">
                <span className="font-mono text-[11px] truncate block" title={member.path}>
                  {member.path}
                </span>
                <span className="flex flex-wrap gap-1 mt-0.5">
                  {member.protected && (
                    <Badge variant="secondary" className="text-[10px]">
                      protected
                    </Badge>
                  )}
                  {member.nlink > 1 && (
                    <Badge variant="outline" className="text-[10px]">
                      {member.nlink} links
                    </Badge>
                  )}
                  {sharesInode && !isKeeper && (
                    <Badge variant="outline" className="text-[10px]">
                      same inode as keeper
                    </Badge>
                  )}
                  {!sameVolume && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Badge variant="outline" className="text-[10px] text-amber-400">
                          other volume
                        </Badge>
                      </TooltipTrigger>
                      <TooltipContent>
                        Hardlinks cannot cross filesystems, so this copy can only be quarantined.
                      </TooltipContent>
                    </Tooltip>
                  )}
                </span>
              </th>
              <td className="px-2 py-1 text-right align-top">
                <button
                  type="button"
                  className={cn(
                    "text-[10px] underline-offset-2",
                    isKeeper ? "text-emerald-400" : "text-muted-foreground hover:underline",
                  )}
                  disabled={isKeeper}
                  onClick={() => onSetKeeper(member.path)}
                >
                  {isKeeper ? "keeper" : "make keeper"}
                </button>
              </td>
              <td className="px-2 py-1 text-right align-top tabular-nums text-muted-foreground">
                {new Date(member.mtime * 1000).toLocaleDateString()}
              </td>
              <td
                className={cn("px-2 py-1 text-right align-top font-semibold text-[10px]", OUTCOME_STYLES[outcome])}
              >
                {OUTCOME_LABELS[outcome]}
              </td>
              <td />
            </tr>
          )
        })}
    </>
  )
}
