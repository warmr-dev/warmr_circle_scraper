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
