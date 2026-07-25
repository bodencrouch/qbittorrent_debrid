import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ControlApi, type UpdateCheckResult } from "@/api/backend"
import { cn, getErrorMessage } from "@/lib/utils"
import { uiLog } from "@/lib/ui-log"
import { toast } from "sonner"

const REDACTED = "********"

export type SettingsSection =
  | "connection"
  | "providers"
  | "anonymity"
  | "interceptor"
  | "matcher"
  | "application"

type ProviderName = "realdebrid" | "alldebrid"

type ProviderForm = {
  name: ProviderName
  enabled: boolean
  priority: number
  api_key: string
  has_secret: boolean
}

type ApplyStatus = "idle" | "applying" | "applied" | "error"

type SettingsForm = {
  // Save: Connection
  qbt_url: string
  qbt_username: string
  qbt_password: string
  qbt_has_secret: boolean
  qbt_verify_tls: boolean
  api_token: string
  api_token_has_secret: boolean
  // Save: Anonymity
  proxy_enabled: boolean
  proxy_url: string
  proxy_has_secret: boolean
  use_proxy_for_debrid: boolean
  use_proxy_for_downloads: boolean
  random_user_agent: boolean
  strip_trackers: boolean
  // Save: Providers
  providers: ProviderForm[]
  // Immediate: Interceptor
  interceptor_enabled: boolean
  delivery_mode: "webseed" | "download"
  stalled_only: boolean
  stalled_min_minutes: number
  min_stalled_seeds: number
  max_stalled_download_speed: number
  max_debrid_per_scan: number
  skip_private: boolean
  require_magnet: boolean
  metadata_handoff: boolean
  manage_without_debrid: boolean
  // Immediate: Matcher
  matcher_enabled: boolean
  matcher_auto_placement: boolean
  matcher_folders: string
  matcher_interval_minutes: number
  matcher_recheck: boolean
  // Immediate: Application
  update_channel: "stable" | "beta"
  update_source_owner: string
  update_source_repo: string
  update_check_on_startup: boolean
  desktop_notifications: boolean
  tray_autostart: boolean
}

const SECTIONS: { id: SettingsSection; label: string; contract: "save" | "immediate" }[] = [
  { id: "connection", label: "Connection", contract: "save" },
  { id: "providers", label: "Providers", contract: "save" },
  { id: "anonymity", label: "Anonymity", contract: "save" },
  { id: "interceptor", label: "Interceptor", contract: "immediate" },
  { id: "matcher", label: "Matcher", contract: "immediate" },
  { id: "application", label: "Application", contract: "immediate" },
]

function emptyProviders(): ProviderForm[] {
  return [
    { name: "alldebrid", enabled: false, priority: 0, api_key: "", has_secret: false },
    { name: "realdebrid", enabled: false, priority: 1, api_key: "", has_secret: false },
  ]
}

function fromConfig(cfg: Record<string, unknown>): SettingsForm {
  const qbt = (cfg.qbt || {}) as Record<string, unknown>
  const server = (cfg.server || {}) as Record<string, unknown>
  const anonymity = (cfg.anonymity || {}) as Record<string, unknown>
  const interceptor = (cfg.interceptor || {}) as Record<string, unknown>
  const matcher = (cfg.matcher || {}) as Record<string, unknown>
  const providersRaw = Array.isArray(cfg.providers) ? (cfg.providers as Record<string, unknown>[]) : []
  const providers = emptyProviders().map((base) => {
    const found = providersRaw.find((p) => p.name === base.name)
    if (!found) return base
    const key = String(found.api_key || "")
    return {
      name: base.name,
      enabled: Boolean(found.enabled),
      priority: Number(found.priority ?? base.priority),
      api_key: key === REDACTED ? "" : key,
      has_secret: key === REDACTED || Boolean(key),
    }
  })
  const updates = (cfg.updates || {}) as Record<string, unknown>
  const desktop = (cfg.desktop || {}) as Record<string, unknown>
  const pw = String(qbt.password || "")
  const token = String(server.api_token || "")
  const proxy = String(anonymity.proxy_url || "")
  const folders = Array.isArray(matcher.folders) ? (matcher.folders as string[]) : []
  return {
    qbt_url: String(qbt.url || ""),
    qbt_username: String(qbt.username || ""),
    qbt_password: pw === REDACTED ? "" : pw,
    qbt_has_secret: pw === REDACTED || Boolean(pw),
    qbt_verify_tls: qbt.verify_tls !== false,
    api_token: token === REDACTED ? "" : token,
    api_token_has_secret: token === REDACTED || Boolean(token),
    proxy_enabled: anonymity.enabled !== false,
    proxy_url: proxy === REDACTED ? "" : proxy,
    proxy_has_secret: proxy === REDACTED,
    use_proxy_for_debrid: anonymity.use_proxy_for_debrid !== false,
    use_proxy_for_downloads: anonymity.use_proxy_for_downloads !== false,
    random_user_agent: anonymity.random_user_agent !== false,
    strip_trackers: anonymity.strip_trackers !== false,
    providers,
    interceptor_enabled: interceptor.enabled !== false,
    delivery_mode: interceptor.delivery_mode === "download" ? "download" : "webseed",
    stalled_only: interceptor.stalled_only !== false,
    stalled_min_minutes: Number(interceptor.stalled_min_minutes ?? 30),
    min_stalled_seeds: Number(interceptor.min_stalled_seeds ?? 1),
    max_stalled_download_speed: Number(interceptor.max_stalled_download_speed ?? 1024),
    max_debrid_per_scan: Number(interceptor.max_debrid_per_scan ?? 1),
    skip_private: interceptor.skip_private !== false,
    require_magnet: interceptor.require_magnet !== false,
    metadata_handoff: interceptor.metadata_handoff !== false,
    manage_without_debrid: interceptor.manage_without_debrid !== false,
    matcher_enabled: Boolean(matcher.enabled),
    matcher_auto_placement: Boolean(matcher.auto_placement),
    matcher_folders: folders.join(", "),
    matcher_interval_minutes: Number(matcher.interval_minutes ?? 60),
    matcher_recheck: matcher.recheck !== false,
    update_channel: updates.channel === "beta" ? "beta" : "stable",
    update_source_owner: String(updates.source_owner || "bodencrouch"),
    update_source_repo: String(updates.source_repo || "qbittorrent_debrid"),
    update_check_on_startup: updates.check_on_startup !== false,
    desktop_notifications: desktop.notifications !== false,
    tray_autostart: Boolean(desktop.tray_autostart),
  }
}

