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

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({}, opts || {}, { headers: headers() }));
    if (!res.ok) {
      const body = await res.json().catch(function () {
        return {};
      });
      throw new Error(body.detail || res.statusText || "request failed");
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

  function notify(msg, isError) {
    try {
      if (window.qBittorrent && window.qBittorrent.Client && window.qBittorrent.Client.showNotification) {
        window.qBittorrent.Client.showNotification(msg);
        return;
      }
    } catch (_) {}
    if (isError) console.error("[qbx]", msg);
    else console.info("[qbx]", msg);
    try {
      window.parent.postMessage({ type: "qbx.toast", message: msg, error: !!isError }, window.location.origin);
    } catch (_) {}
  }

  async function runForSelection(label, fn) {
    const hash = firstHash();
    if (!hash) {
      notify("Select a torrent first", true);
      return;
    }
    try {
      await fn(hash);
      notify(label + " — " + hash.slice(0, 10));
      try {
        window.parent.postMessage({ type: "qbx.selectTorrent", hash: hash }, window.location.origin);
      } catch (_) {}
    } catch (err) {
      notify(String(err.message || err), true);
    }
  }

  const actions = {
    qbxMatch: function () {
      runForSelection("Open match", function (hash) {
        window.open("/?view=match&hash=" + encodeURIComponent(hash), "_blank");
        return Promise.resolve();
      });
    },
    qbxDebrid: function () {
      runForSelection("Force debrid", function (hash) {
        return api("/api/torrents/" + encodeURIComponent(hash) + "/intercept", {
          method: "POST",
          body: "{}",
        });
      });
    },
    qbxRetry: function () {
      runForSelection("Retry", function (hash) {
        return api("/api/torrents/" + encodeURIComponent(hash) + "/retry", {
          method: "POST",
          body: "{}",
        });
      });
    },
    qbxSkip: function () {
      runForSelection("Skip auto", function (hash) {
        return api("/api/torrents/" + encodeURIComponent(hash) + "/skip-auto", {
          method: "POST",
          body: "{}",
        });
      });
    },
    qbxOpenShell: function () {
      runForSelection("Open Control Shell", function (hash) {
        window.open("/?hash=" + encodeURIComponent(hash), "_blank");
        return Promise.resolve();
      });
    },
  };

  function addToolbar() {
    const bar = document.getElementById("mochaToolbar") || document.querySelector(".toolbar");
    if (!bar || document.getElementById("qbxToolbar")) return;
    const wrap = document.createElement("div");
    wrap.id = "qbxToolbar";
    wrap.style.cssText = "display:inline-flex;gap:4px;margin-left:8px;align-items:center;";
    const buttons = [
      ["Match files", "qbxMatch"],
      ["Force debrid", "qbxDebrid"],
      ["Open qbx", "qbxOpenShell"],
    ];
    buttons.forEach(function (pair) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = pair[0];
      btn.title = "qbx: " + pair[0];
      btn.style.cssText =
        "font-size:11px;padding:2px 8px;cursor:pointer;border:1px solid #555;background:#222;color:#eee;border-radius:4px;";
      btn.addEventListener("click", function () {
        actions[pair[1]]();
      });
      wrap.appendChild(btn);
    });
    bar.appendChild(wrap);
  }

  function addContextMenuItems() {
    const menu = document.getElementById("torrentsTableMenu");
    if (!menu || document.getElementById("qbxMenuSep")) return;
    const sep = document.createElement("li");
    sep.id = "qbxMenuSep";
    sep.className = "separator";
    menu.appendChild(sep);
    const items = [
      ["qbxMatch", "qbx: Match files"],
      ["qbxDebrid", "qbx: Force debrid"],
      ["qbxRetry", "qbx: Retry failed"],
      ["qbxSkip", "qbx: Skip auto-debrid"],
      ["qbxOpenShell", "qbx: Show in Control Shell"],
    ];
    items.forEach(function (pair) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = "#" + pair[0];
      a.textContent = pair[1];
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        actions[pair[0]]();
      });
      li.appendChild(a);
      menu.appendChild(li);
    });
  }

  function wireTransferlistActions() {
    // Best-effort: hook into MochaUI action map if present after load.
    try {
      if (window.torrentsTableActions) {
        Object.keys(actions).forEach(function (k) {
          window.torrentsTableActions[k] = actions[k];
        });
      }
    } catch (_) {}
  }

  function boot() {
    addToolbar();
    addContextMenuItems();
    wireTransferlistActions();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
  // WebUI builds menus late — retry a few times.
  setTimeout(boot, 1500);
  setTimeout(boot, 4000);

  window.addEventListener("message", function (ev) {
    if (ev.origin !== window.location.origin) return;
    if (ev.data && ev.data.type === "qbx.selectTorrent" && ev.data.hash) {
      try {
        if (window.torrentsTable && typeof window.torrentsTable.selectRowById === "function") {
          window.torrentsTable.selectRowById(ev.data.hash);
        }
      } catch (_) {}
    }
  });
})();
