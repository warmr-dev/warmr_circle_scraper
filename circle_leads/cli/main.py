"""Command-line interface."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from circle_leads.config.settings import (
    load_dotenv,
    load_community_permissions,
    load_requirements,
)
from circle_leads.discovery.finder import rank_extracted, rank_from_html
from circle_leads.discovery.web_search import discover_by_search
from circle_leads.discovery.persist import new_since, persist_finds
from circle_leads.scraper.member_feed import (
    MemberFeedClient, SessionExpired, fetch_space_posts,
)
from circle_leads.discovery.discover_communities import (
    dedupe,
    extract_from_text,
    load_from_file,
)
from circle_leads.export.exporters import query_leads, to_csv, to_json
from circle_leads.pipeline import classify_pending, discover, ingest_community
from circle_leads.storage.database import Database, purge_community, purge_expired
from circle_leads.storage.models import Community, Lead, Post
from circle_leads.triage.pipeline import triage_text

DEFAULT_PERMISSIONS_DIR = "circle_leads/config/communities"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--db", "db_url", default=None, help="Database URL (default: SQLite).")
@click.option("--config", "config_path", default=None, help="requirements.yaml path.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
@click.pass_context
def cli(ctx, db_url, config_path, verbose):
    """Consent-first hiring-lead discovery across authorized Circle communities."""
    load_dotenv()  # pick up EXA_API_KEY, ANTHROPIC_API_KEY, DASHBOARD_* from .env
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["db"] = Database(db_url)
    ctx.obj["requirements"] = load_requirements(config_path)


# --- Discovery --------------------------------------------------------------


@cli.command("discover")
@click.option("--from-file", "from_file", type=click.Path(exists=True),
              help="File of community URLs (one per line, or CSV).")
@click.option("--from-html", "from_html", type=click.Path(exists=True),
              help="Saved public page to extract Circle links from.")
@click.option("--url", "urls", multiple=True, help="A community URL. Repeatable.")
@click.option("--no-validate", is_flag=True, help="Skip public landing-page checks.")
@click.pass_context
def discover_cmd(ctx, from_file, from_html, urls, no_validate):
    """Record candidate communities from user-provided or public sources."""
    found = []
    if from_file:
        found += load_from_file(from_file)
    if from_html:
        found += extract_from_text(
            Path(from_html).read_text(encoding="utf-8", errors="ignore"),
            source=f"html:{Path(from_html).name}",
        )
    if urls:
        found += extract_from_text("\n".join(urls), source="cli")

    if not found:
        raise click.UsageError(
            "No communities supplied. Use --from-file, --from-html, or --url."
        )

    found = dedupe(found)
    recorded = discover(ctx.obj["db"], found, validate=not no_validate)
    click.echo(f"Recorded {len(recorded)} community/communities.")
    click.echo(
        "\nDiscovery records public metadata only. It does not authorize "
        "ingestion.\nObtain operator approval, then create a permission file "
        f"in {DEFAULT_PERMISSIONS_DIR}/."
    )


@cli.command("find")
@click.option("--from-html", "from_html", type=click.Path(exists=True),
              help="A saved public page (Circle Discover, a directory, search results).")
@click.option("--url", "urls", multiple=True, help="A community URL to score. Repeatable.")
@click.option("--free-only", is_flag=True, help="Only show communities marked free.")
@click.option("--min-score", type=int, default=0, show_default=True)
@click.option("--limit", type=int, default=30, show_default=True)
@click.pass_context
def find_cmd(ctx, from_html, urls, free_only, min_score, limit):
    """Rank candidate communities to join, by likely hiring activity.

    Reads public listing pages only. It never joins anything -- the output is a
    shortlist with a join URL and a reason for each, so you click Join on the
    good ones yourself.
    """
    ranked = []
    if from_html:
        ranked += rank_from_html(
            Path(from_html).read_text(encoding="utf-8", errors="ignore"),
            source=Path(from_html).name,
        )
    if urls:
        ranked += rank_extracted(extract_from_text("\n".join(urls), source="cli"))

    if not ranked:
        raise click.UsageError(
            "Nothing to rank. Save a public page and pass --from-html, or pass --url.\n"
            "Circle's directory is at https://discover.circle.so/ -- save it from your browser."
        )

    if free_only:
        ranked = [c for c in ranked if c.is_free]
    ranked = [c for c in ranked if c.score >= min_score][:limit]

    if not ranked:
        click.echo("No communities matched. Try without --free-only or a lower --min-score.")
        return

    click.echo(f"\n{'TIER':<14}{'SCORE':>5}  {'FREE':<5} COMMUNITY")
    click.echo("-" * 72)
    for c in ranked:
        click.echo(f"{c.tier:<14}{c.score:>5}  {'yes' if c.is_free else 'no':<5} {(c.name or c.slug)[:34]}")
        pos = [r for r in c.reasons[:5] if not r.startswith("not:")]
        if pos:
            click.echo(f"{'':19}why: {', '.join(pos)}")
        click.echo(f"{'':19}join: {c.join_url}")
    click.echo("-" * 72)
    click.echo(
        f"\n{len(ranked)} candidate(s). Open the join URLs and join the ones that "
        "fit -- as yourself, one click each.\nThen read them and paste posts into "
        "`circle-leads triage`."
    )



@cli.command("search-web")
@click.argument("niche")
@click.option("--free-only", is_flag=True, help="Only show communities marked free.")
@click.option("--min-score", type=int, default=15, show_default=True)
@click.option("--limit", type=int, default=30, show_default=True)
@click.option("--no-fetch", is_flag=True, help="Don't fetch result pages (faster, shallower).")
@click.option("--no-directories", is_flag=True, help="Skip Discover / Hive Index.")
@click.pass_context
def search_web_cmd(ctx, niche, free_only, min_score, limit, no_fetch, no_directories):
    """Search the public web for Circle communities in a niche, and rank them.

    Runs topic searches, pulls circle.so links out of the results and known
    directories, and ranks them. It reads public pages only -- it never joins
    anything and never uses your account.

    \b
      circle-leads search-web "flutter developer"
      circle-leads search-web "startup founders" --free-only

    Backend: set BRAVE_API_KEY or SERPAPI_API_KEY for best results; otherwise a
    keyless DuckDuckGo fallback is used (may be rate-limited).
    """
    click.echo(f"Searching public sources for '{niche}' communities...", err=True)
    disc = discover_by_search(
        niche,
        fetch_result_pages=not no_fetch,
        include_directories=not no_directories,
    )
    click.echo(
        f"  backend={disc.backend} queries={disc.queries_run} "
        f"pages_fetched={disc.pages_fetched}",
        err=True,
    )

    ranked = disc.ranked
    if free_only:
        ranked = [c for c in ranked if c.is_free]
    ranked = [c for c in ranked if c.score >= min_score][:limit]

    if not ranked:
        click.echo(
            "\nNothing found. The keyless search backend may be rate-limited -- "
            "set BRAVE_API_KEY (free tier) for reliable results, or save a page "
            "and use `circle-leads find --from-html`."
        )
        return

    click.echo(f"\n{'TIER':<14}{'SCORE':>5}  {'FREE':<5} COMMUNITY")
    click.echo("-" * 72)
    for c in ranked:
        click.echo(f"{c.tier:<14}{c.score:>5}  {'yes' if c.is_free else 'no':<5} {(c.name or c.slug)[:34]}")
        pos=[r for r in c.reasons[:5] if not r.startswith("not:")]
        if pos:
            click.echo(f"{'':19}why: {', '.join(pos)}")
        click.echo(f"{'':19}join: {c.join_url}")
    click.echo("-" * 72)
    click.echo(
        f"\n{len(ranked)} candidate(s). Open the join URLs, join the ones that "
        "fit -- as yourself, one click each."
    )



@cli.command("search-watch")
@click.argument("niches", nargs=-1)
@click.option("--interval", type=int, default=0,
              help="Seconds between runs. 0 = run once and exit (for cron).")
@click.option("--min-score", type=int, default=25, show_default=True,
              help="Only save finds at or above this score.")
@click.option("--free-only", is_flag=True, help="Only save communities marked free.")
@click.option("--quiet", is_flag=True, help="Only print when new communities appear.")
@click.pass_context
def search_watch_cmd(ctx, niches, interval, min_score, free_only, quiet):
    """Search niches on a schedule, save finds, and flag the new ones.

    Run once from cron, or loop as a long-running worker. Either way, new
    communities land in the database and show up in the dashboard's
    Communities tab; a re-run only flags ones not seen before.

    \b
      # one-shot (put this line in cron / a Railway cron service):
      circle-leads search-watch "flutter developer" "startup founders" --min-score 25

      # long-running worker (Railway/Out Plane background service):
      circle-leads search-watch "flutter developer" --interval 86400
    """
    if not niches:
        # Fall back to niches in the config if none given on the CLI.
        niches = tuple(getattr(ctx.obj["requirements"], "target_roles", []) or [])
        niches = tuple(dict.fromkeys(n.split()[0] + " developer" for n in niches))[:3]
    if not niches:
        raise click.UsageError("Give at least one niche, e.g. \"flutter developer\".")

    def one_pass() -> int:
        total_new = 0
        for niche in niches:
            if not quiet:
                click.echo(f"Searching '{niche}'...", err=True)
            disc = discover_by_search(niche)
            ranked = [c for c in disc.ranked if not free_only or c.is_free]
            res = persist_finds(
                ctx.obj["db"], ranked, niche=niche, min_score=min_score, source="search-watch",
            )
            total_new += res.new_count
            if res.new or not quiet:
                click.echo(
                    f"  '{niche}': {res.new_count} NEW, {len(res.updated)} updated "
                    f"(backend={disc.backend})"
                )
                for rc in res.new:
                    click.echo(f"    + {rc.score:>3} {rc.tier:<13} {rc.name or rc.slug}")
                    click.echo(f"          join: {rc.join_url}")
        return total_new

    if interval <= 0:
        n = one_pass()
        click.echo(f"\nDone. {n} new community/communities. "
                   f"See them with `circle-leads communities` or the dashboard.")
        return

    import time as _time
    click.echo(f"Watching {len(niches)} niche(s) every {interval}s. Ctrl-C to stop.", err=True)
    while True:
        try:
            n = one_pass()
            click.echo(f"[{_time.strftime('%Y-%m-%d %H:%M')}] {n} new.", err=True)
            _time.sleep(interval)
        except KeyboardInterrupt:
            click.echo("\nStopped.", err=True)
            return


@cli.command("new-communities")
@click.option("--limit", type=int, default=50, show_default=True)
@click.pass_context
def new_communities_cmd(ctx, limit):
    """List discovered communities you haven't visited yet, best first."""
    rows = new_since(ctx.obj["db"], limit=limit)
    if not rows:
        click.echo("Nothing new. Run `circle-leads search-watch \"<niche>\"` first.")
        return
    click.echo(f"\n{'SCORE':>5}  COMMUNITY")
    click.echo("-" * 60)
    for r in rows:
        free = " (free)" if (r.get("price_label") or "").lower().startswith("free") else ""
        click.echo(f"{int(r['score']):>5}  {(r['name'] or r['slug'])[:34]}{free}")
        click.echo(f"       join: {r['join_url']}")
    click.echo("-" * 60)
    click.echo(f"\n{len(rows)} unvisited. Join the good ones, then `triage` their posts.")



