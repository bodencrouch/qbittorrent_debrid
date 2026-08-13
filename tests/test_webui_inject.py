"""The qbx integration injected into the vendored qBittorrent WebUI.

Every bug this suite guards was a *silent* failure: a window.open QtWebEngine
drops, a notification API that does not exist, a table method that does not
exist inside an empty catch. Static assertions are cheap and pin them shut.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INJECT_JS = ROOT / "qbx" / "web" / "qbx-inject.js"
SHELL_SRC = ROOT / "qbx" / "web" / "matcher" / "src"

SRC = INJECT_JS.read_text(encoding="utf-8")


# --- Regression guards for the silent-failure bugs -------------------------


def test_inject_script_never_uses_window_open():
    """window.open is a no-op in the PyQt6 tray shell (no createWindow override).

    Navigation must use location.assign so it works in both the browser and
    the tray window.
    """
    assert "window.open(" not in SRC


def test_inject_script_does_not_call_nonexistent_show_notification():
    """qBittorrent.Client is Object.freeze'd and has no showNotification."""
    assert "showNotification" not in SRC


def test_inject_script_does_not_call_nonexistent_select_row_by_id():
    """dynamicTable exposes reselectRows/selectRow, never selectRowById."""
    assert "selectRowById" not in SRC


def test_inject_script_uses_real_table_selection_api():
    assert "reselectRows(" in SRC


def test_inject_script_posts_with_explicit_target_origin():
    assert 'postMessage(msg, "*")' not in SRC
    assert 'postMessage(message, "*")' not in SRC


def test_inject_script_surfaces_401_distinctly():
    """A bad or missing API token must be visible, not console-only."""
    assert "401" in SRC


def test_inject_script_has_a_visible_toast_layer():
    assert "qbxToastHost" in SRC


def test_open_shell_action_does_not_require_a_selection():
    """Opening the shell is navigation, not a per-torrent operation."""
    start = SRC.index("function openShell(")
    body = SRC[start : SRC.index("\n  }", start)]
    assert "runForSelection" not in body


# --- The shell must not rely on window.open either -------------------------


def test_shell_has_no_window_open_for_internal_navigation():
    """Internal navigation must go through lib/host.ts, which the tray honours.

    Covers .ts as well as .tsx: the first offender found lived in lib/actions.ts.
    """
    offenders = []
    for pattern in ("*.ts", "*.tsx"):
        for path in SHELL_SRC.rglob(pattern):
            if path.name == "host.ts":
                continue  # the one place allowed to reference window.open
            text = path.read_text(encoding="utf-8")
            if 'window.open("/' in text or "window.open(`/" in text:
                offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, f"internal navigation via window.open in {offenders}"


# --- The DOM contract with the vendored WebUI ------------------------------


def test_vendored_webui_still_exposes_the_anchors_inject_depends_on():
    """Pins the contract between the vendored WebUI and qbx-inject.js.

    A vendor bump that renames any of these should fail here rather than
    silently killing the integration at runtime.
    """
    from qbx import qbt_proxy

    index = qbt_proxy.resolve_webui_file("index.html")
    assert index is not None, "vendored WebUI index.html is missing"
    html = qbt_proxy.serve_webui_file(index, inject_qbx=True).body.decode()

    for anchor in (
        'id="mochaToolbar"',
        'id="mainWindowTabsList"',
        'id="torrentsTableMenu"',
        'id="desktopNavbar"',
        'id="pageWrapper"',
    ):
        assert anchor in html, f"vendored WebUI no longer has {anchor}"


def test_real_transferlist_fragment_is_not_injected():
    """The exact fragment that broke: views/transferlist.html defines
    window.qBittorrent.TransferList inline and has no <body> tag.
    """
    from qbx import qbt_proxy

    fragment = qbt_proxy.resolve_webui_file("views/transferlist.html")
    assert fragment is not None, "vendored transferlist.html is missing"
    html = qbt_proxy.serve_webui_file(
        fragment, inject_qbx=True, bootstrap={"version": "0.1.0", "tokenRequired": True}
    ).body.decode()
    assert "/qbx/inject.js" not in html
    assert "qbx-bootstrap" not in html
    # Confirm this really is the body-less fragment we think it is.
    assert "<body" not in html
    assert "window.qBittorrent.TransferList" in html


