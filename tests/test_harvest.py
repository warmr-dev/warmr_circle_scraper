"""Tests for the harvest orchestrator (search -> read public -> classify)."""

import pytest
from datetime import datetime, timedelta, timezone

from circle_leads.config.settings import load_requirements
from circle_leads.harvest import _community_hosts, harvest
from circle_leads.storage.database import Database, get_or_create_community


@pytest.fixture
def reqs(dev_requirements):
    return dev_requirements


@pytest.fixture
def db(tmp_path):
    d = Database(f"sqlite:///{tmp_path}/harvest.db")
    with d.session() as s:
        c = get_or_create_community(
            s, slug="pub", url="https://pub.circle.so"
        )
        c.relevance_score = 50
    return d


class FakeSpace:
    def __init__(self, sid, name, is_public=None):
        self.id = sid
        self.slug = name.lower().replace(" ", "-")
        self.name = name
        self.is_public = is_public


class FakePublicReader:
    """Stub reader: one public "Job Posts" space with two posts."""

    def __init__(self, host, **kw):
        self.community_host = host
        self.base = f"https://{host}"

    def list_spaces(self):
        return [FakeSpace("1", "Job Posts")]

    def read_space(self, sid, **kw):
        return True, [
            {"id": 10, "name": "Hiring",
             "truncated_content": "We are hiring a software engineer",
             "created_at": "2026-08-30T10:00:00Z"},
            {"id": 11, "name": "",
             "truncated_content": "I'm a dev looking for a job, open to work"},
        ]


def test_harvest_reads_public_community(db, reqs, monkeypatch):
    import circle_leads.harvest as h

    monkeypatch.setattr(h, "PublicReader", FakePublicReader)
    result = harvest(db, reqs, search=False)
    assert result.communities_read == 1
    assert result.public_spaces == 1
    assert result.posts_read == 2
    assert result.leads_found == 1  # the hiring post, not the job seeker


def test_harvest_classifies_join_type(db, reqs, monkeypatch):
    """Every read (public or fully private) also classifies join_type."""
    import circle_leads.harvest as h
    from sqlalchemy import select
    from circle_leads.storage.models import Community
    from circle_leads.discovery.join_type import JoinClassification, JoinType

    class Reader:
        def __init__(self, host, **kw):
            self.community_host = host
            self.base = f"https://{host}"
            self.session = object()  # only needs to exist; fetch is stubbed below

        def list_spaces(self):
            return []  # fully private -- exactly the case join_type most matters for

        def read_space(self, sid, **kw):
            return False, []

    monkeypatch.setattr(h, "PublicReader", Reader)
    monkeypatch.setattr(
        h, "fetch_join_classification",
        lambda host, session=None: JoinClassification(JoinType.FREE_JOIN, "stubbed"),
    )
    harvest(db, reqs, search=False)
    with db.session() as s:
        c = s.scalar(select(Community).where(Community.slug == "pub"))
        assert c.join_type == JoinType.FREE_JOIN
        assert c.join_type_checked_at is not None


def _harvest_private_community(db, reqs, monkeypatch, *, price_label, live):
    """Run the harvest over the one fixture community, fully private, with
    ``live`` as the join check's answer; return (join_type, detail)."""
    import circle_leads.harvest as h
    from sqlalchemy import select
    from circle_leads.storage.models import Community

    class Reader:
        def __init__(self, host, **kw):
            self.community_host = host
            self.base = f"https://{host}"
            self.session = object()

        def list_spaces(self):
            return []

        def read_space(self, sid, **kw):
            return False, []

    with db.session() as s:
        s.scalar(select(Community).where(Community.slug == "pub")).price_label = price_label
    monkeypatch.setattr(h, "PublicReader", Reader)
    monkeypatch.setattr(h, "fetch_join_classification", lambda host, session=None: live)
    harvest(db, reqs, search=False)
    with db.session() as s:
        c = s.scalar(select(Community).where(Community.slug == "pub"))
        return c.join_type, c.join_type_detail


@pytest.mark.parametrize("label, live, expected", [
    ("$99/month", "locked_unknown", "paid"),
    ("Free", "unknown", "free_join"),
    ("Free", "locked_unknown", "locked_unknown"),
])
def test_harvest_refines_a_private_community_by_its_listing_price(db, reqs, monkeypatch, label, live, expected):
    """A private community answers 401 on every read. The harvest used to
    store that bare answer, turning a priced row back into locked_unknown."""
    from circle_leads.discovery.join_type import JoinClassification

    join_type, detail = _harvest_private_community(
        db, reqs, monkeypatch, price_label=label,
        live=JoinClassification(live, "HTTP 401"),
    )
    assert join_type == expected
    assert f"'{label}'" in detail