@cli.command("read-feed")
@click.argument("community_host")
@click.option("--space-id", "space_ids", multiple=True,
              help="A space id to read. Repeatable. Find it in DevTools; see docs/member_feed.md.")
@click.option("--login", is_flag=True,
              help="Open a browser window to sign in (do this once, or when the session expires).")
@click.option("--profile-dir", default=None, help="Where to keep the browser profile.")
@click.option("--show-browser", is_flag=True, help="Show the browser while reading (debug).")
@click.option("--community", default=None, help="Slug to record leads under (default: host).")
@click.option("--max-pages", type=int, default=10, show_default=True)
@click.option("--use-llm", is_flag=True)
@click.pass_context
def read_feed_cmd(ctx, community_host, space_ids, login, profile_dir, show_browser,
                  community, max_pages, use_llm):
    """Read spaces you're a member of, via a browser you log into yourself.

    A Chromium window (persistent profile) holds your Circle login. Python reads
    the rendered feed through it and never sees, copies, or stores your session
    token -- the browser holds it, exactly like your normal browser. Read-only.

    \b
      # once: sign in
      circle-leads read-feed startupandangels.circle.so --login
      # then, any time:
      circle-leads read-feed startupandangels.circle.so --space-id 1595123
    """
    from circle_leads.scraper.browser_reader import (
        BrowserFeedReader, BrowserNotAvailable, NotLoggedIn, fetch_space_posts,
    )
    from circle_leads.triage.pipeline import triage_text

    try:
        reader = BrowserFeedReader(
            community_host, profile_dir=profile_dir, headless=not show_browser,
        )
    except BrowserNotAvailable as exc:
        raise click.ClickException(str(exc))

    if login:
        try:
            reader.login()
        except NotLoggedIn as exc:
            raise click.ClickException(str(exc))
        click.echo("Logged in. Now read a space with --space-id.")
        if not space_ids:
            return

    if not space_ids:
        raise click.UsageError("Give at least one --space-id (or --login first).")

    slug = community or community_host.split(".")[0]
    reqs = ctx.obj["requirements"]
    all_records = []
    try:
        for space_id in space_ids:
            click.echo(f"Reading space {space_id}...", err=True)
            recs = fetch_space_posts(
                reader, space_id, excluded_content=reqs.excluded_content, max_pages=max_pages,
            )
            click.echo(f"  {len(recs)} post(s)", err=True)
            all_records.extend(recs)
    except NotLoggedIn as exc:
        raise click.ClickException(str(exc) + "\nRun with --login to sign in.")

    if not all_records:
        click.echo("No posts read. Check the space id, or re-run with --login.")
        return

    text = "\n\n---\n\n".join(
        (r["title"] + "\n" + r["content"]) if r.get("title") else r["content"]
        for r in all_records
    )
    result = triage_text(
        ctx.obj["db"], text, reqs, community=slug,
        source_url=reader.base, use_llm=use_llm,
    )
    click.echo(
        f"\nRead {result.total_posts} post(s): {len(result.leads)} lead(s), "
        f"{result.not_leads} not-lead, {result.already_seen} seen before."
    )
    for lead in result.leads:
        click.echo(f"  {lead['lead_score']:>3} {lead['priority']:<7} "
                   f"{lead.get('job_title') or lead.get('hire_target') or '-'}")
    click.echo("\nReview them in the dashboard, or `circle-leads search`.")


