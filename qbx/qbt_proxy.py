"""Serve the vendored qBittorrent WebUI and reverse-proxy its WebAPI.

The official WebUI expects ``/api/v2`` on the same origin. qbx mounts the
static tree under ``/qbt/`` and proxies ``/qbt/api/v2/*`` (and ``/api/v2/*``)
to the configured qBittorrent instance.

Fixes applied for embedding under ``/qbt/``:

* Rewrite ``Referer`` / ``Origin`` to the upstream qBittorrent URL (CSRF).
* Strip ``QBT_TR(...)QBT_TR[CONTEXT=...]`` markers (normally done by qbt's
  C++ translator) so English source strings render.
* Rewrite ``Set-Cookie`` path so the SID works for the proxied origin.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import Request, Response
from fastapi.responses import FileResponse, HTMLResponse

log = logging.getLogger("qbx.qbt_proxy")

WEB_DIR = Path(__file__).parent / "web" / "qbittorrent"
PRIVATE = WEB_DIR / "private"
PUBLIC = WEB_DIR / "public"

_CACHE_ID = hashlib.sha1(str(time.time()).encode()).hexdigest()[:10]

# Same pattern qBittorrent's WebApplication uses.
_TR_RE = re.compile(
    r"QBT_TR\((([^\)]|\)(?!QBT_TR))+)\)QBT_TR\[CONTEXT=([a-zA-Z_][a-zA-Z0-9_]*)\]"
)


def _subst(text: str, *, lang: str = "en") -> str:
    text = text.replace("${LANG}", lang).replace("${CACHEID}", _CACHE_ID)
    # Drop translation wrappers; keep the English source text.
    return _TR_RE.sub(r"\1", text)


def resolve_webui_file(path: str) -> Path | None:
    """Map a /qbt/… path to a file under private/ or public/."""
    rel = path.lstrip("/")
    if not rel or rel.endswith("/"):
        rel = (rel + "index.html") if rel else "index.html"
    for root in (PRIVATE, PUBLIC):
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _rewrite_set_cookie(value: str, *, cookie_path: str = "/") -> str:
    """Normalize upstream Set-Cookie for the qbx origin."""
    parts = [p.strip() for p in value.split(";")]
    if not parts:
        return value
    out = [parts[0]]
    for part in parts[1:]:
        lower = part.lower()
        if lower.startswith("path="):
            out.append(f"Path={cookie_path}")
        elif lower.startswith("domain="):
            continue  # drop Domain so cookie binds to the qbx host
        else:
            out.append(part)
    if not any(p.lower().startswith("path=") for p in out[1:]):
        out.append(f"Path={cookie_path}")
    return "; ".join(out)


async def proxy_qbt_api(request: Request, upstream_base: str, subpath: str) -> Response:
    """Forward a request to qBittorrent ``/api/v2/{subpath}``."""
    base = upstream_base.rstrip("/") + "/"
    url = urljoin(base, f"api/v2/{subpath.lstrip('/')}")
    if request.url.query:
        url = f"{url}?{request.url.query}"

    upstream = urlsplit(upstream_base)
    upstream_origin = f"{upstream.scheme}://{upstream.netloc}"

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {
            "host",
            "content-length",
            "connection",
            "referer",
            "origin",
            "cookie",  # rebuild from request.cookies below
        }
    }
    # qBittorrent CSRF: Referer must match the WebUI origin (port 8084 here).
    headers["Referer"] = upstream_origin + "/"
    headers["Origin"] = upstream_origin

    body = await request.body()
    client: httpx.AsyncClient = request.app.state.qbx_proxy_client
    try:
        upstream_resp = await client.request(
            request.method,
            url,
            content=body if body else None,
            headers=headers,
            cookies=dict(request.cookies),
        )
    except httpx.RequestError as exc:
        log.warning("qBittorrent proxy error: %s", exc)
        return Response(content=f"qBittorrent unreachable: {exc}", status_code=502)

    excluded = {
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
        "set-cookie",
    }
    out_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in excluded
    }
    response = Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=out_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )
    # httpx merges set-cookie; use raw list from headers.
    for cookie in upstream_resp.headers.get_list("set-cookie"):
        response.headers.append("set-cookie", _rewrite_set_cookie(cookie, cookie_path="/"))
    return response


def serve_webui_file(path: Path, *, inject_qbx: bool = False) -> Response:
    if path.suffix.lower() in {".html", ".htm", ".js", ".css"}:
        raw = path.read_text(encoding="utf-8", errors="replace")
        processed = _subst(raw)
        if path.suffix.lower() in {".html", ".htm"} and inject_qbx:
            inject = (
                '<script src="/qbx/inject.js" defer></script>\n'
                "</body>"
            )
            if "</body>" in processed:
                processed = processed.replace("</body>", inject, 1)
            else:
                processed = processed + "\n" + inject.replace("</body>", "")
        if path.suffix.lower() in {".html", ".htm"}:
            return HTMLResponse(processed)
        return Response(content=processed, media_type=_media_type(path))
    return FileResponse(path)


def _media_type(path: Path) -> str:
    return {
        ".js": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
    }.get(path.suffix.lower(), "application/octet-stream")


def ensure_proxy_client(app) -> None:
    if getattr(app.state, "qbx_proxy_client", None) is None:
        app.state.qbx_proxy_client = httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=False,
            verify=False,
        )


async def close_proxy_client(app) -> None:
    client = getattr(app.state, "qbx_proxy_client", None)
    if client is not None:
        await client.aclose()
        app.state.qbx_proxy_client = None
