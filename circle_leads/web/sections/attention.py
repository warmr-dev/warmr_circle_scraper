"""Section 5: what needs a person.

Each rule is a question with a yes/no answer on the database -- a service went
quiet, a session died, the bot stopped on a join, Vini parked a lead -- and
produces one item per case, with what to do about it.

Two lessons shape this. A warning that cannot clear teaches the reader to
ignore the colour, so every item disappears on its own once the underlying
state changes. And a list that only grows gets ignored too, so a person can
mark an item as seen; the mark holds only while the item stays in the state
it was seen in (its fingerprint), and is dropped once the item clears, so the
next occurrence shows again.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from circle_leads.join.accounts import is_retired
from circle_leads.reach import JOIN_QUEUE_STATUSES, has_session, on_circle
from circle_leads.storage.heartbeat import HEARTBEAT_LIMITS_S
from circle_leads.storage.models import (
    JOIN_HANDOFF_PREFIX, ActivityLog, AttentionAck, CircleConnection, Community,
    ConnectionPriority, ConnectionState, JoinFormFill, JoinStatus, Lead, Post, ReplaySession,
    ScanJob, Setting, WatchState,
)
from circle_leads.web.overview import build_queues, connection_bucket
from circle_leads.web.sections.common import account_label, lead_live, not_sent_reason_expr
from circle_leads.web.sections.deps import (
    cached, fingerprint, get_db, iso, require_session, utc_now,
)

router = APIRouter(prefix="/api/dash", dependencies=[Depends(require_session)])

MAX_ITEMS = 50
SEVERITIES = ("crit", "warn", "info")


@dataclass
class Ctx:
    s: Session
    now: datetime
    settings: dict[str, str | None]
    runtime: dict[str, dict | None] = field(default_factory=dict)


@dataclass
class Rule:
    key: str
    severity: str
    title: str
    todo: str
    fn: Callable[[Ctx], list[dict]]


def _item(key: str, fp: str, title: str, *, detail: str | None = None,
          at: datetime | str | None = None, link: dict | None = None) -> dict[str, Any]:
    return {"key": key, "fp": fp, "title": title, "detail": detail,
            "at": iso(at) if isinstance(at, datetime) else at, "link": link}


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _shape(text: str | None) -> str:
    """The text with ids and counts taken out, so the same problem on another
    row or with another number is recognised as the same problem."""
    return re.sub(r"\d+", "#", (text or "").strip())[:160]


def _ago(now: datetime, then: datetime | None) -> str:
    if then is None:
        return "never"
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} d ago"


def _plural(n: int, one: str, many: str) -> str:
    """1 lead, 0 leads, 5 leads: the singular only for exactly one."""
    return f"{n} {one if n == 1 else many}"


def _community_link(community_id: int | None) -> dict | None:
    return {"section": "communities", "id": community_id} if community_id else None


# --- Services ----------------------------------------------------------------

def _heartbeat(ctx: Ctx) -> list[dict]:
    items = []
    limits = dict(HEARTBEAT_LIMITS_S)
    # Older deployments do not have these services. Once a lane has reported,
    # its disappearance is actionable rather than a permanent silent gap.
    for lane in ("recovery", "communities"):
        key = f"maintenance_{lane}_heartbeat"
        if key in ctx.settings:
            limits[key] = 900
    for key, limit in limits.items():
        name = key.replace("_heartbeat", "")
        raw = ctx.settings.get(key)
        at = _parse(raw)
        if at is not None and (ctx.now - at).total_seconds() <= limit:
            continue
        if not raw:
            text, state = "never reported", "never"
        elif at is None:
            text, state = "unreadable timestamp", "unreadable"
        else:
            text, state = f"silent, last heard {_ago(ctx.now, at)} (limit {limit // 60} min)", "stale"
        host = (ctx.runtime.get(name) or {}).get("host")
        items.append(_item(name, fingerprint(state, raw), f"{name}: {text}",
                           detail=f"last host: {host}" if host else None, at=at))
    return items


STAGE_LABELS = {"harvest": "Harvest", "icp_classification": "ICP and enrichment",
                "join_type": "Join type check"}


def _stage_error(ctx: Ctx) -> list[dict]:
    items = []
    for stage, label in STAGE_LABELS.items():
        raw = ctx.settings.get(f"{stage}_last_error")
        if not raw:
            continue
        try:
            error = json.loads(raw)
        except (TypeError, ValueError):
            continue
        failed_at = _parse(error.get("at"))
        finished_at = _parse(ctx.settings.get(f"{stage}_last_finish"))
        if finished_at is not None and failed_at is not None and finished_at > failed_at:
            continue  # it has worked since
        text = str(error.get("error") or "")
        items.append(_item(stage, fingerprint(_shape(text)), f"{label}: failed",
                           detail=text, at=failed_at))
    return items


def _harvest_interrupted(ctx: Ctx) -> list[dict]:
    started = _parse(ctx.settings.get("harvest_last_run"))
    finished = _parse(ctx.settings.get("harvest_last_finish"))
    if started is None or (finished is not None and finished >= started):
        return []
    running_for = ctx.now - started
    worker = ctx.runtime.get("worker") or {}
    beat = _parse(worker.get("beat_at"))
    fresh = beat is not None and (ctx.now - beat) < timedelta(minutes=10)
    stage = ((worker.get("stage") or {}).get("stage")) if fresh else None
    if stage in ("harvest", "job:harvest"):
        return []  # it is running right now
    # Without a fresh snapshot a running harvest looks the same as a dead one;
    # wait past the longest one seen (~8 h) before calling it interrupted.
    if running_for < (timedelta(minutes=15) if fresh else timedelta(hours=8)):
        return []
    detail = f"started {_ago(ctx.now, started)}, no finish recorded"
    if fresh:
        detail += f"; worker now: {stage or 'idle'}"
    return [_item("harvest", fingerprint(ctx.settings.get("harvest_last_run")),
                  "Harvest interrupted", detail=detail, at=started)]


def _runtime_env(ctx: Ctx) -> list[dict]:
    items = []
    for service in ("worker", "watcher"):
        snap = ctx.runtime.get(service)
        if not snap:
            continue
        env = snap.get("env") or {}
        missing = [k for k in ("VINI_API_SECRET", "SUPABASE_ANON_KEY") if env.get(k) is False]
        if missing:
            items.append(_item(f"{service}:vini", "missing",
                               f"{service}: no Vini keys — leads are not sent",
                               detail=", ".join(missing)))
        if not (snap.get("llm") or {}).get("backend"):
            items.append(_item(f"{service}:llm", "missing",
                               f"{service}: no model — only the rules decide",
                               detail="neither OPENAI_API_KEY nor ANTHROPIC_API_KEY is set"))
    return items


def _circle_throttled(ctx: Ctx) -> list[dict]:
    items = []
    now_ms = (ctx.now.replace(tzinfo=timezone.utc)).timestamp() * 1000
    for service in ("worker", "watcher"):
        gov = (ctx.runtime.get(service) or {}).get("governor") or {}
        until = gov.get("cooldownUntil")
        if until and float(until) > now_ms:
            until_dt = datetime.fromtimestamp(float(until) / 1000, tz=timezone.utc).replace(tzinfo=None)
            items.append(_item(f"{service}:cooldown", fingerprint(until),
                               f"{service}: Circle is throttling us",
                               detail=f"paused until {until_dt:%H:%M} UTC; {gov.get('cooldownReason') or ''}".strip("; "),
                               at=until_dt))
    recent = ctx.s.scalar(select(func.count(WatchState.id)).where(
        WatchState.last_status.in_(("ratelimited", "challenge")),
        WatchState.last_checked_at >= ctx.now - timedelta(minutes=30),
    )) or 0
    if recent >= 3:
        items.append(_item("watch", "watch",
                           f"Polling: {_plural(recent, 'community', 'communities')} "
                           "hit 429 or Cloudflare in the last 30 min",
                           link={"section": "monitoring", "status": "problem"}))
    return items


# --- Sessions and the feed watcher --------------------------------------------

CONN_LABELS = {"session_expired": "session expired", "access_denied": "access denied",
               "error": "read error"}


def _accounts_by_host(ctx: Ctx) -> dict[str, str]:
    """Which of our accounts is the member at each host, where known: the
    account the community was joined with, else a session label naming one."""
    out = {host: account for host, label in ctx.s.execute(select(
        ReplaySession.host, ReplaySession.member_label,
    )).all() if (account := account_label(label))}
    out.update({host: account for host, account in ctx.s.execute(select(
        Community.host, Community.join_account,
    ).where(Community.host.is_not(None), Community.join_account.is_not(None))).all()})
    return out


def _refresh_as(account: str | None) -> str:
    if not account:
        return " — account not recorded"
    if is_retired(account):
        return f" — logged in as account {account}, which is no longer in use: new login needed"
    return f" — refresh as account {account}"


def _recent_readable_sessions(ctx: Ctx) -> set[str]:
    """A connected flag alone is not current authentication evidence."""
    return set(ctx.s.scalars(select(CircleConnection.host).join(
        ReplaySession, ReplaySession.host == CircleConnection.host
    ).where(
        CircleConnection.state == ConnectionState.CONNECTED.value,
        CircleConnection.last_sync_at >= ctx.now - timedelta(hours=24),
        ReplaySession.updated_at <= CircleConnection.last_sync_at,
        CircleConnection.state_detail.is_not(None),
        ~CircleConnection.state_detail.startswith("No readable posts"),
        CircleConnection.spaces_readable > 0,
    )).all())


def _sessions(ctx: Ctx, *, cloudflare: bool) -> list[dict]:
    items: dict[str, dict] = {}
    accounts = {} if cloudflare else _accounts_by_host(ctx)
    rows = ctx.s.execute(select(
        CircleConnection.host, CircleConnection.state, CircleConnection.state_detail,
        CircleConnection.community_id, CircleConnection.last_sync_at,
    ).where(
        CircleConnection.state.in_(tuple(CONN_LABELS)),
        CircleConnection.priority != ConnectionPriority.PAUSED.value,
    )).all()
    # A community can move off Circle after we stored a session for it; there
    # is no Circle login left to refresh, so its dead session is not news.
    left_circle = set(ctx.s.scalars(select(Community.host).where(
        Community.host.is_not(None), ~on_circle())).all())
    for r in rows:
        blocked = connection_bucket(r.state, r.state_detail) == "cloudflare_blocked"
        if blocked != cloudflare or r.host in left_circle:
            continue
        label = "Cloudflare blocks the server" if blocked else (
            CONN_LABELS.get(r.state, r.state) + _refresh_as(accounts.get(r.host)))
        items[r.host] = _item(f"conn:{r.host}", fingerprint(r.state, _shape(r.state_detail)),
                              f"{r.host}: {label}", detail=r.state_detail, at=r.last_sync_at,
                              link=_community_link(r.community_id)
                              or {"section": "monitoring", "q": r.host})
    if not cloudflare:
        # Access-denied polling needs verification unless a recent readable
        # scan used credentials that have not since been replaced. A connected
        # flag from an old or empty scan cannot establish current access.
        scanned = _recent_readable_sessions(ctx)
        for host, cid, detail, at in ctx.s.execute(select(
            WatchState.host, WatchState.community_id, WatchState.last_detail,
            WatchState.last_checked_at,
        ).where(WatchState.mode == "cookie", WatchState.last_status == "unauthorized")).all():
            if host in scanned:
                continue
            items.setdefault(host, _item(
                f"conn:{host}", fingerprint("unauthorized"),
                f"{host}: session/feed access needs verification{_refresh_as(accounts.get(host))}",
                detail=detail, at=at, link=_community_link(cid)))
    return list(items.values())


def _watch_cookie_feed(ctx: Ctx) -> list[dict]:
    """Feed access failed after a recent readable scan of unchanged credentials."""
    readable = _recent_readable_sessions(ctx)
    rows = ctx.s.execute(select(
        WatchState.community_id, WatchState.host, WatchState.consecutive_errors,
        WatchState.last_checked_at, WatchState.last_detail,
        CircleConnection.last_sync_at, CircleConnection.state_detail,
    ).join(CircleConnection, CircleConnection.host == WatchState.host).where(
        WatchState.mode == "cookie", WatchState.last_status == "unauthorized",
        CircleConnection.state == ConnectionState.CONNECTED.value,
    ).order_by(WatchState.consecutive_errors.desc())).all()
    return [_item(f"watchfeed:{r.community_id}", fingerprint("unauthorized"),
                  f"{r.host}: feed with a session answered “access denied” "
                  f"{_plural(r.consecutive_errors, 'time', 'times')} in a row, "
                  f"after a recent readable cookie scan",
                  detail=f"Watcher: {r.last_detail or '—'}. Cookie scan "
                         f"{_ago(ctx.now, r.last_sync_at)}: {r.state_detail or '—'}",
                  at=r.last_checked_at, link=_community_link(r.community_id)) for r in rows if r.host in readable]


def _session_dead(ctx: Ctx) -> list[dict]:
    return _sessions(ctx, cloudflare=False)


def _session_cloudflare(ctx: Ctx) -> list[dict]:
    return _sessions(ctx, cloudflare=True)


def _watch_failing(ctx: Ctx) -> list[dict]:
    rows = ctx.s.execute(select(
        WatchState.community_id, WatchState.host, WatchState.mode, WatchState.last_status,
        WatchState.last_detail, WatchState.consecutive_errors, WatchState.last_checked_at,
    ).where(
        WatchState.mode != "off", WatchState.consecutive_errors >= 5,
        WatchState.last_status.notin_(("ratelimited", "challenge", "unauthorized")),
    ).order_by(WatchState.consecutive_errors.desc())).all()
    return [_item(f"watch:{r.community_id}", fingerprint(r.mode, r.last_status),
                  f"{r.host}: {_plural(r.consecutive_errors, 'error', 'errors')} in a row "
                  f"({r.last_status})",
                  detail=r.last_detail, at=r.last_checked_at,
                  link=_community_link(r.community_id)) for r in rows]


def _watch_member_off(ctx: Ctx) -> list[dict]:
    rows = ctx.s.execute(select(
        WatchState.community_id, WatchState.host, WatchState.last_status, WatchState.last_detail,
        WatchState.last_checked_at,
    ).join(Community, Community.id == WatchState.community_id).where(
        WatchState.mode == "off",
        (Community.join_status == JoinStatus.JOINED.value) | has_session(),
    )).all()
    return [_item(f"watch:{r.community_id}", fingerprint("off", r.last_status),
                  f"{r.host}: we are members, but polling is off ({r.last_status})",
                  detail=r.last_detail, at=r.last_checked_at,
                  link=_community_link(r.community_id)) for r in rows]


# --- Joins ----------------------------------------------------------------------

JOIN_LABELS = {
    JoinStatus.PROFILE_PENDING.value: "joined, but the profile / email code is still pending",
    JoinStatus.NEEDS_HUMAN.value: "join form needs a person's decision",
    JoinStatus.EXTERNAL_LOGIN.value: "login via the community's own site — needs an account there",
}
# Handoff outcomes where the bot itself could not tell what it saw.
UNCLEAR_HANDOFFS = ("unclear", "driver_error", "nav_failed")


def _join_needs_human(ctx: Ctx) -> list[dict]:
    rows = ctx.s.execute(select(
        Community.id, Community.name, Community.slug, Community.join_status,
        Community.join_status_detail, Community.join_attempted_at,
    ).where(Community.join_status.in_(tuple(JOIN_LABELS)))
      .order_by(Community.join_attempted_at.desc())).all()
    return [_item(f"join:{r.id}", fingerprint(r.join_status, _shape(r.join_status_detail)),
                  f"{r.name or r.slug}: {JOIN_LABELS[r.join_status]}",
                  detail=r.join_status_detail, at=r.join_attempted_at,
                  link=_community_link(r.id)) for r in rows]


def _join_pending_long(ctx: Ctx) -> list[dict]:
    rows = ctx.s.execute(select(
        Community.id, Community.name, Community.slug, Community.join_attempted_at,
    ).where(Community.join_status == JoinStatus.PENDING_APPROVAL.value,
            Community.join_attempted_at < ctx.now - timedelta(days=7))).all()
    return [_item(f"join:{r.id}", fingerprint(iso(r.join_attempted_at)),
                  f"{r.name or r.slug}: join request sent {_ago(ctx.now, r.join_attempted_at)}, "
                  "still awaiting approval",
                  at=r.join_attempted_at, link=_community_link(r.id)) for r in rows]


def _join_handoff(ctx: Ctx) -> list[dict]:
    rows = ctx.s.execute(select(
        Community.id, Community.name, Community.slug, Community.join_status_detail,
    ).where(Community.join_status.in_(JOIN_QUEUE_STATUSES),
            Community.join_status_detail.like(f"{JOIN_HANDOFF_PREFIX}%"))).all()
    items = []
    for r in rows:
        rest = (r.join_status_detail or "")[len(JOIN_HANDOFF_PREFIX):].strip()
        outcome = rest.split(" ", 1)[0] if rest else ""
        why = ("reason unknown" if outcome in UNCLEAR_HANDOFFS or not outcome
               else outcome)
        items.append(_item(f"join:{r.id}", fingerprint(outcome),
                           f"{r.name or r.slug}: join bot stopped — {why}",
                           detail=r.join_status_detail, link=_community_link(r.id)))
    return items


def _form_needs_human(ctx: Ctx) -> list[dict]:
    since = ctx.now - timedelta(days=30)
    open_questions: dict[str, dict] = {}
    rows = ctx.s.execute(select(
        JoinFormFill.host, JoinFormFill.community_id, JoinFormFill.question_id,
        JoinFormFill.label, JoinFormFill.outcome, JoinFormFill.created_at,
    ).where(JoinFormFill.created_at >= since).order_by(JoinFormFill.id)).all()
    for r in rows:
        key = (r.host or "", r.question_id or r.label)
        if r.outcome == "needs_human":
            open_questions[key] = {"host": r.host, "cid": r.community_id, "label": r.label,
                                   "at": r.created_at}
        else:
            open_questions.pop(key, None)  # answered later
    by_host: dict[str, list[dict]] = {}
    for q in open_questions.values():
        by_host.setdefault(q["host"] or "?", []).append(q)
    return [_item(f"form:{host}", fingerprint(*sorted(q["label"] for q in qs)),
                  f"{host}: join form asks something the join bot has no answer for",
                  detail="; ".join(q["label"] for q in qs),
                  at=max(q["at"] for q in qs), link=_community_link(qs[0]["cid"]))
            for host, qs in by_host.items()]


# --- Leads ------------------------------------------------------------------------

UNSENT_LABELS = {
    "held_for_review": "held for review — the model gave no verdict",
    "no_author": "no author — will never be sent",
    "pending": "waiting to be sent for over an hour",
}


def _lead_unsent(ctx: Ctx) -> list[dict]:
    reason = not_sent_reason_expr()
    rows = ctx.s.execute(
        select(reason, func.count(Lead.id), func.max(Lead.id), func.min(Lead.created_at))
        .join(Post, Lead.post_id == Post.id)
        .where(lead_live(), Lead.external_synced_at.is_(None), Lead.vini_status.is_(None),
               Lead.created_at < ctx.now - timedelta(hours=1))
        .group_by(reason)
    ).all()
    return [_item(f"unsent:{r}", fingerprint(newest),
                  f"{_plural(n, 'lead', 'leads')}: {UNSENT_LABELS.get(r, r)}", at=oldest,
                  link={"section": "leads", "segment": "not_sent", "reason": r})
            for r, n, newest, oldest in rows]


VINI_LABELS = {"held": "held by Vini", "rejected": "rejected by Vini",
               "error": "not delivered to Vini"}


def _vini_not_accepted(ctx: Ctx) -> list[dict]:
    rows = ctx.s.execute(
        select(Lead.vini_status, Lead.vini_reason, func.count(Lead.id), func.max(Lead.id),
               func.max(Lead.vini_last_attempt_at))
        .where(lead_live(), Lead.vini_status.in_(tuple(VINI_LABELS)))
        .group_by(Lead.vini_status, Lead.vini_reason)
        .order_by(func.count(Lead.id).desc())
    ).all()
    return [_item(f"vini:{status}:{fingerprint(_shape(reason))}", fingerprint(newest),
                  f"{_plural(n, 'lead', 'leads')}: {VINI_LABELS[status]}",
                  detail=reason or "no reason given",
                  at=last, link={"section": "leads", "segment": status, "reason": reason or ""})
            for status, reason, n, newest, last in rows]


# --- Queues and the log ------------------------------------------------------------

QUEUE_LABELS = {"name": "name lookup", "join_type_recheck": "join type recheck",
                "read": "first read", "join": "join"}


def _scan_jobs(ctx: Ctx) -> list[dict]:
    items = [_item(f"job:{j.id}", "error", f"Job {j.kind} {j.host or ''} failed".replace("  ", " "),
                   detail=j.detail, at=j.finished_at)
             for j in ctx.s.scalars(select(ScanJob).where(
                 ScanJob.state == "error",
                 ScanJob.created_at >= ctx.now - timedelta(days=7),
             ).order_by(ScanJob.id.desc())).all()]
    waiting = ctx.s.scalar(select(func.min(ScanJob.created_at)).where(ScanJob.state == "queued"))
    worker = ctx.runtime.get("worker") or {}
    busy = bool((worker.get("stage") or {}).get("stage"))
    if waiting is not None and ctx.now - waiting > timedelta(minutes=30) and not busy:
        items.append(_item("queue", "waiting", "Job queue not processed for over 30 min",
                           detail="the worker is not picking up jobs: check that it is alive", at=waiting))
    return items


def _queue_stale(ctx: Ctx) -> list[dict]:
    named = and_(Community.name.is_not(None), Community.name != "")
    return [_item(f"queue:{q['key']}", fingerprint(q["last_moved"]),
                  f"Queue “{QUEUE_LABELS.get(q['key'], q['key'])}”: {q['waiting']} waiting, "
                  f"last moved {_ago(ctx.now, _parse(q['last_moved']))}",
                  at=q["last_moved"])
            for q in build_queues(ctx.s, ctx.now, named=named) if q["stale"]]


def _log_problems(ctx: Ctx) -> list[dict]:
    """Warnings and errors of the last day that no other rule already covers,
    grouped by what they say."""
    rows = ctx.s.execute(select(
        ActivityLog.kind, ActivityLog.level, ActivityLog.community, ActivityLog.summary,
        ActivityLog.created_at, ActivityLog.detail,
    ).where(
        ActivityLog.level.in_(("warning", "error")),
        ActivityLog.created_at >= ctx.now - timedelta(hours=24),
        ActivityLog.kind.notin_(("export", "join")),  # rules of their own
    ).order_by(ActivityLog.id.desc()).limit(2000)).all()
    groups: dict[tuple, dict] = {}
    for kind, level, community, summary, at, audit in rows:
        problem = _problem_text(summary)
        if kind == "classify" and isinstance(audit, dict) and audit.get("error"):
            problem = f"Classification: {audit['error']}"
        shape = _shape(problem)
        group = groups.setdefault((kind, level, shape), {
            "kind": kind, "level": level, "shape": shape, "sample": problem,
            "count": 0, "communities": set(), "last": at})
        group["count"] += 1
        if community:
            group["communities"].add(community)
    ranked = sorted(groups.values(), key=lambda g: (g["level"] != "error", -g["count"]))
    items = []
    for g in ranked[:20]:
        where = sorted(g["communities"])
        detail = f"{g['count']}× in 24 h ({g['kind']})"
        if where:
            detail += ": " + ", ".join(where[:5]) + ("…" if len(where) > 5 else "")
        items.append(_item(f"log:{g['kind']}:{fingerprint(g['shape'])}", fingerprint(g["shape"]),
                           g["sample"][:160], detail=detail, at=g["last"],
                           link={"section": "log", "q": g["kind"] if g["kind"] == "classify" else g["sample"][:40]}))
    return items


def _problem_text(summary: str) -> str:
    """What went wrong, without which community it went wrong in: writers put
    the problem after " — " ("HTTP scan of x: 0 post(s) — Session cookie
    invalid"), so the same problem on thirty hosts is one group, not thirty."""
    return summary.split(" — ", 1)[1] if " — " in summary else summary


def _plaintext_cookies(ctx: Ctx) -> list[dict]:
    hosts = sorted(ctx.s.scalars(select(ReplaySession.host).where(
        ReplaySession.encrypted_cookies.like("plain:%"))).all())
    if not hosts:
        return []
    return [_item("plain", fingerprint(*hosts),
                  f"{_plural(len(hosts), 'session is', 'sessions are')} "
                  "stored unencrypted",
                  detail=", ".join(hosts[:20]) + ("…" if len(hosts) > 20 else ""))]


RULES: list[Rule] = [
    Rule("heartbeat", "crit", "Service is silent",
         "Check that the worker and the watcher are running on the server. While they are silent, nothing is read and no leads go out.",
         _heartbeat),
    Rule("stage_error", "crit", "Worker stage failed",
         "The error text is below. The stage reruns on schedule; if the error stays the same, fix the code or the keys.",
         _stage_error),
    Rule("runtime_env", "crit", "Service is missing keys",
         "Add the variables to the service's environment and restart it. Key values never reach this page — only whether each is set.",
         _runtime_env),
    Rule("lead_unsent", "crit", "Leads not sent to Vini",
         "“Held for review” — left after a model failure; they need re-evaluating. “No author” — the post was saved without an author, and Vini will not accept it.",
         _lead_unsent),
    Rule("harvest_interrupted", "warn", "Harvest did not finish",
         "The worker was restarted mid-harvest. The next harvest starts in 10 minutes; if this keeps happening, check the worker's log.",
         _harvest_interrupted),
    Rule("vini_not_accepted", "warn", "Vini did not accept leads",
         "The reason is in Vini's words. “Held” for a missing field is fixed in our sending code; the rest is the client's call.",
         _vini_not_accepted),
    Rule("join_handoff", "warn", "Join did not go through",
         "The join bot stopped and could not decide on its own. Open the community, see what is there, and join by hand or fix the join bot.",
         _join_handoff),
    Rule("join_needs_human", "warn", "Join needs a person",
         "A profile, an email code, a join form or a login via an external site — the join bot cannot do these on its own.",
         _join_needs_human),
    Rule("join_pending_long", "info", "Join request still pending",
         "Over a week without approval. Check the community by hand: they may have declined silently.",
         _join_pending_long),
    Rule("form_needs_human", "warn", "Unanswered join form questions",
         "Add an answer to join_form_answers — the join bot will use it next time.",
         _form_needs_human),
    Rule("session_dead", "warn", "Community session/access needs verification",
         "Log in to the community as the account shown and save a fresh session with the "
         "extension (or paste the cookies into the community card). Another account will not "
         "work: it is not a member there. Account not recorded — set it on the community card. "
         "Account no longer in use — join again with an active join bot account.",
         _session_dead),
    Rule("watch_cookie_feed", "warn", "Feed access fails after a recent readable scan",
         "The recent scan read spaces with credentials unchanged since that scan. "
         "Check the feed error and community permissions; successful scanning does not prove every endpoint is accessible.",
         _watch_cookie_feed),
    Rule("watch_failing", "warn", "Community polling is failing",
         "Five or more errors in a row. Open the community: is the address alive, has the domain changed?",
         _watch_failing),
    Rule("watch_member_off", "warn", "We are members, but polling is off",
         "Check the session and the address: a community we are in should be read every 2 minutes.",
         _watch_member_off),
    Rule("scan_jobs", "warn", "Worker jobs",
         "Failed job — the error text is below. Stuck queue — the worker is not picking up jobs.",
         _scan_jobs),
    Rule("queue_stale", "warn", "Queue is not moving",
         "A day without movement while the queue is not empty: the stage is not running or has nothing to work on.",
         _queue_stale),
    Rule("log_problems", "info", "Log warnings in the last 24 h",
         "Grouped by text. Click to open these entries in the Log.",
         _log_problems),
    Rule("circle_throttled", "info", "Circle is rate-limiting our IP",
         "The request limit kicked in. Nothing to do: the pause lifts on its own; if it happens often, lower the request rate.",
         _circle_throttled),
    Rule("session_cloudflare", "info", "Cloudflare blocks session reads from the server",
         "The cookies are live; the server's IP is blocked. Refreshing the session will not help.",
         _session_cloudflare),
    Rule("plaintext_cookies", "info", "Unencrypted cookies",
         "Set CIRCLE_CRED_KEY (the same on Vercel and the worker) and re-save the sessions.",
         _plaintext_cookies),
]
RULES_BY_KEY = {rule.key: rule for rule in RULES}


def _context(s: Session, now: datetime) -> Ctx:
    settings = dict(s.execute(select(Setting.key, Setting.value)).all())
    runtime = {}
    for service in ("worker", "watcher", "maintenance_recovery", "maintenance_communities"):
        try:
            runtime[service] = json.loads(settings.get(f"{service}_runtime") or "null")
        except (TypeError, ValueError):
            runtime[service] = None
    return Ctx(s=s, now=now, settings=settings, runtime=runtime)


def evaluate(s: Session, now: datetime) -> list[dict[str, Any]]:
    """Every rule's items. A rule that fails shows as an item, not as a 500."""
    ctx = _context(s, now)
    out = []
    for rule in RULES:
        try:
            items, error = rule.fn(ctx), None
        except Exception as exc:  # noqa: BLE001 - one broken rule must not hide the rest
            s.rollback()
            items, error = [], f"{type(exc).__name__}: {exc}"[:300]
        out.append({"key": rule.key, "severity": rule.severity, "title": rule.title,
                    "todo": rule.todo, "items": items, "error": error})
    return out


def apply_acks(s: Session, rules: list[dict[str, Any]], evaluated_at: datetime) -> dict[str, Any]:
    """Mark acknowledged items, and drop acknowledgements that no longer apply.

    An ack is dropped when its item is gone (the problem cleared) or changed
    state, so the next occurrence is shown again -- but only for rules that
    evaluated cleanly, and only acks older than the evaluation.
    """
    acks = {(a.rule, a.item_key): a for a in s.scalars(select(AttentionAck)).all()}
    stale: list[tuple[str, str]] = []
    firing: set[tuple[str, str]] = set()
    result, by_severity, open_total = [], {sev: 0 for sev in SEVERITIES}, 0
    for rule in rules:
        items, open_count = [], 0
        for item in rule["items"]:
            key = (rule["key"], item["key"])
            firing.add(key)
            ack = acks.get(key)
            acked = ack is not None and ack.fingerprint == item["fp"]
            if ack is not None and not acked and ack.acked_at <= evaluated_at:
                stale.append(key)
            if not acked:
                open_count += 1
            items.append({**item, "acked": acked,
                          "acked_at": iso(ack.acked_at) if acked else None,
                          "note": ack.note if acked else None})
        by_severity[rule["severity"]] += open_count
        open_total += open_count
        # Open items first, newest first within each group (two stable sorts).
        items.sort(key=lambda i: i["at"] or "", reverse=True)
        items.sort(key=lambda i: i["acked"])
        result.append({**rule, "items": items[:MAX_ITEMS], "count": len(items),
                       "open": open_count})
    clean = {rule["key"] for rule in rules if rule["error"] is None}
    for key, ack in acks.items():
        if key[0] in clean and key not in firing and ack.acked_at <= evaluated_at:
            stale.append(key)
    for rule_key, item_key in stale:
        s.execute(delete(AttentionAck).where(AttentionAck.rule == rule_key,
                                             AttentionAck.item_key == item_key))
    return {"rules": result, "open": open_total, "by_severity": by_severity}


@router.get("/attention")
def api_attention(request: Request, db=Depends(get_db),
                  now: datetime = Depends(utc_now)) -> dict[str, Any]:
    def compute() -> tuple[datetime, list[dict[str, Any]]]:
        with db.session() as s:
            return now, evaluate(s, now)

    evaluated_at, rules = cached(request, "attention", 60, compute)
    with db.session() as s:
        out = apply_acks(s, rules, evaluated_at)
    return {"evaluated_at": iso(evaluated_at), **out}


@router.post("/attention/ack")
def api_attention_ack(payload: dict, db=Depends(get_db),
                      now: datetime = Depends(utc_now)) -> dict[str, Any]:
    rule = str((payload or {}).get("rule") or "")
    item_key = str((payload or {}).get("item_key") or "")[:255]
    fp = str((payload or {}).get("fingerprint") or "")[:64]
    if rule not in RULES_BY_KEY or not item_key or not fp:
        raise HTTPException(400, "rule, item_key and fingerprint are required")
    note = (str(payload.get("note") or "").strip() or None)
    with db.session() as s:
        s.merge(AttentionAck(rule=rule, item_key=item_key, fingerprint=fp,
                             note=note[:1000] if note else None, acked_at=now))
    return {"ok": True}


@router.post("/attention/unack")
def api_attention_unack(payload: dict, db=Depends(get_db)) -> dict[str, Any]:
    rule = str((payload or {}).get("rule") or "")
    item_key = str((payload or {}).get("item_key") or "")
    with db.session() as s:
        s.execute(delete(AttentionAck).where(AttentionAck.rule == rule,
                                             AttentionAck.item_key == item_key))
    return {"ok": True}
