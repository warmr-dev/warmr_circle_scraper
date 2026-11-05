#!/usr/bin/env python3
"""Canonical documentation validator, shared by local publishing and CI."""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT).decode()


def policy():
    return json.loads((ROOT / "scripts/pr-policy.json").read_text())


def delivery(path, rules):
    return path.endswith(".md") and any(path.startswith(p) for p in rules["delivery_prefixes"])


def canonical(path, rules):
    return (path.endswith((".md", ".rst", ".txt")) and not delivery(path, rules)
            and (path in rules["canonical_files"]
                 or any(path.startswith(p) for p in rules["canonical_prefixes"])))


def functional(path, rules):
    # Unknown paths require docs. New runtime/config directories cannot evade the gate.
    parts = PurePosixPath(path).parts
    name = parts[-1]
    if name in {"AGENTS.md", "CLAUDE.md", "SKILL.md"}:
        return True
    if canonical(path, rules) or delivery(path, rules) or path.endswith(".md"):
        return False
    if any(p in {"tests", "__tests__", "fixtures", "__snapshots__"} for p in parts):
        return False
    if re.search(r"(?:[._](?:test|spec)[._]|_test\.py$|^test_.*\.py$)", name):
        return False
    if name.endswith(".hashes.json"):
        return False
    return True


def validate(paths, body, rules, available=None):
    errors = []
    needs_docs = any(functional(p, rules) for p in paths)
    available = set(paths) if available is None else available
    if needs_docs:
        if not any(canonical(p, rules) for p in available):
            errors.append("Functional change requires a changed canonical runbook.")
        if not any(delivery(p, rules) for p in available):
            errors.append("Functional change requires a changed docs/changes delivery record.")
    if body is None:
        return errors
    sections = re.findall(r"^## Documentation impact[ \t]*\r?$", body, re.M)
    if len(sections) != 1:
        return errors + ["PR body must contain exactly one ## Documentation impact section."]
    section = re.split(r"^## Documentation impact[ \t]*\r?$", body, flags=re.M)[1]
    section = re.split(r"^## |<!-- CURSOR_AGENT_PR_BODY_END -->", section, maxsplit=1, flags=re.M)[0]
    if re.search(r"<[^>]+>|docs/path-to-|YYYY-MM-DD|<canonical", section):
        errors.append("Unfilled Documentation impact placeholder.")
    labels = {}
    for line in section.splitlines():
        match = re.match(r"^\s*(?:-\s*)?(Documentation added|Delivery record|Documentation not required reason|Documentation not required):\s*(.*?)\s*$", line)
        if match:
            label, value = match.groups()
            if label in labels:
                errors.append("Duplicate documentation declaration: " + label)
            labels[label] = value
    legacy_reason = labels.get("Documentation not required")
    doc = labels.get("Documentation added", "")
    record = labels.get("Delivery record", "")
    no_docs = legacy_reason is not None or (doc.strip("`") == "N/A" and record.strip("`") == "N/A")
    if no_docs:
        reason = legacy_reason if legacy_reason is not None else labels.get("Documentation not required reason", "")
        if legacy_reason is not None and (doc or record):
            errors.append("Use either legacy exemption or documentation paths, not both.")
        if len(reason.strip()) < 15 or reason.strip() == "N/A":
            errors.append("Documentation exemption requires a specific reason (at least 15 characters).")
        if needs_docs:
            errors.append("Runtime/configuration changes cannot use a documentation exemption.")
        return errors
    for label, value, predicate in [("Documentation added", doc, canonical), ("Delivery record", record, delivery)]:
        # Accept the existing single bare path and Scout's multiple backticked paths.
        named = re.findall(r"`([^`\r\n]+)`", value) or ([value] if value and not re.search(r"\s", value) else [])
        if not named:
            errors.append(label + " must name concrete changed paths.")
        for path in named:
            if path not in available or not predicate(path, rules):
                errors.append(label + " must reference an existing, changed " + ("canonical runbook: " if label == "Documentation added" else "delivery record: ") + path)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--changed-files-file")
    parser.add_argument("--pr-body-file")
    parser.add_argument("--require-pr-body", action="store_true")
    args = parser.parse_args()
    if args.require_pr_body and not args.pr_body_file:
        parser.error("--require-pr-body needs --pr-body-file")
    if args.changed_files_file:
        paths = Path(args.changed_files_file).read_text().splitlines()
        available = set(paths)
    else:
        # Include both sides of renames; deleted runtime files remain functional.
        paths = git("diff", "--no-renames", "--name-only", "-z", args.base + "..." + args.head).split("\0")
        paths = [p for p in paths if p]
        available = set(git("diff", "--no-renames", "--diff-filter=AMT", "--name-only", "-z", args.base + "..." + args.head).split("\0"))
        # A symlink is not a documentation update, even if its suffix is .md.
        available = {p for p in available if p and git("ls-tree", args.head, "--", p).startswith("100")}
    body = Path(args.pr_body_file).read_text() if args.pr_body_file else None
    errors = validate(paths, body, policy(), available)
    if errors:
        raise ValueError("\n".join(errors))
    print("Documentation impact OK: diff and supplied PR body validated.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("Documentation gate failed: " + str(error), file=sys.stderr)
        sys.exit(1)
