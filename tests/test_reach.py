"""One reading rule for the harvest, the feed poller and the join bot.

circle_leads/reach.py, as set on 2026-09-24: a free community is read
anonymously or joined and read with the bot's login; a paid one is never
joined and is read only when a member session for it exists. Without one it
is counted and listed, and takes part in nothing else.
"""

import tempfile
from pathlib import Path

from sqlalchemy import select

from circle_leads.harvest import _community_hosts
from circle_leads.join import joiner
from circle_leads.storage.database import Database, get_or_create_community
from circle_leads.storage.models import ReplaySession, WatchState
from circle_leads.watch.poller import ensure_watch_rows


def _db():
    return Database("sqlite:///" + tempfile.mktemp(suffix=".db"))


def _c(s, slug, *, url=None, platform="circle", join_type="free_join", icp=True):
    c = get_or_create_community(s, slug=slug, url=url or f"https://{slug}.circle.so")
    c.name, c.platform, c.join_type, c.icp_flag = slug, platform, join_type, icp
    return c


def _seed(db):
    with db.session() as s:
        _c(s, "free")
        _c(s, "paid-no-login", join_type="paid")
        _c(s, "paid-bought", join_type="paid")
        s.add(ReplaySession(host="paid-bought.circle.so", encrypted_cookies="x", cookie_count=1))
        _c(s, "listed", platform="discover", url="https://www.listed.com/?utm_source=circle_discover")
        _c(s, "card", platform="discover", url="https://discover.circle.so/products/card")
        _c(s, "legacy-custom", platform=None, url="https://community.legacy.com")
        _c(s, "legacy-unproven", platform=None, url="https://www.somesite.com", join_type=None)
        _c(s, "lapsed", join_type="subscription_expired")
        _c(s, "landing", platform="other", url="https://www.landing.com")


def test_the_harvest_reads_what_the_rule_allows():
    db = _db()
    _seed(db)
    slugs = {slug for _, slug, _, _ in _community_hosts(db, limit=50)}
    # A paid community is read with a login someone bought, never without.
    # A directory community is read at its real address, never at the card.
    # A community older than the platform column counts once Circle's own API
    # answered for it (community.freelancemvp.com was skipped on URL shape).
    assert slugs == {"free", "paid-bought", "listed", "legacy-custom"}


def test_the_harvest_reads_the_real_address_not_the_stored_link():
    db = _db()
    _seed(db)
    hosts = {slug: host for host, slug, _, _ in _community_hosts(db, limit=50)}
    assert hosts["listed"] == "www.listed.com"


def test_the_feed_poller_watches_what_the_rule_allows():
    db = _db()
    _seed(db)
    ensure_watch_rows(db)
    with db.session() as s:
        hosts = set(s.scalars(select(WatchState.host)).all())
    # The same set the harvest reads: one rule, not two.
    assert hosts == {"free.circle.so", "paid-bought.circle.so", "www.listed.com",
                     "community.legacy.com"}


def test_the_join_bot_never_queues_a_paid_community(monkeypatch):
    monkeypatch.setattr(joiner, "ATTEMPT_LOG_PATH", Path(tempfile.mktemp(suffix=".log")))
    db = _db()
    _seed(db)
    slugs = {c["slug"] for c in joiner.select_join_candidates(db)}
    assert slugs == {"free", "listed", "legacy-custom"}


def test_paused_member_is_excluded_from_all_scheduled_readers_and_can_resume():
    from datetime import datetime, timedelta
    from circle_leads.scanning import cookie_hosts_vip_first
    from circle_leads.storage.models import CircleConnection, Community, WatchState
    from circle_leads.watch.poller import due_states

    db = _db()
    with db.session() as s:
        paused = _c(s, 'paused-member', join_type='paid')
        paused.join_status = 'joined'
        s.flush()
        paused_id = paused.id
        s.add(CircleConnection(host=paused.host, priority='paused'))
        s.add(ReplaySession(host=paused.host, encrypted_cookies='plain:[]', cookie_count=0))
        s.add(WatchState(community_id=paused.id, host=paused.host, mode='off',
                         next_check_at=datetime.utcnow() - timedelta(minutes=1)))
        _c(s, 'active')

    ensure_watch_rows(db)
    assert 'paused-member.circle.so' not in {r['host'] for r in due_states(db)}
    assert 'paused-member' not in {slug for _, slug, _, _ in _community_hosts(db, limit=50)}
    assert 'paused-member.circle.so' not in cookie_hosts_vip_first(db)
    with db.session() as s:
        assert s.scalar(select(WatchState).where(WatchState.community_id == paused_id)).mode == 'off'
        assert s.get(Community, paused_id) is not None
        conn = s.scalar(select(CircleConnection).where(CircleConnection.host == 'paused-member.circle.so'))
        conn.priority = 'normal'
    ensure_watch_rows(db)
    assert 'paused-member.circle.so' in {r['host'] for r in due_states(db)}
    assert 'paused-member' in {slug for _, slug, _, _ in _community_hosts(db, limit=50)}
    assert 'paused-member.circle.so' in cookie_hosts_vip_first(db)


def test_pause_takes_effect_for_an_already_due_anonymous_feed():
    from datetime import datetime, timedelta
    from circle_leads.storage.models import CircleConnection, WatchState
    from circle_leads.watch.poller import due_states

    db = _db()
    with db.session() as s:
        c = _c(s, 'anonymous-paused')
        s.flush()
        s.add(WatchState(community_id=c.id, host=c.host, mode='anon',
                         next_check_at=datetime.utcnow() - timedelta(minutes=1)))
        s.add(CircleConnection(host=c.host, priority='paused'))
    assert due_states(db) == []
    assert _community_hosts(db, limit=50) == []
    ensure_watch_rows(db)
    assert due_states(db) == []
