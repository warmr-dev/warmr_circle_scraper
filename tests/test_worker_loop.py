"""The harvest CLI's worker modes: --scheduled skips when not due; --loop exists."""

from __future__ import annotations

import tempfile

from click.testing import CliRunner

from circle_leads.cli.main import cli
from circle_leads.storage.database import Database
from circle_leads.storage.settings_store import set_schedule


def _db_url():
    url = "sqlite:///" + tempfile.mktemp(suffix=".db")
    set_schedule(Database(url), "off")  # off -> never due
    return url


def test_scheduled_skips_when_not_due():
    url = _db_url()
    res = CliRunner().invoke(cli, ["--db", url, "harvest", "--scheduled"])
    assert res.exit_code == 0
    assert "not due yet" in res.output


def test_loop_flag_is_available():
    res = CliRunner().invoke(cli, ["harvest", "--help"])
    assert res.exit_code == 0
    assert "--loop" in res.output
    assert "--poll-seconds" in res.output
