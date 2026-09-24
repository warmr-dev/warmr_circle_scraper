import pytest

from circle_leads.storage.database import (
    Database,
    content_hash,
    find_near_duplicate,
    get_or_create_community,
    hamming_distance,
    purge_community,
    simhash,
    upsert_post,
)


@pytest.fixture
def db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/test.db")


def test_upsert_is_idempotent(db):
    record = {"source_content_id": "p1", "content": "We are hiring a Flutter developer."}
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        _, first = upsert_post(s, community_id=c.id, record=record)
        cid = c.id
    with db.session() as s:
        _, second = upsert_post(s, community_id=cid, record=record)
    assert first == "new"
    assert second == "unchanged"


def test_edited_content_is_updated_and_requeued(db):
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        post, _ = upsert_post(
            s, community_id=c.id,
            record={"source_content_id": "p1", "content": "Original text"},
        )
        post.classified = True
        cid, pid = c.id, post.id

    with db.session() as s:
        post, outcome = upsert_post(
            s, community_id=cid,
            record={"source_content_id": "p1", "content": "Edited text, now hiring"},
        )
        assert outcome == "updated"
        # An edit must be re-examined rather than keeping a stale verdict.
        assert post.classified is False


def test_same_id_different_communities_do_not_collide(db):
    record = {"source_content_id": "1", "content": "Hiring a developer."}
    with db.session() as s:
        a = get_or_create_community(s, slug="a", url="https://a.circle.so")
        b = get_or_create_community(s, slug="b", url="https://b.circle.so")
        _, r1 = upsert_post(s, community_id=a.id, record=record)
        _, r2 = upsert_post(s, community_id=b.id, record=record)
    assert r1 == "new" and r2 == "new"


def test_content_hash_ignores_whitespace_and_case():
    assert content_hash("Hiring  a Dev") == content_hash("hiring a dev")
    assert content_hash("Hiring a dev") != content_hash("Hiring a designer")


def test_simhash_detects_near_duplicates():
    a = simhash("We are looking for a backend developer to build our API service")
    b = simhash("We are looking for a backend developer to build our API service!")
    c = simhash("Completely unrelated text about gardening and flowers in spring")
    assert hamming_distance(a, b) <= 3
    assert hamming_distance(a, c) > 12


def test_find_near_duplicate_across_spaces(db):
    text = "We are looking for a senior backend engineer to help build our API."
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        upsert_post(s, community_id=c.id,
                    record={"source_content_id": "p1", "content": text})
        cid = c.id
    with db.session() as s:
        dup, _ = upsert_post(
            s, community_id=cid,
            record={"source_content_id": "p2", "content": text + " Thanks!"},
        )
        assert find_near_duplicate(s, dup) is not None


def test_purge_community_is_a_kill_switch(db):
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        c.permission_status = "approved"
        for i in range(3):
            upsert_post(s, community_id=c.id,
                        record={"source_content_id": f"p{i}", "content": f"Post {i}"})

    with db.session() as s:
        assert purge_community(s, "acme") == 3

    with db.session() as s:
        from sqlalchemy import select
        from circle_leads.storage.models import Community, Post

        assert s.scalars(select(Post)).all() == []
        assert s.scalar(select(Community).where(Community.slug == "acme")).permission_status == "revoked"


def test_concurrent_duplicate_insert_is_race_safe(db):
    """Two processes inserting the same post (dashboard + worker) must not crash."""
    import threading

    with db.session() as s:
        c = get_or_create_community(s, slug="x", url="https://x.circle.so")
        cid = c.id
    record = {"source_content_id": "p1", "content": "We are hiring a Flutter developer"}
    results = []

    def insert():
        with db.session() as s:
            _, outcome = upsert_post(s, community_id=cid, record=record)
            results.append(outcome)

    threads = [threading.Thread(target=insert) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No exception; exactly one 'new', the other resolves to unchanged.
    assert "new" in results
    assert all(r in ("new", "unchanged") for r in results)
    from sqlalchemy import func, select
    from circle_leads.storage.models import Post
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Post)) == 1


@pytest.mark.parametrize("url,expected_prefix", [
    ("postgresql://u:p@host:5432/db", "postgresql+psycopg://"),
    ("postgres://u:p@host:5432/db", "postgresql+psycopg://"),
])
def test_postgres_url_uses_psycopg_v3(url, expected_prefix, monkeypatch):
    """A bare postgres URL must target psycopg (v3), which we ship, not psycopg2."""
    from circle_leads.storage import database as dbmod

    # Skip the real connect/create_all so the test needs no Postgres.
    monkeypatch.setattr(dbmod.Base.metadata, "create_all", lambda *a, **k: None)
    monkeypatch.setattr(dbmod, "create_engine", lambda u, **k: type("E", (), {"url": u})())

    d = dbmod.Database(url)
    assert d.url.startswith(expected_prefix)


