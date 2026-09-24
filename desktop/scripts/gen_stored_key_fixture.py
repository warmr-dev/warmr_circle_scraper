"""Python's stored key for member-API posts and comments, computed by the real
Python code path: fetch_space_posts (content composition + redaction) followed
by triage_records' content rule and content_hash[:24].

Redaction uses DEFAULT_EXCLUDED_CONTENT ("content the system never collects"),
as harvest, the CLI and the dashboard do. scanning.py's cookie scan forgets to
pass it and stores contacts as-is; the desktop app follows the policy, so a
post with an email or phone gets a different key there.

Run from the repo root: .venv/bin/python desktop/scripts/gen_stored_key_fixture.py
and put the output under "stored_keys" in desktop/tests/fixtures_python.json."""
import json, sys
from circle_leads.scraper.member_api_reader import fetch_space_posts
from circle_leads.storage.database import content_hash
from circle_leads.config.settings import DEFAULT_EXCLUDED_CONTENT

POSTS = [
    {"id": 42, "name": "Looking for a React dev", "body_plain_text": "We need help with an MVP. Email me at jane@example.com", "created_at": "2026-09-01T10:00:00Z", "comments_count": 2},
    {"id": 43, "name": "", "body_plain_text": "No title here, just   a body\nwith lines", "created_at": "2026-09-02T10:00:00Z"},
    {"id": 44, "name": "Same", "body_plain_text": "Same", "created_at": "2026-09-03T10:00:00Z"},
    {"id": 45, "name": "Call +1 (415) 555-0100 now", "body_plain_text": "Title had a phone number", "created_at": "2026-09-04T10:00:00Z"},
    {"id": 46, "name": "Only a title", "body_plain_text": "", "created_at": "2026-09-05T10:00:00Z"},
]
COMMENTS = {42: [{"id": 7, "body_plain_text": "DM me, I build SaaS", "created_at": "2026-09-02T00:00:00Z", "replies_count": 1}]}
REPLIES = {7: [{"id": 8, "body_plain_text": "Thanks!", "created_at": "2026-09-02T01:00:00Z"}]}

class FakeReader:
    base = "https://x.circle.so"
    def list_posts(self, space_id, max_pages=10):
        return POSTS
    def list_comments(self, pid, max_pages=2):
        return COMMENTS.get(int(pid), [])
    def list_replies(self, cid):
        return REPLIES.get(int(cid), [])

out = []
for r in fetch_space_posts(FakeReader(), 1, with_comments=True, excluded_content=list(DEFAULT_EXCLUDED_CONTENT)):
    title = r.get("title")
    # triage/pipeline.py triage_records, verbatim rule
    content = r["content"] if not title or r["content"].lstrip().startswith(title) else title + "\n" + r["content"]
    out.append({"circle_id": r["source_content_id"].lstrip("c"), "kind": r["content_type"],
                "key": "triage:" + content_hash(content)[:24], "content": content, "dedup_hash": content_hash(content)})
json.dump({"input": {"posts": POSTS, "comments": {str(k): v for k, v in COMMENTS.items()}, "replies": {str(k): v for k, v in REPLIES.items()}}, "expected": out}, sys.stdout, indent=1)
