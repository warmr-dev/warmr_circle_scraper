"""Dashboard for reviewing leads.

Local-first: binds to 127.0.0.1 unless told otherwise, and requires a password
from the environment. The database holds other people's posts, so the default
posture is closed.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select

from circle_leads.config.settings import (
    load_requirements, requirements_to_dict,
)
from circle_leads.export.exporters import query_leads
from circle_leads.storage.activity import log_activity, recent_activity
from circle_leads.storage.database import Database
from circle_leads.storage.models import Community, Lead, Post
from circle_leads.triage.pipeline import triage_text
from circle_leads.triage.reply import draft_reply
from circle_leads.web.jobs import JobRegistry
from circle_leads.web.auth import (
    COOKIE_NAME,
    SESSION_MAX_AGE,
    AuthNotConfigured,
    SessionManager,
    get_password,
    verify_password,
)

STATIC_DIR = Path(__file__).parent / "static"
REVIEW_STATUSES = {"pending_review", "contacted", "replied", "rejected", "won"}


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
    requirements_holder = {"req": load_requirements(config_path)}
    config_file = config_path

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
    jobs = JobRegistry()

    def requirements():
        return requirements_holder["req"]

    def log_activity_holder(**kw):
        with db.session() as s:
            log_activity(s, **kw)

    def require_auth(request: Request) -> None:
        if not sessions.valid(request.cookies.get(COOKIE_NAME)):
            raise HTTPException(status_code=401, detail="Not authenticated")

    # --- Auth -------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str | None = None) -> HTMLResponse:
        if sessions.valid(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse((STATIC_DIR / "login.html").read_text(encoding="utf-8"))

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

    # --- Pages ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        if not sessions.valid(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    # --- Leads ------------------------------------------------------------

    @app.get("/api/leads")
    def api_leads(
        request: Request,
        role: str | None = None,
        skills: str | None = None,
        community: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        min_score: int = 0,
        limit: int = 200,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        skill_list = [s.strip() for s in skills.split(",")] if skills else None
        with db.session() as s:
            rows = query_leads(
                s, role=role, skills=skill_list, community=community,
                priority=priority, min_score=min_score, limit=limit,
            )
            # query_leads does not carry review state, so join it on here.
            review = {
                lid: (st, note)
                for lid, st, note in s.execute(
                    select(Lead.id, Lead.review_status, Lead.reason)
                ).all()
            }
            ids = {
                (p.content, l.id)
                for l, p in s.execute(
                    select(Lead, Post).join(Post, Lead.post_id == Post.id)
                ).all()
            }
            by_content = {c: i for c, i in ids}

        for row in rows:
            lead_id = by_content.get(row["content"])
            row["id"] = lead_id
            row["review_status"] = (
                review.get(lead_id, ("pending_review", None))[0]
                if lead_id
                else "pending_review"
            )
            row["reply_draft"] = draft_reply(
                row
            ).text

        if status:
            rows = [r for r in rows if r.get("review_status") == status]
        return {"leads": rows, "count": len(rows)}

    @app.post("/api/leads/{lead_id}/status")
    def set_status(
        lead_id: int, payload: dict, _: None = Depends(require_auth)
    ) -> dict[str, Any]:
        new_status = str(payload.get("status", "")).strip()
        if new_status not in REVIEW_STATUSES:
            raise HTTPException(400, f"status must be one of {sorted(REVIEW_STATUSES)}")
        with db.session() as s:
            lead = s.get(Lead, lead_id)
            if lead is None:
                raise HTTPException(404, "Lead not found")
            lead.review_status = new_status
            log_activity(
                s,
                kind="review",
                summary=f"Lead {lead_id} marked {new_status}",
                detail={"job_title": lead.job_title, "score": lead.lead_score},
            )
        return {"ok": True, "status": new_status}

    @app.post("/api/communities/{slug}/visited")
    def mark_visited(slug: str, payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        from circle_leads.storage.models import AccessState
        new_state = payload.get("state", AccessState.JOINED.value)
        with db.session() as s:
            c = s.scalar(select(Community).where(Community.slug == slug))
            if c is None:
                raise HTTPException(404, "Community not found")
            c.access_status = new_state
            log_activity(s, kind="review", community=slug,
                         summary=f"Community {slug} marked {new_state}")
        return {"ok": True, "state": new_state}

    @app.post("/api/communities/{slug}/watch")
    def set_watch(slug: str, payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        """Subscribe/unsubscribe a community. Watched ones are polled first on
        the fast (5-min) harvest lane, so their new posts surface within minutes."""
        watching = bool(payload.get("watching", True))
        with db.session() as s:
            c = s.scalar(select(Community).where(Community.slug == slug))
            if c is None:
                raise HTTPException(404, "Community not found")
            c.watching = watching
            log_activity(
                s, kind="review", community=slug,
                summary=f"Community {slug} {'subscribed (watching)' if watching else 'unsubscribed'}",
            )
        return {"ok": True, "watching": watching}

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

        slug = host.split(".")[0]
        url = f"https://{host}"

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
                discovery_source="dashboard:add-by-link",
                access_status=AccessState.NOT_VISITED.value,
                permission_status=PermissionStatus.CANDIDATE.value,
            )
            if real_name and (not c.name or c.name == slug):
                c.name = real_name
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
        from circle_leads.storage.models import CircleConnection, SCAN_ORDER
        with db.session() as s:
            rows = s.scalars(select(CircleConnection)).all()
            # Sort in Python so the ordering matches SCAN_ORDER exactly rather
            # than the alphabetical accident of the stored string.
            rows = sorted(rows, key=lambda c: (SCAN_ORDER.get(c.priority, 1), c.host))
            return {"connections": [_connection_dict(c) for c in rows]}

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
        host: str, payload: dict, _: None = Depends(require_auth)
    ) -> dict[str, Any]:
        """Store a member session cookie for a community, encrypted at rest.

        Circle's /internal_api is not behind Cloudflare, so a valid
        _circle_session cookie lets the backend read this community over plain
        HTTP -- no browser, runs anywhere. The cookie is a live credential:
        stored AES-GCM encrypted (needs CIRCLE_CRED_KEY), never logged, never
        returned to the frontend. One cookie per community (Circle scopes the
        session per subdomain).
        """
        from circle_leads.web.replay_store import ReplayKeyMissing, store_session
        host = _clean_host(host)
        session = str((payload or {}).get("session_cookie") or "").strip()
        if not session:
            raise HTTPException(400, "Paste the _circle_session cookie value.")
        # Accept either the bare value or a "name=value" paste.
        if session.startswith("_circle_session="):
            session = session.split("=", 1)[1]
        cookies = [{"domain": host, "name": "_circle_session", "value": session,
                    "path": "/", "secure": True, "session": True}]
        remember = str((payload or {}).get("remember_token") or "").strip()
        if remember:
            cookies.append({"domain": host, "name": "remember_user_token",
                            "value": remember, "path": "/", "secure": True})
        try:
            store_session(db, host, cookies)
        except ReplayKeyMissing as exc:
            raise HTTPException(400, str(exc)) from exc
        log_activity_holder(kind="review", community=host.split(".")[0],
                            summary=f"Session cookie stored for {host} (encrypted)")
        return {"ok": True, "host": host, "cookies": len(cookies)}

    @app.post("/api/connections/{host}/scan")
    def scan_connection_http(
        host: str, _: None = Depends(require_auth)
    ) -> dict[str, Any]:
        """Read a community over HTTP with its stored session cookie, right now.

        This is the cloud-native path: no local connector, no browser. It uses
        the encrypted session cookie to call /internal_api and ingest posts.
        """
        from circle_leads.web.replay_store import ReplayKeyMissing, load_cookies
        from circle_leads.scraper.member_api_reader import (
            MemberApiReader, SessionInvalid, ChallengeHit, fetch_space_posts,
        )
        from circle_leads.storage.models import CircleConnection, ConnectionState
        host = _clean_host(host)
        try:
            cookie_list = load_cookies(db, host)
        except ReplayKeyMissing as exc:
            raise HTTPException(400, str(exc)) from exc
        if not cookie_list:
            raise HTTPException(404, f"No stored session for {host}. Add one first.")
        cookies = {c["name"]: c["value"] for c in cookie_list}

        from circle_leads.triage.pipeline import triage_records

        def _run(job):
            reader = MemberApiReader(host, cookies=cookies)
            state = ConnectionState.CONNECTED
            detail = ""
            total = 0
            readable = 0
            try:
                if not reader.check_session():
                    state = ConnectionState.SESSION_EXPIRED
                    detail = "Session cookie expired -- refresh it."
                else:
                    spaces = reader.list_spaces()
                    for sp in spaces:
                        recs = fetch_space_posts(reader, sp["id"], max_pages=5)
                        if not recs:
                            continue
                        readable += 1
                        total += len(recs)
                        triage_records(
                            db, recs, requirements(), community=host.split(".")[0],
                            source_url=f"https://{host}",
                            use_llm=bool(os.environ.get("OPENAI_API_KEY")),
                        )
                    job.result = {"spaces": len(spaces), "readable": readable,
                                  "posts": total}
                    job.detail = f"{total} post(s) from {readable}/{len(spaces)} space(s)"
            except SessionInvalid:
                state = ConnectionState.SESSION_EXPIRED
                detail = "Session rejected -- refresh the cookie."
            except ChallengeHit as exc:
                state = ConnectionState.ERROR
                detail = str(exc)
            with db.session() as s:
                conn = s.scalar(select(CircleConnection).where(CircleConnection.host == host))
                if conn is None:
                    conn = CircleConnection(host=host)
                    s.add(conn)
                conn.state = state.value
                conn.state_detail = detail or None
                conn.spaces_readable = readable
            log_activity_holder(
                kind="ingest", level="success" if total else "info",
                community=host.split(".")[0],
                summary=f"HTTP scan of {host}: {total} post(s) from {readable} space(s)",
                items_seen=total,
            )

        job = jobs.start("read", f"HTTP scan {host}", _run)
        return {"ok": True, "job_id": job.id, "host": host}

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
        return {"ok": True}

    # --- Triage -----------------------------------------------------------

    @app.post("/api/triage")
    def api_triage(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        text = str(payload.get("text") or "")
        if not text.strip():
            raise HTTPException(400, "No text supplied.")
        result = triage_text(
            db, text, requirements(),
            community=str(payload.get("community") or "manual").strip() or "manual",
            space=payload.get("space") or None,
            source_url=payload.get("url") or None,
            use_llm=bool(payload.get("use_llm")),
        )
        return {
            "total_posts": result.total_posts,
            "leads": result.leads,
            "not_leads": result.not_leads,
            "filtered": result.filtered,
            "duplicates": result.duplicates,
            "already_seen": result.already_seen,
        }

    # --- Triggered jobs (search / read) -----------------------------------

    @app.post("/api/jobs/search")
    def start_search(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        niche = str(payload.get("niche") or "").strip()
        if not niche:
            raise HTTPException(400, "Give a niche to search for.")
        min_score = int(payload.get("min_score", 15))

        def run(job):
            from circle_leads.discovery.web_search import discover_by_search
            from circle_leads.discovery.persist import persist_finds

            job.detail = f"Searching '{niche}'..."
            disc = discover_by_search(niche)
            res = persist_finds(
                db, disc.ranked, niche=niche, min_score=min_score, source="dashboard",
            )
            job.result = {
                "new": res.new_count, "updated": len(res.updated),
                "backend": disc.backend,
                "new_names": [c.name or c.slug for c in res.new[:10]],
            }
            job.detail = f"{res.new_count} new community/communities found."

        job = jobs.start("search", f"Search: {niche}", run)
        return {"job": job.as_dict()}

    @app.post("/api/jobs/read")
    def start_read(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        host = str(payload.get("host") or "").strip()
        space_ids = payload.get("space_ids") or []
        slug = str(payload.get("slug") or (host.split(".")[0] if host else "")).strip()
        if not host or not space_ids:
            raise HTTPException(400, "Give a community host and at least one space id.")

        def run(job):
            from circle_leads.scraper.browser_reader import (
                BrowserFeedReader, NotLoggedIn, fetch_space_posts,
            )
            from circle_leads.triage.pipeline import triage_text

            reader = BrowserFeedReader(host)
            if not reader.status():
                job.state = "error"
                job.detail = (
                    f"Not signed into {host}. In a terminal run: "
                    f"circle-leads read-feed {host} --login"
                )
                return
            records = []
            for sid in space_ids:
                try:
                    records.extend(
                        fetch_space_posts(reader, sid,
                                          excluded_content=requirements().excluded_content)
                    )
                except NotLoggedIn:
                    job.state = "error"
                    job.detail = "Session expired mid-read; re-run --login."
                    return
            if not records:
                job.detail = "No posts read."
                return
            text = "\n\n---\n\n".join(
                (r["title"] + "\n" + r["content"]) if r.get("title") else r["content"]
                for r in records
            )
            result = triage_text(
                db, text, requirements(), community=slug, source_url=reader.base,
            )
            job.result = {"leads": len(result.leads), "posts": result.total_posts,
                          "seen": result.already_seen}
            job.detail = f"{len(result.leads)} lead(s) from {result.total_posts} post(s)."

        job = jobs.start("read", f"Read: {host}", run)
        return {"job": job.as_dict()}

    @app.post("/api/jobs/harvest")
    def start_harvest(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        niches = payload.get("niches") or None  # list, or None for defaults
        only_new = bool(payload.get("only_new", True))
        no_search = bool(payload.get("no_search", False))

        # Default AI on whenever an OpenAI key is present, so ambiguous hiring
        # posts (e.g. "Forward Deployed Engineer x3, looking for...") escalate
        # to the LLM instead of being dropped by the rule classifier. The
        # "Use AI" checkbox can still explicitly force it on or off.
        _llm_default = bool(os.environ.get("OPENAI_API_KEY"))
        use_llm = bool(payload.get("use_llm", _llm_default))
        include_comments = bool(payload.get("include_comments", False))
        recency_days = int(payload.get("recency_days", requirements().harvest_recency_days))
        all_spaces = bool(payload.get("all_spaces", requirements().harvest_all_spaces))
        # "Re-check communities already read" (only_new=False) means: read the
        # ones already synced too, so bypass the recent-recheck skip window.
        force_recheck = bool(payload.get("force_recheck", not only_new))

        def run(job):
            from circle_leads.harvest import harvest

            log_activity_holder(
                kind="harvest", level="info",
                summary="Harvest started (dashboard)",
                detail={"only_new": only_new, "search": not no_search,
                        "use_llm": use_llm, "all_spaces": all_spaces,
                        "recency_days": recency_days},
            )
            job.detail = "Discovering + reading public communities..."
            res = harvest(
                db, requirements(), niches=niches, search=not no_search,
                only_new=only_new, verbose_log=True, use_llm=use_llm,
                include_comments=include_comments, recency_days=recency_days,
                all_spaces=all_spaces, force_recheck=force_recheck,
            )
            job.result = {
                "new_communities": res.new_communities,
                "communities_read": res.communities_read,
                "public_spaces": res.public_spaces,
                "posts_read": res.posts_read,
                "leads": res.leads_found,
            }
            job.detail = (
                f"{res.new_communities} new, read {res.communities_read} "
                f"community/communities, {res.leads_found} lead(s)."
            )
            log_activity_holder(
                kind="harvest",
                level="success" if res.leads_found else "info",
                summary=(
                    f"Harvest finished: {res.new_communities} new communities, "
                    f"read {res.communities_read}, {res.leads_found} lead(s)"
                ),
                items_seen=res.posts_read, leads_found=res.leads_found,
            )

        job = jobs.start("harvest", "Harvest", run)
        return {"job": job.as_dict()}

    @app.post("/api/jobs/reclassify")
    def start_reclassify(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        def run(job):
            from circle_leads.pipeline import classify_pending
            from circle_leads.storage.models import Post
            from sqlalchemy import update
            # Mark everything unclassified so the new rules re-decide it.
            with db.session() as s:
                s.execute(update(Post).values(classified=False))
            stats = classify_pending(db, requirements())
            job.result = stats
            job.detail = (
                f"Re-classified {stats.get('classified', 0)}: "
                f"{stats.get('leads', 0)} lead(s)."
            )
        job = jobs.start("reclassify", "Re-classify with new rules", run)
        return {"job": job.as_dict()}

    @app.get("/api/jobs")
    def list_jobs(_: None = Depends(require_auth)) -> dict[str, Any]:
        return {"jobs": jobs.list(), "search_running": jobs.active("search"),
                "read_running": jobs.active("read"),
                "harvest_running": jobs.active("harvest")}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, _: None = Depends(require_auth)) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return {"job": job.as_dict()}

    # --- Stats and activity ----------------------------------------------

    @app.get("/api/stats")
    def api_stats(_: None = Depends(require_auth)) -> dict[str, Any]:
        with db.session() as s:
            communities = s.scalar(select(func.count()).select_from(Community)) or 0
            posts = s.scalar(select(func.count()).select_from(Post)) or 0
            leads = s.scalar(
                select(func.count()).select_from(Lead).where(Lead.classification == "LEAD")
            ) or 0
            by_priority = dict(
                s.execute(
                    select(Lead.priority, func.count()).group_by(Lead.priority)
                ).all()
            )
            by_status = dict(
                s.execute(
                    select(Lead.review_status, func.count()).group_by(Lead.review_status)
                ).all()
            )
            by_community = dict(
                s.execute(
                    select(Community.slug, func.count(Lead.id))
                    .join(Post, Post.community_id == Community.id)
                    .join(Lead, Lead.post_id == Post.id)
                    .group_by(Community.slug)
                ).all()
            )
            skill_rows = s.scalars(select(Lead.skills)).all()
            decided = dict(
                s.execute(
                    select(Lead.decided_by, func.count()).group_by(Lead.decided_by)
                ).all()
            )

            # Leads and communities per day for the last fortnight (drives the
            # Overview growth chart).
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=14)
            daily_rows = s.execute(
                select(Lead.created_at).where(Lead.created_at >= cutoff)
            ).all()
            community_daily_rows = s.execute(
                select(Community.discovered_at).where(Community.discovered_at >= cutoff)
            ).all()

        skills = Counter()
        for row in skill_rows:
            for skill in row or []:
                skills[skill] += 1

        def daily_timeline(rows: list) -> list[dict[str, Any]]:
            counts = Counter(d[0].date().isoformat() for d in rows if d[0])
            points = [
                {
                    "date": (
                        datetime.now(timezone.utc).date() - timedelta(days=i)
                    ).isoformat(),
                    "count": 0,
                }
                for i in range(13, -1, -1)
            ]
            for point in points:
                point["count"] = counts.get(point["date"], 0)
            return points

        leads_timeline = daily_timeline(daily_rows)
        communities_timeline = daily_timeline(community_daily_rows)

        return {
            "communities": communities,
            "posts": posts,
            "leads": leads,
            "by_priority": by_priority,
            "by_status": by_status,
            "by_community": by_community,
            "top_skills": skills.most_common(10),
            "decided_by": decided,
            # "timeline" kept for backward compatibility with older clients;
            # new dashboards should use leads_timeline / communities_timeline.
            "timeline": leads_timeline,
            "leads_timeline": leads_timeline,
            "communities_timeline": communities_timeline,
        }

    @app.get("/api/activity")
    def api_activity(
        limit: int = 100, kind: str | None = None, _: None = Depends(require_auth)
    ) -> dict[str, Any]:
        with db.session() as s:
            return {"activity": recent_activity(s, limit=limit, kind=kind)}

    @app.get("/api/communities")
    def api_communities(_: None = Depends(require_auth)) -> dict[str, Any]:
        with db.session() as s:
            rows = s.scalars(
                select(Community).order_by(Community.relevance_score.desc())
            ).all()
            return {
                "communities": [
                    {
                        "slug": c.slug,
                        "name": c.name,
                        "url": c.url,
                        "price_label": c.price_label,
                        "relevance_score": c.relevance_score,
                        "relevance_reasons": c.relevance_reasons or [],
                        "discovered_at": (
                            c.discovered_at.isoformat() if c.discovered_at else None
                        ),
                        "relevant": c.relevant,
                        "watching": bool(c.watching),
                        "access_status": c.access_status,
                        "permission_status": c.permission_status,
                        "last_synced_at": (
                            c.last_synced_at.isoformat() if c.last_synced_at else None
                        ),
                    }
                    for c in rows
                ]
            }

    @app.get("/api/new-communities")
    def api_new_communities(limit: int = 50, _: None = Depends(require_auth)) -> dict[str, Any]:
        from circle_leads.discovery.persist import new_since
        return {"communities": new_since(db, limit=limit)}

    @app.post("/api/tick")
    @app.get("/api/tick")
    def api_tick(request: Request) -> dict[str, Any]:
        """Wake-up endpoint for an external cron pinger (free-tier scheduling).

        Runs a harvest only if the dashboard-set schedule says it is due, in a
        background thread so the ping returns immediately. Auth: either a signed
        session, or the TICK_TOKEN secret as ?token= (so a pinger can call it).
        """
        import os as _os
        from circle_leads.storage.settings_store import is_harvest_due, mark_harvest_run

        token = request.query_params.get("token", "")
        tick_token = _os.environ.get("TICK_TOKEN", "")
        authed = sessions.valid(request.cookies.get(COOKIE_NAME)) or (
            tick_token and token == tick_token
        )
        if not authed:
            raise HTTPException(401, "Not authenticated")

        if not is_harvest_due(db):
            return {"ran": False, "reason": "not due yet"}
        if jobs.active("harvest"):
            return {"ran": False, "reason": "already running"}

        mark_harvest_run(db)

        # On a fast (sub-hourly) schedule, run a light "read only" lane: don't
        # web-search every few minutes (slow + Exa cost), just re-read known
        # communities for new posts. Conditional 304s make that nearly free.
        # Bypass the re-check skip window so a 5-minute tick actually re-reads.
        # A full discovery search still runs on the first tick and about once a
        # day, so new communities are still found.
        from circle_leads.storage.settings_store import (
            _schedule_minutes, get_schedule, get_setting, set_setting,
        )
        import datetime as _dt

        minutes = _schedule_minutes(get_schedule(db)) or 999999
        fast_lane = minutes < 60
        do_search = True
        if fast_lane:
            last_search = get_setting(db, "harvest_last_search")
            do_search = not last_search
            if last_search:
                try:
                    prev = _dt.datetime.fromisoformat(last_search)
                    do_search = (_dt.datetime.utcnow() - prev) >= _dt.timedelta(hours=24)
                except ValueError:
                    do_search = True

        def run(job):
            from circle_leads.harvest import harvest
            res = harvest(
                db, requirements(), verbose_log=True,
                use_llm=bool(_os.environ.get("OPENAI_API_KEY")),
                search=do_search,
                force_recheck=fast_lane,   # fast lane ignores the 6h skip window
            )
            if do_search:
                set_setting(db, "harvest_last_search", _dt.datetime.utcnow().isoformat())
            job.result = {"new": res.new_communities, "read": res.communities_read,
                          "leads": res.leads_found}
            lane = "fast read" if fast_lane else "full"
            job.detail = f"Scheduled harvest ({lane}): {res.leads_found} lead(s)"

        jobs.start("harvest", "Scheduled harvest (tick)", run)
        return {"ran": True, "fast_lane": fast_lane, "searched": do_search}

    @app.get("/api/schedule")
    def api_schedule(_: None = Depends(require_auth)) -> dict[str, Any]:
        from circle_leads.storage.settings_store import (
            get_schedule, get_setting, SCHEDULE_INTERVALS, KEY_LAST_RUN,
        )
        return {
            "schedule": get_schedule(db),
            "options": list(SCHEDULE_INTERVALS.keys()),
            "last_run": get_setting(db, KEY_LAST_RUN),
        }

    @app.post("/api/schedule")
    def api_schedule_save(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        from circle_leads.storage.settings_store import set_schedule, get_schedule
        try:
            set_schedule(db, str(payload.get("schedule", "")))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        log_activity_holder(kind="review",
                            summary=f"Harvest schedule set to {get_schedule(db)}")
        return {"ok": True, "schedule": get_schedule(db)}

    @app.get("/api/config")
    def api_config(_: None = Depends(require_auth)) -> dict[str, Any]:
        data = requirements_to_dict(requirements())
        data["llm_available"] = bool(
            os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        )
        data["llm_provider"] = (
            "OpenAI" if os.environ.get("OPENAI_API_KEY")
            else "Anthropic" if os.environ.get("ANTHROPIC_API_KEY") else None
        )
        data["search_backend"] = (
            "Exa" if os.environ.get("EXA_API_KEY")
            else "Brave" if os.environ.get("BRAVE_API_KEY")
            else "SerpAPI" if os.environ.get("SERPAPI_API_KEY")
            else "DuckDuckGo (keyless — fewer results; set EXA_API_KEY)"
        )
        return data

    @app.post("/api/config")
    def api_config_save(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        # Never let the editor change what content is excluded, or the rate
        # limits -- those are safety settings, not lead tuning. Keep current.
        current = requirements_to_dict(requirements())
        payload.pop("llm_available", None)
        for locked in ("excluded_content", "rate_limit"):
            payload[locked] = current[locked]
        # Persist to the DB (writable) rather than the packaged YAML, which is
        # read-only on a serverless deploy (/var/task -> Errno 30).
        from circle_leads.config.settings import validate_requirements
        from circle_leads.storage.settings_store import set_requirements_override
        try:
            new_req = validate_requirements(payload)
        except Exception as exc:  # noqa: BLE001 - report validation errors to UI
            raise HTTPException(400, f"Invalid config: {exc}")
        # Store the canonical serialized form so it round-trips exactly.
        canonical = requirements_to_dict(new_req)
        set_requirements_override(db, canonical)
        requirements_holder["req"] = new_req
        log_activity_holder(kind="review", summary="Lead requirements updated via dashboard")
        return {"ok": True, "config": canonical}

    # --- Remote browser (server-hosted interactive Chromium) --------------
    # Proof of concept: the Circle session originates in a browser that lives
    # here, so nothing is exported from another machine and replayed. Opt-in
    # via REMOTE_BROWSER_ENABLED; every route requires the dashboard session.
    from circle_leads.web.remote_browser_api import (
        build_replay_router as _replay_router, build_router as _rb_router,
    )

    app.include_router(_rb_router(require_auth))
    # Version B experiment: cookie store + server-side replay (opt-in, encrypted).
    app.include_router(_replay_router(require_auth, db))

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