# --- Malformed DB URL sanitizing (Railway/Supabase env-var gotchas) --------

import pytest as _pytest
from circle_leads.storage.database import _sanitize_db_url


@_pytest.mark.parametrize("raw,expected", [
    # Empty port (unset $PORT) -> colon removed; this was the Railway crash.
    ("postgresql://u:p@host:/db", "postgresql://u:p@host/db"),
    ("postgres://u:p@host:/db", "postgres://u:p@host/db"),
    ("postgresql://u:p@host:/db?sslmode=require",
     "postgresql://u:p@host/db?sslmode=require"),
    # Normal URL with a real port is untouched.
    ("postgresql://u:p@host:5432/db", "postgresql://u:p@host:5432/db"),
    # Whitespace / newlines / surrounding quotes are stripped.
    ("  postgresql://u:p@host:5432/db\n", "postgresql://u:p@host:5432/db"),
    ('"postgresql://u:p@host:/db"', "postgresql://u:p@host/db"),
])
def test_sanitize_db_url(raw, expected):
    assert _sanitize_db_url(raw) == expected


def test_supabase_session_pooler_rewrites_to_transaction_port(monkeypatch):
    """Session-mode :5432 on the Supabase pooler is rewritten to :6543."""
    from circle_leads.storage import database as dbmod

    captured = {}

    def fake_engine(u, **k):
        captured["url"] = u
        captured["kwargs"] = k
        return type("E", (), {"url": u})()

    monkeypatch.setattr(dbmod.Base.metadata, "create_all", lambda *a, **k: None)
    monkeypatch.setattr(dbmod.Database, "_ensure_columns", lambda self: None)
    monkeypatch.setattr(dbmod, "create_engine", fake_engine)

    d = dbmod.Database(
        "postgresql://u:p@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres"
    )
    assert d.url.startswith("postgresql+psycopg://")
    assert ":6543/" in d.url
    assert captured["kwargs"]["pool_size"] == 2
    assert captured["kwargs"]["max_overflow"] == 1
    assert captured["kwargs"]["connect_args"]["prepare_threshold"] is None


def test_empty_port_url_builds_engine(monkeypatch):
    """The empty-port URL must no longer crash Database(); it builds an engine."""
    from circle_leads.storage import database as dbmod
    monkeypatch.setattr(dbmod.Base.metadata, "create_all", lambda *a, **k: None)
    monkeypatch.setattr(dbmod.Database, "_ensure_columns", lambda self: None)
    monkeypatch.setattr(
        dbmod, "create_engine", lambda u, **k: type("E", (), {"url": u})()
    )
    # Would previously raise ValueError: invalid literal for int() with base 10.
    d = dbmod.Database("postgresql://u:p@host:/circle")
    assert d.url == "postgresql+psycopg://u:p@host/circle"


# --- preview -> full text (2026-09-19) --------------------------------------
# Every post read before 2026-09-18 was stored as Circle's ~255-char preview,
# and a read's identity is a hash of its text. Reading the same post in full
# must upgrade that row, not store the post a second time.

def _dated(content, sid, when):
    return {"source_content_id": sid, "content": content, "published_at": when}


_WHEN = __import__("datetime").datetime(2026, 6, 17, 13, 31, 4, 984000)
_FULL = ("Weekly update\n\n" + "We shipped the new onboarding flow this week. " * 8
         + "Also: we are hiring a contract Flutter developer, DM me.")
# What the reader used to store: the title, then the body cut at 255 chars
# (mid-word) with an ellipsis, on one line.
_PREVIEW = "Weekly update\n\n" + " ".join(_FULL.split("\n\n", 1)[1][:255].split()) + "…"


def test_full_text_upgrades_the_stored_preview_in_place(db):
    from sqlalchemy import func, select
    from circle_leads.storage.models import Post

    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        old, _ = upsert_post(s, community_id=c.id, record=_dated(_PREVIEW, "triage:old", _WHEN))
        old.classified = True
        cid, pid = c.id, old.id

    with db.session() as s:
        post, outcome = upsert_post(s, community_id=cid, record=_dated(_FULL, "triage:new", _WHEN))
        assert outcome == "updated"
        assert post.id == pid                      # same row, not a second one
        assert post.content == _FULL
        assert post.source_content_id == "triage:new"
        assert post.classified is False            # re-judged on the full text
        assert post.edited_at is None              # we read more; nobody edited

    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Post)) == 1
        # The next read of the full text finds the row by identity.
        _, again = upsert_post(s, community_id=cid, record=_dated(_FULL, "triage:new", _WHEN))
        assert again == "unchanged"