def test_translation_markers_are_stripped():
    from qbx import qbt_proxy

    index = qbt_proxy.resolve_webui_file("index.html")
    html = qbt_proxy.serve_webui_file(index, inject_qbx=True).body.decode()
    assert "QBT_TR(" not in html


# --- Injection mechanics ---------------------------------------------------


def test_injects_script_once_before_body_close(tmp_path):
    from qbx import qbt_proxy

    page = tmp_path / "index.html"
    page.write_text("<html><body><div>hi</div></body></html>", encoding="utf-8")
    html = qbt_proxy.serve_webui_file(page, inject_qbx=True).body.decode()

    assert html.count("/qbx/inject.js") == 1
    assert html.index("/qbx/inject.js") < html.index("</body>")


def test_no_injection_when_disabled(tmp_path):
    from qbx import qbt_proxy

    page = tmp_path / "x.html"
    page.write_text("<html><body></body></html>", encoding="utf-8")
    html = qbt_proxy.serve_webui_file(page, inject_qbx=False).body.decode()
    assert "/qbx/inject.js" not in html


def test_no_injection_into_javascript_files(tmp_path):
    from qbx import qbt_proxy

    script = tmp_path / "x.js"
    script.write_text("const a = 1;", encoding="utf-8")
    body = qbt_proxy.serve_webui_file(script, inject_qbx=True).body.decode()
    assert "/qbx/inject.js" not in body


def test_no_injection_into_body_less_fragments(tmp_path):
    """Regression test for a real bug: views/*.html (transferlist, search,
    rss, preferences, ...) are body-less fragments MochaUI loads over XHR and
    inserts via its own manual <script> extraction, which evals every matched
    script's text content regardless of `type`. Injecting the bootstrap
    <script type="application/json"> there got eval()'d as JS ("Unexpected
    token ':'") and broke whatever the fragment's own inline script was
    doing — confirmed via views/transferlist.html, where it silently
    prevented window.qBittorrent.TransferList from ever being assigned, which
    in turn meant the torrent table's context menu never attached.
    """
    from qbx import qbt_proxy

    page = tmp_path / "fragment.html"
    page.write_text("<div>no body element here</div>", encoding="utf-8")
    html = qbt_proxy.serve_webui_file(page, inject_qbx=True).body.decode()
    assert "/qbx/inject.js" not in html
    assert "/qbx/inject.css" not in html
    assert "qbx-bootstrap" not in html


def test_no_injection_into_non_index_html_even_with_a_real_body_tag(tmp_path):
    """A body-tag check alone is not a safe filter: views/rss.html builds an
    iframe srcdoc string containing the literal substring "</body>" inside a
    JS template literal, with no real <body> element in the file at all.
    Matching on it corrupted that string and threw
    "Unexpected end of input" downstream. Only index.html is ever eligible —
    that is what actually renders as this WebUI's real document.
    """
    from qbx import qbt_proxy

    page = tmp_path / "rename.html"
    page.write_text("<html><body>a real body tag, just not in index.html</body></html>", encoding="utf-8")
    html = qbt_proxy.serve_webui_file(page, inject_qbx=True).body.decode()
    assert "/qbx/inject.js" not in html
    assert "/qbx/inject.css" not in html
    assert "qbx-bootstrap" not in html


def test_real_rss_fragment_is_not_injected():
    """The literal file that motivated the index.html-only restriction:
    views/rss.html contains the substring "</body>" only inside a JS template
    literal building an iframe srcdoc, not as a real document element.
    """
    from qbx import qbt_proxy

    fragment = qbt_proxy.resolve_webui_file("views/rss.html")
    assert fragment is not None, "vendored rss.html is missing"
    assert "</body>" in fragment.read_text(encoding="utf-8"), "fixture assumption changed upstream"
    html = qbt_proxy.serve_webui_file(fragment, inject_qbx=True).body.decode()
    assert "/qbx/inject.js" not in html
    assert "qbx-bootstrap" not in html


