"""scripts/install_autojoin_launchd.sh -- the only path by which auto-join gets
scheduled at all.

There is no other coverage of the shell scripts, and the one bug they have had
so far was invisible until a human ran the installer: a function named
``install()`` whose body called ``install -m 755`` shadowed /usr/bin/install,
recursed into itself until zsh hit FUNCNEST, and aborted with the launcher
never copied. These tests drive the installer's ``--check`` mode against a
throwaway root so that failure mode (and its class) fails a test run instead.

Nothing here loads, bootstraps or kickstarts a LaunchAgent: ``--check`` is
launchctl-free by construction, and test_check_mode_never_calls_launchctl
proves it with a PATH shim rather than taking the script's word for it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
INSTALLER = SCRIPTS_DIR / "install_autojoin_launchd.sh"
LAUNCHER = SCRIPTS_DIR / "auto_join_launchd.sh"
PLIST_TEMPLATE = SCRIPTS_DIR / "com.warmr.autojoin.plist"

# zsh + plutil + the launchd layout are all macOS; the agent only ever runs on
# the operator's Mac (ego-lite is macOS-only), so elsewhere there is nothing to
# assert rather than something to fail.
darwin_only = pytest.mark.skipif(
    sys.platform != "darwin", reason="LaunchAgent installer is macOS-only"
)

# The rendered paths carry a mktemp root, so a placeholder is anything
# double-underscored that survived the sed pass.
PLACEHOLDER_RE = re.compile(r"__[A-Z0-9_]+__")
FUNC_DEF_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)", re.MULTILINE)


def run_check(root: Path, extra_env: dict[str, str] | None = None, cwd: Path | None = None):
    env = dict(os.environ, WARMR_INSTALL_ROOT=str(root))
    env.update(extra_env or {})
    return subprocess.run(
        [str(INSTALLER), "--check"],
        cwd=str(cwd or REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def installed(tmp_path):
    """One --check run into tmp_path, shared by the assertions about its output."""
    result = run_check(tmp_path)
    assert result.returncode == 0, f"--check failed:\n{result.stdout}\n{result.stderr}"
    return tmp_path


@darwin_only
def test_check_mode_copies_the_launcher(installed):
    # The copy IS the install: with install() shadowing /usr/bin/install this
    # file was never created and the agent pointed at a path that did not exist.
    copied = installed / "Library/Application Support/warmr/auto_join_launchd.sh"
    assert copied.is_file(), "installer did not copy the launcher"
    assert os.access(copied, os.X_OK), "copied launcher is not executable"
    assert copied.read_bytes() == LAUNCHER.read_bytes()


@darwin_only
def test_check_mode_renders_a_loadable_plist(installed):
    plist = installed / "Library/LaunchAgents/com.warmr.autojoin.plist"
    assert plist.is_file(), "installer did not render the plist template"
    rendered = plist.read_text()

    leftovers = PLACEHOLDER_RE.findall(rendered)
    assert not leftovers, f"unrendered placeholders in the plist: {sorted(set(leftovers))}"

    program = installed / "Library/Application Support/warmr/auto_join_launchd.sh"
    assert f"<string>{program}</string>" in rendered
    assert f"<string>{REPO_ROOT}</string>" in rendered

    # plutil is what launchctl silently disagrees with; a plist it rejects is
    # one that loads and then never runs.
    lint = subprocess.run(["plutil", "-lint", str(plist)], capture_output=True, text=True)
    assert lint.returncode == 0, lint.stdout + lint.stderr


@darwin_only
def test_installed_paths_stay_outside_tcc_protected_folders(installed):
    # The reason any of this copying exists: a launchd job inherits no Full
    # Disk Access, so anything it must read has to sit outside ~/Documents.
    home = Path.home()
    protected = (home / "Documents", home / "Desktop", home / "Downloads")
    plist = installed / "Library/LaunchAgents/com.warmr.autojoin.plist"
    rendered = plist.read_text()

    for key in ("ProgramArguments", "StandardOutPath", "StandardErrorPath"):
        assert key in rendered

    # Only the repo path may live under ~/Documents; the launcher and the logs
    # may not, and the defaults the script ships with decide that.
    body = INSTALLER.read_text()
    assert 'INSTALL_DIR="$INSTALL_ROOT/Library/Application Support/warmr"' in body
    assert 'LOG_DIR="$INSTALL_ROOT/Library/Logs/warmr"' in body
    assert 'INSTALL_ROOT="${WARMR_INSTALL_ROOT:-$HOME}"' in body

    for path in protected:
        assert f"<string>{path}" not in rendered.replace(f"<string>{REPO_ROOT}", "")


@darwin_only
def test_check_mode_runs_the_copied_launcher_as_a_dry_run(installed):
    # Proves the copy is not just bytes on disk but an executable zsh script
    # that reaches the repo and the venv -- and that it stops before joining.
    status = installed / "Library/Logs/warmr/selftest.status"
    assert status.read_text().strip() == "PASS"
    log = (installed / "Library/Logs/warmr/auto_join.log").read_text()
    assert "preflight: ok" in log
    assert "dry run: stopping before auto-join" in log


@darwin_only
def test_check_mode_never_calls_launchctl(tmp_path):
    # --check must be safe to run anywhere, including CI and this test suite:
    # loading the real label starts browser automation on a live account. A
    # PATH shim is the honest proof; reading the script is not.
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    sentinel = tmp_path / "launchctl-was-called"
    shim = shim_dir / "launchctl"
    shim.write_text(f'#!/bin/sh\necho "$@" >> "{sentinel}"\nexit 0\n')
    shim.chmod(0o755)

    root = tmp_path / "root"
    result = run_check(root, {"PATH": f"{shim_dir}:{os.environ['PATH']}"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert not sentinel.exists(), f"--check invoked launchctl: {sentinel.read_text()}"


@pytest.mark.parametrize("script", [INSTALLER, LAUNCHER], ids=lambda p: p.name)
def test_no_function_shadows_a_command_on_path(script):
    """The class of bug, not the one instance.

    A zsh function takes precedence over a PATH binary of the same name for the
    rest of the script, so ``install() { install -m 755 ...; }`` calls itself.
    Keeping every helper warmr_/verb-prefixed is the rule; this is the check.
    """
    names = FUNC_DEF_RE.findall(script.read_text())
    assert names, f"no function definitions found in {script.name} -- regex stale?"
    shadowed = {name: shutil.which(name) for name in names if shutil.which(name)}
    assert not shadowed, (
        f"{script.name} defines functions that shadow commands it may call: {shadowed}"
    )


@darwin_only
def test_unrendered_placeholder_in_the_template_is_refused(tmp_path):
    # A template knob the installer does not substitute yields a plist that
    # lints, loads, and then points at a literal __TOKEN__ forever -- the agent
    # simply never runs, with nothing in any log to say why.
    fake_repo = tmp_path / "repo"
    (fake_repo / "circle_leads").mkdir(parents=True)
    (fake_repo / "pyproject.toml").write_text("[project]\nname='fake'\n")
    fake_scripts = fake_repo / "scripts"
    fake_scripts.mkdir()
    shutil.copy2(INSTALLER, fake_scripts / INSTALLER.name)
    shutil.copy2(LAUNCHER, fake_scripts / LAUNCHER.name)
    template = PLIST_TEMPLATE.read_text().replace(
        "<key>StartInterval</key>",
        "<key>WarmrUnknownKnob</key><string>__WARMR_UNKNOWN__</string>\n    <key>StartInterval</key>",
    )
    (fake_scripts / PLIST_TEMPLATE.name).write_text(template)

    result = subprocess.run(
        [str(fake_scripts / INSTALLER.name), "--check"],
        env=dict(os.environ, WARMR_INSTALL_ROOT=str(tmp_path / "root")),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 70, result.stdout + result.stderr
    assert "__WARMR_UNKNOWN__" in result.stderr
    assert not (tmp_path / "root/Library/LaunchAgents/com.warmr.autojoin.plist").exists()


@darwin_only
def test_redirected_install_root_is_refused_outside_check_mode(tmp_path):
    # WARMR_INSTALL_ROOT is a test fixture. Honouring it for a real install
    # would write a LaunchAgent nobody loads while the live agent keeps
    # pointing at the stale copy.
    result = subprocess.run(
        [str(INSTALLER), "--no-selftest"],
        cwd=str(REPO_ROOT),
        env=dict(os.environ, WARMR_INSTALL_ROOT=str(tmp_path)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 64
    assert "only honoured by --check" in result.stderr
    assert not (tmp_path / "Library").exists()
