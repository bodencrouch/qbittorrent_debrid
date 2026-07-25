import { useEffect, useMemo, useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { ControlApi, type UpdateCheckResult } from "@/api/backend"
import { getErrorMessage } from "@/lib/utils"
import { uiLog } from "@/lib/ui-log"
import { toast } from "sonner"

const REDACTED = "********"

type ProviderName = "realdebrid" | "alldebrid"

type ProviderForm = {
  name: ProviderName
  enabled: boolean
  priority: number
  api_key: string
  has_secret: boolean
}

type SettingsForm = {
  qbt_url: string
  qbt_username: string
  qbt_password: string
  qbt_has_secret: boolean
  api_token: string
  api_token_has_secret: boolean
  delivery_mode: "webseed" | "download"
  proxy_enabled: boolean
  proxy_url: string
  proxy_has_secret: boolean
  use_proxy_for_debrid: boolean
  use_proxy_for_downloads: boolean
  random_user_agent: boolean
  strip_trackers: boolean
  providers: ProviderForm[]
  update_channel: "stable" | "beta"
  update_source_owner: string
  update_source_repo: string
  update_check_on_startup: boolean
  desktop_notifications: boolean
  tray_autostart: boolean
}

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
  return {
    qbt_url: String(qbt.url || ""),
    qbt_username: String(qbt.username || ""),
    qbt_password: pw === REDACTED ? "" : pw,
    qbt_has_secret: pw === REDACTED || Boolean(pw),
    api_token: token === REDACTED ? "" : token,
    api_token_has_secret: token === REDACTED || Boolean(token),
    delivery_mode: (interceptor.delivery_mode === "download" ? "download" : "webseed"),
    proxy_enabled: anonymity.enabled !== false,
    proxy_url: proxy === REDACTED ? "" : proxy,
    proxy_has_secret: proxy === REDACTED,
    use_proxy_for_debrid: anonymity.use_proxy_for_debrid !== false,
    use_proxy_for_downloads: anonymity.use_proxy_for_downloads !== false,
    random_user_agent: anonymity.random_user_agent !== false,
    strip_trackers: anonymity.strip_trackers !== false,
    providers,
    update_channel: updates.channel === "beta" ? "beta" : "stable",
    // Blank values (older installs) fall back to the public upstream repo.
    update_source_owner: String(updates.source_owner || "bodencrouch"),
    update_source_repo: String(updates.source_repo || "qbittorrent_debrid"),
    update_check_on_startup: updates.check_on_startup !== false,
    desktop_notifications: desktop.notifications !== false,
    tray_autostart: Boolean(desktop.tray_autostart),
  }
}

interface SettingsPanelProps {
  open: boolean
  onClose: () => void
  onSaved?: () => void
}