def _load_member_feeds(path=None):
    """Load the member-feeds config (communities + spaces you read)."""
    import yaml
    from pathlib import Path

    default = Path("circle_leads/config/communities/member_feeds.yaml")
    cfg_path = Path(path) if path else default
    if not cfg_path.exists():
        return []
    data = yaml.safe_load(cfg_path.read_text()) or {}
    return data.get("feeds", [])


@cli.command("sessions")
@click.option("--config", "feeds_config", default=None, help="member_feeds.yaml path.")
@click.pass_context
def sessions_cmd(ctx, feeds_config):
    """Show login status for each community you read feeds from.

    Your login lives in a per-community browser profile on this machine. This
    checks which are still signed in. Sign in with `read-feed <host> --login`.
    """
    from circle_leads.scraper.browser_reader import BrowserFeedReader, BrowserNotAvailable

    feeds = _load_member_feeds(feeds_config)
    if not feeds:
        click.echo(
            "No member feeds configured. Copy "
            "circle_leads/config/communities/member_feeds.yaml.template to "
            "member_feeds.yaml and list your communities."
        )
        return

    try:
        click.echo(f"\n{'STATUS':<14} {'SPACES':>6}  COMMUNITY")
        click.echo("-" * 60)
        for feed in feeds:
            host = feed["host"]
            reader = BrowserFeedReader(host)
            signed_in = reader.status()
            state = "signed in" if signed_in else "sign in needed"
            click.echo(f"{state:<14} {len(feed.get('spaces', [])):>6}  {host}")
        click.echo("-" * 60)
        click.echo("\nSign in:  circle-leads read-feed <host> --login")
        click.echo("Read all: circle-leads read-all")
    except BrowserNotAvailable as exc:
        raise click.ClickException(str(exc))