def test_a_post_at_another_time_is_not_taken_for_a_preview(db):
    from datetime import timedelta

    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        upsert_post(s, community_id=c.id, record=_dated(_PREVIEW, "triage:old", _WHEN))
        _, outcome = upsert_post(
            s, community_id=c.id,
            record=_dated(_FULL, "triage:new", _WHEN + timedelta(seconds=1)),
        )
    assert outcome == "new"


def test_undated_posts_are_never_matched_as_previews(db):
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        upsert_post(s, community_id=c.id, record=_dated(_PREVIEW, "triage:old", None))
        _, outcome = upsert_post(s, community_id=c.id, record=_dated(_FULL, "triage:new", None))
    assert outcome == "new"


def test_a_timezone_aware_timestamp_still_finds_the_preview(db):
    from datetime import timezone

    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        old, _ = upsert_post(s, community_id=c.id, record=_dated(_PREVIEW, "triage:old", _WHEN))
        post, outcome = upsert_post(
            s, community_id=c.id,
            record=_dated(_FULL, "triage:new", _WHEN.replace(tzinfo=timezone.utc)),
        )
        same_row = post.id == old.id
    assert outcome == "updated" and same_row


def test_is_preview_of_ignores_whitespace_and_a_cut_mid_word():
    from circle_leads.storage.database import _is_preview_of

    full = "Title\n\n" + "We are looking for a senior engineer to join our team in Berlin. " * 3
    assert _is_preview_of("Title We are looking for a senior engineer to join our team in Ber…", full)
    assert _is_preview_of("Title\nWe are looking for a senior engineer to join our team in Berl...", full)
    # Different words early on: another post, not a preview of this one.
    assert not _is_preview_of("Title We are looking for a junior designer to join our team in Ber…", full)
    assert not _is_preview_of(full, full)          # not shorter: nothing was cut
    assert not _is_preview_of("Title We are…", full)  # too short to tell


def test_display_name_only_authors_are_reused(db):
    # Readers that know only a name created a new author row per post per
    # read: 54,579 rows for 2,256 names in prod on 2026-09-19.
    from circle_leads.storage.database import get_or_create_author

    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        other = get_or_create_community(s, slug="other", url="https://other.circle.so")
        a1 = get_or_create_author(s, community_id=c.id, source_author_id=None, display_name="Jane Doe")
        a2 = get_or_create_author(s, community_id=c.id, source_author_id=None, display_name="Jane Doe")
        b = get_or_create_author(s, community_id=c.id, source_author_id=None, display_name="John Roe")
        elsewhere = get_or_create_author(s, community_id=other.id, source_author_id=None, display_name="Jane Doe")
        assert a1.id == a2.id
        assert b.id != a1.id
        assert elsewhere.id != a1.id               # names are per community


def test_a_later_read_attaches_the_member_id_and_releases_the_lead(db):
    """The post already points at the name-only row. A later read must fill
    that row's Circle member id and unstamp the lead the portal parked."""
    from datetime import datetime

    from sqlalchemy import select

    from circle_leads.storage.database import get_or_create_author
    from circle_leads.storage.models import Author, Lead, Post

    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        author = get_or_create_author(
            s, community_id=c.id, source_author_id=None, display_name="Keesha Brown",
        )
        post = Post(
            community_id=c.id, author_id=author.id, source_content_id="p1",
            content_type="post", content="We are hiring a Flutter developer.",
            url="https://acme.circle.so/c/jobs/1", dedup_hash="h1",
        )
        s.add(post)
        s.flush()
        s.add(Lead(
            post_id=post.id, classification="LEAD",
            external_synced_at=datetime(2026, 9, 21),
        ))
        author_id = author.id
        post_id = post.id

    with db.session() as s:
        again = get_or_create_author(
            s, community_id=s.get(Author, author_id).community_id,
            source_author_id="83308608", display_name="Keesha Brown",
            profile_url="https://acme.circle.so/u/b3a08215",
        )
        assert again.id == author_id
        assert again.source_author_id == "83308608"
        assert again.profile_url == "https://acme.circle.so/u/b3a08215"
        lead = s.scalar(select(Lead).where(Lead.post_id == post_id))
        assert lead.external_synced_at is None
