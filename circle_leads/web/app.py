"""The dashboard: sign-in, the page, and the routes the page and the tools use.

The six sections of the page have their own modules (circle_leads/web/sections,
under /api/dash); circle_leads/web/ui.py serves the page itself. What stays here
is what other clients call: the local connector, the browser extension that
stores sessions, the per-community session actions, the watchdog cron and the
tick pinger.

Local-first: binds to 127.0.0.1 unless told otherwise, and requires a password
from the environment. The database holds other people's posts, so the default
posture is closed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select

from circle_leads.config.settings import load_requirements
from circle_leads.storage.activity import log_activity
from circle_leads.storage.database import Database
from circle_leads.web.auth import (
    COOKIE_NAME,
    SESSION_MAX_AGE,
    AuthNotConfigured,
    SessionManager,
    get_password,
    verify_password,
)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    db_url: str | None = None,
    config_path: str | None = None,
    db: Database | None = None,
) -> FastAPI:
    # Fail fast and loudly rather than serving other people's posts openly.
    get_password()

    app = FastAPI(title="Warmr Circle", docs_url=None, redoc_url=None)
    # Reuse a caller-supplied Database (the CLI already opened one) so a
    # Render/Railway start does not open a second SQLAlchemy pool against
    # the same Supabase session-mode cap.
    db = db or Database(db_url or os.environ.get("CIRCLE_LEADS_DB") or None)

    def _load_effective_requirements():
        """Packaged defaults, with any DB-stored dashboard edits merged on top.

        Config is persisted in the DB (writable) rather than the packaged YAML
        (read-only on serverless), so an override wins when present.
        """
        from circle_leads.config.settings import validate_requirements
        from circle_leads.storage.settings_store import get_requirements_override
        try:
            override = get_requirements_override(db)
        except Exception:  # noqa: BLE001 - a fresh/empty DB just means no override
            override = None
        if override:
            try:
                return validate_requirements(override)
            except Exception:  # noqa: BLE001 - a bad stored blob must not brick startup
                pass
        return load_requirements(config_path)

    requirements_holder = {"req": _load_effective_requirements()}
    sessions = SessionManager()

    # What the section routers (circle_leads/web/sections) reach for.
    app.state.db = db
    app.state.sessions = sessions
    app.state.config_path = config_path
    app.state.requirements_holder = requirements_holder
    app.state.cache = {}

    @app.middleware("http")
    async def _forget_cached_numbers_on_writes(request: Request, call_next):
        # A change made on the page (a session pasted, an item marked as seen)
        # must show on the next load, not once the cache runs out.
        response = await call_next(request)
        if request.method != "GET" and request.url.path.startswith("/api/"):
            app.state.cache.clear()
        return response

    def requirements():
        return requirements_holder["req"]

    def log_activity_holder(**kw):
        with db.session() as s:
            log_activity(s, **kw)

    def require_auth(request: Request) -> None:
        if not sessions.valid(request.cookies.get(COOKIE_NAME)):
            raise HTTPException(status_code=401, detail="Not authenticated")

    def require_auth_or_extension_token(request: Request) -> None:
        """Either a signed dashboard session, or the EXTENSION_API_TOKEN secret
        as the X-Extension-Token header -- so the cookie-grabber browser
        extension can post a session cookie without logging into the dashboard.
        Same shape as /api/tick's TICK_TOKEN. Scoped to one route (session
        storage), not every authed endpoint, so a leaked token can only write a
        cookie for a host of the caller's choosing -- it can't read leads or
        change config.
        """
        token = os.environ.get("EXTENSION_API_TOKEN", "")
        if token and request.headers.get("x-extension-token") == token:
            return
        require_auth(request)

    # --- Auth -------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str | None = None) -> HTMLResponse:
        if sessions.valid(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse((STATIC_DIR / "login.html").read_text(encoding="utf-8"))

    @app.get("/api/health")
    def api_health() -> dict[str, Any]:
        """Fast DB reachability check. No auth so a deploy can be diagnosed even
        when the DB is down. Reports whether Postgres answers within seconds."""
        import time as _t
        from sqlalchemy import text as _text
        info: dict[str, Any] = {
            "db_url_host": (db.url.split("@")[-1].split("/")[0] if "@" in db.url else "sqlite"),
            "skip_db_init": os.environ.get("SKIP_DB_INIT", "").lower() == "true",
        }
        t0 = _t.time()
        try:
            with db.engine.connect() as conn:
                conn.execute(_text("SELECT 1"))
            info["db"] = "ok"
        except Exception as exc:  # noqa: BLE001 - report, don't hang
            info["db"] = "error"
            info["error"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
        info["ms"] = int((_t.time() - t0) * 1000)
        return info

    @app.post("/login")
    def login(response: Response, password: str = Form(...)) -> JSONResponse:
        if not verify_password(password):
            return JSONResponse({"error": "Incorrect password."}, status_code=401)
        token = sessions.issue()
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            COOKIE_NAME,
            token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            # Set on HTTPS deployments; would break plain-HTTP localhost.
            secure=os.environ.get("DASHBOARD_HTTPS", "").lower() == "true",
        )
        return resp

    @app.post("/logout")
    def logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME)
        return resp

    @app.post("/api/communities/add")
    def add_community(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        """Add a Circle community by URL and check if it's readable.

        Paste any community link (https://<slug>.circle.so or a custom domain).
        We probe its PUBLIC JSON API: if spaces are readable, it's saved ready
        to harvest. If it's members-only, it's still saved, but reading it needs
        the LOCAL browser sign-in flow (`circle-leads read-feed <host> --login`),
        where you log in yourself -- the server never handles your credentials.
        """
        import re as _re
        from urllib.parse import urlparse
        from circle_leads.discovery.discover_communities import (
            detect_platform, unique_slug_for_host,
        )
        from circle_leads.scraper.public_reader import PublicReader
        from circle_leads.storage.database import get_or_create_community
        from circle_leads.storage.models import AccessState, PermissionStatus

        raw = str(payload.get("url") or "").strip()
        if not raw:
            raise HTTPException(400, "No URL supplied.")
        if "://" not in raw:
            raw = "https://" + raw
        host = (urlparse(raw).hostname or "").lower().strip()
        if not host or "." not in host:
            raise HTTPException(400, "That doesn't look like a community URL.")
        # login.circle.so / discover.circle.so aren't communities themselves.
        if host in ("login.circle.so", "discover.circle.so", "app.circle.so",
                    "community.circle.so", "circle.so", "www.circle.so"):
            raise HTTPException(400, f"{host} is not a community — paste the community's own URL.")

        # unique_slug_for_host, not community_slug_for_host: the latter is a
        # label ("which organisation?") and is deliberately not unique, so two
        # sibling subdomains would merge onto one row via the `slug OR url`
        # match in get_or_create_community and the second URL would be lost.
        slug = unique_slug_for_host(host)
        url = f"https://{host}"
        platform = detect_platform(url)

        # Probe the public API: reachable? readable spaces? real name?
        readable = False
        spaces_found = 0
        real_name = None
        try:
            reader = PublicReader(host)
            spaces = reader.list_spaces()
            spaces_found = len(spaces)
            real_name = reader.community_name()
            for sp in spaces[:6]:
                ok, recs = reader.read_space(sp.id, max_pages=1, since=None)
                if ok and recs:
                    readable = True
                    break
        except Exception:  # noqa: BLE001 - unreachable is a valid, reported result
            pass

        with db.session() as s:
            c = get_or_create_community(
                s, slug=slug, url=url,
                name=real_name or slug,
                platform=platform,
                discovery_source="dashboard:add-by-link",
                access_status=AccessState.NOT_VISITED.value,
                permission_status=PermissionStatus.CANDIDATE.value,
            )
            if real_name and (not c.name or c.name == slug):
                c.name = real_name
            if platform and c.platform != platform:
                c.platform = platform
            log_activity(
                s, kind="review", community=slug,
                summary=(
                    f"Added {host} by link — "
                    + ("public spaces readable" if readable
                       else f"{spaces_found} space(s), members-only" if spaces_found
                       else "not publicly reachable")
                ),
                detail={"url": url, "spaces_found": spaces_found, "readable": readable},
            )

        if readable:
            note = "Public spaces are readable — it'll be harvested on the next run."
        elif spaces_found:
            note = ("Saved, but its spaces are members-only. To read it, sign in "
                    "yourself locally: circle-leads read-feed " + host + " --login")
        else:
            note = ("Saved, but the public API wasn't reachable (login wall or "
                    "bot-check). If you're a member, read it locally: "
                    "circle-leads read-feed " + host + " --login")
        return {"ok": True, "slug": slug, "name": real_name or slug,
                "readable": readable, "spaces_found": spaces_found, "note": note}

    # --- Local Circle Connector (private communities) ---------------------
    #
    # The connector runs on the USER's computer, holds the authenticated Circle
    # browser session locally, and uploads only normalized content. Railway
    # never sees Circle credentials/cookies/profiles. Two auth surfaces:
    #   * dashboard (human, cookie) manages pairing + connections
    #   * connector (bearer token from pairing) sends heartbeat + content

    def require_connector(request: Request):
        from circle_leads.web.connector_auth import verify_connector_token
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        c = verify_connector_token(db, token)
        if c is None:
            raise HTTPException(401, "Invalid or unpaired connector token")
        return c

    @app.post("/api/connector/pair")
    def connector_pair(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        """Dashboard action: mint a one-time pairing code to enter in the connector."""
        from circle_leads.web.connector_auth import create_pairing
        return create_pairing(db, name=(payload or {}).get("name"))

    @app.post("/api/connector/claim")
    def connector_claim(payload: dict) -> dict[str, Any]:
        """Connector action (no session): exchange a pairing code for a token."""
        from circle_leads.web.connector_auth import claim_pairing
        result = claim_pairing(
            db, str((payload or {}).get("code") or ""),
            agent_info=str((payload or {}).get("agent_info") or "")[:255] or None,
        )
        if result is None:
            raise HTTPException(400, "Invalid or expired pairing code.")
        return result

    @app.post("/api/connector/heartbeat")
    def connector_heartbeat(connector=Depends(require_connector)) -> dict[str, Any]:
        """Connector liveness ping. Updates last_seen (done in verify)."""
        return {"ok": True, "connector_id": connector.id}

    @app.get("/api/connector/worklist")
    def connector_worklist(connector=Depends(require_connector)) -> dict[str, Any]:
        """The communities this connector should scan, highest priority first.

        This is what makes the dashboard the control plane: the connector asks
        the server what to work on instead of being handed a host list on the
        command line. PAUSED communities are withheld entirely.
        """
        from circle_leads.storage.models import (
            CircleConnection, ConnectionPriority, SCAN_ORDER,
        )
        with db.session() as s:
            rows = s.scalars(
                select(CircleConnection).where(
                    CircleConnection.priority != ConnectionPriority.PAUSED.value
                )
            ).all()
            rows = sorted(rows, key=lambda c: (SCAN_ORDER.get(c.priority, 1), c.host))
            return {"communities": [
                {"host": c.host, "priority": c.priority, "state": c.state,
                 "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None}
                for c in rows
            ]}

    @app.get("/api/connectors")
    def list_connectors(_: None = Depends(require_auth)) -> dict[str, Any]:
        from circle_leads.storage.models import Connector
        import datetime as _dt
        now = _dt.datetime.utcnow()
        with db.session() as s:
            rows = s.scalars(select(Connector).where(Connector.paired.is_(True))).all()
            out = []
            for c in rows:
                online = bool(c.last_seen_at and (now - c.last_seen_at).total_seconds() < 120)
                out.append({
                    "id": c.id, "name": c.name, "agent_info": c.agent_info,
                    "online": online,
                    "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                })
            return {"connectors": out}

    @app.post("/api/connector/connections")
    def upsert_connection(payload: dict, connector=Depends(require_connector)) -> dict[str, Any]:
        """Connector reports a private community's auth state + space counts.

        Carries only non-sensitive fields (host, state, name, counts). No cookies,
        no session, no profile data.
        """
        from circle_leads.storage.models import CircleConnection, ConnectionState
        host = str(payload.get("host") or "").strip().lower().replace("https://", "").strip("/")
        if not host or "." not in host:
            raise HTTPException(400, "A community host is required.")
        state = str(payload.get("state") or ConnectionState.NOT_CONNECTED.value)
        valid = {s.value for s in ConnectionState}
        if state not in valid:
            raise HTTPException(400, f"Unknown state {state!r}.")
        with db.session() as s:
            conn = s.scalar(select(CircleConnection).where(CircleConnection.host == host))
            if conn is None:
                conn = CircleConnection(host=host)
                s.add(conn)
            conn.connector_id = connector.id
            conn.state = state
            conn.state_detail = (payload.get("state_detail") or None)
            if payload.get("name"):
                conn.name = str(payload["name"])[:512]
            if payload.get("member_label"):
                conn.member_label = str(payload["member_label"])[:255]
            if payload.get("spaces_total") is not None:
                conn.spaces_total = int(payload["spaces_total"])
            if payload.get("spaces_readable") is not None:
                conn.spaces_readable = int(payload["spaces_readable"])
            import datetime as _dt
            if state == ConnectionState.CONNECTED.value:
                conn.last_sync_at = _dt.datetime.utcnow()
        return {"ok": True, "host": host, "state": state}

    @app.post("/api/connector/ingest")
    def connector_ingest(payload: dict, connector=Depends(require_connector)) -> dict[str, Any]:
        """Connector uploads normalized posts from a private community; we
        classify + store them exactly like any other source. The records are
        already normalized text -- no Circle session ever reaches here."""
        host = str(payload.get("host") or "").strip().lower().replace("https://", "").strip("/")
        records = payload.get("records") or []
        if not host:
            raise HTTPException(400, "host is required.")
        if not isinstance(records, list) or not records:
            return {"ok": True, "leads": 0, "posts": 0, "note": "no records"}
        slug = host.split(".")[0]
        from circle_leads.triage.pipeline import triage_records
        res = triage_records(
            db, records, requirements(), community=slug,
            source_url=f"https://{host}",
            use_llm=bool(os.environ.get("OPENAI_API_KEY")),
        )
        log_activity_holder(
            kind="ingest", level="success" if res.leads else "info",
            community=slug,
            summary=f"Connector ingest from {host}: {len(res.leads)} lead(s) from {res.total_posts} post(s)",
            detail={"host": host, "connector_id": connector.id},
            items_seen=res.total_posts, leads_found=len(res.leads),
        )
        return {"ok": True, "leads": len(res.leads), "posts": res.total_posts}

    def _clean_host(raw: Any) -> str:
        """Normalize a community host, or raise if it isn't one."""
        host = (str(raw or "").strip().lower()
                .replace("https://", "").replace("http://", "").strip("/"))
        host = host.split("/")[0]  # tolerate a pasted deep link
        if not host or "." not in host:
            raise HTTPException(400, "Enter a community host, e.g. altea.circle.so")
        return host

    def _connection_dict(c) -> dict[str, Any]:
        return {
            "id": c.id, "host": c.host, "name": c.name,
            "member_label": c.member_label, "state": c.state,
            "state_detail": c.state_detail,
            "priority": c.priority, "notes": c.notes,
            "spaces_total": c.spaces_total, "spaces_readable": c.spaces_readable,
            "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None,
        }

    @app.get("/api/connections")
    def list_connections(_: None = Depends(require_auth)) -> dict[str, Any]:
        """List connected private communities in scan order: VIP first."""
        from circle_leads.storage.models import CircleConnection, ReplaySession, SCAN_ORDER
        with db.session() as s:
            rows = s.scalars(select(CircleConnection)).all()
            # Which communities have a stored session cookie?
            hosts_with_session = {
                r.host for r in s.scalars(select(ReplaySession)).all()
            }
            rows = sorted(rows, key=lambda c: (SCAN_ORDER.get(c.priority, 1), c.host))
            out = []
            for c in rows:
                d = _connection_dict(c)
                d["has_session"] = c.host in hosts_with_session
                out.append(d)
            return {"connections": out}

    @app.post("/api/connections/add")
    def add_connection(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        """Dashboard: register a private community host to monitor. The actual
        login happens locally in the connector; this just creates the record."""
        from circle_leads.storage.models import (
            CircleConnection, ConnectionPriority, ConnectionState,
        )
        host = _clean_host(payload.get("host"))
        priority = str(payload.get("priority") or ConnectionPriority.NORMAL.value).lower()
        if priority not in ConnectionPriority.values():
            raise HTTPException(400, f"Unknown priority {priority!r}.")
        with db.session() as s:
            conn = s.scalar(select(CircleConnection).where(CircleConnection.host == host))
            created = conn is None
            if conn is None:
                conn = CircleConnection(host=host, state=ConnectionState.NOT_CONNECTED.value)
                s.add(conn)
            # Re-adding an existing host updates its settings rather than
            # silently ignoring what you typed.
            conn.priority = priority
            if payload.get("name"):
                conn.name = str(payload["name"])[:512]
            if payload.get("notes") is not None:
                conn.notes = str(payload["notes"])[:2000] or None
            log_activity(
                s, kind="review", community=host.split(".")[0],
                summary=(f"Private community {host} "
                         f"{'added' if created else 'updated'} "
                         f"({priority}; awaiting local login)"),
            )
        return {"ok": True, "host": host, "priority": priority, "created": created}

    @app.post("/api/connections/{host}/session")
    def set_connection_session(
        host: str, payload: dict, _: None = Depends(require_auth_or_extension_token)
    ) -> dict[str, Any]:
        """Store a member session cookie for a community.

        Circle's /internal_api is not behind Cloudflare, so a valid
        _circle_session cookie lets the backend read this community over plain
        HTTP -- no browser, runs anywhere. Stored encrypted if CIRCLE_CRED_KEY
        is set, else plaintext (the user's choice). Never logged, never returned
        to the frontend. One cookie per community (Circle scopes it per
        subdomain).

        Two ways to provide it (use EITHER):
          - ``cookies``: the full cookie-export JSON array (both cookies are
            extracted automatically), or
          - ``session_cookie`` + ``user_session_identifier``: the two values
            pasted individually.
        """
        import json as _json
        from circle_leads.web.replay_store import connect_host
        from circle_leads.scraper.member_api_reader import SESSION_COOKIE_NAMES

        host = _clean_host(host)
        got: dict[str, str] = {}

        # Path 1: a full cookie-export JSON array (string or already-parsed).
        raw = (payload or {}).get("cookies")
        if raw:
            data = raw
            if isinstance(raw, str):
                try:
                    data = _json.loads(raw)
                except (ValueError, TypeError):
                    raise HTTPException(400, "The cookie box must contain the JSON export array.")
            if isinstance(data, list):
                for c in data:
                    if isinstance(c, dict) and c.get("name") in SESSION_COOKIE_NAMES:
                        got[c["name"]] = str(c.get("value") or "")

        # Path 2: the two values pasted individually (OR alternative to JSON).
        for field_name, cookie_name in (
            ("session_cookie", "_circle_session"),
            ("user_session_identifier", "user_session_identifier"),
            ("remember_token", "remember_user_token"),
        ):
            val = str((payload or {}).get(field_name) or "").strip()
            if val:
                # Tolerate a "name=value" paste.
                got[cookie_name] = (val.split("=", 1)[1]
                                    if "=" in val and val.startswith(cookie_name) else val)

        if "_circle_session" not in got and "user_session_identifier" not in got:
            raise HTTPException(
                400, "Paste either the cookie-export JSON, or the _circle_session "
                "and user_session_identifier values.")
        # Both are needed to actually read Circle; warn but still store what we got
        # so the user can fix it, and the scan will report if the session is invalid.
        missing = [n for n in ("_circle_session", "user_session_identifier") if n not in got]

        cookies = [{"domain": host, "name": n, "value": v, "path": "/",
                    "secure": True, "session": True} for n, v in got.items()]
        # connect_host() stores the cookie AND auto-creates the CircleConnection
        # row if this is a brand-new host, so pasting a cookie is the ONLY step
        # needed -- no separate "Add community" first.
        connect_host(db, host, cookies)

        log_activity_holder(kind="review", community=host.split(".")[0],
                            summary=f"Session cookies stored for {host}")
        return {"ok": True, "host": host, "cookies": len(cookies),
                "missing": missing}

    @app.post("/api/connections/{host}/scan")
    def scan_connection_http(
        host: str, _: None = Depends(require_auth)
    ) -> dict[str, Any]:
        """Enqueue a scan of one community; the Railway worker runs it.

        The dashboard does NOT scan inside the request -- it writes a job to the
        durable queue and returns instantly. The always-on worker (warm, pooled,
        no timeout) does the actual reading fast. Results appear on the
        connection row + Activity when the worker finishes.
        """
        from circle_leads.web.replay_store import load_cookies
        from circle_leads.storage.job_queue import enqueue
        host = _clean_host(host)
        if not load_cookies(db, host):
            raise HTTPException(404, f"No stored session for {host}. Add one first.")
        # VIP communities enqueue at higher priority (lower number = first).
        from circle_leads.storage.models import CircleConnection, ConnectionPriority
        with db.session() as s:
            conn = s.scalar(select(CircleConnection).where(CircleConnection.host == host))
            prio = 0 if (conn and conn.priority == ConnectionPriority.VIP.value) else 1
        job_id = enqueue(db, "scan", host=host, priority=prio)
        return {"ok": True, "host": host, "queued": True, "job_id": job_id,
                "note": "Queued — the worker will scan it shortly."}

    @app.post("/api/connections/scan-all")
    def scan_all_cookie_hosts(_: None = Depends(require_auth)) -> dict[str, Any]:
        """Enqueue a VIP-first scan of every cookie-backed community."""
        from circle_leads.scanning import cookie_hosts_vip_first
        from circle_leads.storage.job_queue import enqueue
        hosts = cookie_hosts_vip_first(db)
        if not hosts:
            raise HTTPException(400, "No communities have a session cookie yet.")
        job_id = enqueue(db, "scan_all", priority=0)
        return {"ok": True, "queued": True, "job_id": job_id, "hosts": hosts,
                "note": "Queued — the worker will scan them VIP-first."}

    @app.post("/api/connections/{host}/priority")
    def set_connection_priority(
        host: str, payload: dict, _: None = Depends(require_auth)
    ) -> dict[str, Any]:
        """Set a community's scan priority (vip / normal / low / paused)."""
        from circle_leads.storage.models import CircleConnection, ConnectionPriority
        host = _clean_host(host)
        priority = str((payload or {}).get("priority") or "").lower()
        if priority not in ConnectionPriority.values():
            raise HTTPException(
                400, f"priority must be one of {sorted(ConnectionPriority.values())}."
            )
        with db.session() as s:
            conn = s.scalar(select(CircleConnection).where(CircleConnection.host == host))
            if conn is None:
                raise HTTPException(404, f"{host} is not a connected community.")
            conn.priority = priority
            log_activity(s, kind="review", community=host.split(".")[0],
                         summary=f"{host} scan priority set to {priority}")
        return {"ok": True, "host": host, "priority": priority}

    @app.post("/api/connections/{host}/notes")
    def set_connection_notes(
        host: str, payload: dict, _: None = Depends(require_auth)
    ) -> dict[str, Any]:
        """Attach a private note to a community (why it matters, who you know)."""
        from circle_leads.storage.models import CircleConnection
        host = _clean_host(host)
        notes = str((payload or {}).get("notes") or "")[:2000]
        with db.session() as s:
            conn = s.scalar(select(CircleConnection).where(CircleConnection.host == host))
            if conn is None:
                raise HTTPException(404, f"{host} is not a connected community.")
            conn.notes = notes or None
        return {"ok": True, "host": host, "notes": notes or None}

    @app.post("/api/connections/{host}/remove")
    def remove_connection(host: str, _: None = Depends(require_auth)) -> dict[str, Any]:
        from circle_leads.storage.models import CircleConnection
        host = host.strip().lower()
        with db.session() as s:
            conn = s.scalar(select(CircleConnection).where(CircleConnection.host == host))
            if conn is not None:
                s.delete(conn)
        # Also drop any stored session cookie for this host -- removing a
        # community should not leave its credential behind.
        from circle_leads.web.replay_store import delete_session
        delete_session(db, host)
        return {"ok": True}

    @app.post("/api/connections/{host}/session/clear")
    def clear_connection_session(host: str, _: None = Depends(require_auth)) -> dict[str, Any]:
        """Delete just the stored session cookie for a community (e.g. a dead
        one), keeping the community itself."""
        from circle_leads.web.replay_store import delete_session
        host = _clean_host(host)
        delete_session(db, host)
        log_activity_holder(kind="review", community=host.split(".")[0],
                            summary=f"Session cookie cleared for {host}")
        return {"ok": True, "host": host}

    @app.get("/api/watchdog")
    def api_watchdog(request: Request) -> dict[str, Any]:
        """Is the server still alive? Nothing on it can answer that itself.

        The worker and the watcher each stamp a heartbeat into the database.
        This runs on Vercel, on a schedule, and shouts on Telegram when a stamp
        goes stale -- which is the only way we hear about a droplet that has
        stopped, run out of disk, or lost its network.

        Open by design: it takes no input, returns no secrets, and Vercel's own
        cron cannot send an Authorization header. The worst a stranger can do
        is learn whether two timestamps are recent.
        """
        from datetime import datetime as _dtm

        from circle_leads.notify import notify
        from circle_leads.storage.settings_store import get_setting

        # How long a stamp may be missing before it is a problem. Both services
        # now beat from a thread beside the work rather than from the top of
        # their loop, so a window no longer has to cover the length of a job.
        # The worker's used to be 90 minutes to survive a harvest and went
        # stale anyway, crying wolf while the journal showed it working. It
        # keeps the wider of the two only because it competes for a
        # three-connection pool, where a beat can be skipped under load.
        LIMITS = {"watcher_heartbeat": 900, "worker_heartbeat": 1800}

        now = _dtm.utcnow()
        report: dict[str, Any] = {"checked_at": now.isoformat(), "services": {}}
        stale: list[str] = []
        for key, limit in LIMITS.items():
            raw = get_setting(db, key)
            name = key.replace("_heartbeat", "")
            if not raw:
                report["services"][name] = {"state": "never", "age_s": None}
                stale.append(f"{name}: никогда не отчитывался")
                continue
            try:
                age = (now - _dtm.fromisoformat(raw)).total_seconds()
            except ValueError:
                report["services"][name] = {"state": "unreadable", "age_s": None}
                stale.append(f"{name}: непонятная отметка времени")
                continue
            ok = age <= limit
            report["services"][name] = {
                "state": "ok" if ok else "stale",
                "age_s": round(age),
                "limit_s": limit,
            }
            if not ok:
                stale.append(f"{name}: молчит {age / 60:.0f} мин (порог {limit // 60})")

        report["ok"] = not stale
        if stale:
            notify(
                "Warmr: служба молчит",
                "\n".join(stale) + "\n\nПроверить: <code>systemctl status warmr-worker "
                "warmr-watcher</code> на 168.144.131.38",
                level="error",
                # One message per hour per distinct problem, not one per cron tick.
                dedup_key="watchdog:" + "|".join(sorted(stale)),
            )
        return report

    @app.post("/api/tick")
    @app.get("/api/tick")
    def api_tick(request: Request) -> dict[str, Any]:
        """Wake-up endpoint for an external cron pinger.

        Queues a harvest for the worker when the dashboard-set schedule says
        one is due. Auth: a signed session, or the TICK_TOKEN secret as
        ?token= (so a pinger can call it).
        """
        from circle_leads.storage.job_queue import enqueue
        from circle_leads.storage.settings_store import is_harvest_due, mark_harvest_run

        token = request.query_params.get("token", "")
        tick_token = os.environ.get("TICK_TOKEN", "")
        authed = sessions.valid(request.cookies.get(COOKIE_NAME)) or (
            tick_token and token == tick_token
        )
        if not authed:
            raise HTTPException(401, "Not authenticated")
        if not is_harvest_due(db):
            return {"ran": False, "reason": "not due yet"}
        mark_harvest_run(db)
        # The tick only ENQUEUES; the always-on worker runs the harvest. (The
        # worker also checks the schedule itself, so this is belt-and-braces
        # for a cron pinger.)
        job_id = enqueue(db, "harvest", priority=1)
        return {"ran": True, "queued": True, "job_id": job_id,
                "note": "Harvest queued for the worker."}

    # Remote Browser (server-hosted interactive Chromium PoC) and the Session
    # Replay experiment (Version B) were removed outright -- both were
    # explicitly marked "proven non-working (Cloudflare-blocked)" / "kept for
    # the PoC record" and shipped hidden. Their routes lived in
    # circle_leads/web/remote_browser_api.py, now deleted; the underlying
    # circle_leads/remote_browser/ package and web/replay_store.py are
    # untouched (replay_store.py is live: scanning.py and the auto-join bot
    # use store_session/load_cookies directly, no HTTP route needed).

    from circle_leads.web import sections, ui

    app.include_router(ui.router)
    sections.register(app)

    return app


def run(
    host: str = "127.0.0.1",
    port: int = 8000,
    db_url: str | None = None,
    db: Database | None = None,
) -> None:
    import uvicorn

    try:
        app = create_app(db_url=db_url, db=db)
    except AuthNotConfigured as exc:
        raise SystemExit(f"\n{exc}\n")

    if host not in ("127.0.0.1", "localhost"):
        print(
            f"\n  WARNING: binding to {host} exposes the dashboard beyond this "
            "machine.\n  It serves other people's posts. Use a tunnel or a "
            "firewall rather than a public bind.\n"
        )
    print(f"\n  Warmr Circle dashboard → http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