@cli.command("read-all")
@click.option("--config", "feeds_config", default=None, help="member_feeds.yaml path.")
@click.option("--use-llm", is_flag=True)
@click.pass_context
def read_all_cmd(ctx, feeds_config, use_llm):
    """Read every configured community's spaces in one run, on your machine.

    Reads each community you're signed into (skipping ones that need a login),
    classifies the posts, and files the leads. Run this on a schedule for
    hands-off lead collection from communities you belong to.
    """
    from circle_leads.scraper.browser_reader import (
        BrowserFeedReader, BrowserNotAvailable, NotLoggedIn, fetch_space_posts,
    )
    from circle_leads.triage.pipeline import triage_text

    feeds = _load_member_feeds(feeds_config)
    if not feeds:
        raise click.UsageError("No member feeds configured (see member_feeds.yaml.template).")

    reqs = ctx.obj["requirements"]
    total_leads = 0
    try:
        for feed in feeds:
            host = feed["host"]
            slug = feed.get("slug") or host.split(".")[0]
            reader = BrowserFeedReader(host)
            if not reader.status():
                click.echo(f"SKIP {host}: sign in needed (read-feed {host} --login)", err=True)
                continue

            records = []
            for space in feed.get("spaces", []):
                try:
                    recs = fetch_space_posts(
                        reader, space["id"], excluded_content=reqs.excluded_content,
                    )
                    records.extend(recs)
                    click.echo(f"  {host} / {space.get('name', space['id'])}: {len(recs)} post(s)", err=True)
                except NotLoggedIn:
                    click.echo(f"  {host}: session expired mid-run", err=True)
                    break

            if not records:
                continue
            text = "\n\n---\n\n".join(
                (r["title"] + "\n" + r["content"]) if r.get("title") else r["content"]
                for r in records
            )
            result = triage_text(
                ctx.obj["db"], text, reqs, community=slug,
                source_url=reader.base, use_llm=use_llm,
            )
            total_leads += len(result.leads)
            click.echo(f"{host}: {len(result.leads)} lead(s), {result.already_seen} seen before")
    except BrowserNotAvailable as exc:
        raise click.ClickException(str(exc))

    click.echo(f"\nDone. {total_leads} lead(s) across all communities. See the dashboard.")