def test_harvest_keeps_a_manual_verdict(db, reqs, monkeypatch):
    from sqlalchemy import select
    from circle_leads.discovery.join_type import JoinClassification, JoinType
    from circle_leads.storage.models import Community

    with db.session() as s:
        c = s.scalar(select(Community).where(Community.slug == "pub"))
        c.join_type = JoinType.INVITE_ONLY
        c.join_type_detail = "manual check 2026-09-19: invite only"
    join_type, detail = _harvest_private_community(
        db, reqs, monkeypatch, price_label=None,
        live=JoinClassification(JoinType.LOCKED_UNKNOWN, "HTTP 401"),
    )
    assert (join_type, detail) == (JoinType.INVITE_ONLY, "manual check 2026-09-19: invite only")


def test_harvest_stores_a_conclusive_live_answer_over_the_listing_price(db, reqs, monkeypatch):
    from circle_leads.discovery.join_type import JoinClassification, JoinType

    join_type, detail = _harvest_private_community(
        db, reqs, monkeypatch, price_label="$99/month",
        live=JoinClassification(JoinType.FREE_JOIN, "allow_signups_to_public_community=true"),
    )
    assert join_type == JoinType.FREE_JOIN
    assert detail == "allow_signups_to_public_community=true"


def test_harvest_join_classification_failure_does_not_break_the_read(db, reqs, monkeypatch):
    """A classifier exception must not abort the read (matches real readers
    with no .session attribute -- the AttributeError itself is one example)."""
    import circle_leads.harvest as h

    monkeypatch.setattr(h, "PublicReader", FakePublicReader)
    monkeypatch.setattr(
        h, "fetch_join_classification",
        lambda host, session=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    result = harvest(db, reqs, search=False)
    assert result.communities_read == 1  # the read still completed


def test_harvest_marks_communities_synced(db, reqs, monkeypatch):
    import circle_leads.harvest as h
    from sqlalchemy import select
    from circle_leads.storage.models import Community

    class EmptyReader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"
        def list_spaces(self): return []
        def read_space(self, sid, **kw): return False, []
    monkeypatch.setattr(h, "PublicReader", EmptyReader)
    harvest(db, reqs, search=False)
    with db.session() as s:
        c = s.scalar(select(Community).where(Community.slug == "pub"))
        assert c.last_synced_at is not None


def test_harvest_only_new_skips_synced(db, reqs, monkeypatch):
    import circle_leads.harvest as h
    from datetime import datetime

    calls = []
    class CountingReader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"
        def list_spaces(self): calls.append(1); return []
        def read_space(self, sid, **kw): return False, []
    monkeypatch.setattr(h, "PublicReader", CountingReader)

    harvest(db, reqs, search=False)          # reads + marks synced
    calls.clear()
    harvest(db, reqs, search=False, only_new=True)  # should skip
    assert calls == []


def test_harvest_search_persists_new_communities(db, reqs, monkeypatch):
    import circle_leads.harvest as h

    class FakeDisc:
        backend = "stub"
        ranked = []

    class EmptyReader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"
        def list_spaces(self): return []
        def read_space(self, sid, **kw): return False, []
    monkeypatch.setattr(h, "discover_by_search", lambda *a, **k: FakeDisc())
    monkeypatch.setattr(h, "PublicReader", EmptyReader)
    result = harvest(db, reqs, niches=["x"], search=True)
    assert isinstance(result.new_communities, int)


def test_harvest_skips_non_subdomain_communities(db, reqs, monkeypatch):
    """Discover /products listings can't be read; only subdomains are."""
    import circle_leads.harvest as h

    with db.session() as s:
        c = get_or_create_community(
            s, slug="disc", url="https://discover.circle.so/products/x"
        )
        c.relevance_score = 90

    read = []
    class RecordingReader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"; read.append(host)
        def list_spaces(self): return []
        def read_space(self, sid, **kw): return False, []
    monkeypatch.setattr(h, "PublicReader", RecordingReader)
    harvest(db, reqs, search=False)
    assert "discover.circle.so" not in " ".join(read)


def test_harvest_reads_a_circle_custom_domain(db, reqs, monkeypatch):
    """platform='circle' on a custom domain is read, not just *.circle.so."""
    import circle_leads.harvest as h
    from circle_leads.scraper.public_reader import PublicSpace

    with db.session() as s:
        c = get_or_create_community(
            s, slug="forum", url="https://forum.joelpilger.com",
        )
        c.platform = "circle"
        c.relevance_score = 40

    read = []
    class Reader:
        def __init__(self, host, **kw):
            self.base = f"https://{host}"; read.append(host)
        def list_spaces(self):
            return [PublicSpace("1", "job-posts", "Job Posts")]
        def read_space(self, sid, **kw):
            return True, []
        def community_name(self): return "Forum"
    monkeypatch.setattr(h, "PublicReader", Reader)
    harvest(db, reqs, search=False)
    assert "forum.joelpilger.com" in read


def test_harvest_strips_a_deep_link_path_from_the_stored_url(db, reqs, monkeypatch):
    """A circle_directory-resolved join_url can be a deep link (checkout page,
    invitation link), not the community's origin. A naive scheme-strip left
    that path glued onto the host, breaking every API call built from it."""
    import circle_leads.harvest as h
    from circle_leads.scraper.public_reader import PublicSpace

    with db.session() as s:
        c = get_or_create_community(
            s, slug="aifire",
            url="https://community.aifire.co/c/welcome-checklist/?utm_source=circle_discover",
        )
        c.platform = "circle"
        c.relevance_score = 40

    read = []
    class Reader:
        def __init__(self, host, **kw):
            self.base = f"https://{host}"; read.append(host)
        def list_spaces(self):
            return [PublicSpace("1", "job-posts", "Job Posts")]
        def read_space(self, sid, **kw):
            return True, []
        def community_name(self): return "AI Fire"
    monkeypatch.setattr(h, "PublicReader", Reader)
    harvest(db, reqs, search=False)
    assert "community.aifire.co" in read


def test_harvest_skips_non_circle_platforms(db, reqs, monkeypatch):
    """A find classified as facebook/slack is never read."""
    import circle_leads.harvest as h

    with db.session() as s:
        c = get_or_create_community(
            s, slug="fbgroup", url="https://www.facebook.com/groups/devs",
        )
        c.platform = "facebook"
        c.relevance_score = 95  # would be first in line if it were readable

    read = []
    class Reader:
        def __init__(self, host, **kw):
            self.base = f"https://{host}"; read.append(host)
        def list_spaces(self): return []
        def read_space(self, sid, **kw): return False, []
        def community_name(self): return None
    monkeypatch.setattr(h, "PublicReader", Reader)
    harvest(db, reqs, search=False)
    assert not any("facebook.com" in host for host in read)


def test_harvest_rereads_known_communities(db, reqs, monkeypatch):
    """A previously-read community is re-read for new posts, not skipped."""
    import circle_leads.harvest as h
    from datetime import datetime, timedelta, timezone

    # Mark the seeded community as read a week ago.
    from sqlalchemy import select
    from circle_leads.storage.models import Community
    with db.session() as s:
        c = s.scalar(select(Community).where(Community.slug == "pub"))
        c.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)

    read_calls = []
    class Reader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"
        def list_spaces(self):
            from circle_leads.scraper.public_reader import PublicSpace
            return [PublicSpace("1", "job-posts", "Job Posts")]
        def read_space(self, sid, since=None, **kw):
            read_calls.append(since)  # records the watermark used
            return True, []
    monkeypatch.setattr(h, "PublicReader", Reader)

    h.harvest(db, reqs, search=False)  # not only_new -> should re-read
    assert read_calls  # the known community WAS read
    assert read_calls[0] is not None  # with a since watermark


