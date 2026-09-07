"""`circle-connector` — the local connector CLI (runs on YOUR computer).

  circle-connector pair --backend https://your-app.up.railway.app --code ABCD1234
  circle-connector login altea.circle.so         # opens a browser; you sign in
  circle-connector sync altea.circle.so          # read + upload once
  circle-connector run altea.circle.so other.circle.so --interval 600   # loop
  circle-connector status                         # local + backend status
"""

from __future__ import annotations

import sys
import time

import click

from circle_leads.connector.client import BackendClient, ConnectorConfig
from circle_leads.connector.runner import authenticate, sync_community


def _client() -> BackendClient:
    cfg = ConnectorConfig.load()
    if cfg is None or not cfg.token:
        raise click.ClickException("Not paired. Run `circle-connector pair` first.")
    return BackendClient(cfg)


@click.group()
def cli() -> None:
    """Local Circle Connector — keeps your Circle session on this machine."""


@cli.command("pair")
@click.option("--backend", required=True, help="Your Railway backend URL.")
@click.option("--code", required=True, help="One-time pairing code from the dashboard.")
def pair_cmd(backend: str, code: str) -> None:
    """Pair this connector with your Railway dashboard using a one-time code."""
    cfg = ConnectorConfig(backend_url=backend)
    client = BackendClient(cfg)
    client.claim(code)
    click.echo("Paired. Token saved locally to ~/.circle-leads/connector.json (never uploaded).")


@cli.command("login")
@click.argument("host")
def login_cmd(host: str) -> None:
    """Open a browser so you can log into a private Circle community yourself."""
    authenticate(host)
    click.echo(f"Signed in to {host}. Session saved to your local browser profile.")


@cli.command("sync")
@click.argument("host")
@click.option("--max-pages", type=int, default=5, show_default=True)
def sync_cmd(host: str, max_pages: int) -> None:
    """Read one community with your local session and upload normalized posts."""
    client = _client()
    result = sync_community(host, client, max_pages=max_pages)
    click.echo(str(result))


@cli.command("run")
@click.argument("hosts", nargs=-1)
@click.option("--interval", type=int, default=600, show_default=True,
              help="Seconds between sync cycles.")
@click.option("--max-pages", type=int, default=5, show_default=True)
def run_cmd(hosts, interval, max_pages) -> None:
    """Loop: heartbeat + sync every --interval seconds.

    With no HOSTS, the connector asks the dashboard what to scan and follows
    the priority you set there (VIP first; paused communities skipped). Pass
    hosts explicitly to override that for a one-off run.
    """
    client = _client()
    if hosts:
        click.echo(f"Connector running for {len(hosts)} community/communities. "
                   "Ctrl-C to stop.")
    else:
        click.echo("Connector running from the dashboard worklist "
                   "(VIP first). Ctrl-C to stop.")

    while True:
        client.heartbeat()

        targets = list(hosts)
        if not targets:
            try:
                work = client.worklist()
                targets = [c["host"] for c in work]
                if not targets:
                    click.echo("  no communities to scan "
                               "(add one in the dashboard, or un-pause it)")
            except Exception as exc:  # noqa: BLE001 - backend blip; retry next cycle
                click.echo(f"  could not fetch the worklist: "
                           f"{exc.__class__.__name__}", err=True)

        for host in targets:
            try:
                result = sync_community(host, client, max_pages=max_pages)
                click.echo(f"  {host}: {result.get('state')} "
                           f"({result.get('posts', 0)} post(s))")
            except Exception as exc:  # noqa: BLE001 - one bad host must not stop the loop
                click.echo(f"  {host}: error {exc.__class__.__name__}", err=True)
        time.sleep(max(30, interval))


@cli.command("status")
def status_cmd() -> None:
    """Show local pairing + backend reachability."""
    cfg = ConnectorConfig.load()
    if cfg is None:
        click.echo("Not configured. Run `circle-connector pair`.")
        return
    click.echo(f"Backend: {cfg.backend_url}")
    click.echo(f"Paired:  {'yes' if cfg.token else 'no'}")
    if cfg.token:
        ok = BackendClient(cfg).heartbeat()
        click.echo(f"Backend reachable: {'yes' if ok else 'no'}")


def main() -> None:
    cli()


if __name__ == "__main__":
    sys.exit(main())
