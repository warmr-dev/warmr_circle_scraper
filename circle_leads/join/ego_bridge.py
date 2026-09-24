"""Python <-> ego-browser bridge for one join attempt.

Every ``ego-browser nodejs`` call is a fresh Node process; only the ego-browser
TaskSpace survives between calls (by numeric id). This module shells out to
the CLI per attempt and reads back one ``EGO_JOIN_RESULT=<json>`` line --
see ``ego_join_driver.mjs`` for the in-browser side. It doesn't store any
credential itself (``joiner.py`` reads CIRCLE_EMAIL/CIRCLE_PASSWORD from the
environment per call); this module only ever passes them through, embedded
in the script text, never logged.

Parameters are passed by substituting ``__TOKEN__`` placeholders in the script
text with ``json.dumps(...)`` literals, not via ``env=`` -- confirmed live
that ego-browser's embedded Node runtime does not propagate custom
environment variables into the script (only a safelisted subset such as
``PATH``/``HOME`` comes through), so an env-var approach silently produces
``undefined`` in the script every time.

Also confirmed live: ``ego-browser nodejs`` writes the script's own
``console.log()`` output to **stderr**, not stdout (the reverse of the usual
convention) -- so the result markers are read from stderr, not stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

DRIVER_SCRIPT_PATH = Path(__file__).with_name("ego_join_driver.mjs")

EGO_BROWSER_BIN = os.environ.get("EGO_BROWSER_BIN", "ego-browser")

_OPEN_SPACE_SCRIPT_TEMPLATE = (
    'const task = await taskSpace(__EGO_JOIN_SPACE_NAME__);\n'
    'console.log("EGO_JOIN_SPACE_ID=" + task.spaceId);\n'
)

# A space picks its browser profile when it is created, and that profile's
# cookie jar is shared by every space on it (ego-browser docs,
# references/clearing-state.md) -- so the profile, not the space, is what keeps
# two Circle accounts apart. taskSpace() refuses a profileId for a name that
# already exists, so an existing space is reused only if it already sits on the
# wanted profile; one on another profile is an error, never a silent reuse.
_OPEN_PROFILE_SPACE_SCRIPT_TEMPLATE = """
const wanted = __EGO_JOIN_PROFILE__;
const spaceName = __EGO_JOIN_SPACE_NAME__;
const existingId = __EGO_JOIN_EXISTING_SPACE_ID__;
const listed = await profiles();
const all = Array.isArray(listed) ? listed : (listed && listed.profiles) || [];
const prof = all.find((p) => p.name === wanted || p.id === wanted);
const rawSpaces = await listTaskSpaces();
const spaces = Array.isArray(rawSpaces) ? rawSpaces : (rawSpaces && (rawSpaces.spaces || rawSpaces.taskSpaces)) || [];
if (!prof) {
  console.log("EGO_JOIN_SPACE_ERROR=" + JSON.stringify(
    "Ego Lite profile " + JSON.stringify(wanted) + " not found; have: " + all.map((p) => p.name).join(", ")));
} else if (existingId !== null) {
  const sp = spaces.find((x) => Number(x.id) === Number(existingId));
  if (!sp) console.log("EGO_JOIN_SPACE_ERROR=" + JSON.stringify("task space " + existingId + " not found"));
  else if (sp.profileId && sp.profileId !== prof.id) console.log("EGO_JOIN_SPACE_ERROR=" + JSON.stringify(
    "task space " + existingId + " is on profile " + (sp.profileName || sp.profileId) + ", not " + prof.name));
  else console.log("EGO_JOIN_SPACE_ID=" + sp.id);
} else {
  const same = spaces.find((x) => x.name === spaceName);
  if (same && same.profileId && same.profileId !== prof.id) {
    console.log("EGO_JOIN_SPACE_ERROR=" + JSON.stringify(
      "a space named " + spaceName + " already exists on profile " + (same.profileName || same.profileId)));
  } else if (same) {
    console.log("EGO_JOIN_SPACE_ID=" + same.id);
  } else {
    const task = await taskSpace(spaceName, { profileId: prof.id });
    console.log("EGO_JOIN_SPACE_ID=" + task.spaceId);
  }
}
"""


class EgoBrowserError(RuntimeError):
    """The ego-browser CLI/bridge itself failed -- not installed, not
    connected, crashed, or produced no parseable result. Distinct from a
    community-level join outcome: the caller should stop the batch, not
    record this against the community being attempted."""


@dataclass
class EgoJoinResult:
    status: str
    detail: str
    screenshot: str | None = None
    # Only ever non-None when status == "joined" -- the host's session cookies
    # (see ego_join_driver.mjs::captureSessionCookies), shaped for
    # circle_leads.web.replay_store.store_session(): [{"name", "value", "domain"}].
    cookies: list[dict] | None = None
    # Only on "profile_form": {"fields": [...]} -- the required profile
    # questions Circle asked, for circle_leads.join.forms.resolve_form().
    form: dict | None = None


def _run_node(script: str, *, timeout: int) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            [EGO_BROWSER_BIN, "nodejs"],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise EgoBrowserError(
            f"'{EGO_BROWSER_BIN}' not found on PATH -- is ego-browser (Ego Lite) installed?"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise EgoBrowserError(f"ego-browser nodejs timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise EgoBrowserError(f"ego-browser nodejs exited {proc.returncode}:\n{proc.stderr}")
    return proc


def open_join_space(
    name: str,
    *,
    profile: str | None = None,
    existing_space_id: int | None = None,
    timeout: int = 60,
) -> int:
    """Create (or reuse) the TaskSpace a batch runs in; returns its numeric id.

    With ``profile`` (an Ego Lite profile name or id) the space lives on that
    profile -- the only thing that isolates one Circle account from another.
    ``existing_space_id`` (``--space-id``) is checked to be on that profile
    instead of creating a space. Reuse the id across every ``attempt_join``
    call in the same run -- a new space per host would lose the login state.
    """
    if profile is None:
        script = _OPEN_SPACE_SCRIPT_TEMPLATE.replace("__EGO_JOIN_SPACE_NAME__", json.dumps(name))
    else:
        script = (
            _OPEN_PROFILE_SPACE_SCRIPT_TEMPLATE
            .replace("__EGO_JOIN_PROFILE__", json.dumps(profile))
            .replace("__EGO_JOIN_SPACE_NAME__", json.dumps(name))
            .replace("__EGO_JOIN_EXISTING_SPACE_ID__", json.dumps(existing_space_id))
        )
    proc = _run_node(script, timeout=timeout)
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.startswith("EGO_JOIN_SPACE_ERROR="):
            raise EgoBrowserError(json.loads(line.split("=", 1)[1]))
        if line.startswith("EGO_JOIN_SPACE_ID="):
            return int(line.split("=", 1)[1])
    raise EgoBrowserError(
        f"could not read a space id from ego-browser output:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )


def attempt_join(
    space_id: int,
    url: str,
    *,
    email: str | None = None,
    password: str | None = None,
    screenshot_dir: str | None = None,
    answers: dict | None = None,
    timeout: int = 240,
) -> EgoJoinResult:
    """Run one join attempt against ``url`` inside the shared task space.

    ``email``/``password``, when both given, let the driver script log in
    itself on a host that isn't already authenticated (Circle sessions from a
    *.circle.so subdomain don't carry over to a custom domain -- confirmed
    live, so a fresh login prompt per custom-domain host is expected). Passed
    the same way as every other parameter -- substituted into the script
    text, never printed -- and never included in any emitted status/detail
    string by the driver.

    ``answers`` (Circle profile field id -> value) is the second pass after a
    ``profile_form`` outcome: the driver fills those required fields and saves
    the new-member profile. The timeout covers a login, the join and that step.
    """
    script = (
        DRIVER_SCRIPT_PATH.read_text()
        .replace("__EGO_JOIN_SPACE_ID__", json.dumps(space_id))
        .replace("__EGO_JOIN_URL__", json.dumps(url))
        .replace("__EGO_JOIN_SCREENSHOT_DIR__", json.dumps(screenshot_dir or ""))
        .replace("__EGO_JOIN_EMAIL__", json.dumps(email or ""))
        .replace("__EGO_JOIN_PASSWORD__", json.dumps(password or ""))
        .replace("__EGO_JOIN_ANSWERS__", json.dumps(answers))
    )
    proc = _run_node(script, timeout=timeout)
    combined_lines = (proc.stdout + proc.stderr).splitlines()
    result_line = next(
        (line for line in reversed(combined_lines) if line.startswith("EGO_JOIN_RESULT=")),
        None,
    )
    if result_line is None:
        raise EgoBrowserError(
            f"ego-browser produced no EGO_JOIN_RESULT line for {url}:\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    payload = json.loads(result_line.split("=", 1)[1])
    return EgoJoinResult(
        status=payload["status"],
        detail=payload.get("detail", ""),
        screenshot=payload.get("screenshot"),
        cookies=payload.get("cookies"),
        form=payload.get("form"),
    )