@cli.command("read-public")
@click.argument("community_host")
@click.option("--space-id", "space_ids", multiple=True,
              help="Only read these space ids/slugs. Default: auto-discover public lead spaces.")
@click.option("--list-spaces", is_flag=True, help="Just list the community's spaces and which are public.")
@click.option("--all-spaces", is_flag=True, help="Try every space, not only likely lead spaces.")
@click.option("--community", default=None, help="Slug to record leads under.")
@click.option("--use-llm", is_flag=True)
@click.pass_context
def read_public_cmd(ctx, community_host, space_ids, list_spaces, all_spaces, community, use_llm):
    """Read a community's PUBLIC spaces -- no membership or login required.

    Some communities publish spaces openly (e.g. a public "I need a consultant"
    board). This auto-discovers the spaces, reads the ones that are public, and
    classifies the posts. Private spaces (401/403) are skipped.

    \b
      circle-leads read-public the-technical-freelancer-academy.circle.so --list-spaces
      circle-leads read-public the-technical-freelancer-academy.circle.so
    """
    from circle_leads.scraper.public_reader import PublicReader, discover_and_read_public
    from circle_leads.triage.pipeline import triage_records

    reader = PublicReader(community_host)

    if list_spaces:
        spaces = reader.list_spaces()
        if not spaces:
            click.echo("No public space list for this community (it may be fully private).")
            return
        click.echo(f"\n{len(spaces)} space(s) found. Probing which are public...\n")
        for sp in spaces:
            ok, _ = reader.read_space(sp.id, max_pages=1)
            click.echo(f"  {'PUBLIC ' if ok else 'private'}  id={sp.id:<10} {sp.name[:36]}")
        return

    spaces, records = discover_and_read_public(
        reader,
        space_ids=list(space_ids) or None,
        only_lead_spaces=not all_spaces and not space_ids,
        excluded_content=ctx.obj["requirements"].excluded_content,
    )
    if not records:
        click.echo("No public posts read. Try --list-spaces to see what's available.")
        return

    public = [s for s in spaces if s.is_public]
    click.echo(f"Read {len(records)} post(s) from {len(public)} public space(s).", err=True)

    slug = community or community_host.split(".")[0]
    result = triage_records(
        ctx.obj["db"], records, ctx.obj["requirements"], community=slug,
        source_url=reader.base, use_llm=use_llm,
    )
    click.echo(
        f"\nRead {result.total_posts} post(s): {len(result.leads)} lead(s), "
        f"{result.not_leads} not-lead, {result.already_seen} seen before."
    )
    for lead in result.leads:
        click.echo(f"  {lead['lead_score']:>3} {lead['priority']:<7} "
                   f"{lead.get('job_title') or lead.get('hire_target') or '-'}")



@cli.command("communities")
@click.option("--relevant-only", is_flag=True, help="Only communities scored relevant.")
@click.pass_context
def communities_cmd(ctx, relevant_only):
    """List known communities with access and permission status."""
    from sqlalchemy import select

    with ctx.obj["db"].session() as s:
        stmt = select(Community).order_by(Community.relevance_score.desc())
        if relevant_only:
            stmt = stmt.where(Community.relevant.is_(True))
        rows = list(s.scalars(stmt).all())

        if not rows:
            click.echo("No communities recorded yet. Run `discover` first.")
            return

        click.echo(f"{'SLUG':<28} {'REL':>4}  {'ACCESS':<22} {'PERMISSION':<12}")
        click.echo("-" * 74)
        for c in rows:
            click.echo(
                f"{c.slug[:27]:<28} {int(c.relevance_score):>4}  "
                f"{c.access_status:<22} {c.permission_status:<12}"
            )
        click.echo(f"\n{len(rows)} community/communities.")


# --- Ingestion --------------------------------------------------------------