def test_only_new_skips_known_communities(db, reqs, monkeypatch):
    import circle_leads.harvest as h
    from datetime import datetime
    from sqlalchemy import select
    from circle_leads.storage.models import Community
    with db.session() as s:
        s.scalar(select(Community).where(Community.slug == "pub")).last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)

    read = []
    class Reader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"
        def list_spaces(self): read.append(host := host if False else 1); return []
        def read_space(self, sid, **kw): return False, []
    monkeypatch.setattr(h, "PublicReader", Reader)
    h.harvest(db, reqs, search=False, only_new=True)  # skip the already-read one
    assert read == []


def test_first_read_is_clamped_to_the_age_cap(db, reqs, monkeypatch):
    """A never-read community's first pass must still be bounded by the age cap,
    not read as an unbounded full backlog (since=None)."""
    import circle_leads.harvest as h
    from circle_leads.scraper.public_reader import PublicSpace

    reqs = reqs.model_copy(update={"max_post_age_days": 365})
    since_used = []

    class Reader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"
        def list_spaces(self):
            return [PublicSpace("1", "job-posts", "Job Posts")]
        def read_space(self, sid, since=None, **kw):
            since_used.append(since)
            return True, []
    monkeypatch.setattr(h, "PublicReader", Reader)

    h.harvest(db, reqs, search=False)
    assert since_used and since_used[0] is not None
    age = datetime.now(timezone.utc).replace(tzinfo=None) - since_used[0]
    assert timedelta(days=364) < age < timedelta(days=366)