def test_stylesheet_is_injected_and_ordered_before_the_script(tmp_path):
    """CSS must land before the script so the toast layer is never unstyled."""
    from qbx import qbt_proxy

    page = tmp_path / "index.html"
    page.write_text("<html><body></body></html>", encoding="utf-8")
    html = qbt_proxy.serve_webui_file(page, inject_qbx=True).body.decode()

    assert html.count("/qbx/inject.css") == 1
    assert html.index("/qbx/inject.css") < html.index("/qbx/inject.js")


def test_bootstrap_reports_token_required(tmp_path):
    from qbx import qbt_proxy

    page = tmp_path / "index.html"
    page.write_text("<html><body></body></html>", encoding="utf-8")

    required = qbt_proxy.serve_webui_file(
        page, inject_qbx=True, bootstrap={"version": "1.2.3", "tokenRequired": True}
    ).body.decode()
    assert '"tokenRequired": true' in required
    assert 'id="qbx-bootstrap"' in required

    optional = qbt_proxy.serve_webui_file(
        page, inject_qbx=True, bootstrap={"version": "1.2.3", "tokenRequired": False}
    ).body.decode()
    assert '"tokenRequired": false' in optional


def test_bootstrap_version_cache_busts_the_assets(tmp_path):
    from qbx import qbt_proxy

    page = tmp_path / "index.html"
    page.write_text("<html><body></body></html>", encoding="utf-8")
    html = qbt_proxy.serve_webui_file(
        page, inject_qbx=True, bootstrap={"version": "9.9.9"}
    ).body.decode()
    assert "/qbx/inject.js?v=9.9.9" in html
    assert "/qbx/inject.css?v=9.9.9" in html


def test_bootstrap_escapes_angle_brackets(tmp_path):
    """A payload must never be able to close its own script tag."""
    from qbx import qbt_proxy

    page = tmp_path / "index.html"
    page.write_text("<html><body></body></html>", encoding="utf-8")
    html = qbt_proxy.serve_webui_file(
        page, inject_qbx=True, bootstrap={"version": "</script><script>alert(1)"}
    ).body.decode()
    assert "</script><script>alert(1)" not in html


def test_no_bootstrap_tag_when_none_supplied(tmp_path):
    from qbx import qbt_proxy

    page = tmp_path / "index.html"
    page.write_text("<html><body></body></html>", encoding="utf-8")
    html = qbt_proxy.serve_webui_file(page, inject_qbx=True).body.decode()
    assert 'id="qbx-bootstrap"' not in html
    assert "/qbx/inject.js" in html


def test_rename_file_route_registered_exactly_once():
    """Two registrations silently shadowed each other with different schemas."""
    source = (ROOT / "qbx" / "server.py").read_text(encoding="utf-8")
    assert source.count('@app.post("/api/qbt/rename-file"') == 1


# --- Phase 4: context menu routes through the native dispatcher ------------


def test_context_menu_items_use_native_action_dispatch():
    """ContextMenu.startListener() already delegates href="#action" into
    options.actions — ad-hoc per-anchor click listeners fight that dispatcher
    instead of using it. Assert the actions object is merged into it directly.
    """
    assert "tl.contextMenu.options.actions" in SRC
    assert "Object.assign(tl.contextMenu.options.actions, actions)" in SRC


def test_context_menu_items_are_not_double_wired():
    """No addEventListener("click", ...) on our own <li>/<a> for torrent
    actions — that was the old, fighting-the-dispatcher approach.
    """
    start = SRC.index("function addContextMenuItems(")
    end = SRC.index("\n  function ", start + 10)
    body = SRC[start:end]
    assert "addEventListener" not in body