@cli.command("ingest")
@click.option("--community", help="Only this community_id.")
@click.option("--permissions-dir", default=DEFAULT_PERMISSIONS_DIR, show_default=True)
@click.option("--full", is_flag=True, help="Ignore the watermark; refetch everything.")
@click.option("--no-comments", is_flag=True, help="Skip comments.")
@click.option("--max-pages", type=int, default=None, help="Cap pages per space.")
@click.option("--request-budget", type=int, default=None,
              help="Stop after N API requests (protects the monthly allowance).")
@click.pass_context
def ingest_cmd(ctx, community, permissions_dir, full, no_comments, max_pages, request_budget):
    """Collect approved content from communities with operator approval."""
    perms = load_community_permissions(permissions_dir)
    if community:
        perms = [p for p in perms if p.community_id == community]
    if not perms:
        click.echo(
            f"No permission files found in {permissions_dir}/.\n"
            "Copy example.yaml.template, fill it in after the operator approves, "
            "and set permission_status to 'approved'.",
            err=True,
        )
        raise SystemExit(1)

    approved = [p for p in perms if p.is_approved]
    skipped = [p for p in perms if not p.is_approved]
    for p in skipped:
        click.echo(
            f"SKIP {p.community_id}: permission_status='{p.permission_status}' "
            "(needs 'approved')."
        )

    if not approved:
        click.echo("\nNothing to ingest: no community is approved.", err=True)
        raise SystemExit(1)

    for perm in approved:
        click.echo(f"\nIngesting {perm.community_id} via {perm.ingestion_route}...")
        summary = ingest_community(
            ctx.obj["db"],
            perm,
            ctx.obj["requirements"],
            incremental=not full,
            include_comments=not no_comments,
            max_pages=max_pages,
            request_budget=request_budget,
        )
        click.echo(
            f"  state={summary.state} seen={summary.items_seen} "
            f"new={summary.items_new} updated={summary.items_updated}"
        )
        for err in summary.errors:
            click.echo(f"  ! {err}", err=True)


@cli.command("classify")
@click.option("--use-llm", is_flag=True, help="Escalate ambiguous posts to an LLM.")
@click.option("--limit", type=int, default=None, help="Max posts to classify.")
@click.pass_context
def classify_cmd(ctx, use_llm, limit):
    """Classify unclassified content and score the resulting leads."""
    stats = classify_pending(
        ctx.obj["db"], ctx.obj["requirements"], use_llm=use_llm, limit=limit
    )
    click.echo(
        f"Classified {stats['classified']}: {stats['leads']} lead(s), "
        f"{stats['not_leads']} not-lead, {stats['filtered']} filtered by "
        f"requirements, {stats['duplicates']} duplicate(s)."
    )


@cli.command("run")
@click.option("--permissions-dir", default=DEFAULT_PERMISSIONS_DIR, show_default=True)
@click.option("--use-llm", is_flag=True)
@click.option("--full", is_flag=True)
@click.pass_context
def run_cmd(ctx, permissions_dir, use_llm, full):
    """Ingest every approved community, then classify and score."""
    ctx.invoke(
        ingest_cmd, permissions_dir=permissions_dir, full=full,
        community=None, no_comments=False, max_pages=None, request_budget=None,
    )
    ctx.invoke(classify_cmd, use_llm=use_llm, limit=None)


# --- Search and export ------------------------------------------------------


@cli.command("search")
@click.option("--role", default=None, help='e.g. "Backend Developer"')
@click.option("--skills", default=None, help='Comma-separated, e.g. "Python,AWS"')
@click.option("--community", default=None)
@click.option("--min-score", type=int, default=0, show_default=True)
@click.option("--priority", type=click.Choice(["HIGH", "MEDIUM", "LOW"], case_sensitive=False))
@click.option("--exclude-job-seekers", is_flag=True, default=True,
              help="On by default; only LEAD rows are ever returned.")
