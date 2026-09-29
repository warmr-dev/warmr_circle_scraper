"""The dashboard page: a small HTML shell and the ES modules it loads.

The modules are served from ``/ui/<version>/...`` where the version is a hash
of their contents. A relative ``import`` inside a module inherits that prefix,
so after a deploy the browser fetches every module fresh, while between
deploys it keeps them cached. (A ``?v=`` on the entry script alone would not
do that: the modules it imports would still come from the old cache.)
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from circle_leads.web.auth import COOKIE_NAME

STATIC_DIR = Path(__file__).parent / "static"
UI_DIR = STATIC_DIR / "ui"
MEDIA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
# The page shows posts written by strangers; nothing but our own scripts runs.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")

router = APIRouter()


@lru_cache(maxsize=1)
def ui_version() -> str:
    digest = hashlib.sha1()
    for path in sorted(p for p in UI_DIR.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(UI_DIR)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _signed_in(request: Request) -> bool:
    return request.app.state.sessions.valid(request.cookies.get(COOKIE_NAME))


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _signed_in(request):
        return RedirectResponse("/login", status_code=303)
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        html.replace("__UI_VERSION__", ui_version()),
        headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP},
    )


@router.get("/ui/{version}/{path:path}")
def ui_asset(version: str, path: str, request: Request) -> Response:
    if not _signed_in(request):
        raise HTTPException(401, "Not authenticated")
    target = (UI_DIR / path).resolve()
    if UI_DIR.resolve() not in target.parents or not target.is_file():
        raise HTTPException(404, "Not found")
    media_type = MEDIA_TYPES.get(target.suffix)
    if media_type is None:
        raise HTTPException(404, "Not found")
    cache = ("private, max-age=31536000, immutable" if version == ui_version()
             else "no-store")
    return Response(target.read_bytes(), media_type=media_type,
                    headers={"Cache-Control": cache})