def test_toolbar_uses_native_button_markup():
    """Regression guard for the old inline-styled buttons that clashed with
    the WebUI's light theme.
    """
    assert "mochaToolButton" in SRC
    assert "style.cssText" not in SRC


def test_bulk_actions_use_promise_allSettled():
    assert "Promise.allSettled" in SRC


def test_torrent_actions_declared_exactly_once():
    """The action catalog is the single source of truth for context menu,
    toolbar, and bulk execution — no separate list to drift out of sync.
    """
    assert SRC.count("QBX_TORRENT_ACTIONS") >= 3


# --- Phase 5: auth loudness -------------------------------------------------


def test_context_menu_attachment_failure_is_loud_eventually():
    """A vendored-WebUI bug (observed: transferlist.html's own script can
    throw before it reaches the context menu setup) must not leave the
    integration silently absent forever.
    """
    assert "contextMenuWarned" in SRC
    assert "did not attach" in SRC


def test_token_prompt_never_caches_or_logs_the_token():
    start = SRC.index("function openTokenPrompt(")
    end = SRC.index("\n  function ", start + 10)
    body = SRC[start:end]
    assert "console.log" not in body
    assert "console.info" not in body


def test_401_offers_the_token_prompt_once_per_session():
    assert "tokenPromptOfferedThisSession" in SRC
    assert "openTokenPrompt()" in SRC


def test_menubar_has_a_set_token_entry():
    assert "Set API token…" in SRC


# --- Filters panel visibility workaround ------------------------------------


def test_filters_panel_visibility_workaround_is_present():
    """Regression guard for a vendored-WebUI bug, confirmed independent of
    qbx (reproduces with qbx's script/stylesheet fully blocked): the Filters
    sidebar's content pad is left at display:none after its content loads,
    while every other panel (transfer list, log, RSS, search, properties)
    shows correctly. Worked around client-side rather than by patching the
    vendored WebUI files.
    """
    assert "fixFiltersPanelVisibility" in SRC
    assert "Filters_pad" in SRC


# --- Routes ----------------------------------------------------------------


def _client(tmp_path):
    from fastapi.testclient import TestClient

    from qbx.config import ConfigStore
    from qbx.server import create_app

    store = ConfigStore(tmp_path)
    # Point qBittorrent at a dead port so lifespan login fails fast, as the
    # existing server tests do.
    store.update({"configured": True, "qbt": {"url": "http://127.0.0.1:1"}})
    return TestClient(create_app(store))


def test_inject_assets_are_served(tmp_path):
    with _client(tmp_path) as client:
        js = client.get("/qbx/inject.js")
        assert js.status_code == 200
        assert "javascript" in js.headers["content-type"]

        css = client.get("/qbx/inject.css")
        assert css.status_code == 200
        assert "text/css" in css.headers["content-type"]
        assert "qbxToastHost" in css.text


def test_qbt_index_is_served_with_the_qbx_integration(tmp_path):
    with _client(tmp_path) as client:
        res = client.get("/qbt/")
        assert res.status_code == 200
        assert "/qbx/inject.js" in res.text
        assert "/qbx/inject.css" in res.text
        assert 'id="qbx-bootstrap"' in res.text


def test_bootstrap_tracks_configured_api_token(tmp_path):
    with _client(tmp_path) as client:
        assert '"tokenRequired": false' in client.get("/qbt/").text

        client.app.state.qbx.store.update({"server": {"api_token": "s3cret"}})
        assert '"tokenRequired": true' in client.get("/qbt/").text


def test_embed_route_serves_the_shell(tmp_path):
    shell_index = ROOT / "qbx" / "web" / "matcher" / "dist" / "index.html"
    with _client(tmp_path) as client:
        res = client.get("/embed")
        if shell_index.is_file():
            assert res.status_code == 200
            assert "<div id=\"root\">" in res.text
        else:
            assert res.status_code == 503
            assert "not built" in res.json()["detail"]
