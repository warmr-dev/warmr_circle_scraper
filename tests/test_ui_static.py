"""The page and its modules: versioned paths, types, the sign-in gate, and
nothing loaded from anywhere but here.

No test runs the JavaScript; these pin what breaks a deploy silently -- an
import that points at a file that is not there, a module served as text/plain,
a cached old module after a deploy.
"""

import re
from pathlib import Path

from circle_leads.web.ui import UI_DIR, ui_version
from tests.dash_fixtures import make_app

MODULES = sorted(UI_DIR.rglob("*.js"))


def test_the_page_names_the_current_version(tmp_path, monkeypatch):
    client, _db, _app = make_app(tmp_path, monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    assert f"/ui/{ui_version()}/main.js" in r.text
    assert "__UI_VERSION__" not in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_modules_are_served_with_their_types(tmp_path, monkeypatch):
    client, _db, _app = make_app(tmp_path, monkeypatch)
    v = ui_version()
    js = client.get(f"/ui/{v}/main.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert "immutable" in js.headers["cache-control"]
    css = client.get(f"/ui/{v}/app.css")
    assert css.headers["content-type"].startswith("text/css")
    # An old version is still served, but never cached.
    assert client.get("/ui/old/main.js").headers["cache-control"] == "no-store"


def test_the_gate_and_the_walls(tmp_path, monkeypatch):
    client, _db, _app = make_app(tmp_path, monkeypatch)
    v = ui_version()
    assert client.get(f"/ui/{v}/../../app.py").status_code == 404
    assert client.get(f"/ui/{v}/sections/nope.js").status_code == 404
    client.post("/logout")
    client.cookies.clear()
    assert client.get(f"/ui/{v}/main.js").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303


def test_every_import_points_at_a_file():
    pattern = re.compile(r"""(?:import|export)\s[^'"]*?from\s+['"](\.[^'"]+)['"]|import\(\s*['"](\.[^'"]+)['"]""")
    for module in MODULES:
        for match in pattern.finditer(module.read_text()):
            target = (module.parent / (match.group(1) or match.group(2))).resolve()
            assert target.is_file(), f"{module.relative_to(UI_DIR)} imports missing {target.name}"


def test_every_imported_name_is_exported():
    exports: dict[Path, set[str]] = {}
    for module in MODULES:
        text = module.read_text()
        names = set(re.findall(r"export\s+(?:async\s+)?(?:function|const|let|class)\s+([\w$]+)", text))
        for group in re.findall(r"export\s*\{([^}]*)\}", text):
            names |= {n.strip().split(" as ")[-1] for n in group.split(",") if n.strip()}
        exports[module.resolve()] = names
    pattern = re.compile(r"import\s*\{([^}]*)\}\s*from\s*['\"](\.[^'\"]+)['\"]")
    for module in MODULES:
        for group, path in pattern.findall(module.read_text()):
            target = (module.parent / path).resolve()
            for name in (n.strip().split(" as ")[0] for n in group.split(",") if n.strip()):
                assert name in exports[target], f"{module.name} imports {name} from {target.name}"


def test_nothing_is_loaded_from_elsewhere():
    for path in [*MODULES, UI_DIR / "app.css", UI_DIR.parent / "index.html"]:
        text = path.read_text()
        assert not re.search(r"""(?:src|href)\s*=\s*["']https?://""", text), path.name
        assert "@import" not in text, path.name
        assert not re.search(r"""from\s+['"]https?://""", text), path.name
