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

from circle_leads.config.settings import load_requirements
from circle_leads.export.exporters import query_leads
from circle_leads.storage.activity import log_activity, recent_activity
from circle_leads.storage.database import Database
from circle_leads.storage.models import Community, Lead, Post
from circle_leads.triage.pipeline import triage_text
from circle_leads.triage.reply import draft_reply
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


def create_app(db_url: str | None = None, config_path: str | None = None) -> FastAPI:
    # Fail fast and loudly rather than serving other people's posts openly.
    get_password()

    app = FastAPI(title="Circle Leads", docs_url=None, redoc_url=None)
    db = Database(db_url or os.environ.get("CIRCLE_LEADS_DB") or None)
    requirements = load_requirements(config_path)
    sessions = SessionManager()

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
                row, your_name=os.environ.get("DASHBOARD_YOUR_NAME")
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

    # --- Triage -----------------------------------------------------------

    @app.post("/api/triage")
    def api_triage(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        text = str(payload.get("text") or "")
        if not text.strip():
            raise HTTPException(400, "No text supplied.")
        result = triage_text(
            db, text, requirements,
            community=str(payload.get("community") or "manual").strip() or "manual",
            space=payload.get("space") or None,
            source_url=payload.get("url") or None,
            use_llm=bool(payload.get("use_llm")),
            your_name=payload.get("your_name") or os.environ.get("DASHBOARD_YOUR_NAME"),
        )
        return {
            "total_posts": result.total_posts,
            "leads": result.leads,
            "not_leads": result.not_leads,
            "filtered": result.filtered,
            "duplicates": result.duplicates,
            "already_seen": result.already_seen,
        }

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

            # Leads per day for the last fortnight.
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=14)
            daily_rows = s.execute(
                select(Lead.created_at).where(Lead.created_at >= cutoff)
            ).all()

        skills = Counter()
        for row in skill_rows:
            for skill in row or []:
                skills[skill] += 1

        daily = Counter(d[0].date().isoformat() for d in daily_rows if d[0])
        timeline = [
            {
                "date": (
                    datetime.now(timezone.utc).date() - timedelta(days=i)
                ).isoformat(),
                "count": 0,
            }
            for i in range(13, -1, -1)
        ]
        for point in timeline:
            point["count"] = daily.get(point["date"], 0)

        return {
            "communities": communities,
            "posts": posts,
            "leads": leads,
            "by_priority": by_priority,
            "by_status": by_status,
            "by_community": by_community,
            "top_skills": skills.most_common(10),
            "decided_by": decided,
            "timeline": timeline,
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
                        "url": c.url,
                        "relevance_score": c.relevance_score,
                        "relevant": c.relevant,
                        "access_status": c.access_status,
                        "permission_status": c.permission_status,
                        "last_synced_at": (
                            c.last_synced_at.isoformat() if c.last_synced_at else None
                        ),
                    }
                    for c in rows
                ]
            }

    @app.get("/api/config")
    def api_config(_: None = Depends(require_auth)) -> dict[str, Any]:
        return {
            "target_roles": requirements.target_roles,
            "target_skills": requirements.target_skills,
            "minimum_confidence": requirements.minimum_confidence,
            "priority_high": requirements.priority_thresholds.high,
            "priority_medium": requirements.priority_thresholds.medium,
            "llm_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
        }

    return app


def run(host: str = "127.0.0.1", port: int = 8000, db_url: str | None = None) -> None:
    import uvicorn

    try:
        app = create_app(db_url=db_url)
    except AuthNotConfigured as exc:
        raise SystemExit(f"\n{exc}\n")

    if host not in ("127.0.0.1", "localhost"):
        print(
            f"\n  WARNING: binding to {host} exposes the dashboard beyond this "
            "machine.\n  It serves other people's posts. Use a tunnel or a "
            "firewall rather than a public bind.\n"
        )
    print(f"\n  Circle Leads dashboard → http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
