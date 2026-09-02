"""Command-line interface."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from circle_leads.config.settings import (
    load_community_permissions,
    load_requirements,
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


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
