/**
 * HTTP client for the qbx Control Shell (REST + helpers).
 */

function tokenHeaders(): HeadersInit {
  const token = localStorage.getItem("qbx_token") || "";
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (token) h["X-API-Token"] = token;
  return h;
}

async function api<T>(path: string, opts: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { ...opts, headers: { ...tokenHeaders(), ...(opts.headers || {}) } });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg === "Failed to fetch" || msg.includes("NetworkError") || msg.includes("fetch")) {
      throw new Error(
        "Cannot reach qbx API (server busy or offline). Wait a moment, or restart `qbx serve`.",
      );
    }
    throw err instanceof Error ? err : new Error(msg);
  }
  if (res.status === 401) {
    // Avoid window.prompt — it blocks the UI and fails in embedded/browser-preview contexts.
    throw new Error("API token required. Open Settings and enter the token, then Save.");
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error((detail as { detail?: string }).detail || res.statusText || `HTTP ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export type TorrentInfo = {
  hash: string;
  name: string;
  size: number;
  progress: number;
  state: string;
  savePath: string;
  contentPath: string;
  dlspeed?: number;
  upspeed?: number;
  eta?: number;
  ratio?: number;
  category?: string;
  tags?: string;
  priority?: number;
  num_seeds?: number;
  num_leechs?: number;
  qbx_status?: string;
  qbx_reason?: string;
  qbx_inflight?: boolean;
  qbx_tags?: string[];
};

export type TorrentFile = {
  index: number;
  name: string;
  size: number;
  progress?: number;
};

export type DiskFile = {
  path: string;
  name: string;
  size: number;
  link_type?: string;
};

export type MatchInfo = {
  torrentFile: { index: number; name: string; size: number };
  diskFiles: DiskFile[];
  selected: DiskFile | null;
  autoMatched: boolean;
  link_type?: string | null;
};

export type HealthInfo = {
  ok: boolean;
  configured: boolean;
  debrid_enabled: boolean;
  interceptor_running: boolean;
  automation_running: boolean;
  interceptor: Record<string, unknown>;
  automation: Record<string, unknown>;
  boot_id: string;
  last_event_id: number;
  last_log_id: number;
  contract?: ContractSummary;
  attention?: AttentionSummary;
};

export type AttentionSummary = {
  open_count: number;
  critical_count: number;
  warning_count?: number;
  info_count?: number;
};

export type AttentionItem = {
  id: string;
  kind: "contract" | "interceptor" | "storage" | "torrent";
  severity: "critical" | "warning" | "info";
  title: string;
  detail: string;
  primary_action: Record<string, unknown>;
  href: string;
  ts: number;
};

export type AttentionPayload = {
  items: AttentionItem[];
  counts: { critical: number; warning: number; info: number };
};

export type ContractSummary = {
  status: "ok" | "degraded" | "blocked";
  hard_fails: number;
  soft_warns: number;
  checked_at: number;
};

export type ContractCheck = {
  id: string;
  severity: "hard" | "soft";
  title: string;
  detail: string;
  remediation: string;
  settings_section: string;
};

export type ContractReport = ContractSummary & {
  checks: ContractCheck[];
};

export type LogEntry = {
  id: number;
  ts: number;
  level: string;
  source: string;
  message: string;
};

export type EventEntry = {
  id: number;
  kind: string;
  message: string;
  ts: number;
  hash?: string;
  [key: string]: unknown;
};

export type VersionInfo = {
  ok: boolean;
  app: string;
  version: string;
  channel: "stable" | "beta";
  source: { owner: string; repo: string };
  check_on_startup: boolean;
};

export type UpdateCheckResult = {
  ok: boolean;
  update_available: boolean;
  downgrade: boolean;
  current: string;
  latest: string | null;
  channel: string;
  source: { owner: string; repo: string };
  release: {
    tag: string;
    name: string;
    prerelease: boolean;
    published_at?: string;
    html_url?: string;
    body?: string;
  } | null;
  guided_commands: string[];
  error: string | null;
};

export type UpdateSource = {
  owner: string;
  repo: string;
  upstream: boolean;
  html_url: string;
  full_name: string;
};

export type UpdateSourcesResult = {
  ok: boolean;
  upstream: { owner: string; repo: string };
  configured?: { owner: string; repo: string };
  sources: UpdateSource[];
  error: string | null;
};

export type UpdateRelease = {
  tag: string;
  name: string;
  prerelease: boolean;
  published_at?: string;
  html_url?: string;
  guided_commands: string[];
};

export type UpdateReleasesResult = {
  ok: boolean;
  owner: string;
  repo: string;
  channel: string;
  releases: UpdateRelease[];
  error: string | null;
};

export type TrayAutostartResult = {
  ok: boolean;
  tray_autostart: boolean;
  sync: { ok: boolean; enabled: boolean; path: string; action: string; reason?: string };
};

export const QBitService = {
  async Connect(_creds?: { url: string; username: string; password: string }): Promise<void> {
    const r = await api<{ ok: boolean; error?: string }>("/api/qbt/test", { method: "POST" });
    if (!r.ok) throw new Error(r.error || "qBittorrent connection failed");
  },

  async GetVersion(): Promise<string> {
    const r = await api<{ ok: boolean; version?: string }>("/api/qbt/test", { method: "POST" });
    return r.version || "unknown";
  },

  async GetTorrents(): Promise<TorrentInfo[]> {
    const rows = await api<Record<string, unknown>[]>("/api/qbt/torrents");
    return rows.map(mapTorrent);
  },

  async GetTorrentFiles(hash: string): Promise<TorrentFile[]> {
    return api<TorrentFile[]>(`/api/qbt/torrents/${encodeURIComponent(hash)}/files`);
  },

  async RenameFile(hash: string, oldPath: string, newPath: string): Promise<void> {
    await api("/api/qbt/rename-file", {
      method: "POST",
      body: JSON.stringify({ hash, oldPath, newPath }),
    });
  },

  async SetFilePriority(hash: string, fileIDs: string, priority: number): Promise<void> {
    await api("/api/qbt/file-priority", {
      method: "POST",
      body: JSON.stringify({ hash, id: fileIDs, priority }),
    });
  },

  async RecheckTorrent(hash: string): Promise<void> {
    await api("/api/qbt/recheck", {
      method: "POST",
      body: JSON.stringify({ hash }),
    });
  },
};

export const MatcherService = {
  async DirExists(path: string): Promise<boolean> {
    const r = await api<{ exists: boolean }>("/api/matcher/dir-exists", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    return r.exists;
  },

  async ScanDir(path: string): Promise<DiskFile[]> {
    const r = await api<{ files: DiskFile[] }>("/api/matcher/scan", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    return r.files;
  },

  async FindMatches(args: {
    torrentFiles: { index: number; name: string; size: number }[];
    diskFiles: DiskFile[];
    requireSameExtension: boolean;
  }): Promise<{
    matches: MatchInfo[];
    unmatched: { index: number; name: string; size: number }[];
    totalFiles: number;
    matchedCount: number;
  }> {
    return api("/api/matcher/find", {
      method: "POST",
      body: JSON.stringify(args),
    });
  },

  async GenRenames(args: {
    matches: MatchInfo[];
    searchPath: string;
  }): Promise<{ oldPath: string; newPath: string }[]> {
    const r = await api<{ renames: { oldPath: string; newPath: string }[] }>("/api/matcher/renames", {
      method: "POST",
      body: JSON.stringify(args),
    });
    return r.renames;
  },

  async Run(args: {
    hash: string;
    path?: string;
    dry_run?: boolean;
    require_same_extension?: boolean;
    skip_unmatched?: boolean;
    recheck?: boolean;
  }): Promise<Record<string, unknown>> {
    return api("/api/matcher/run", {
      method: "POST",
      body: JSON.stringify(args),
    });
  },
};

/** Integration contract checks (paths, writability, qBT alignment). */
export const IntegrationService = {
  get(): Promise<ContractReport> {
    return api("/api/integration/contract");
  },

  run(): Promise<ContractReport> {
    return api("/api/integration/contract/run", { method: "POST" });
  },

  snooze(checkId: string, until: number): Promise<{ ok: boolean }> {
    return api("/api/integration/contract/snooze", {
      method: "POST",
      body: JSON.stringify({ check_id: checkId, until }),
    });
  },
};

/** Needs-attention queue (Overview surface). */
export const AttentionService = {
  get(): Promise<AttentionPayload> {
    return api("/api/attention");
  },
};

/** Exact-content duplicate manager (Storage surface). */
export type DuplicateMember = {
  path: string;
  size: number;
  dev: number;
  ino: number;
  nlink: number;
  mtime: number;
  root: string;
  protected: boolean;
};

export type DuplicateGroup = {
  digest: string;
  size: number;
  members: DuplicateMember[];
  distinct_inodes: number;
  reclaimable_bytes: number;
  has_existing_hardlinks: boolean;
  suggested_keeper: string;
  suggested_losers: string[];
};

export type StorageScanProgress = {
  files_seen: number;
  candidates: number;
  hashed: number;
  groups_found: number;
  reclaimable_bytes: number;
  elapsed: number;
  cancelled: boolean;
  stage: string;
};

export type StorageStatus = {
  running: boolean;
  roots: string[];
  protected_roots: string[];
  scanned_at: number;
  progress: StorageScanProgress;
  groups: number;
  reclaimable_bytes: number;
  suppressed?: number;
};

export type StorageGroups = StorageStatus & {
  truncated: boolean;
  items: DuplicateGroup[];
};

export type ReclaimAction = "keep" | "link" | "delete";

export type ReclaimOutcome = {
  path: string;
  action: "keep" | "link" | "delete" | "skip";
  reason: string;
  bytes_freed: number;
  bytes_pending_purge: number;
  quarantine_id: string;
};

export type ReclaimResult = {
  ok: boolean;
  linked: number;
  deleted: number;
  skipped: number;
  bytes_freed: number;
  bytes_pending_purge: number;
  outcomes: ReclaimOutcome[];
};

export type QuarantineEntry = {
  id: string;
  ts: number;
  original: string;
  quarantined: string;
  size: number;
  digest: string;
  state: string;
};

export type SuppressedEntry = {
  id: string;
  digest: string;
  ts: number;
  reason?: string;
  permanent: boolean;
  state: string;
};

export const StorageService = {
  status(): Promise<StorageStatus> {
    return api("/api/storage/status");
  },

  groups(limit = 500): Promise<StorageGroups> {
    return api(`/api/storage/groups?limit=${limit}`);
  },

  scan(): Promise<{ accepted: boolean; roots?: string[] }> {
    return api("/api/storage/scan", { method: "POST" });
  },

  cancelScan(): Promise<{ cancelled: boolean; reason?: string }> {
    return api("/api/storage/scan/cancel", { method: "POST" });
  },

  apply(
    items: { digest: string; keeper_path: string; actions: { path: string; action: ReclaimAction }[] }[],
  ): Promise<ReclaimResult> {
    return api("/api/storage/apply", {
      method: "POST",
      body: JSON.stringify({ items }),
    });
  },

  quarantine(): Promise<{ items: QuarantineEntry[]; bytes_pending_purge: number }> {
    return api("/api/storage/quarantine");
  },

  restore(ids: string[]): Promise<{ ok: boolean; restored: number }> {
    return api("/api/storage/quarantine/restore", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
  },

  purge(ids: string[]): Promise<{ ok: boolean; purged: number; bytes_freed: number }> {
    return api("/api/storage/quarantine/purge", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
  },

  audit(limit = 100): Promise<{ items: Record<string, unknown>[] }> {
    return api(`/api/storage/audit?limit=${limit}`);
  },

  listSuppressed(): Promise<{ items: SuppressedEntry[]; count: number }> {
    return api("/api/storage/suppressed");
  },

  suppress(digest: string, permanent = true): Promise<{ ok: boolean; id?: string; session_only?: boolean }> {
    return api("/api/storage/suppress", {
      method: "POST",
      body: JSON.stringify({ digest, permanent }),
    });
  },

  restoreSuppressed(ids: string[]): Promise<{ ok: boolean; restored: number }> {
    return api("/api/storage/suppressed/restore", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
  },

  reveal(path: string): Promise<{ ok: boolean; path?: string }> {
    return api("/api/storage/reveal", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
  },
};

export const ControlApi = {
  health(): Promise<HealthInfo> {
    return api("/api/health");
  },

  listTorrents(params: {
    filter?: string;
    category?: string;
    tag?: string;
    sort?: string;
    reverse?: boolean;
    limit?: number;
    offset?: number;
  } = {}): Promise<{ torrents: TorrentInfo[]; count: number; limit: number; offset: number }> {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      // Allow limit=0 ("all"); only skip undefined/null/empty-string.
      if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
    });
    if (params.limit === 0) q.set("limit", "0");
    return api(`/api/torrents?${q.toString()}`).then((r: any) => ({
      ...r,
      torrents: (r.torrents || []).map(mapTorrent),
    }));
  },

  getTorrent(hash: string): Promise<TorrentInfo & { properties?: Record<string, unknown>; webseeds?: { url: string }[]; magnet_uri?: string }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}`).then((r: any) => ({
      ...mapTorrent(r),
      properties: r.properties,
      webseeds: r.webseeds,
      magnet_uri: r.magnet_uri ? String(r.magnet_uri) : undefined,
    }));
  },

  intercept(hash: string): Promise<{ accepted: boolean }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/intercept`, { method: "POST", body: "{}" });
  },

  nudge(hash: string): Promise<{ accepted: boolean }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/nudge`, { method: "POST", body: "{}" });
  },

  skipAuto(hash: string): Promise<{ ok: boolean }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/skip-auto`, { method: "POST", body: "{}" });
  },

  retry(hash: string): Promise<{ ok: boolean }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/retry`, { method: "POST", body: "{}" });
  },

  pause(hash: string): Promise<{ ok: boolean }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/pause`, { method: "POST", body: "{}" });
  },

  resume(hash: string): Promise<{ ok: boolean }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/resume`, { method: "POST", body: "{}" });
  },

  delete(hash: string, deleteFiles = false): Promise<{ ok: boolean }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/delete`, {
      method: "POST",
      body: JSON.stringify({ deleteFiles }),
    });
  },

  reannounce(hash: string): Promise<{ ok: boolean }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/reannounce`, { method: "POST", body: "{}" });
  },

  forceStart(hash: string, value = true): Promise<{ ok: boolean }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/force-start`, {
      method: "POST",
      body: JSON.stringify({ value }),
    });
  },

  queue(hash: string, action: "top" | "up" | "down" | "bottom"): Promise<{ ok: boolean }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/queue`, {
      method: "POST",
      body: JSON.stringify({ action }),
    });
  },

  interceptorStatus(): Promise<Record<string, unknown> & { running?: boolean }> {
    return api("/api/interceptor/status");
  },

  webseeds(hash: string): Promise<{ webseeds: { url: string }[] }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/webseeds`);
  },

  mutateWebseeds(hash: string, action: "add" | "remove", urls: string[]): Promise<{ ok: boolean; webseeds: { url: string }[] }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/webseeds`, {
      method: "POST",
      body: JSON.stringify({ action, urls }),
    });
  },

  tags(hash: string, add: string[] = [], remove: string[] = []): Promise<{ ok: boolean }> {
    return api(`/api/torrents/${encodeURIComponent(hash)}/tags`, {
      method: "POST",
      body: JSON.stringify({ add, remove }),
    });
  },

  interceptorStart(): Promise<{ running: boolean }> {
    return api("/api/interceptor/start", { method: "POST" });
  },

  interceptorStop(): Promise<{ running: boolean }> {
    return api("/api/interceptor/stop", { method: "POST" });
  },

  interceptorScan(): Promise<Record<string, unknown>> {
    return api("/api/interceptor/scan", { method: "POST" });
  },

  getConfig(): Promise<Record<string, unknown>> {
    return api("/api/config");
  },

  updateConfig(patch: Record<string, unknown>): Promise<Record<string, unknown>> {
    return api("/api/config", { method: "POST", body: JSON.stringify(patch) });
  },

  version(): Promise<VersionInfo> {
    return api("/api/version");
  },

  updateCheck(): Promise<UpdateCheckResult> {
    return api("/api/update/check");
  },

  updateSources(): Promise<UpdateSourcesResult> {
    return api("/api/update/sources");
  },

  updateReleases(opts?: {
    owner?: string;
    repo?: string;
    channel?: "stable" | "beta";
  }): Promise<UpdateReleasesResult> {
    const q = new URLSearchParams();
    if (opts?.owner) q.set("owner", opts.owner);
    if (opts?.repo) q.set("repo", opts.repo);
    if (opts?.channel) q.set("channel", opts.channel);
    const qs = q.toString();
    return api(`/api/update/releases${qs ? `?${qs}` : ""}`);
  },

  setTrayAutostart(autostart: boolean): Promise<TrayAutostartResult> {
    return api("/api/config/tray-autostart", {
      method: "POST",
      body: JSON.stringify({ autostart }),
    });
  },
};