export function SettingsPanel({ open, onClose, onSaved }: SettingsPanelProps) {
  const [form, setForm] = useState<SettingsForm | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [version, setVersion] = useState("")
  const [updateBusy, setUpdateBusy] = useState(false)
  const [updateResult, setUpdateResult] = useState<UpdateCheckResult | null>(null)
  const [trayBusy, setTrayBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    let attempt = 0
    setError("")
    setForm(null)

    const load = () => {
      attempt += 1
      ControlApi.getConfig()
        .then((cfg) => {
          if (!cancelled) {
            setForm(fromConfig(cfg))
            setError("")
          }
        })
        .catch((err) => {
          if (cancelled) return
          const msg = getErrorMessage(err)
          // One retry — common when a large policy pass briefly saturates the loop.
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
    }
  }, [open])

  const ordered = useMemo(() => {
    if (!form) return []
    return [...form.providers].sort((a, b) => a.priority - b.priority)
  }, [form])

  if (!open) return null

  const setProvider = (name: ProviderName, patch: Partial<ProviderForm>) => {
    setForm((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        providers: prev.providers.map((p) => (p.name === name ? { ...p, ...patch } : p)),
      }
    })
  }

  const checkUpdates = async () => {
    setUpdateBusy(true)
    setUpdateResult(null)
    try {
      const res = await ControlApi.updateCheck()
      setUpdateResult(res)
      if (!res.ok) {
        toast.error(res.error || "Update check failed")
      } else if (res.update_available) {
        toast.info(`qbx ${res.latest} is available`)
      } else if (res.error) {
        // e.g. "no stable releases published yet" — not a hard failure
        toast.message(res.error)
      } else {
        toast.success("qbx is up to date")
      }
    } catch (err) {
      toast.error(getErrorMessage(err))
    } finally {
      setUpdateBusy(false)
    }
  }

  const toggleTrayAutostart = async (next: boolean) => {
    if (!form) return
    setTrayBusy(true)
    const prev = form.tray_autostart
    setForm({ ...form, tray_autostart: next })
    try {
      const res = await ControlApi.setTrayAutostart(next)
      if (!res.ok) {
        setForm((f) => (f ? { ...f, tray_autostart: prev } : f))
        toast.error(res.sync?.reason || "Could not update tray autostart")
      } else {
        toast.success(next ? "Tray will start at login" : "Tray autostart disabled")
      }
    } catch (err) {
      setForm((f) => (f ? { ...f, tray_autostart: prev } : f))
      toast.error(getErrorMessage(err))
    } finally {
      setTrayBusy(false)
    }
  }

  const save = async () => {
    if (!form) return
    setBusy(true)
    setError("")
    uiLog("ui.settings.save", "Saving settings (providers / proxy / qbt)")
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
        },
        server: {
          api_token: form.api_token.trim()
            ? form.api_token.trim()
            : form.api_token_has_secret
              ? REDACTED
              : "",
        },
        interceptor: {
          delivery_mode: form.delivery_mode,
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
        updates: {
          channel: form.update_channel,
          source_owner: form.update_source_owner.trim(),
          source_repo: form.update_source_repo.trim(),
          check_on_startup: form.update_check_on_startup,
        },
        desktop: {
          notifications: form.desktop_notifications,
          // tray_autostart is persisted through its dedicated endpoint
          // (it has an OS side effect) — do not clobber it here.
        },
      })
      if (form.api_token.trim()) {
        localStorage.setItem("qbx_token", form.api_token.trim())
      } else if (!form.api_token_has_secret) {
        localStorage.removeItem("qbx_token")
      }
      uiLog("ui.settings.save.ok", "Settings saved — WebUI config.toml is authoritative")
      toast.success("Settings saved")
      onSaved?.()
      onClose()
    } catch (err) {
      const msg = getErrorMessage(err)
      setError(msg)
      uiLog("ui.settings.save.failed", msg)
      toast.error(msg)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="border-b border-border bg-card/40 px-4 py-3 space-y-4 text-sm max-h-[70vh] overflow-auto">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold tracking-wide">Settings</h2>
          <p className="text-[11px] text-muted-foreground mt-0.5 max-w-2xl">
            Priority: <span className="text-foreground">WebUI (this form)</span>
            {" > "}
            env / CLI
            {" > "}
            provisional YAML. Secrets stay encrypted in config.toml.
          </p>
        </div>
        <div className="flex gap-2 shrink-0">
          <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={onClose} disabled={busy}>
            Close
          </Button>
          <Button size="sm" className="h-7 text-xs" onClick={() => void save()} disabled={busy || !form}>
            {busy ? "Saving…" : "Save"}
          </Button>
        </div>
      </div>

      {error && <p className="text-xs text-red-400">{error}</p>}
      {!form && !error && <p className="text-xs text-muted-foreground">Loading config…</p>}

      {form && (
        <div className="grid gap-4 lg:grid-cols-2">
          <section className="space-y-2">
            <h3 className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
              qBittorrent
            </h3>
            <div className="space-y-1.5">
              <Label className="text-[11px]">WebUI URL</Label>
              <Input
                className="h-8 text-xs font-mono"
                value={form.qbt_url}
                onChange={(e) => setForm({ ...form, qbt_url: e.target.value })}
                placeholder="http://host.docker.internal:8080"
              />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div className="space-y-1.5">
                <Label className="text-[11px]">Username</Label>
                <Input
                  className="h-8 text-xs"
                  value={form.qbt_username}
                  onChange={(e) => setForm({ ...form, qbt_username: e.target.value })}
                />
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px]">
                  Password{form.qbt_has_secret ? " (saved)" : ""}
                </Label>
                <Input
                  className="h-8 text-xs"
                  type="password"
                  value={form.qbt_password}
                  onChange={(e) => setForm({ ...form, qbt_password: e.target.value })}
                  placeholder={form.qbt_has_secret ? "unchanged" : ""}
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px]">Delivery mode</Label>
              <select
                className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-xs"
                value={form.delivery_mode}
                onChange={(e) =>
                  setForm({ ...form, delivery_mode: e.target.value as "webseed" | "download" })
                }
              >
                <option value="webseed">webseed (inject HTTP links)</option>
                <option value="download">download (in-process)</option>
              </select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px]">
                API token{form.api_token_has_secret ? " (saved)" : ""}
              </Label>
              <Input
                className="h-8 text-xs font-mono"
                type="password"
                value={form.api_token}
                onChange={(e) => setForm({ ...form, api_token: e.target.value, api_token_has_secret: false })}
                placeholder={form.api_token_has_secret ? "unchanged" : "empty = no auth on loopback"}
              />
            </div>
          </section>

          <section className="space-y-2">
            <h3 className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
              Proxy / anonymity
            </h3>
            <label className="flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                className="accent-sky-500"
                checked={form.proxy_enabled}
                onChange={(e) => setForm({ ...form, proxy_enabled: e.target.checked })}
              />
              Enable anonymity layer
            </label>
            <div className="space-y-1.5">
              <Label className="text-[11px]">Proxy URL{form.proxy_has_secret ? " (saved)" : ""}</Label>
              <Input
                className="h-8 text-xs font-mono"
                value={form.proxy_url}
                onChange={(e) => setForm({ ...form, proxy_url: e.target.value, proxy_has_secret: false })}
                placeholder={form.proxy_has_secret ? "unchanged (credentials hidden)" : "socks5://127.0.0.1:9050"}
                disabled={!form.proxy_enabled}
              />
            </div>
            <div className="flex flex-col gap-1.5 text-xs">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  className="accent-sky-500"
                  checked={form.use_proxy_for_debrid}
                  disabled={!form.proxy_enabled}
                  onChange={(e) => setForm({ ...form, use_proxy_for_debrid: e.target.checked })}
                />
                Use proxy for debrid API
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  className="accent-sky-500"
                  checked={form.use_proxy_for_downloads}
                  disabled={!form.proxy_enabled}
                  onChange={(e) => setForm({ ...form, use_proxy_for_downloads: e.target.checked })}
                />
                Use proxy for downloads
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  className="accent-sky-500"
                  checked={form.random_user_agent}
                  disabled={!form.proxy_enabled}
                  onChange={(e) => setForm({ ...form, random_user_agent: e.target.checked })}
                />
                Randomize User-Agent
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  className="accent-sky-500"
                  checked={form.strip_trackers}
                  disabled={!form.proxy_enabled}
                  onChange={(e) => setForm({ ...form, strip_trackers: e.target.checked })}
                />
                Strip trackers from magnets
              </label>
            </div>
          </section>

          <section className="space-y-3 lg:col-span-2">
            <div className="flex items-center gap-2">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                Debrid providers
              </h3>
              <Badge variant="outline" className="text-[10px]">
                try order: {ordered.map((p) => p.name.replace("debrid", "")).join(" → ") || "—"}
              </Badge>
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              {form.providers.map((p) => (
                <div key={p.name} className="rounded-md border border-border p-3 space-y-2">
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
                  <div className="space-y-1.5">
                    <Label className="text-[11px]">
                      API key{p.has_secret ? " (saved)" : ""}
                    </Label>
                    <Input
                      className="h-8 text-xs font-mono"
                      type="password"
                      value={p.api_key}
                      disabled={!p.enabled}
                      onChange={(e) => setProvider(p.name, { api_key: e.target.value })}
                      placeholder={p.has_secret ? "unchanged" : "paste key"}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-[11px]">Priority (lower = first)</Label>
                    <Input
                      className="h-8 text-xs font-mono w-24"
                      type="number"
                      value={p.priority}
                      disabled={!p.enabled}
                      onChange={(e) => setProvider(p.name, { priority: Number(e.target.value) || 0 })}
                    />
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className="space-y-3 lg:col-span-2">
            <div className="flex items-center gap-2">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                Application
              </h3>
              {version && (
                <Badge variant="outline" className="text-[10px] font-mono">
                  qbx v{version}
                </Badge>
              )}
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              <div className="space-y-2">
                <div className="grid grid-cols-2 gap-2">
                  <div className="space-y-1.5">
                    <Label className="text-[11px]">Update channel</Label>
                    <select
                      className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-xs"
                      value={form.update_channel}
                      onChange={(e) =>
                        setForm({ ...form, update_channel: e.target.value as "stable" | "beta" })
                      }
                    >
                      <option value="stable">stable</option>
                      <option value="beta">beta (prereleases)</option>
                    </select>
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-[11px]">GitHub source (owner / repo)</Label>
                    <div className="flex gap-1">
                      <Input
                        className="h-8 text-xs font-mono"
                        value={form.update_source_owner}
                        onChange={(e) => setForm({ ...form, update_source_owner: e.target.value })}
                        placeholder="bodencrouch"
                      />
                      <Input
                        className="h-8 text-xs font-mono"
                        value={form.update_source_repo}
                        onChange={(e) => setForm({ ...form, update_source_repo: e.target.value })}
                        placeholder="qbittorrent_debrid"
                      />
                    </div>
                  </div>
                </div>
                <label className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    className="accent-sky-500"
                    checked={form.update_check_on_startup}
                    onChange={(e) => setForm({ ...form, update_check_on_startup: e.target.checked })}
                  />
                  Check for updates when the shell opens
                </label>
                <div className="flex items-center gap-2">
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
                  {updateResult?.ok && !updateResult.update_available && !updateResult.error && (
                    <span className="text-xs text-muted-foreground">up to date</span>
                  )}
                  {updateResult && updateResult.error && (
                    <span className="text-xs text-amber-400">{updateResult.error}</span>
                  )}
                </div>
                {updateResult?.ok && updateResult.update_available && updateResult.guided_commands.length > 0 && (
                  <pre className="rounded-md border border-border bg-card/60 p-2 text-[10px] font-mono overflow-auto">
                    {updateResult.guided_commands.join("\n")}
                  </pre>
                )}
              </div>
              <div className="space-y-2">
                <label className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    className="accent-sky-500"
                    checked={form.desktop_notifications}
                    onChange={(e) => setForm({ ...form, desktop_notifications: e.target.checked })}
                  />
                  Desktop notifications (debrid delivery, failures)
                </label>
                <label className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    className="accent-sky-500"
                    checked={form.tray_autostart}
                    disabled={trayBusy}
                    onChange={(e) => void toggleTrayAutostart(e.target.checked)}
                  />
                  Start tray at login (applies immediately)
                </label>
                <p className="text-[10px] text-muted-foreground max-w-md">
                  Defaults to{" "}
                  <a
                    className="text-sky-400 underline"
                    href="https://github.com/bodencrouch/qbittorrent_debrid"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    bodencrouch/qbittorrent_debrid
                  </a>
                  {" "}(
                  <a
                    className="text-sky-400 underline"
                    href="https://bodecloud.com/qbittorrent_debrid"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    bodecloud.com/qbittorrent_debrid
                  </a>
                  ). Checks are check-only — nothing is downloaded or applied automatically.
                </p>
              </div>
            </div>
          </section>
        </div>
      )}
    </div>
  )
}