@click.option("--include-duplicates", is_flag=True)
@click.option("--limit", type=int, default=25, show_default=True)
@click.pass_context
def search_cmd(ctx, role, skills, community, min_score, priority,
               exclude_job_seekers, include_duplicates, limit):
    """Search stored leads."""
    skill_list = [s.strip() for s in skills.split(",")] if skills else None
    with ctx.obj["db"].session() as s:
        rows = query_leads(
            s, role=role, skills=skill_list, community=community,
            min_score=min_score, priority=priority,
            exclude_duplicates=not include_duplicates, limit=limit,
        )

    if not rows:
        click.echo("No leads matched.")
        return

    for row in rows:
        click.echo("=" * 68)
        click.echo(f"Community:      {row['community']}")
        if row.get("space"):
            click.echo(f"Space:          {row['space']}")
        click.echo(f"Lead Score:     {row['lead_score']}  ({row['priority']})")
        click.echo(f"Classification: {row['classification']} "
                   f"(confidence {row['confidence']}, via {row['decided_by']})")
        if row.get("job_title"):
            click.echo(f"Role:           {row['job_title']}")
        if row.get("skills"):
            click.echo(f"Skills:         {', '.join(row['skills'])}")
        if row.get("hire_target"):
            click.echo(f"Wants:          {row['hire_target']}")
        if row.get("budget"):
            click.echo(f"Budget:         {row['budget']}")
        if row.get("author"):
            click.echo(f"Author:         {row['author']}")
        click.echo(f"Posted:         {row.get('published_at') or 'unknown'}")
        body = (row.get("content") or "").strip().replace("\n", " ")
        click.echo(f"Post:           \"{body[:220]}{'...' if len(body) > 220 else ''}\"")
        if row.get("evidence_quote"):
            click.echo(f"Evidence:       \"{row['evidence_quote'][:160]}\"")
        click.echo(f"URL:            {row.get('url') or 'n/a'}")
    click.echo("=" * 68)
    click.echo(f"\n{len(rows)} lead(s). Review each before any outreach.")


@cli.command("export")
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]), default="csv", show_default=True)
@click.option("--output", "-o", default=None, help="Output path.")
@click.option("--min-score", type=int, default=0, show_default=True)
@click.option("--priority", type=click.Choice(["HIGH", "MEDIUM", "LOW"], case_sensitive=False))
@click.option("--community", default=None)
@click.option("--extended", is_flag=True, help="Include all lead fields in CSV.")
@click.pass_context
def export_cmd(ctx, fmt, output, min_score, priority, community, extended):
    """Export leads to CSV or JSON."""
    with ctx.obj["db"].session() as s:
        rows = query_leads(
            s, min_score=min_score, priority=priority, community=community
        )
    if not rows:
        click.echo("No leads to export.")
        return

    path = Path(output) if output else Path("exports") / f"leads.{fmt}"
    written = to_csv(rows, path, extended=extended) if fmt == "csv" else to_json(rows, path)
    click.echo(f"Exported {len(rows)} lead(s) to {written}")


# --- Manual triage ----------------------------------------------------------


@cli.command("triage")
@click.option("--file", "path", type=click.Path(exists=True),
              help="File of pasted community text.")
@click.option("--community", default="manual", show_default=True,
              help="Which community this text came from.")
@click.option("--space", default=None, help="Which space, for your own notes.")
@click.option("--url", "source_url", default=None,
              help="Link back to the thread, so you can return to it.")
@click.option("--use-llm", is_flag=True, help="Escalate ambiguous posts to an LLM.")
@click.option("--name", "your_name", default=None, help="Sign reply drafts with this name.")
@click.option("--min-score", type=int, default=0, show_default=True)
@click.option("--show-replies/--no-replies", default=True, show_default=True,
              help="Show a draft opening reply for each lead.")
@click.pass_context
def triage_cmd(ctx, path, community, space, source_url, use_llm, your_name,
               min_score, show_replies):
    """Find leads in community text you paste in.

    For communities you have joined as an ordinary member: read the pages you
    are entitled to read, copy what is on screen, and this ranks the people
    worth replying to.

    \b
      circle-leads triage --file posts.txt --community flutter-devs
      pbpaste | circle-leads triage --community flutter-devs
    """
    if path:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    else:
        if sys.stdin.isatty():
            click.echo(
                "Paste the community text, then press Ctrl-D (Ctrl-Z on Windows):\n",
                err=True,
            )
        text = sys.stdin.read()

    if not text.strip():
        raise click.UsageError("No text supplied. Use --file or pipe text in.")

    result = triage_text(
        ctx.obj["db"], text, ctx.obj["requirements"],
        community=community, space=space, source_url=source_url,
        use_llm=use_llm, your_name=your_name,
    )

    shown = [x for x in result.leads if x["lead_score"] >= min_score]
    click.echo(
        f"\nRead {result.total_posts} post(s): {len(result.leads)} lead(s), "
        f"{result.not_leads} not-lead, {result.filtered} filtered, "
        f"{result.duplicates} duplicate(s), {result.already_seen} seen before."
    )

    if not shown:
        click.echo("\nNothing worth replying to in this batch.")
        return

    for lead in shown:
        click.echo("\n" + "=" * 68)
        head = f"{lead['lead_score']}  {lead['priority']}"
        if lead.get("is_duplicate"):
            head += "  (duplicate)"
        click.echo(head)
        if lead.get("author"):
            click.echo(f"From:      {lead['author']}"
                       + (f"  ({lead['posted_label']})" if lead.get("posted_label") else ""))
        if lead.get("job_title"):
            click.echo(f"Needs:     {lead['job_title']}")
        elif lead.get("hire_target"):
            click.echo(f"Needs:     {lead['hire_target']}")
        if lead.get("skills"):
            click.echo(f"Skills:    {', '.join(lead['skills'])}")
        if lead.get("budget"):
            click.echo(f"Budget:    {lead['budget']}")
        if lead.get("urgency"):
            click.echo(f"Urgency:   {lead['urgency']}")
        body = " ".join((lead.get("content") or "").split())
        click.echo(f"Post:      \"{body[:200]}{'...' if len(body) > 200 else ''}\"")
        if lead.get("url"):
            click.echo(f"URL:       {lead['url']}")

        if show_replies:
            click.echo("\n  Draft reply:")
            for line in lead["reply_draft"].split("\n"):
                click.echo(f"    {line}")
            for note in lead.get("reply_notes") or []:
                click.echo(f"    ! {note}")

    click.echo("\n" + "=" * 68)
    click.echo(
        f"{len(shown)} lead(s). Read the original thread before replying, and "
        "follow the community's rules on promotion."
    )