def test_age_cap_of_zero_keeps_the_full_backlog_first_read(db, reqs, monkeypatch):
    import circle_leads.harvest as h
    from circle_leads.scraper.public_reader import PublicSpace

    reqs = reqs.model_copy(update={"max_post_age_days": 0})
    since_used = []

    class Reader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"
        def list_spaces(self):
            return [PublicSpace("1", "job-posts", "Job Posts")]
        def read_space(self, sid, since=None, **kw):
            since_used.append(since)
            return True, []
    monkeypatch.setattr(h, "PublicReader", Reader)

    h.harvest(db, reqs, search=False)
    assert since_used == [None]  # unbounded first read, as before


def test_harvest_survives_a_bad_space(db, reqs, monkeypatch):
    """One space that raises must not abort the whole run."""
    import circle_leads.harvest as h
    from circle_leads.scraper.public_reader import PublicSpace

    class Reader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"
        def list_spaces(self):
            return [PublicSpace("1", "job-board", "Job Board"),
                    PublicSpace("2", "job-posts", "Job Posts")]
        def read_space(self, sid, **kw):
            if str(sid) == "1":
                raise ValueError("malformed payload")
            return True, [{"id": 9, "name": "Hiring",
                           "truncated_content": "We are hiring a software engineer",
                           "slug": "hiring"}]
        def read_comments(self, pid): return []

    monkeypatch.setattr(h, "PublicReader", Reader)
    result = h.harvest(db, reqs, search=False, all_spaces=True)
    # The good space was still read despite the other one erroring.
    assert result.public_spaces == 1
    assert any("Job Board" in e for e in result.errors)


# --- Which communities the scraper picks, and in what order --------------------


def _seed(db, rows):
    """rows: (slug, icp_flag, icp_score, last_synced_at, watching)."""
    from circle_leads.storage.database import get_or_create_community

    with db.session() as s:
        for slug, icp, score, synced, watching in rows:
            c = get_or_create_community(s, slug=slug, url=f"https://{slug}.circle.so")
            c.platform = "circle"
            c.icp_flag = icp
            c.icp_score = score
            c.last_synced_at = synced
            c.watching = watching
            # Deliberately inverted against the intended order: the old code
            # sorted on this column, so a test that passes on it by accident
            # would be worthless.
            c.relevance_score = 0 if icp else 99


def test_scraper_reads_icp_fit_communities_before_the_rest(tmp_path):
    """The ICP queue must actually feed the scraper.

    Ordering used to be relevance_score (the older web-search heuristic), which
    never looked at icp_flag -- so the dashboard's "queued for the scraper"
    backlog fed nothing.
    """
    db = Database(f"sqlite:///{tmp_path}/order.db")
    _seed(db, [
        ("not-icp-high-relevance", False, 0, None, False),
        ("icp-fit", True, 30, None, False),
    ])

    slugs = [slug for _, slug, _, _ in _community_hosts(db, limit=10)]
    assert slugs.index("icp-fit") < slugs.index("not-icp-high-relevance")


def test_never_read_communities_come_before_already_read_ones(tmp_path):
    """The head must rotate, not set.

    In prod 195 rows permanently occupied a 150-row head and nothing outside it
    had ever been synced, because the sort key was fixed. Sorting by
    last_synced_at (NULLs first) means reading a community moves it to the back.
    """
    db = Database(f"sqlite:///{tmp_path}/rotate.db")
    old = datetime(2026, 9, 1)
    older = datetime(2026, 8, 1)
    _seed(db, [
        ("read-recently", True, 90, old, False),
        ("read-long-ago", True, 40, older, False),
        ("never-read", True, 10, None, False),
    ])

    slugs = [slug for _, slug, _, _ in _community_hosts(db, limit=10)]
    # Never-read first, then least-recently-read -- score only breaks ties, so
    # the highest-scoring row no longer wins every single run.
    assert slugs == ["never-read", "read-long-ago", "read-recently"]


def test_watched_communities_still_take_the_fast_lane(tmp_path):
    """The subscription fast lane predates this change and must survive it."""
    db = Database(f"sqlite:///{tmp_path}/watch.db")
    _seed(db, [
        ("icp-never-read", True, 90, None, False),
        ("watched-already-read", False, 0, datetime(2026, 9, 1), True),
    ])

    slugs = [slug for _, slug, _, _ in _community_hosts(db, limit=10)]
    assert slugs[0] == "watched-already-read"
