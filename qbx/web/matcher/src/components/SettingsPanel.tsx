import { useEffect, useMemo, useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { ControlApi } from "@/api/backend"
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
        </div>
      )}
    </div>
  )
}