function saveSlice(form: SettingsForm) {
  return {
    qbt_url: form.qbt_url,
    qbt_username: form.qbt_username,
    qbt_password: form.qbt_password,
    qbt_verify_tls: form.qbt_verify_tls,
    api_token: form.api_token,
    proxy_enabled: form.proxy_enabled,
    proxy_url: form.proxy_url,
    use_proxy_for_debrid: form.use_proxy_for_debrid,
    use_proxy_for_downloads: form.use_proxy_for_downloads,
    random_user_agent: form.random_user_agent,
    strip_trackers: form.strip_trackers,
    providers: form.providers.map((p) => ({
      name: p.name,
      enabled: p.enabled,
      priority: p.priority,
      api_key: p.api_key,
    })),
  }
}

interface SettingsPanelProps {
  open: boolean
  onClose: () => void
  onSaved?: () => void
  initialSection?: SettingsSection
}

export function SettingsPanel({ open, onClose, onSaved, initialSection }: SettingsPanelProps) {
  const [section, setSection] = useState<SettingsSection>("connection")
  const [form, setForm] = useState<SettingsForm | null>(null)
  const [baseline, setBaseline] = useState<SettingsForm | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [version, setVersion] = useState("")
  const [updateBusy, setUpdateBusy] = useState(false)
  const [updateResult, setUpdateResult] = useState<UpdateCheckResult | null>(null)
  const [rowStatus, setRowStatus] = useState<Record<string, ApplyStatus>>({})
  const softTimers = useRef<Record<string, number>>({})

  useEffect(() => {
    if (!open) return
    if (initialSection) setSection(initialSection)
  }, [open, initialSection])

  useEffect(() => {
    if (!open) return
    let cancelled = false
    let attempt = 0
    setError("")
    setForm(null)
    setBaseline(null)
    setRowStatus({})

    const load = () => {
      attempt += 1
      ControlApi.getConfig()
        .then((cfg) => {
          if (!cancelled) {
            const next = fromConfig(cfg)
            setForm(next)
            setBaseline(next)
            setError("")
          }
        })
        .catch((err) => {
          if (cancelled) return
          const msg = getErrorMessage(err)
          if (attempt < 2 && /busy|offline|Failed to fetch|Cannot reach/i.test(msg)) {
            window.setTimeout(() => {
              if (!cancelled) load()
            }, 1500)
            setError("Server busy — retrying…")
            return
          }
          setError(msg)
        })
    }
    load()
    ControlApi.version()
      .then((v) => {
        if (!cancelled) setVersion(v.version)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
      for (const id of Object.values(softTimers.current)) window.clearTimeout(id)
      softTimers.current = {}
    }
  }, [open])

  const dirtySave = useMemo(() => {
    if (!form || !baseline) return false
    return JSON.stringify(saveSlice(form)) !== JSON.stringify(saveSlice(baseline))
  }, [form, baseline])

  const ordered = useMemo(() => {
    if (!form) return []
    return [...form.providers].sort((a, b) => a.priority - b.priority)
  }, [form])

  const requestClose = useCallback(() => {
    if (dirtySave) {
      const ok = window.confirm("Discard unsaved Connection / Providers / Anonymity changes?")
      if (!ok) return
    }
    onClose()
  }, [dirtySave, onClose])

  const setStatus = (key: string, status: ApplyStatus) => {
    setRowStatus((prev) => ({ ...prev, [key]: status }))
    if (status === "applied") {
      window.setTimeout(() => {
        setRowStatus((prev) => (prev[key] === "applied" ? { ...prev, [key]: "idle" } : prev))
      }, 1600)
    }
  }

  const applySoft = useCallback(
    (key: string, patch: Record<string, unknown>, revert: () => void) => {
      if (softTimers.current[key]) window.clearTimeout(softTimers.current[key])
      setStatus(key, "applying")
      softTimers.current[key] = window.setTimeout(() => {
        void (async () => {
          try {
            await ControlApi.updateConfig(patch)
            setStatus(key, "applied")
            setBaseline((prev) => (prev && form ? { ...prev, ...pickSoftBaseline(form, patch) } : prev))
            onSaved?.()
          } catch (err) {
            revert()
            setStatus(key, "error")
            toast.error(getErrorMessage(err))
            window.setTimeout(() => setStatus(key, "idle"), 2000)
          }
        })()
      }, 400)
    },
    [form, onSaved],
  )

  const discardSave = () => {
    if (!baseline) return
    setForm((prev) => {
      if (!prev) return prev
      return { ...prev, ...saveSlice(baseline), providers: baseline.providers.map((p) => ({ ...p })) }
    })
  }

  const saveHard = async () => {
    if (!form) return
    setBusy(true)
    setError("")
    uiLog("ui.settings.save", "Saving connection / providers / anonymity")
    try {
      const providers = form.providers.map((p) => ({
        name: p.name,
        enabled: p.enabled,
        priority: p.priority,
        api_key: p.api_key.trim() ? p.api_key.trim() : p.has_secret ? REDACTED : "",
      }))
      await ControlApi.updateConfig({
        configured: true,
        qbt: {
          url: form.qbt_url.trim(),
          username: form.qbt_username.trim(),
          password: form.qbt_password.trim()
            ? form.qbt_password.trim()
            : form.qbt_has_secret
              ? REDACTED
              : "",
          verify_tls: form.qbt_verify_tls,
        },
        server: {
          api_token: form.api_token.trim()
            ? form.api_token.trim()
            : form.api_token_has_secret
              ? REDACTED
              : "",
        },
        anonymity: {
          enabled: form.proxy_enabled,
          proxy_url: form.proxy_url.trim()
            ? form.proxy_url.trim()
            : form.proxy_has_secret
              ? REDACTED
              : "",
          use_proxy_for_debrid: form.use_proxy_for_debrid,
          use_proxy_for_downloads: form.use_proxy_for_downloads,
          random_user_agent: form.random_user_agent,
          strip_trackers: form.strip_trackers,
        },
        providers,
      })
      if (form.api_token.trim()) {
        localStorage.setItem("qbx_token", form.api_token.trim())
      } else if (!form.api_token_has_secret) {
        localStorage.removeItem("qbx_token")
      }
      const refreshed = fromConfig(await ControlApi.getConfig())
      setForm((prev) => (prev ? { ...prev, ...saveSlice(refreshed), providers: refreshed.providers } : refreshed))
      setBaseline((prev) =>
        prev ? { ...prev, ...saveSlice(refreshed), providers: refreshed.providers } : refreshed,
      )
      uiLog("ui.settings.save.ok", "Settings saved")
      toast.success("Saved connection settings")
      onSaved?.()
    } catch (err) {
      const msg = getErrorMessage(err)
      setError(msg)
      uiLog("ui.settings.save.failed", msg)
      toast.error(msg)
    } finally {
      setBusy(false)
    }
  }

  const toggleTrayAutostart = async (next: boolean) => {
    if (!form) return
    const prev = form.tray_autostart
    setForm({ ...form, tray_autostart: next })
    setStatus("tray", "applying")
    try {
      const res = await ControlApi.setTrayAutostart(next)
      if (!res.ok) {
        setForm((f) => (f ? { ...f, tray_autostart: prev } : f))
        setStatus("tray", "error")
        toast.error(res.sync?.reason || "Could not update tray autostart")
      } else {
        setBaseline((b) => (b ? { ...b, tray_autostart: next } : b))
        setStatus("tray", "applied")
      }
    } catch (err) {
      setForm((f) => (f ? { ...f, tray_autostart: prev } : f))
      setStatus("tray", "error")
      toast.error(getErrorMessage(err))
    }
  }

  const checkUpdates = async () => {
    setUpdateBusy(true)
    setUpdateResult(null)
    try {
      const res = await ControlApi.updateCheck()
      setUpdateResult(res)
      if (!res.ok) toast.error(res.error || "Update check failed")
      else if (res.update_available) toast.info(`qbx ${res.latest} is available`)
      else if (res.error) toast.message(res.error)
      else toast.success("qbx is up to date")
    } catch (err) {
      toast.error(getErrorMessage(err))
    } finally {
      setUpdateBusy(false)
    }
  }

  const setProvider = (name: ProviderName, patch: Partial<ProviderForm>) => {
    setForm((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        providers: prev.providers.map((p) => (p.name === name ? { ...p, ...patch } : p)),
      }
    })
  }

  const StatusHint = ({ id }: { id: string }) => {
    const s = rowStatus[id] || "idle"
    if (s === "idle") return null
    return (
      <span
        className={cn(
          "text-[10px] ml-2 animate-in fade-in",
          s === "applying" && "text-muted-foreground",
          s === "applied" && "text-emerald-400",
          s === "error" && "text-red-400",
        )}
      >
        {s === "applying" ? "Applying…" : s === "applied" ? "Applied" : "Error"}
      </span>
    )
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) requestClose()
      }}
    >
      <DialogContent
        className="max-w-4xl w-[min(96vw,56rem)] h-[min(90vh,40rem)] p-0 gap-0 overflow-hidden flex flex-col"
        onEscapeKeyDown={(e) => {
          if (dirtySave) {
            e.preventDefault()
            requestClose()
          }
        }}
        onInteractOutside={(e) => {
          if (dirtySave) {
            e.preventDefault()
            requestClose()
          }
        }}
      >
        <DialogHeader className="px-4 pt-4 pb-2 border-b border-border shrink-0 space-y-1">
          <DialogTitle className="text-sm font-semibold tracking-wide">Settings</DialogTitle>
          <DialogDescription className="text-[11px]">
            Connection, providers, and anonymity require Save. Interceptor, matcher, and application
            prefs apply as you change them. Tray autostart uses its own OS sync.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-1 min-h-0">
          <nav className="w-40 shrink-0 border-r border-border bg-card/30 p-2 space-y-0.5 overflow-auto">
            {SECTIONS.map((s) => (
              <button
                key={s.id}
                type="button"
                className={cn(
                  "w-full text-left rounded-md px-2 py-1.5 text-xs transition-colors",
                  section === s.id ? "bg-accent text-accent-foreground" : "hover:bg-muted/60 text-muted-foreground",
                )}
                onClick={() => setSection(s.id)}
              >
                <div className="font-medium text-foreground/90">{s.label}</div>
                <div className="text-[10px] opacity-70">
                  {s.contract === "save" ? "Save to apply" : "Applies immediately"}
                </div>
              </button>
            ))}
          </nav>

          <div className="flex-1 min-w-0 flex flex-col">
            <div className="flex-1 overflow-auto p-4 text-sm space-y-4">
              {error && <p className="text-xs text-red-400">{error}</p>}
              {!form && !error && <p className="text-xs text-muted-foreground">Loading config…</p>}

              {form && section === "connection" && (
                <SectionBlock title="Connection" contract="Save">
                  <Field label="qBittorrent WebUI URL">
                    <Input
                      className="h-8 text-xs font-mono"
                      value={form.qbt_url}
                      onChange={(e) => setForm({ ...form, qbt_url: e.target.value })}
                      placeholder="http://127.0.0.1:8080"
                    />
                  </Field>
                  <div className="grid grid-cols-2 gap-2">
                    <Field label="Username">
                      <Input
                        className="h-8 text-xs"
                        value={form.qbt_username}
                        onChange={(e) => setForm({ ...form, qbt_username: e.target.value })}
                      />
                    </Field>
                    <Field label={`Password${form.qbt_has_secret ? " (saved)" : ""}`}>
                      <Input
                        className="h-8 text-xs"
                        type="password"
                        value={form.qbt_password}
                        onChange={(e) => setForm({ ...form, qbt_password: e.target.value })}
                        placeholder={form.qbt_has_secret ? "unchanged" : ""}
                      />
                    </Field>
                  </div>
                  <label className="flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      className="accent-sky-500"
                      checked={form.qbt_verify_tls}
                      onChange={(e) => setForm({ ...form, qbt_verify_tls: e.target.checked })}
                    />
                    Verify TLS for qBittorrent
                  </label>
                  <Field label={`API token${form.api_token_has_secret ? " (saved)" : ""}`}>
                    <Input
                      className="h-8 text-xs font-mono"
                      type="password"
                      value={form.api_token}
                      onChange={(e) =>
                        setForm({ ...form, api_token: e.target.value, api_token_has_secret: false })
                      }
                      placeholder={form.api_token_has_secret ? "unchanged" : "empty = no auth on loopback"}
                    />
                  </Field>
                </SectionBlock>
              )}

              {form && section === "providers" && (
                <SectionBlock title="Providers" contract="Save">
                  <div className="flex items-center gap-2 mb-2">
                    <Badge variant="outline" className="text-[10px]">
                      try order: {ordered.map((p) => p.name.replace("debrid", "")).join(" → ") || "—"}
                    </Badge>
                  </div>
                  <div className="space-y-3">
                    {form.providers.map((p) => (
                      <div key={p.name} className="border-b border-border/60 pb-3 space-y-2 last:border-0">
                        <div className="flex items-center justify-between gap-2">
                          <div className="font-medium text-xs">
                            {p.name === "alldebrid" ? "AllDebrid" : "Real-Debrid"}
                          </div>
                          <label className="flex items-center gap-1.5 text-[11px]">
                            <input
                              type="checkbox"
                              className="accent-sky-500"
                              checked={p.enabled}
                              onChange={(e) => setProvider(p.name, { enabled: e.target.checked })}
                            />
                            Enabled
                          </label>
                        </div>
                        <Field label={`API key${p.has_secret ? " (saved)" : ""}`}>
                          <Input
                            className="h-8 text-xs font-mono"
                            type="password"
                            value={p.api_key}
                            disabled={!p.enabled}
                            onChange={(e) => setProvider(p.name, { api_key: e.target.value })}
                            placeholder={p.has_secret ? "unchanged" : "paste key"}
                          />
                        </Field>
                        <Field label="Priority (lower = first)">
                          <Input
                            className="h-8 text-xs font-mono w-24"
                            type="number"
                            value={p.priority}
                            disabled={!p.enabled}
                            onChange={(e) => setProvider(p.name, { priority: Number(e.target.value) || 0 })}
                          />
                        </Field>
                      </div>
                    ))}
                  </div>
                </SectionBlock>
              )}

              {form && section === "anonymity" && (
                <SectionBlock title="Anonymity" contract="Save">
                  <label className="flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      className="accent-sky-500"
                      checked={form.proxy_enabled}
                      onChange={(e) => setForm({ ...form, proxy_enabled: e.target.checked })}
                    />
                    Enable anonymity layer
                  </label>
                  <Field label={`Proxy URL${form.proxy_has_secret ? " (saved)" : ""}`}>
                    <Input
                      className="h-8 text-xs font-mono"
                      value={form.proxy_url}
                      onChange={(e) =>
                        setForm({ ...form, proxy_url: e.target.value, proxy_has_secret: false })
                      }
                      placeholder={
                        form.proxy_has_secret ? "unchanged (credentials hidden)" : "socks5://127.0.0.1:9050"
                      }
                      disabled={!form.proxy_enabled}
                    />
                  </Field>
                  {(
                    [
                      ["use_proxy_for_debrid", "Use proxy for debrid API"],
                      ["use_proxy_for_downloads", "Use proxy for downloads"],
                      ["random_user_agent", "Randomize User-Agent"],
                      ["strip_trackers", "Strip trackers from magnets"],
                    ] as const
                  ).map(([key, label]) => (
                    <label key={key} className="flex items-center gap-2 text-xs">
                      <input
                        type="checkbox"
                        className="accent-sky-500"
                        checked={form[key]}
                        disabled={!form.proxy_enabled}
                        onChange={(e) => setForm({ ...form, [key]: e.target.checked })}
                      />
                      {label}
                    </label>
                  ))}
                </SectionBlock>
              )}

              {form && section === "interceptor" && (
                <SectionBlock title="Interceptor" contract="Immediate">
                  <SoftCheck
                    label="Interceptor enabled"
                    checked={form.interceptor_enabled}
                    statusId="ix.enabled"
                    StatusHint={StatusHint}
                    onChange={(v) => {
                      const prev = form.interceptor_enabled
                      setForm({ ...form, interceptor_enabled: v })
                      // enabled is structural → hard rebind path on server
                      applySoft(
                        "ix.enabled",
                        { interceptor: { enabled: v } },
                        () => setForm((f) => (f ? { ...f, interceptor_enabled: prev } : f)),
                      )
                    }}
                  />
                  <Field label="Delivery mode" hint={<StatusHint id="ix.delivery" />}>
                    <select
                      className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-xs"
                      value={form.delivery_mode}
                      onChange={(e) => {
                        const v = e.target.value as "webseed" | "download"
                        const prev = form.delivery_mode
                        setForm({ ...form, delivery_mode: v })
                        applySoft(
                          "ix.delivery",
                          { interceptor: { delivery_mode: v } },
                          () => setForm((f) => (f ? { ...f, delivery_mode: prev } : f)),
                        )
                      }}
                    >
                      <option value="webseed">webseed (inject HTTP links)</option>
                      <option value="download">download (in-process)</option>
                    </select>
                  </Field>
                  <SoftCheck
                    label="Stalled-only debrid candidates"
                    checked={form.stalled_only}
                    statusId="ix.stalled_only"
                    StatusHint={StatusHint}
                    onChange={(v) => {
                      const prev = form.stalled_only
                      setForm({ ...form, stalled_only: v })
                      applySoft(
                        "ix.stalled_only",
                        { interceptor: { stalled_only: v } },
                        () => setForm((f) => (f ? { ...f, stalled_only: prev } : f)),
                      )
                    }}
                  />
                  <SoftNumber
                    label="Stalled min minutes"
                    value={form.stalled_min_minutes}
                    min={1}
                    max={24 * 60}
                    statusId="ix.stalled_min"
                    StatusHint={StatusHint}
                    onCommit={(v) => {
                      const prev = form.stalled_min_minutes
                      setForm({ ...form, stalled_min_minutes: v })
                      applySoft(
                        "ix.stalled_min",
                        { interceptor: { stalled_min_minutes: v } },
                        () => setForm((f) => (f ? { ...f, stalled_min_minutes: prev } : f)),
                      )
                    }}
                  />
                  <SoftNumber
                    label="Min stalled seeds"
                    value={form.min_stalled_seeds}
                    min={0}
                    max={100}
                    statusId="ix.min_seeds"
                    StatusHint={StatusHint}
                    onCommit={(v) => {
                      const prev = form.min_stalled_seeds
                      setForm({ ...form, min_stalled_seeds: v })
                      applySoft(
                        "ix.min_seeds",
                        { interceptor: { min_stalled_seeds: v } },
                        () => setForm((f) => (f ? { ...f, min_stalled_seeds: prev } : f)),
                      )
                    }}
                  />
                  <SoftNumber
                    label="Max stalled download speed (B/s)"
                    value={form.max_stalled_download_speed}
                    min={0}
                    max={10_000_000}
                    statusId="ix.max_speed"
                    StatusHint={StatusHint}
                    onCommit={(v) => {
                      const prev = form.max_stalled_download_speed
                      setForm({ ...form, max_stalled_download_speed: v })
                      applySoft(
                        "ix.max_speed",
                        { interceptor: { max_stalled_download_speed: v } },
                        () => setForm((f) => (f ? { ...f, max_stalled_download_speed: prev } : f)),
                      )
                    }}
                  />
                  <SoftNumber
                    label="Max debrid per scan"
                    value={form.max_debrid_per_scan}
                    min={1}
                    max={50}
                    statusId="ix.max_debrid"
                    StatusHint={StatusHint}
                    onCommit={(v) => {
                      const prev = form.max_debrid_per_scan
                      setForm({ ...form, max_debrid_per_scan: v })
                      applySoft(
                        "ix.max_debrid",
                        { interceptor: { max_debrid_per_scan: v } },
                        () => setForm((f) => (f ? { ...f, max_debrid_per_scan: prev } : f)),
                      )
                    }}
                  />
                  <SoftCheck
                    label="Skip private torrents"
                    checked={form.skip_private}
                    statusId="ix.skip_private"
                    StatusHint={StatusHint}
                    onChange={(v) => {
                      const prev = form.skip_private
                      setForm({ ...form, skip_private: v })
                      applySoft(
                        "ix.skip_private",
                        { interceptor: { skip_private: v } },
                        () => setForm((f) => (f ? { ...f, skip_private: prev } : f)),
                      )
                    }}
                  />
                  <SoftCheck
                    label="Require magnet"
                    checked={form.require_magnet}
                    statusId="ix.require_magnet"
                    StatusHint={StatusHint}
                    onChange={(v) => {
                      const prev = form.require_magnet
                      setForm({ ...form, require_magnet: v })
                      applySoft(
                        "ix.require_magnet",
                        { interceptor: { require_magnet: v } },
                        () => setForm((f) => (f ? { ...f, require_magnet: prev } : f)),
                      )
                    }}
                  />
                  <SoftCheck
                    label="Metadata handoff"
                    checked={form.metadata_handoff}
                    statusId="ix.metadata"
                    StatusHint={StatusHint}
                    onChange={(v) => {
                      const prev = form.metadata_handoff
                      setForm({ ...form, metadata_handoff: v })
                      applySoft(
                        "ix.metadata",
                        { interceptor: { metadata_handoff: v } },
                        () => setForm((f) => (f ? { ...f, metadata_handoff: prev } : f)),
                      )
                    }}
                  />
                  <SoftCheck
                    label="Manage without debrid"
                    checked={form.manage_without_debrid}
                    statusId="ix.manage"
                    StatusHint={StatusHint}
                    onChange={(v) => {
                      const prev = form.manage_without_debrid
                      setForm({ ...form, manage_without_debrid: v })
                      applySoft(
                        "ix.manage",
                        { interceptor: { manage_without_debrid: v } },
                        () => setForm((f) => (f ? { ...f, manage_without_debrid: prev } : f)),
                      )
                    }}
                  />
                </SectionBlock>
              )}

              {form && section === "matcher" && (
                <SectionBlock title="Matcher" contract="Immediate">
                  <SoftCheck
                    label="Matcher enabled"
                    checked={form.matcher_enabled}
                    statusId="mt.enabled"
                    StatusHint={StatusHint}
                    onChange={(v) => {
                      const prev = form.matcher_enabled
                      setForm({ ...form, matcher_enabled: v })
                      applySoft(
                        "mt.enabled",
                        { matcher: { enabled: v } },
                        () => setForm((f) => (f ? { ...f, matcher_enabled: prev } : f)),
                      )
                    }}
                  />
                  <SoftCheck
                    label="Auto placement"
                    checked={form.matcher_auto_placement}
                    statusId="mt.auto"
                    StatusHint={StatusHint}
                    onChange={(v) => {
                      const folders = form.matcher_folders
                        .split(",")
                        .map((x) => x.trim())
                        .filter(Boolean)
                      if (v && folders.length === 0) {
                        toast.error("Add at least one search folder before enabling auto placement")
                        return
                      }
                      const prev = form.matcher_auto_placement
                      setForm({ ...form, matcher_auto_placement: v })
                      applySoft(
                        "mt.auto",
                        { matcher: { auto_placement: v } },
                        () => setForm((f) => (f ? { ...f, matcher_auto_placement: prev } : f)),
                      )
                    }}
                  />
                  <Field label="Search folders (comma-separated)" hint={<StatusHint id="mt.folders" />}>
                    <Input
                      className="h-8 text-xs font-mono"
                      value={form.matcher_folders}
                      onChange={(e) => setForm({ ...form, matcher_folders: e.target.value })}
                      onBlur={() => {
                        const folders = form.matcher_folders
                          .split(",")
                          .map((x) => x.trim())
                          .filter(Boolean)
                        if (form.matcher_auto_placement && folders.length === 0) {
                          toast.error("Auto placement requires at least one folder")
                          return
                        }
                        const prev = baseline?.matcher_folders ?? form.matcher_folders
                        applySoft(
                          "mt.folders",
                          { matcher: { folders } },
                          () => setForm((f) => (f ? { ...f, matcher_folders: prev } : f)),
                        )
                      }}
                      placeholder="/data/media, /mnt/library"
                    />
                  </Field>
                  <SoftNumber
                    label="Interval minutes"
                    value={form.matcher_interval_minutes}
                    min={1}
                    max={24 * 60}
                    statusId="mt.interval"
                    StatusHint={StatusHint}
                    onCommit={(v) => {
                      const prev = form.matcher_interval_minutes
                      setForm({ ...form, matcher_interval_minutes: v })
                      applySoft(
                        "mt.interval",
                        { matcher: { interval_minutes: v } },
                        () => setForm((f) => (f ? { ...f, matcher_interval_minutes: prev } : f)),
                      )
                    }}
                  />
                  <SoftCheck
                    label="Recheck after match"
                    checked={form.matcher_recheck}
                    statusId="mt.recheck"
                    StatusHint={StatusHint}
                    onChange={(v) => {
                      const prev = form.matcher_recheck
                      setForm({ ...form, matcher_recheck: v })
                      applySoft(
                        "mt.recheck",
                        { matcher: { recheck: v } },
                        () => setForm((f) => (f ? { ...f, matcher_recheck: prev } : f)),
                      )
                    }}
                  />
                </SectionBlock>
              )}

              {form && section === "application" && (
                <SectionBlock title="Application" contract="Immediate">
                  <div className="flex items-center gap-2 mb-1">
                    {version && (
                      <Badge variant="outline" className="text-[10px] font-mono">
                        qbx v{version}
                      </Badge>
                    )}
                  </div>
                  <Field label="Update channel" hint={<StatusHint id="up.channel" />}>
                    <select
                      className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-xs"
                      value={form.update_channel}
                      onChange={(e) => {
                        const v = e.target.value as "stable" | "beta"
                        const prev = form.update_channel
                        setForm({ ...form, update_channel: v })
                        applySoft(
                          "up.channel",
                          { updates: { channel: v } },
                          () => setForm((f) => (f ? { ...f, update_channel: prev } : f)),
                        )
                      }}
                    >
                      <option value="stable">stable</option>
                      <option value="beta">beta (prereleases)</option>
                    </select>
                  </Field>
                  <Field label="GitHub source (owner / repo)" hint={<StatusHint id="up.source" />}>
                    <div className="flex gap-1">
                      <Input
                        className="h-8 text-xs font-mono"
                        value={form.update_source_owner}
                        onChange={(e) => setForm({ ...form, update_source_owner: e.target.value })}
                        onBlur={() => {
                          const prevO = baseline?.update_source_owner ?? form.update_source_owner
                          const prevR = baseline?.update_source_repo ?? form.update_source_repo
                          applySoft(
                            "up.source",
                            {
                              updates: {
                                source_owner: form.update_source_owner.trim(),
                                source_repo: form.update_source_repo.trim(),
                              },
                            },
                            () =>
                              setForm((f) =>
                                f
                                  ? { ...f, update_source_owner: prevO, update_source_repo: prevR }
                                  : f,
                              ),
                          )
                        }}
                        placeholder="bodencrouch"
                      />
                      <Input
                        className="h-8 text-xs font-mono"
                        value={form.update_source_repo}
                        onChange={(e) => setForm({ ...form, update_source_repo: e.target.value })}
                        onBlur={() => {
                          const prevO = baseline?.update_source_owner ?? form.update_source_owner
                          const prevR = baseline?.update_source_repo ?? form.update_source_repo
                          applySoft(
                            "up.source",
                            {
                              updates: {
                                source_owner: form.update_source_owner.trim(),
                                source_repo: form.update_source_repo.trim(),
                              },
                            },
                            () =>
                              setForm((f) =>
                                f
                                  ? { ...f, update_source_owner: prevO, update_source_repo: prevR }
                                  : f,
                              ),
                          )
                        }}
                        placeholder="qbittorrent_debrid"
                      />
                    </div>
                  </Field>
                  <SoftCheck
                    label="Check for updates when the shell opens"
                    checked={form.update_check_on_startup}
                    statusId="up.startup"
                    StatusHint={StatusHint}
                    onChange={(v) => {
                      const prev = form.update_check_on_startup
                      setForm({ ...form, update_check_on_startup: v })
                      applySoft(
                        "up.startup",
                        { updates: { check_on_startup: v } },
                        () => setForm((f) => (f ? { ...f, update_check_on_startup: prev } : f)),
                      )
                    }}
                  />
                  <SoftCheck
                    label="Desktop notifications"
                    checked={form.desktop_notifications}
                    statusId="desk.notify"
                    StatusHint={StatusHint}
                    onChange={(v) => {
                      const prev = form.desktop_notifications
                      setForm({ ...form, desktop_notifications: v })
                      applySoft(
                        "desk.notify",
                        { desktop: { notifications: v } },
                        () => setForm((f) => (f ? { ...f, desktop_notifications: prev } : f)),
                      )
                    }}
                  />
                  <label className="flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      className="accent-sky-500"
                      checked={form.tray_autostart}
                      onChange={(e) => void toggleTrayAutostart(e.target.checked)}
                    />
                    Start tray at login
                    <StatusHint id="tray" />
                  </label>
                  <div className="flex items-center gap-2 pt-1">
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 text-xs"
                      onClick={() => void checkUpdates()}
                      disabled={updateBusy}
                    >
                      {updateBusy ? "Checking…" : "Check for updates"}
                    </Button>
                    {updateResult?.ok && updateResult.update_available && updateResult.release?.html_url && (
                      <a
                        className="text-xs text-sky-400 underline"
                        href={updateResult.release.html_url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {updateResult.latest} release notes
                      </a>
                    )}
                  </div>
                  <p className="text-[10px] text-muted-foreground max-w-md pt-1">
                    Advanced: WebUI config.toml wins over env / provisional YAML. Checks are check-only.
                  </p>
                </SectionBlock>
              )}
            </div>

            {(section === "connection" || section === "providers" || section === "anonymity") && (
              <div className="shrink-0 border-t border-border px-4 py-2 flex items-center gap-2 bg-card/40">
                <span className="text-[11px] text-muted-foreground flex-1">
                  {dirtySave ? "Unsaved changes" : "No unsaved changes"}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 text-xs"
                  disabled={!dirtySave || busy}
                  onClick={discardSave}
                >
                  Discard
                </Button>
                <Button
                  size="sm"
                  className="h-7 text-xs"
                  disabled={!dirtySave || busy || !form}
                  onClick={() => void saveHard()}
                >
                  {busy ? "Saving…" : "Save"}
                </Button>
              </div>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

function pickSoftBaseline(form: SettingsForm, patch: Record<string, unknown>): Partial<SettingsForm> {
  const out: Partial<SettingsForm> = {}
  const ix = patch.interceptor as Record<string, unknown> | undefined
  const mt = patch.matcher as Record<string, unknown> | undefined
  const up = patch.updates as Record<string, unknown> | undefined
  const desk = patch.desktop as Record<string, unknown> | undefined
  if (ix) {
    if ("enabled" in ix) out.interceptor_enabled = form.interceptor_enabled
    if ("delivery_mode" in ix) out.delivery_mode = form.delivery_mode
    if ("stalled_only" in ix) out.stalled_only = form.stalled_only
    if ("stalled_min_minutes" in ix) out.stalled_min_minutes = form.stalled_min_minutes
    if ("min_stalled_seeds" in ix) out.min_stalled_seeds = form.min_stalled_seeds
    if ("max_stalled_download_speed" in ix) out.max_stalled_download_speed = form.max_stalled_download_speed
    if ("max_debrid_per_scan" in ix) out.max_debrid_per_scan = form.max_debrid_per_scan
    if ("skip_private" in ix) out.skip_private = form.skip_private
    if ("require_magnet" in ix) out.require_magnet = form.require_magnet
    if ("metadata_handoff" in ix) out.metadata_handoff = form.metadata_handoff
    if ("manage_without_debrid" in ix) out.manage_without_debrid = form.manage_without_debrid
  }
  if (mt) {
    if ("enabled" in mt) out.matcher_enabled = form.matcher_enabled
    if ("auto_placement" in mt) out.matcher_auto_placement = form.matcher_auto_placement
    if ("folders" in mt) out.matcher_folders = form.matcher_folders
    if ("interval_minutes" in mt) out.matcher_interval_minutes = form.matcher_interval_minutes
    if ("recheck" in mt) out.matcher_recheck = form.matcher_recheck
  }
  if (up) {
    if ("channel" in up) out.update_channel = form.update_channel
    if ("source_owner" in up) out.update_source_owner = form.update_source_owner
    if ("source_repo" in up) out.update_source_repo = form.update_source_repo
    if ("check_on_startup" in up) out.update_check_on_startup = form.update_check_on_startup
  }
  if (desk && "notifications" in desk) out.desktop_notifications = form.desktop_notifications
  return out
}

function SectionBlock({
  title,
  contract,
  children,
}: {
  title: string
  contract: string
  children: ReactNode
}) {
  return (
    <section className="space-y-3">
      <div className="flex items-baseline gap-2">
        <h3 className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
          {title}
        </h3>
        <span className="text-[10px] text-muted-foreground/80">· {contract}</span>
      </div>
      <div className="space-y-2.5">{children}</div>
    </section>
  )
}

function Field({
  label,
  children,
  hint,
}: {
  label: string
  children: ReactNode
  hint?: ReactNode
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center">
        <Label className="text-[11px]">{label}</Label>
        {hint}
      </div>
      {children}
    </div>
  )
}

function SoftCheck({
  label,
  checked,
  onChange,
  statusId,
  StatusHint,
}: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
  statusId: string
  StatusHint: ({ id }: { id: string }) => ReactNode
}) {
  return (
    <label className="flex items-center gap-2 text-xs">
      <input
        type="checkbox"
        className="accent-sky-500"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      {label}
      <StatusHint id={statusId} />
    </label>
  )
}

function SoftNumber({
  label,
  value,
  min,
  max,
  onCommit,
  statusId,
  StatusHint,
}: {
  label: string
  value: number
  min: number
  max: number
  onCommit: (v: number) => void
  statusId: string
  StatusHint: ({ id }: { id: string }) => ReactNode
}) {
  const [local, setLocal] = useState(String(value))
  useEffect(() => setLocal(String(value)), [value])
  return (
    <Field label={label} hint={<StatusHint id={statusId} />}>
      <Input
        className="h-8 text-xs font-mono w-32"
        type="number"
        value={local}
        min={min}
        max={max}
        onChange={(e) => setLocal(e.target.value)}
        onBlur={() => {
          const n = Number(local)
          if (!Number.isFinite(n) || n < min || n > max) {
            toast.error(`${label}: enter a number between ${min} and ${max}`)
            setLocal(String(value))
            return
          }
          if (n !== value) onCommit(n)
        }}
      />
    </Field>
  )
}