function mapTorrent(t: Record<string, unknown>): TorrentInfo {
  return {
    hash: String(t.hash || ""),
    name: String(t.name || ""),
    size: Number(t.size || 0),
    progress: Number(t.progress || 0),
    state: String(t.state || ""),
    savePath: String(t.save_path || t.savePath || ""),
    contentPath: String(t.content_path || t.contentPath || t.save_path || ""),
    dlspeed: Number(t.dlspeed || 0),
    upspeed: Number(t.upspeed || 0),
    eta: Number(t.eta || 0),
    ratio: Number(t.ratio || 0),
    category: String(t.category || ""),
    tags: String(t.tags || ""),
    priority: Number(t.priority ?? t.queue ?? 0),
    num_seeds: Number(t.num_seeds || 0),
    num_leechs: Number(t.num_leechs || 0),
    qbx_status: String(t.qbx_status || ""),
    qbx_reason: String(t.qbx_reason || ""),
    qbx_inflight: Boolean(t.qbx_inflight),
    qbx_tags: Array.isArray(t.qbx_tags) ? (t.qbx_tags as string[]) : undefined,
  };
}

export function eventsUrl(since = 0): string {
  const token = localStorage.getItem("qbx_token") || "";
  const q = new URLSearchParams();
  if (since) q.set("since", String(since));
  if (token) q.set("token", token);
  const qs = q.toString();
  return `/api/events${qs ? `?${qs}` : ""}`;
}

export function logsUrl(since = 0, level = "", grep = ""): string {
  const token = localStorage.getItem("qbx_token") || "";
  const q = new URLSearchParams();
  if (since) q.set("since", String(since));
  if (level) q.set("level", level);
  if (grep) q.set("grep", grep);
  if (token) q.set("token", token);
  const qs = q.toString();
  return `/api/logs${qs ? `?${qs}` : ""}`;
}
