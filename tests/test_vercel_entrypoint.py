"""Vercel requires a top-level FastAPI `app` at a recognized entrypoint."""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "app.py"


def test_vercel_entrypoint_file_assigns_top_level_app():
    """Match Vercel's AST scan: a module-level assignment to `app`."""
    tree = ast.parse(ENTRYPOINT.read_text(encoding="utf-8"))
    assigned = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.append(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assigned.append(alias.asname or alias.name)
    assert "app" in assigned


def test_vercel_entrypoint_importable_without_password(monkeypatch):
    """Vercel may import the module at build time before env vars are applied."""
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    sys.modules.pop("app", None)
    vercel_app = importlib.import_module("app")
    try:
        assert vercel_app.app is not None
        assert getattr(vercel_app.app, "asgi", None) is None
    finally:
        sys.modules.pop("app", None)


def test_vercel_entrypoint_serves_login(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DASHBOARD_PASSWORD", "dashboard-test-pw")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-signing-key")
    monkeypatch.setenv("CIRCLE_LEADS_DB", f"sqlite:///{tmp_path / 'vercel.db'}")

    sys.modules.pop("app", None)
    vercel_app = importlib.import_module("app")
    try:
        client = TestClient(vercel_app.app)
        assert client.get("/login").status_code == 200
    finally:
        sys.modules.pop("app", None)