# --- Config and retention ---------------------------------------------------


@cli.command("config")
@click.pass_context
def config_cmd(ctx):
    """Show the active lead requirements."""
    r = ctx.obj["requirements"]
    click.echo(f"Target roles:        {', '.join(r.target_roles) or '(any)'}")
    click.echo(f"Target skills:       {', '.join(r.target_skills) or '(any)'}")
    click.echo(f"Exclude job seekers: {r.exclude_job_seekers}")
    click.echo(f"Minimum confidence:  {r.minimum_confidence}")
    click.echo(f"LLM escalation at:   rule score < {r.llm_escalation_threshold}")
    click.echo(f"Include keywords:    {', '.join(r.keywords.include) or '(none)'}")
    click.echo(f"Exclude keywords:    {', '.join(r.keywords.exclude) or '(none)'}")
    click.echo(f"Priority: HIGH>={r.priority_thresholds.high}  "
               f"MEDIUM>={r.priority_thresholds.medium}")
    click.echo(f"Retention days:      {r.retention_days}")
    click.echo(f"Excluded content:    {', '.join(r.excluded_content)}")


@cli.command("purge")
@click.option("--community", default=None, help="Delete one community's content (kill switch).")
@click.option("--expired", is_flag=True, help="Delete content past the retention window.")
@click.confirmation_option(prompt="Permanently delete the selected stored content?")
@click.pass_context
def purge_cmd(ctx, community, expired):
    """Delete stored content: operator kill switch, or retention expiry."""
    if not community and not expired:
        raise click.UsageError("Specify --community or --expired.")
    with ctx.obj["db"].session() as s:
        if community:
            n = purge_community(s, community)
            click.echo(f"Deleted {n} item(s) for '{community}' and marked it revoked.")
        if expired:
            n = purge_expired(s, ctx.obj["requirements"].retention_days)
            click.echo(f"Deleted {n} item(s) past retention.")


@cli.command("stats")
@click.pass_context
def stats_cmd(ctx):
    """Show database counts."""
    from sqlalchemy import func, select

    with ctx.obj["db"].session() as s:
        communities = s.scalar(select(func.count()).select_from(Community)) or 0
        posts = s.scalar(select(func.count()).select_from(Post)) or 0
        unclassified = s.scalar(
            select(func.count()).select_from(Post).where(Post.classified.is_(False))
        ) or 0
        leads = s.scalar(
            select(func.count()).select_from(Lead).where(Lead.classification == "LEAD")
        ) or 0
        high = s.scalar(
            select(func.count()).select_from(Lead).where(Lead.priority == "HIGH")
        ) or 0

    click.echo(f"Communities:   {communities}")
    click.echo(f"Content items: {posts} ({unclassified} unclassified)")
    click.echo(f"Leads:         {leads} ({high} high priority)")


@cli.command("dashboard")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind address. Keep it local unless you know what you're exposing.")
@click.option("--port", type=int, default=8000, show_default=True)
@click.pass_context
def dashboard_cmd(ctx, host, port):
    """Open the web dashboard: leads, triage, activity and stats.

    Requires DASHBOARD_PASSWORD in your environment (put it in .env).
    """
    from circle_leads.web.app import run

    run(host=host, port=port, db_url=ctx.obj["db"].url)



def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
