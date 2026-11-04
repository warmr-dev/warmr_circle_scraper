"""The watchdog alert must name the machine that exists.

The alert is the only channel that reports a droplet which has stopped, run out
of disk, or lost its network -- so its one actionable line is where to look. It
used to hard-code the address of the droplet as first provisioned. That droplet
was replaced and the text was not, so a real alert pointed the operator at a
host that was no longer theirs: worse than no hint, because it looks confident.

These tests pin that the address comes from configuration, and that the shipped
default is the host the deploy script documents.
"""
from __future__ import annotations

import importlib

import pytest


def _reload_app(monkeypatch, hint=None):
    """Re-import the module so the module-level default is recomputed."""
    from circle_leads.web import app as app_module

    if hint is None:
        monkeypatch.delenv("CIRCLE_DEPLOY_HOST_HINT", raising=False)
    else:
        monkeypatch.setenv("CIRCLE_DEPLOY_HOST_HINT", hint)
    return importlib.reload(app_module)


def test_the_default_matches_the_documented_deploy_host(monkeypatch):
    """deploy/droplet/README.md is the source of truth for the live address."""
    from pathlib import Path

    readme = (
        Path(__file__).resolve().parents[1] / "deploy" / "droplet" / "README.md"
    ).read_text(encoding="utf-8")
    module = _reload_app(monkeypatch)
    try:
        assert f"root@{module.DEPLOY_HOST_HINT}" in readme, (
            "the watchdog alert names a host the deploy runbook does not"
        )
    finally:
        _reload_app(monkeypatch)


def test_the_retired_address_is_not_the_default(monkeypatch):
    module = _reload_app(monkeypatch)
    try:
        # warmr-1 as first provisioned; replaced, and its address now answers
        # nothing useful. Nothing in the shipped defaults may point at it.
        assert module.DEPLOY_HOST_HINT != "168.144.131.38"
    finally:
        _reload_app(monkeypatch)


def test_the_environment_overrides_the_default(monkeypatch):
    module = _reload_app(monkeypatch, hint="root@203.0.113.9")
    try:
        assert module.DEPLOY_HOST_HINT == "root@203.0.113.9"
    finally:
        _reload_app(monkeypatch)


def test_surrounding_whitespace_is_trimmed(monkeypatch):
    """A stray newline in an env file must not reach the chat message."""
    module = _reload_app(monkeypatch, hint="  157.230.181.187\n")
    try:
        assert module.DEPLOY_HOST_HINT == "157.230.181.187"
    finally:
        _reload_app(monkeypatch)


def test_an_empty_override_falls_back_to_the_default(monkeypatch):
    """Setting the variable to nothing is how an env file disables a line;
    it must not blank the address out of a real alert."""
    module = _reload_app(monkeypatch, hint="")
    try:
        assert module.DEPLOY_HOST_HINT
    finally:
        _reload_app(monkeypatch)
