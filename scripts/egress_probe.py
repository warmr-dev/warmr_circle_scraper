#!/usr/bin/env python3
"""Measure how Circle answers this machine (or this proxy) on the real read paths.

Phase 0 of the server move: before buying proxies or moving the worker, run the
same probe from every candidate egress and compare the tables. The Mac run is
the baseline -- numbers from a server mean nothing without it.

It only reads. Nothing is written to the database, no login is attempted, no
challenge is ever solved or worked around: a challenge is a result, and two of
them in a row stop the run so a bad IP is not hammered.

Usage:

    python3 egress_probe.py --label mac --hosts hosts.txt --out mac.jsonl
    python3 egress_probe.py --label proxy1 --hosts hosts.txt --proxy http://user:pass@ip:port
    python3 egress_probe.py --label vps --hosts hosts.txt --cookies cookies.json

`hosts.txt`: one `host kind` per line, kind is open | cookie | closed.
`cookies.json`: {"host": {"_circle_session": "...", ...}} -- values are never
printed or written to the output file.

Needs only `requests` (pip install requests).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# The same user agent the worker sends (scraper/http_client.py), so the probe
# measures the reading path we actually use, not a different client.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
COOKIE_NAMES = ("_circle_session", "remember_user_token", "user_session_identifier")

# Ported from the desktop app's looksChallenged (desktop/src/engine/circle/http.ts):
# Cloudflare injects challenge-platform scripts into ordinary pages, so only
# these markers mean a real challenge.
CHALLENGE_MARKERS = ("__cf_chl_", "cf-chl-", "<title>just a moment", "verifying you are a human")

CHECKS = (
    ("anon_current", "/internal_api/communities/current", False),
    ("anon_spaces", "/internal_api/spaces", False),
    ("anon_feed", "/internal_api/home_page_posts?sort=latest&page=1&per_page=3", False),
    ("cookie_spaces", "/internal_api/spaces", True),
    ("cookie_feed", "/internal_api/home_page_posts?sort=latest&page=1&per_page=3", True),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify(resp: requests.Response) -> str:
    """One word for what Circle did with this request."""
    if resp.headers.get("cf-mitigated") == "challenge":
        return "challenge"
    body = resp.text[:4000].lower() if resp.content else ""
    if resp.status_code in (403, 503) and any(m in body for m in CHALLENGE_MARKERS):
        return "challenge"
    if resp.status_code == 429:
        return "ratelimited"
    if resp.status_code in (200, 304):
        return "ok"
    if resp.status_code in (401, 403):
        return "unauthorized"
    if resp.status_code == 404:
        return "notfound"
    return f"http_{resp.status_code}"


def body_facts(resp: requests.Response) -> dict:
    """Record counts and the newest post id -- never any post text or cookie."""
    out: dict = {}
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return out
    try:
        data = resp.json()
    except ValueError:
        out["json_error"] = True
        return out
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        recs = data["records"]
        out["records"] = len(recs)
        out["total"] = data.get("count")
        if recs:
            out["top_id"] = recs[0].get("id")
            out["top_created_at"] = recs[0].get("created_at")
            out["top_pinned"] = bool(
                recs[0].get("pin_to_top") or recs[0].get("pinned_at_top_of_space")
            )
    elif isinstance(data, list):
        out["records"] = len(data)
    elif isinstance(data, dict):
        out["keys"] = sorted(data.keys())[:6]
    return out


def probe(session: requests.Session, host: str, name: str, path: str, cookies: dict,
          etag: str | None) -> dict:
    url = f"https://{host}{path}"
    headers = {"Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    row = {"at": now(), "host": host, "check": name, "conditional": bool(etag)}
    started = time.monotonic()
    try:
        resp = session.get(url, headers=headers, cookies=cookies or None, timeout=25,
                           allow_redirects=False)
    except requests.RequestException as exc:
        row.update(verdict="error", error=type(exc).__name__, ms=int((time.monotonic() - started) * 1000))
        return row
    row.update(
        status=resp.status_code,
        ms=int((time.monotonic() - started) * 1000),
        content_type=resp.headers.get("content-type", "").split(";")[0],
        cf_mitigated=resp.headers.get("cf-mitigated"),
        cf_ray=(resp.headers.get("cf-ray") or "").split("-")[-1] or None,
        bytes=len(resp.content),
        etag=bool(resp.headers.get("etag")),
        verdict=classify(resp),
    )
    row.update(body_facts(resp))
    row["_etag_value"] = resp.headers.get("etag")  # kept in memory, stripped before writing
    return row


def load_hosts(path: Path) -> list[tuple[str, str]]:
    hosts = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        hosts.append((parts[0], parts[1] if len(parts) > 1 else "open"))
    return hosts


def public_ip(session: requests.Session) -> str:
    try:
        return session.get("https://api.ipify.org", timeout=15).text.strip()
    except requests.RequestException:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True, help="where this run happens: mac, vps, droplet, proxy1 ...")
    ap.add_argument("--hosts", required=True, type=Path)
    ap.add_argument("--cookies", type=Path, help="JSON: {host: {cookie_name: value}}")
    ap.add_argument("--proxy", help="http://user:pass@host:port -- used for every request")
    ap.add_argument("--rpm", type=float, default=8.0, help="requests per minute (default 8)")
    ap.add_argument("--out", type=Path, help="JSONL with one line per request")
    ap.add_argument("--conditional", action="store_true", help="also repeat feed reads with If-None-Match")
    args = ap.parse_args()

    hosts = load_hosts(args.hosts)
    cookie_jar: dict[str, dict] = {}
    if args.cookies:
        raw = json.loads(args.cookies.read_text())
        for host, jar in raw.items():
            cookie_jar[host] = {k: v for k, v in jar.items() if k in COOKIE_NAMES}

    session = requests.Session()
    session.trust_env = False  # never pick up ambient HTTPS_PROXY: the label must mean the egress
    session.headers.update({"User-Agent": UA})
    if args.proxy:
        session.proxies = {"http": args.proxy, "https": args.proxy}

    ip = public_ip(session)
    gap = 60.0 / args.rpm
    print(f"# label={args.label} ip={ip} hosts={len(hosts)} pace={args.rpm}/min", flush=True)

    out_fh = args.out.open("a") if args.out else None
    rows: list[dict] = []
    pushback_streak = 0
    try:
        for host, kind in hosts:
            etags: dict[str, str] = {}
            for name, path, needs_cookie in CHECKS:
                jar = cookie_jar.get(host, {})
                if needs_cookie and not jar:
                    continue
                row = probe(session, host, name, path, jar if needs_cookie else {}, None)
                etag_value = row.pop("_etag_value", None)
                if etag_value and name.endswith("_feed"):
                    etags[name] = etag_value
                row.update(label=args.label, ip=ip, kind=kind, proxied=bool(args.proxy))
                rows.append(row)
                print(
                    f"{host:38.38} {name:14} {row.get('status','-'):>4} "
                    f"{row['verdict']:<13} {row.get('records','-'):>3} rec "
                    f"{row.get('ms','-'):>5}ms",
                    flush=True,
                )
                if out_fh:
                    out_fh.write(json.dumps(row) + "\n")
                    out_fh.flush()

                if row["verdict"] in ("challenge", "ratelimited"):
                    pushback_streak += 1
                    if pushback_streak >= 2:
                        print("\n!! two pushbacks in a row -- stopping so this IP is not hammered",
                              flush=True)
                        return 2
                else:
                    pushback_streak = 0
                time.sleep(gap + random.uniform(0, gap * 0.25))

            # The cheap "nothing changed" path: this is what the watcher will do all day.
            if args.conditional:
                for name, etag_value in etags.items():
                    path = dict((n, p) for n, p, _ in CHECKS)[name]
                    jar = cookie_jar.get(host, {}) if name.startswith("cookie") else {}
                    row = probe(session, host, f"{name}_304", path, jar, etag_value)
                    row.pop("_etag_value", None)
                    row.update(label=args.label, ip=ip, kind=kind, proxied=bool(args.proxy))
                    rows.append(row)
                    print(f"{host:38.38} {name+'_304':14} {row.get('status','-'):>4} "
                          f"{row['verdict']:<13} {row.get('bytes','-'):>4} B", flush=True)
                    if out_fh:
                        out_fh.write(json.dumps(row) + "\n")
                        out_fh.flush()
                    time.sleep(gap + random.uniform(0, gap * 0.25))
    except KeyboardInterrupt:
        print("\n# interrupted", flush=True)
    finally:
        if out_fh:
            out_fh.close()

    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        summary.setdefault(row["check"], {}).setdefault(row["verdict"], 0)
        summary[row["check"]][row["verdict"]] += 1
    print(f"\n# summary for label={args.label} ip={ip}")
    for check in sorted(summary):
        parts = ", ".join(f"{v} {k}" for k, v in sorted(summary[check].items()))
        print(f"  {check:18} {parts}")
    bad = sum(c.get("challenge", 0) + c.get("ratelimited", 0) for c in summary.values())
    good = sum(c.get("ok", 0) for c in summary.values())
    print(f"  total: {good} ok, {bad} challenged/ratelimited, {len(rows)} requests")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
