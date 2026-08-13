/**
 * qbx inject script for the vendored qBittorrent WebUI (/qbt/).
 * Adds toolbar buttons + context-menu actions that call the qbx Control API.
 */
(function () {
  "use strict";

  const TOKEN_KEY = "qbx_token";

  function token() {
    try {
      return localStorage.getItem(TOKEN_KEY) || "";
    } catch {
      return "";
    }
  }

  function headers() {
    const h = { "Content-Type": "application/json" };
    const t = token();
    if (t) h["X-API-Token"] = t;
    return h;
  }

  /** Error carrying the HTTP status so callers can special-case 401. */
  function ApiError(message, status) {
    const err = new Error(message);
    err.status = status;
    return err;
  }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({}, opts || {}, { headers: headers() }));
    if (!res.ok) {
      // Read as text first: error bodies are not always JSON (proxy 502s, tracebacks).
      const raw = await res.text().catch(function () {
        return "";
      });
      let detail = "";
      try {
        detail = JSON.parse(raw).detail || "";
      } catch (_) {
        detail = raw.slice(0, 200);
      }
      if (res.status === 401) {
        throw ApiError(
          detail || "qbx API token required or invalid",
          401,
        );
      }
      throw ApiError(detail || res.statusText || "request failed", res.status);
    }
    return res.status === 204 ? null : res.json();
  }

  function selectedHashes() {
    try {
      if (window.torrentsTable && typeof window.torrentsTable.selectedRowsIds === "function") {
        return window.torrentsTable.selectedRowsIds() || [];
      }
    } catch (_) {}
    const rows = document.querySelectorAll("#torrentsTable tr.selected, #torrentsTable .selected");
    const out = [];
    rows.forEach(function (row) {
      const h = row.getAttribute("data-hash") || row.id;
      if (h) out.push(h.replace(/^torrent_/, ""));
    });
    return out;
  }

  function firstHash() {
    const hashes = selectedHashes();
    return hashes[0] || "";
  }

  // --- Toast layer -------------------------------------------------------
  // This WebUI has no notification primitive of its own: qBittorrent.Client is
  // frozen and exposes nothing for toasts, and the rest of the UI falls back to
  // alert(). Every qbx success and failure used to reach the console only, so
  // nothing we did was ever visible. Render our own, styled off the WebUI's
  // colour tokens so it follows the host's light/dark theme.
  const TOAST_CSS = [
    "#qbxToastHost{position:fixed;right:12px;bottom:12px;z-index:100000;display:flex;",
    "flex-direction:column;gap:6px;pointer-events:none;",
    "font:12px/1.4 system-ui,-apple-system,sans-serif;}",
    ".qbxToast{pointer-events:auto;min-width:220px;max-width:380px;padding:8px 10px;",
    "border-radius:4px;border:1px solid var(--color-background-hover,#999);cursor:pointer;",
    "background:var(--color-background-popup,var(--color-background-default,#fff));",
    "color:var(--color-text-default,#222);box-shadow:0 2px 10px rgb(0 0 0 / .25);}",
    ".qbxToast.error{border-color:var(--color-text-red,#c33);}",
    ".qbxToast.success{border-color:var(--color-text-green,#2a2);}",
    ".qbxToast.warning{border-color:var(--color-text-orange,#e80);}",
    ".qbxToastDetail{margin-top:3px;opacity:.75;font-size:11px;word-break:break-word;}",
  ].join("");

  function toastHost() {
    let host = document.getElementById("qbxToastHost");
    if (host) return host;
    const style = document.createElement("style");
    style.id = "qbxToastStyle";
    style.textContent = TOAST_CSS;
    document.head.appendChild(style);
    host = document.createElement("div");
    host.id = "qbxToastHost";
    document.body.appendChild(host);
    return host;
  }

  function toast(message, level, detail) {
    const lvl = level || "info";
    if (lvl === "error") console.error("[qbx]", message, detail || "");
    else console.info("[qbx]", message);

    const el = document.createElement("div");
    el.className = "qbxToast " + lvl;
    el.textContent = message;
    if (detail) {
      const d = document.createElement("div");
      d.className = "qbxToastDetail";
      d.textContent = detail;
      el.appendChild(d);
    }
    toastHost().appendChild(el);
    const remove = function () {
      el.remove();
    };
    el.addEventListener("click", remove);
    setTimeout(remove, lvl === "error" ? 8000 : 4000);
  }

  let tokenPromptOfferedThisSession = false;

  function reportError(err) {
    if (err && err.status === 401) {
      toast("qbx API token required or invalid", "error", "Set it in qbx Settings, then retry.");
      // Offer the fix once, not on every single 401 a burst of requests
      // might produce — but do offer it, rather than leaving the user to
      // find Settings on their own after a wall of red toasts.
      if (!tokenPromptOfferedThisSession) {
        tokenPromptOfferedThisSession = true;
        openTokenPrompt();
      }
      return;
    }
    toast(String((err && err.message) || err), "error");
  }

  /** For actions that genuinely operate on a torrent. Navigation must NOT use this. */
  async function runForSelection(label, fn) {
    const hash = firstHash();
    if (!hash) {
      toast("Select a torrent first", "error");
      return;
    }
    try {
      await fn(hash);
      toast(label + " — " + hash.slice(0, 10), "success");
    } catch (err) {
      reportError(err);
    }
  }

  // Navigate in place. Never window.open: the PyQt6 tray shell uses a bare
  // QWebEngineView with no createWindow() override, so QtWebEngine drops
  // window.open silently — no window, no error, nothing.
  function go(url) {
    window.location.assign(url);
  }

  function postAction(hash, verb) {
    return api("/api/torrents/" + encodeURIComponent(hash) + "/" + verb, {
      method: "POST",
      body: "{}",
    });
  }

  async function revealInFileManager(hash) {
    const torrent = await api("/api/torrents/" + encodeURIComponent(hash));
    const path = torrent && (torrent.content_path || torrent.save_path);
    if (!path) throw ApiError("No content path for this torrent", 0);
    return api("/api/storage/reveal", { method: "POST", body: JSON.stringify({ path }) });
  }

  function openTorrentWindow(kind, hash) {
    if (!hash) {
      toast("Select a torrent first", "error");
      return;
    }
    openQbxWindow(kind, { hash: hash });
  }

  // Navigation, not a torrent operation: works with nothing selected.
  // Prefer switching to the qbx tab (no reload, state preserved); only fall
  // back to the standalone shell if the tab never attached.
  function openShell(hash) {
    const link = document.getElementById("qbxTabLink");
    const tabsList = document.getElementById("mainWindowTabsList");
    if (!link) {
      go(hash ? "/?hash=" + encodeURIComponent(hash) : "/");
      return;
    }
    if (tabsList) {
      try {
        MochaUI.selected(link, tabsList);
      } catch (_) {}
    }
    showQbxTab("overview", hash);
  }

  /**
   * One definition per per-torrent action, shared by the context menu, the
   * toolbar, and bulk execution. `multi: true` actions run over the whole
   * selection; the rest require exactly one torrent and open a window.
   */
  const QBX_TORRENT_ACTIONS = [
    { id: "qbxDebrid", label: "Send to debrid", multi: true, run: (h) => postAction(h, "intercept") },
    { id: "qbxRetry", label: "Retry failed debrid", multi: true, run: (h) => postAction(h, "retry") },
    { id: "qbxNudge", label: "Nudge", multi: true, run: (h) => postAction(h, "nudge") },
    { id: "qbxSkip", label: "Skip auto-debrid", multi: true, run: (h) => postAction(h, "skip-auto") },
    { id: "qbxMatch", label: "Match files…", window: "match" },
    { id: "qbxDebridDetails", label: "Debrid details…", window: "debrid" },
    { id: "qbxReveal", label: "Reveal in file manager", run: (h) => revealInFileManager(h) },
    { id: "qbxOpenShell", label: "Show in qbx", special: "openShell" },
  ];

  /** Run one action across a whole selection, with one aggregate toast. */
  async function runBulk(item, hashes) {
    if (!hashes.length) {
      toast("Select a torrent first", "error");
      return;
    }
    const CONCURRENCY = 6;
    const results = [];
    for (let i = 0; i < hashes.length; i += CONCURRENCY) {
      const batch = hashes.slice(i, i + CONCURRENCY);
      results.push(...(await Promise.allSettled(batch.map(item.run))));
    }
    const failed = results.filter(function (r) {
      return r.status === "rejected";
    });
    if (!failed.length) {
      toast(item.label + ": " + hashes.length + " torrent(s)", "success");
    } else if (failed.length === hashes.length) {
      reportError(failed[0].reason);
    } else {
      toast(
        item.label + ": " + (hashes.length - failed.length) + " ok, " + failed.length + " failed",
        "warning",
        String((failed[0].reason && failed[0].reason.message) || failed[0].reason),
      );
    }
  }

  function runTorrentAction(id) {
    const item = QBX_TORRENT_ACTIONS.find(function (a) {
      return a.id === id;
    });
    if (!item) return;
    const hashes = selectedHashes();
    if (item.special === "openShell") {
      openShell(hashes[0] || "");
      return;
    }
    if (item.window) {
      openTorrentWindow(item.window, hashes[0] || "");
      return;
    }
    if (item.multi) {
      void runBulk(item, hashes);
      return;
    }
    void runForSelection(item.label, function (hash) {
      return item.run(hash);
    });
  }

  const actions = {};
  QBX_TORRENT_ACTIONS.forEach(function (item) {
    actions[item.id] = function () {
      runTorrentAction(item.id);
    };
  });

  // --- Readiness gate ------------------------------------------------------
  // This script is `defer`red, so it runs BEFORE DOMContentLoaded — and
  // client.js's own DOMContentLoaded handler awaits several things before it
  // builds the tab columns. Neither DOMContentLoaded nor load is a safe hook;
  // poll for the specific globals the tab integration actually needs.
  function whenReady(cb) {
    function ready() {
      return (
        window.MochaUI &&
        MochaUI.Desktop &&
        MochaUI.Desktop.pageWrapper &&
        document.getElementById("mainColumn") && // only exists after buildTransfersTab()
        document.getElementById("mainWindowTabsList") &&
        typeof MochaUI.selected === "function"
      );
    }
    if (ready()) {
      cb();
      return;
    }
    let tries = 0;
    // client.js awaits several qBittorrent API calls (preferences, categories,
    // ...) before it builds mainColumn, and those calls get slower as the
    // real torrent count grows — a library of thousands of torrents can take
    // tens of seconds longer to reach this point than an empty test instance.
    // Budget generously (~90s) rather than giving up early: a late qbx tab
    // beats a permanently absent one, and the toast still fires if it
    // genuinely never arrives.
    const MAX_TRIES = 1800;
    const id = setInterval(function () {
      if (ready()) {
        clearInterval(id);
        cb();
      } else if (++tries > MAX_TRIES) {
        // Loud failure, not silent: this is the exact bug class this
        // integration exists to stop repeating.
        clearInterval(id);
        toast("qbx integration failed to attach", "error", "The qBittorrent WebUI may have changed.");
      }
    }, 50);
  }

  // --- Bridge (postMessage to/from the embedded React iframe) -------------
  // Envelope is always { v: 1, type, ...payload }, posted with an explicit
  // target origin. This WebUI hosts several other same-origin iframes (Add
  // Torrent, Preferences, ...), so origin alone would let any of them spoof
  // the qbx iframe — every handler also checks ev.source.
  let qbxIframeEl = null; // the persistent tab iframe
  const qbxWindows = {}; // windowId -> { el, iframe }

  function qbxIframeWindow() {
    return qbxIframeEl && qbxIframeEl.contentWindow;
  }

  function postToIframe(win, message) {
    if (!win) return;
    try {
      win.postMessage(Object.assign({ v: 1 }, message), window.location.origin);
    } catch (_) {}
  }

  function postToTab(message) {
    postToIframe(qbxIframeWindow(), message);
  }

  function currentTheme() {
    return document.documentElement.classList.contains("dark") ? "dark" : "light";
  }

  function findWindowIdForSource(source) {
    for (const id in qbxWindows) {
      if (qbxWindows[id].iframe && qbxWindows[id].iframe.contentWindow === source) return id;
    }
    return null;
  }

  function closeQbxWindow(id) {
    const entry = qbxWindows[id];
    if (!entry) return;
    delete qbxWindows[id];
    try {
      if (window.MochaUI && typeof MochaUI.closeWindow === "function" && entry.el) {
        MochaUI.closeWindow(entry.el);
      } else if (entry.el) {
        entry.el.remove();
      }
    } catch (err) {
      console.error("[qbx] failed to close window", err);
    }
  }

  /** Open a qbx panel in a native-feeling draggable window, mirroring the
   * pattern this WebUI already uses for Add Torrent / Preferences. */
  function openQbxWindow(kind, opts) {
    opts = opts || {};
    const id = "qbxWindow_" + kind + (opts.hash ? "_" + opts.hash.slice(0, 8) : "");
    if (qbxWindows[id]) {
      try {
        if (window.MochaUI && typeof MochaUI.focusWindow === "function") {
          MochaUI.focusWindow(qbxWindows[id].el);
          return;
        }
      } catch (_) {}
    }
    if (!window.MochaUI || typeof MochaUI.Window !== "function") {
      toast("qbx integration not ready yet", "error");
      return;
    }
    const q = new URLSearchParams({ panel: kind, theme: currentTheme() });
    if (opts.hash) q.set("hash", opts.hash);
    if (opts.section) q.set("section", opts.section);
    const titles = { settings: "qbx: Settings", match: "qbx: Match files", debrid: "qbx: Debrid" };
    try {
      new MochaUI.Window({
        id: id,
        title: titles[kind] || "qbx",
        loadMethod: "iframe",
        contentURL: "/embed?" + q.toString(),
        scrollbars: false,
        maximizable: true,
        closable: true,
        paddingVertical: 0,
        paddingHorizontal: 0,
        width: kind === "settings" ? 900 : 760,
        height: kind === "settings" ? 640 : 520,
        onCloseComplete: function () {
          delete qbxWindows[id];
        },
      });
      const el = document.getElementById(id);
      const iframe = el && el.querySelector("iframe");
      qbxWindows[id] = { el: el, iframe: iframe };
    } catch (err) {
      console.error("[qbx] failed to open window", err);
      toast("Could not open qbx window", "error", String((err && err.message) || err));
    }
  }

  /**
   * A small native window for setting localStorage["qbx_token"] without
   * leaving the WebUI. Neither side caches the token — both this script and
   * the embedded React app read localStorage per request — so saving here
   * takes effect on the very next call with no extra signalling needed.
   */
  function openTokenPrompt() {
    const id = "qbxTokenPrompt";
    if (document.getElementById(id)) {
      try {
        MochaUI.focusWindow(document.getElementById(id));
      } catch (_) {}
      return;
    }
    if (!window.MochaUI || typeof MochaUI.Window !== "function") {
      toast("qbx integration not ready yet", "error");
      return;
    }
    const html = [
      '<div class="qbxTokenForm">',
      "<p>qbx needs an API token to reach its endpoints from this WebUI.</p>",
      '<input type="password" id="qbxTokenInput" autocomplete="off" placeholder="API token">',
      '<div class="qbxTokenActions">',
      '<button type="button" id="qbxTokenCancel">Cancel</button>',
      '<button type="button" id="qbxTokenSave">Save</button>',
      "</div>",
      "</div>",
    ].join("");
    try {
      new MochaUI.Window({
        id: id,
        title: "qbx: API token",
        loadMethod: "html",
        content: html,
        width: 360,
        height: 170,
        closable: true,
        maximizable: false,
      });
    } catch (err) {
      console.error("[qbx] failed to open token prompt", err);
      return;
    }
    const input = document.getElementById("qbxTokenInput");
    if (input) input.value = token();
    const finish = function () {
      try {
        MochaUI.closeWindow(document.getElementById(id));
      } catch (_) {}
    };
    const save = document.getElementById("qbxTokenSave");
    if (save) {
      save.addEventListener("click", function () {
        const value = ((input && input.value) || "").trim();
        try {
          if (value) localStorage.setItem(TOKEN_KEY, value);
          else localStorage.removeItem(TOKEN_KEY);
        } catch (_) {}
        toast("qbx API token saved", "success");
        finish();
      });
    }
    const cancel = document.getElementById("qbxTokenCancel");
    if (cancel) cancel.addEventListener("click", finish);
  }

  function handleShellMessage(ev, msg) {
    switch (msg.type) {
      case "qbx.ready": {
        const win = ev.source;
        postToIframe(win, {
          type: "qbx.host.hello",
          theme: currentTheme(),
          selection: selectedHashes(),
          activeHash: firstHash() || null,
        });
        break;
      }
      case "qbx.toast":
        toast(msg.message, msg.level, msg.detail);
        break;
      case "qbx.selectTorrent":
        if (msg.hash) selectTorrentRow(msg.hash);
        break;
      case "qbx.openWindow":
        openQbxWindow(msg.window, { hash: msg.hash, section: msg.section });
        break;
      case "qbx.closeWindow": {
        const winId = findWindowIdForSource(ev.source);
        if (winId) closeQbxWindow(winId);
        break;
      }
      case "qbx.switchTab": {
        const linkId =
          msg.tab === "qbx" ? "qbxTabLink" : msg.tab === "transfers" ? "transfersTabLink" : msg.tab + "TabLink";
        const link = document.getElementById(linkId);
        if (link) link.click();
        break;
      }
      case "qbx.error":
        reportError({ status: msg.status, message: msg.message });
        break;
      default:
        break;
    }
  }

  window.addEventListener("message", function (ev) {
    if (ev.origin !== window.location.origin) return;
    const data = ev.data;
    if (!data || typeof data.type !== "string") return;

    // Pre-bridge shape, still emitted by the standalone shell at "/". Only
    // meaningful when it is not our own tab iframe talking to itself.
    if (data.type === "qbx.selectTorrent" && data.hash && ev.source !== qbxIframeWindow()) {
      selectTorrentRow(data.hash);
      return;
    }

    // Everything else must come from a frame we actually own: the tab iframe
    // or one of our modal windows. This WebUI hosts other same-origin
    // iframes (Add Torrent, Preferences, ...) that must never be able to
    // drive qbx.
    const fromTab = ev.source === qbxIframeWindow();
    const fromWindow = findWindowIdForSource(ev.source) !== null;
    if (!fromTab && !fromWindow) return;

    handleShellMessage(ev, data);
  });

  // --- The qbx tab ----------------------------------------------------------
  const QBX_TAB_ACTIVE_KEY = "qbx_tab_active"; // never write to selected_window_tab

  function installQbxTab() {
    const tabsList = document.getElementById("mainWindowTabsList");
    if (!tabsList || document.getElementById("qbxTabLink")) return;

    try {
      new MochaUI.Column({ id: "qbxTabColumn", placement: "main", width: null });
    } catch (err) {
      console.error("[qbx] could not create tab column", err);
      toast("qbx tab failed to attach", "error");
      return;
    }
    const column = document.getElementById("qbxTabColumn");
    if (!column) return;
    column.classList.add("invisible");

    const li = document.createElement("li");
    li.id = "qbxTabLink";
    const a = document.createElement("a");
    const img = document.createElement("img");
    img.src = "/qbx/icon.svg";
    img.width = 16;
    img.height = 16;
    img.alt = "";
    img.onerror = function () {
      img.remove();
    };
    a.appendChild(img);
    a.appendChild(document.createTextNode("qbx"));
    li.appendChild(a);
    tabsList.appendChild(li);

    li.addEventListener("click", function () {
      try {
        MochaUI.selected(li, tabsList);
      } catch (_) {}
      showQbxTab();
    });

    // Native tab clicks were wired before ours existed, so their show/hide
    // closures have no idea we exist and cannot hide our column. Do it here.
    ["transfersTabLink", "searchTabLink", "rssTabLink", "logTabLink"].forEach(function (id) {
      const el = document.getElementById(id);
      if (el) el.addEventListener("click", hideQbxTab);
    });

    // updateTabDisplay() (client.js) hides #mainWindowTabs entirely when
    // Search + RSS + Log are all disabled. Our tab must survive that.
    const tabsWrap = document.getElementById("mainWindowTabs");
    if (tabsWrap) {
      tabsWrap.classList.remove("invisible");
      new MutationObserver(function () {
        if (tabsWrap.classList.contains("invisible")) tabsWrap.classList.remove("invisible");
      }).observe(tabsWrap, { attributes: true, attributeFilter: ["class"] });
    }

    wrapSelectionSync();
    watchTheme();

    if (localStorage.getItem(QBX_TAB_ACTIVE_KEY) === "1") {
      setTimeout(function () {
        li.click();
      }, 0);
    }
  }

  let qbxTabBuilt = false;

  function showQbxTab(panel, hash) {
    if (!qbxTabBuilt) {
      const q = new URLSearchParams({ panel: panel || "overview", theme: currentTheme() });
      if (hash) q.set("hash", hash);
      qbxIframeEl = document.createElement("iframe");
      qbxIframeEl.src = "/embed?" + q.toString();
      document.getElementById("qbxTabColumn").appendChild(qbxIframeEl);
      qbxTabBuilt = true;
    } else {
      // Iframe already exists and has presumably already said qbx.ready; if it
      // hasn't, these are simply ignored until it does — a stale panel/
      // selection is a much smaller problem than racing iframe creation.
      if (panel) postToTab({ type: "qbx.host.panel", panel: panel });
      if (hash) postToTab({ type: "qbx.host.selection", selection: [hash], activeHash: hash });
    }
    document.getElementById("qbxTabColumn").classList.remove("invisible");
    [
      "filtersColumn",
      "filtersColumn_handle",
      "mainColumn",
      "torrentsFilterToolbar",
      "searchTabColumn",
      "rssTabColumn",
      "logTabColumn",
    ].forEach(function (id) {
      const el = document.getElementById(id);
      if (el) el.classList.add("invisible");
    });
    try {
      MochaUI.Desktop.setDesktopSize();
    } catch (_) {}
    localStorage.setItem(QBX_TAB_ACTIVE_KEY, "1");
    postToTab({ type: "qbx.host.activated" });
  }

  function hideQbxTab() {
    const column = document.getElementById("qbxTabColumn");
    if (!column) return;
    column.classList.add("invisible");
    try {
      MochaUI.Desktop.setDesktopSize();
    } catch (_) {}
    localStorage.setItem(QBX_TAB_ACTIVE_KEY, "0");
    postToTab({ type: "qbx.host.deactivated" });
  }

  /** Relay qBittorrent's own selection changes to the embedded iframe. */
  function wrapSelectionSync() {
    const table = window.torrentsTable;
    if (!table || typeof table.onSelectedRowChanged !== "function" || table.__qbxWrapped) return;
    table.__qbxWrapped = true;
    const original = table.onSelectedRowChanged.bind(table);
    let timer = null;
    table.onSelectedRowChanged = function () {
      original();
      clearTimeout(timer);
      timer = setTimeout(function () {
        postToTab({
          type: "qbx.host.selection",
          selection: selectedHashes(),
          activeHash: firstHash() || null,
        });
      }, 100);
    };
  }

  /** Follow qBittorrent's own light/dark toggle (color-scheme.js). */
  function watchTheme() {
    new MutationObserver(function () {
      const theme = currentTheme();
      postToTab({ type: "qbx.host.theme", theme: theme });
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  }

  // Two buttons only — everything else lives in the qbx menu and the
  // context-menu submenu. #mochaToolbar is already crowded and does not wrap.
  // Icons are existing WebUI assets; branded icons are a later polish pass.
  const QBX_TOOLBAR_BUTTONS = [
    { id: "qbxDebridButton", action: "qbxDebrid", label: "qbx: Send to debrid", icon: "images/torrent-magnet.svg" },
    { id: "qbxMatchButton", action: "qbxMatch", label: "qbx: Match files…", icon: "images/edit-find.svg" },
  ];

  function addToolbar() {
    const bar = document.getElementById("mochaToolbar");
    const tabs = document.getElementById("mainWindowTabs");
    if (!bar || document.getElementById(QBX_TOOLBAR_BUTTONS[0].id)) return;
    QBX_TOOLBAR_BUTTONS.forEach(function (btn, i) {
      const a = document.createElement("a");
      a.id = btn.id;
      if (i === 0) a.className = "divider";
      const img = document.createElement("img");
      img.className = "mochaToolButton";
      img.src = btn.icon;
      img.title = btn.label;
      img.alt = btn.label;
      img.width = 24;
      img.height = 24;
      a.appendChild(img);
      a.addEventListener("click", function () {
        actions[btn.action]();
      });
      if (tabs) bar.insertBefore(a, tabs);
      else bar.appendChild(a);
    });
  }

  function addContextMenuItems() {
    const tl = window.qBittorrent && window.qBittorrent.TransferList;
    if (!tl || !tl.contextMenu) return false; // views/transferlist.html loads over xhr; not ready yet
    const menu = document.getElementById("torrentsTableMenu");
    if (!menu || document.getElementById("qbxMenuRoot")) return true;

    const sep = document.createElement("li");
    sep.className = "separator";
    menu.appendChild(sep);

    // One submenu, native flyout via the existing .arrow-right + nested <ul>
    // pattern (see Queue, Category, Tags in this same menu) — nothing extra
    // to wire, the WebUI's own CSS handles the hover reveal.
    const root = document.createElement("li");
    root.id = "qbxMenuRoot";
    const rootLink = document.createElement("a");
    rootLink.href = "#qbxRoot"; // no action registered under this id: intentional no-op
    rootLink.className = "arrow-right";
    rootLink.appendChild(document.createTextNode("qbx"));
    const sub = document.createElement("ul");
    QBX_TORRENT_ACTIONS.forEach(function (item) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = "#" + item.id;
      a.textContent = item.label;
      li.appendChild(a);
      sub.appendChild(li);
    });
    root.appendChild(rootLink);
    root.appendChild(sub);
    menu.appendChild(root);

    // Route through the WebUI's own delegated dispatcher (contextmenu.js
    // ContextMenu.startListener()) instead of ad-hoc listeners — the old
    // per-anchor listeners fought that dispatcher instead of using it.
    Object.assign(tl.contextMenu.options.actions, actions);

    const previousUpdate = tl.contextMenu.updateMenuItems
      ? tl.contextMenu.updateMenuItems.bind(tl.contextMenu)
      : null;
    tl.contextMenu.updateMenuItems = function () {
      if (previousUpdate) previousUpdate();
      const n = selectedHashes().length;
      QBX_TORRENT_ACTIONS.forEach(function (item) {
        try {
          tl.contextMenu.setEnabled(item.id, item.multi ? n >= 1 : n === 1);
        } catch (_) {}
      });
    };
    return true;
  }

  // --- Menubar --------------------------------------------------------------
  function menuIcon(label) {
    const img = document.createElement("img");
    img.className = "MyMenuIcon";
    img.src = "/qbx/icon.svg";
    img.width = 16;
    img.height = 16;
    img.alt = label;
    img.onerror = function () {
      img.remove();
    };
    return img;
  }

  function menuItem(label, onClick, opts) {
    opts = opts || {};
    const li = document.createElement("li");
    if (opts.divider) li.className = "divider";
    const a = document.createElement("a");
    a.appendChild(menuIcon(label));
    a.appendChild(document.createTextNode(label));
    a.addEventListener("click", onClick);
    li.appendChild(a);
    return li;
  }

  async function toggleInterceptor() {
    try {
      const status = await api("/api/interceptor/status");
      if (status && status.running) {
        await api("/api/interceptor/stop", { method: "POST", body: "{}" });
        toast("Interceptor stopped", "success");
      } else {
        await api("/api/interceptor/start", { method: "POST", body: "{}" });
        toast("Interceptor started", "success");
      }
    } catch (err) {
      reportError(err);
    }
  }

  async function runPolicyScan() {
    try {
      await api("/api/interceptor/scan", { method: "POST", body: "{}" });
      toast("Policy scan queued", "success");
    } catch (err) {
      reportError(err);
    }
  }

  async function scanStorage() {
    try {
      await api("/api/storage/scan", { method: "POST", body: "{}" });
      toast("Storage scan queued", "success");
    } catch (err) {
      reportError(err);
    }
  }

  function openQbxTabPanel(panel) {
    const link = document.getElementById("qbxTabLink");
    const tabsList = document.getElementById("mainWindowTabsList");
    if (!link) {
      toast("qbx tab not available yet", "error");
      return;
    }
    if (tabsList) {
      try {
        MochaUI.selected(link, tabsList);
      } catch (_) {}
    }
    showQbxTab(panel);
  }

  function installMenubar() {
    const navList = document.querySelector("#desktopNavbar > ul");
    if (!navList || document.getElementById("qbxMenuTop")) return;

    const top = document.createElement("li");
    top.id = "qbxMenuTop";
    const trigger = document.createElement("a");
    trigger.className = "returnFalse";
    trigger.textContent = "qbx";
    const sub = document.createElement("ul");

    sub.appendChild(menuItem("Overview", () => openQbxTabPanel("overview")));
    sub.appendChild(menuItem("Storage", () => openQbxTabPanel("storage")));
    sub.appendChild(menuItem("Live log", () => openQbxTabPanel("log")));
    sub.appendChild(menuItem("Settings…", () => openQbxWindow("settings"), { divider: true }));
    sub.appendChild(menuItem("Matcher rules…", () => openQbxWindow("settings", { section: "matcher" })));
    sub.appendChild(menuItem("Start/stop interceptor", () => void toggleInterceptor(), { divider: true }));
    sub.appendChild(menuItem("Run policy scan", () => void runPolicyScan()));
    sub.appendChild(menuItem("Scan storage", () => void scanStorage()));
    sub.appendChild(menuItem("Set API token…", () => openTokenPrompt(), { divider: true }));

    top.appendChild(trigger);
    top.appendChild(sub);

    // Insert before Help (always the last top-level item) so it lands after
    // Tools, matching the plan's menu order.
    const help = navList.lastElementChild;
    if (help) navList.insertBefore(top, help);
    else navList.appendChild(top);
  }

  let contextMenuAttached = false;
  let contextMenuWarned = false;

  /**
   * Workaround for a vendored-WebUI bug, not something qbx caused: the
   * Filters panel (Status/Categories/Tags/Trackers sidebar) is built with
   * `header: false`, and whatever code path normally clears a panel's
   * `_pad` back to visible after its XHR content loads does not run for
   * that combination — every other panel (transfer list, log, RSS, search,
   * properties) shows fine; only #Filters_pad is left stuck at
   * `display:none`, even though its content loaded correctly underneath.
   * Confirmed independent of qbx: reproduces identically with qbx's own
   * script and stylesheet completely blocked.
   */
  function fixFiltersPanelVisibility() {
    const pad = document.getElementById("Filters_pad");
    if (pad && getComputedStyle(pad).display === "none" && pad.children.length > 0) {
      pad.style.display = "block";
    }
  }

  function boot() {
    addToolbar();
    if (!contextMenuAttached) contextMenuAttached = addContextMenuItems();
    installMenubar();
    fixFiltersPanelVisibility();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
  // views/transferlist.html loads its context menu over xhr — retry while it
  // does. A real qBittorrent instance with a large library answers every API
  // call more slowly than an empty one, so this needs real headroom: a
  // library of thousands of torrents was observed taking 30-40s longer to
  // reach this point than an empty test instance. Only warn on the final
  // attempt, and only if it genuinely never attached.
  [1500, 4000, 12000, 20000, 40000, 90000].forEach(function (delay, i, all) {
    setTimeout(function () {
      boot();
      const isLast = i === all.length - 1;
      if (isLast && !contextMenuAttached && !contextMenuWarned) {
        contextMenuWarned = true;
        toast(
          "qbx context menu items did not attach",
          "error",
          "The qBittorrent WebUI may not have finished loading its torrent list.",
        );
      }
    }, delay);
  });

  // The tab needs MochaUI.Desktop/Column, which are not ready at any of the
  // points above (see whenReady()'s doc comment).
  whenReady(installQbxTab);

  /** Select a row in qBittorrent's table and scroll it into view. */
  function selectTorrentRow(hash) {
    const table = window.torrentsTable;
    // reselectRows() is the real dynamicTable API. The previous code called a
    // by-id helper that does not exist on the table, inside an empty catch, so
    // this whole path failed silently.
    if (!table || typeof table.reselectRows !== "function") {
      toast("Cannot select torrent: table not ready", "error");
      return;
    }
    table.reselectRows([hash]);
    // reselectRows() sets the class but does not fire onSelectedRowChanged()
    // the way selectRow() does, so the detail panel below the table would go
    // stale. Nudge it explicitly.
    if (typeof table.onSelectedRowChanged === "function") table.onSelectedRowChanged();
    const row = document.querySelector(
      '#torrentsTableDiv tr[data-row-id="' + (window.CSS && CSS.escape ? CSS.escape(hash) : hash) + '"]',
    );
    if (row) row.scrollIntoView({ block: "nearest" });
  }

  window.addEventListener("message", function (ev) {
    if (ev.origin !== window.location.origin) return;
    if (ev.data && ev.data.type === "qbx.selectTorrent" && ev.data.hash) {
      selectTorrentRow(ev.data.hash);
    }
  });
})();
