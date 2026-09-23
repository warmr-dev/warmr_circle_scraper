#!/usr/bin/env python3
"""Merge community rows that describe the same host.

`get_or_create_community` matches on an exact `url` string, so the same
community entered twice under different spellings -- once from a directory link
(`/join?invitation_token=...`, `?utm_source=circle_discover`) and once from the
bare address -- becomes two rows on one host. The harvest then reads the same
posts into both, and both sides produce leads: on 2026-09-23 that was 121
doubled posts on community.launchthedamnthing.com alone.

This script reports the merge plan by default and only touches the database
with --apply. Each host is merged in its own transaction, and everything that
gets deleted is written to a JSON backup first.

Nothing here is Circle-facing: it makes no network requests.

    python3 merge_duplicate_communities.py                 # show the plan
    python3 merge_duplicate_communities.py --host x.com    # one host
    python3 merge_duplicate_communities.py --apply         # do it
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

# Directory pages, not communities: `discover.circle.so/products/<slug>` is a
# Circle marketing listing that answers 404 to the read paths. Rows on this
# host share it by accident and must never be merged into one community.
DIRECTORY_HOSTS = {"discover.circle.so", "circle.so", "www.circle.so"}

# Child tables whose rows simply move to the keeper: no unique key that could
# collide with a row the keeper already owns.
PLAIN_CHILD_TABLES = (
    ("public", "scrape_runs"),
    ("warmr_app", "tasks"),
    ("public", "join_attempts"),
    ("public", "join_form_fills"),
)


def host_of(url: str) -> str:
    return (urlsplit(url).netloc or "").lower()


def db_url(raw: str) -> str:
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+psycopg://", raw)


def load_groups(conn: Connection, only_host: str | None) -> dict[str, list[dict]]:
    rows = conn.execute(text("""
        SELECT id, slug, name, url, join_status, joined_at, join_attempts, watching,
               icp_flag, icp_score, relevant, access_status, last_synced_at,
               external_directory_id, discovered_at,
               (SELECT count(*) FROM posts p WHERE p.community_id = c.id) AS posts
        FROM communities c
    """)).mappings().all()

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        host = host_of(row["url"])
        if not host or host in DIRECTORY_HOSTS:
            continue
        groups[host].append(dict(row))
    out = {h: v for h, v in groups.items() if len(v) > 1}
    if only_host:
        out = {h: v for h, v in out.items() if h == only_host}
    return out


def pick_keeper(rows: list[dict]) -> dict:
    """The row that already holds the most history wins; ties go to the oldest id."""
    return max(rows, key=lambda r: (r["posts"], r["join_status"] == "joined",
                                    bool(r["watching"]), -r["id"]))


def plan_group(conn: Connection, host: str, rows: list[dict]) -> dict:
    keeper = pick_keeper(rows)
    losers = [r for r in rows if r["id"] != keeper["id"]]
    plan = {"host": host, "keeper": keeper["id"], "keeper_url": keeper["url"],
            "canonical_url": f"https://{host}", "losers": [], "blocked": []}

    for loser in losers:
        ids = {"keeper": keeper["id"], "loser": loser["id"]}
        moved = conn.execute(text("""
            SELECT count(*) FROM posts l
            WHERE l.community_id = :loser AND NOT EXISTS (
              SELECT 1 FROM posts k WHERE k.community_id = :keeper
                AND k.source_content_id = l.source_content_id
                AND k.content_type = l.content_type)"""), ids).scalar()
        collide = conn.execute(text("""
            SELECT count(*) FROM posts l
            WHERE l.community_id = :loser AND EXISTS (
              SELECT 1 FROM posts k WHERE k.community_id = :keeper
                AND k.source_content_id = l.source_content_id
                AND k.content_type = l.content_type)"""), ids).scalar()
        # A colliding post that carries a lead cannot simply be dropped: the
        # lead may already be in Vini. It is re-pointed when the keeper's post
        # has no lead of its own, and reported for a human otherwise.
        repointable = conn.execute(text("""
            SELECT count(*) FROM posts l
            JOIN leads ll ON ll.post_id = l.id
            JOIN posts k ON k.community_id = :keeper
              AND k.source_content_id = l.source_content_id
              AND k.content_type = l.content_type
            WHERE l.community_id = :loser
              AND NOT EXISTS (SELECT 1 FROM leads kl WHERE kl.post_id = k.id)"""), ids).scalar()
        both_leads = conn.execute(text("""
            SELECT l.source_content_id,
                   (SELECT id FROM leads WHERE post_id = l.id) AS loser_lead,
                   (SELECT id FROM leads WHERE post_id = k.id) AS keeper_lead,
                   (SELECT external_synced_at FROM leads WHERE post_id = l.id) AS loser_synced
            FROM posts l
            JOIN posts k ON k.community_id = :keeper
              AND k.source_content_id = l.source_content_id
              AND k.content_type = l.content_type
            WHERE l.community_id = :loser
              AND EXISTS (SELECT 1 FROM leads ll WHERE ll.post_id = l.id)
              AND EXISTS (SELECT 1 FROM leads kl WHERE kl.post_id = k.id)"""), ids).mappings().all()

        entry = {
            "id": loser["id"], "url": loser["url"], "slug": loser["slug"],
            "posts_move": moved, "posts_collide": collide,
            "leads_repointed": repointable,
            "leads_double": [dict(r) for r in both_leads],
            "spaces": conn.execute(text("SELECT count(*) FROM spaces WHERE community_id=:loser"), ids).scalar(),
            "authors": conn.execute(text("SELECT count(*) FROM authors WHERE community_id=:loser"), ids).scalar(),
            "connections": conn.execute(text("SELECT count(*) FROM circle_connections WHERE community_id=:loser"), ids).scalar(),
        }
        if both_leads:
            plan["blocked"].append(entry)
        else:
            plan["losers"].append(entry)
    return plan


def merge_group(conn: Connection, host: str, rows: list[dict], backup: list) -> None:
    keeper = pick_keeper(rows)
    for loser in [r for r in rows if r["id"] != keeper["id"]]:
        ids = {"keeper": keeper["id"], "loser": loser["id"]}

        # 1. Spaces: the keeper's space wins; the loser's posts are re-pointed
        #    at it before the duplicate space row goes away.
        conn.execute(text("""
            UPDATE posts p SET space_id = k.id
            FROM spaces l JOIN spaces k
              ON k.community_id = :keeper AND k.source_space_id = l.source_space_id
            WHERE l.community_id = :loser AND p.space_id = l.id"""), ids)
        conn.execute(text("""
            DELETE FROM spaces l WHERE l.community_id = :loser AND EXISTS (
              SELECT 1 FROM spaces k WHERE k.community_id = :keeper
                AND k.source_space_id = l.source_space_id)"""), ids)
        conn.execute(text("UPDATE spaces SET community_id=:keeper WHERE community_id=:loser"), ids)

        # 2. Authors: same shape as spaces.
        conn.execute(text("""
            UPDATE posts p SET author_id = k.id
            FROM authors l JOIN authors k
              ON k.community_id = :keeper AND k.source_author_id = l.source_author_id
            WHERE l.community_id = :loser AND p.author_id = l.id"""), ids)
        conn.execute(text("""
            DELETE FROM authors l WHERE l.community_id = :loser AND EXISTS (
              SELECT 1 FROM authors k WHERE k.community_id = :keeper
                AND k.source_author_id = l.source_author_id)"""), ids)
        conn.execute(text("UPDATE authors SET community_id=:keeper WHERE community_id=:loser"), ids)

        # 3. A lead on a doubled post survives on the keeper's copy.
        conn.execute(text("""
            UPDATE leads ll SET post_id = k.id
            FROM posts l JOIN posts k ON k.community_id = :keeper
              AND k.source_content_id = l.source_content_id
              AND k.content_type = l.content_type
            WHERE ll.post_id = l.id AND l.community_id = :loser
              AND NOT EXISTS (SELECT 1 FROM leads kl WHERE kl.post_id = k.id)"""), ids)

        # 4. Doubled posts are dropped, the rest move over.
        dropped = conn.execute(text("""
            DELETE FROM posts l WHERE l.community_id = :loser AND EXISTS (
              SELECT 1 FROM posts k WHERE k.community_id = :keeper
                AND k.source_content_id = l.source_content_id
                AND k.content_type = l.content_type)
            RETURNING l.id, l.source_content_id, l.content_type"""), ids).mappings().all()
        backup.append({"host": host, "loser": loser["id"], "deleted_posts": [dict(r) for r in dropped]})
        conn.execute(text("UPDATE posts SET community_id=:keeper WHERE community_id=:loser"), ids)

        # 5. Cookies are keyed by host, so a second connection row is noise.
        conn.execute(text("""
            DELETE FROM circle_connections WHERE community_id = :loser AND EXISTS (
              SELECT 1 FROM circle_connections k WHERE k.community_id = :keeper)"""), ids)
        conn.execute(text("UPDATE circle_connections SET community_id=:keeper WHERE community_id=:loser"), ids)

        # 6. Desktop-app state: one row per community, keeper's wins.
        conn.execute(text("DELETE FROM warmr_app.community_state WHERE community_id=:loser"), ids)
        conn.execute(text("""
            DELETE FROM warmr_app.space_sync l WHERE l.community_id = :loser AND EXISTS (
              SELECT 1 FROM warmr_app.space_sync k WHERE k.community_id = :keeper
                AND k.source_space_id = l.source_space_id)"""), ids)
        conn.execute(text("UPDATE warmr_app.space_sync SET community_id=:keeper WHERE community_id=:loser"), ids)

        for schema, table in PLAIN_CHILD_TABLES:
            conn.execute(text(f"UPDATE {schema}.{table} SET community_id=:keeper WHERE community_id=:loser"), ids)

        # 7. The keeper inherits whatever the loser knew and the loser goes.
        conn.execute(text("""
            UPDATE communities k SET
              join_status = CASE WHEN k.join_status = 'joined' THEN k.join_status
                                 WHEN l.join_status <> 'not_attempted' THEN l.join_status
                                 ELSE k.join_status END,
              joined_at = COALESCE(k.joined_at, l.joined_at),
              join_attempted_at = GREATEST(k.join_attempted_at, l.join_attempted_at),
              join_attempts = k.join_attempts + l.join_attempts,
              join_type = COALESCE(k.join_type, l.join_type),
              join_type_checked_at = COALESCE(k.join_type_checked_at, l.join_type_checked_at),
              watching = k.watching OR l.watching,
              icp_flag = k.icp_flag OR l.icp_flag,
              icp_score = GREATEST(k.icp_score, l.icp_score),
              icp_checked_at = COALESCE(k.icp_checked_at, l.icp_checked_at),
              relevant = k.relevant OR l.relevant,
              relevance_score = GREATEST(k.relevance_score, l.relevance_score),
              name = COALESCE(k.name, l.name),
              description = COALESCE(k.description, l.description),
              price_label = COALESCE(k.price_label, l.price_label),
              external_directory_id = COALESCE(k.external_directory_id, l.external_directory_id),
              access_status = CASE WHEN k.access_status = 'visited' THEN k.access_status
                                   ELSE l.access_status END,
              last_synced_at = GREATEST(k.last_synced_at, l.last_synced_at),
              notes = concat_ws(E'\\n', k.notes, l.notes),
              updated_at = now()
            FROM communities l
            WHERE k.id = :keeper AND l.id = :loser"""), ids)
        conn.execute(text("DELETE FROM communities WHERE id = :loser"), ids)

    # The row with the most posts may be the one entered from a directory link,
    # so the survivor can be left holding `/join?invitation_token=...` as its
    # address -- which the readers then use to build permalinks, and which
    # carries an invitation token we should not keep on file. The bare host is
    # free by now: any row that held it has just been deleted.
    conn.execute(text("UPDATE communities SET url = :url, updated_at = now() WHERE id = :keeper"),
                 {"url": f"https://{host}", "keeper": keeper["id"]})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("CIRCLE_LEADS_DB"))
    ap.add_argument("--host", help="merge only this host")
    ap.add_argument("--apply", action="store_true", help="write to the database")
    ap.add_argument("--backup", type=Path, help="where deleted rows are dumped (required with --apply)")
    args = ap.parse_args()

    if not args.db:
        print("no database url: pass --db or set CIRCLE_LEADS_DB", file=sys.stderr)
        return 2
    if args.apply and not args.backup:
        print("--apply needs --backup <path>", file=sys.stderr)
        return 2

    engine = create_engine(db_url(args.db), connect_args={"prepare_threshold": None})
    with engine.connect() as conn:
        if not args.apply:
            conn.execute(text("SET TRANSACTION READ ONLY"))
        groups = load_groups(conn, args.host)
        if not groups:
            print("no duplicate hosts")
            return 0

        plans = [plan_group(conn, host, rows) for host, rows in sorted(groups.items())]
        for p in plans:
            print(f"\n{p['host']}  keep id={p['keeper']}  {p['keeper_url']}")
            if p["keeper_url"] != p["canonical_url"]:
                print(f"  url   -> {p['canonical_url']}")
            for l in p["losers"]:
                print(f"  merge id={l['id']:<6} move {l['posts_move']:>4} posts, "
                      f"drop {l['posts_collide']:>4} doubled, "
                      f"re-point {l['leads_repointed']} lead(s), "
                      f"{l['spaces']} spaces, {l['authors']} authors, "
                      f"{l['connections']} connection(s)")
                print(f"               {l['url']}")
            for l in p["blocked"]:
                print(f"  SKIP  id={l['id']:<6} {len(l['leads_double'])} post(s) carry a lead on BOTH "
                      f"sides -- needs a human (leads may already be in Vini)")
                for d in l["leads_double"]:
                    print(f"                 post {d['source_content_id']}: "
                          f"leads {d['loser_lead']} / {d['keeper_lead']}, "
                          f"loser synced_at={d['loser_synced']}")

        if not args.apply:
            print("\ndry run -- nothing was written. Re-run with --apply --backup <path>.")
            return 0

        backup: list = []
        merged = 0
        for p in plans:
            if p["blocked"]:
                print(f"skipping {p['host']}: blocked rows above")
                continue
            with conn.begin():
                merge_group(conn, p["host"], groups[p["host"]], backup)
            merged += 1
        args.backup.write_text(json.dumps(
            {"at": datetime.now(timezone.utc).isoformat(), "groups": backup}, indent=1, default=str))
        print(f"\nmerged {merged} host(s); deleted rows dumped to {args.backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
