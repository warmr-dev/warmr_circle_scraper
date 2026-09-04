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
    load_requirements, requirements_to_dict, save_requirements,
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


def create_app(db_url: str | None = None, config_path: str | None = None) -> FastAPI:
    # Fail fast and loudly rather than serving other people's posts openly.
    get_password()

    app = FastAPI(title="Circle Leads", docs_url=None, redoc_url=None)
    db = Database(db_url or os.environ.get("CIRCLE_LEADS_DB") or None)
    requirements_holder = {"req": load_requirements(config_path)}
    config_file = config_path
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
                all_spaces=all_spaces,
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
                        "name": c.name,
                        "url": c.url,
                        "price_label": c.price_label,
                        "relevance_score": c.relevance_score,
                        "relevance_reasons": c.relevance_reasons or [],
                        "discovered_at": (
                            c.discovered_at.isoformat() if c.discovered_at else None
                        ),
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

        def run(job):
            from circle_leads.harvest import harvest
            res = harvest(db, requirements(), verbose_log=True,
                          use_llm=bool(_os.environ.get("OPENAI_API_KEY")))
            job.result = {"new": res.new_communities, "read": res.communities_read,
                          "leads": res.leads_found}
            job.detail = f"Scheduled harvest: {res.leads_found} lead(s)"

        jobs.start("harvest", "Scheduled harvest (tick)", run)
        return {"ran": True}

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
        try:
            new_req = save_requirements(payload, config_file)
        except Exception as exc:  # noqa: BLE001 - report validation errors to UI
            raise HTTPException(400, f"Invalid config: {exc}")
        requirements_holder["req"] = new_req
        log_activity_holder(kind="review", summary="Lead requirements updated via dashboard")
        return {"ok": True, "config": requirements_to_dict(new_req)}

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
