#!/usr/bin/env python3
"""Fail-closed PR publishing, documentation attestation and exact-head merging."""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
from documentation_gate import ROOT, policy


def require(condition, message):
    if not condition:
        raise ValueError(message)


class Workflow:
    def __init__(self):
        self.rules = policy()
        self.repo = self.rules["repository"]
        self.base = self.rules["base"]

    def run(self, *args):
        return subprocess.check_output(args, cwd=ROOT).decode()

    def git(self, *args):
        return self.run("git", *args).strip()

    def api(self, endpoint, *args):
        return json.loads(self.run("gh", "api", endpoint, *args))

    def identity(self):
        remote = self.git("remote", "get-url", "origin").removesuffix(".git")
        require(remote in {"https://github.com/" + self.repo,
                           "https://warmr-dev@github.com/" + self.repo,
                           "git@github.com:" + self.repo}, "Unexpected origin repository")
        require(self.git("config", "--global", "--get", "user.name") == "warmr-dev"
                and self.git("config", "--global", "--get", "user.email") == "warmr@rapidevelopers.com",
                "Wrong Git publishing identity")
        require(self.api("user")["login"] == "warmr-dev", "Wrong GitHub publishing account")

    def clean_head(self, expected=None):
        require(not self.git("status", "--porcelain"), "Checkout must be clean")
        branch = self.git("branch", "--show-current")
        require(branch.startswith("codex/"), "Use an isolated codex task branch")
        head = self.git("rev-parse", "HEAD")
        require(expected is None or head == expected, "Checkout differs from expected head")
        return head

    def check(self, body_file, no_fetch=False, base_sha=None):
        head = self.clean_head()
        if not no_fetch:
            self.git("fetch", "origin", self.base)
        base = self.git("rev-parse", "origin/" + self.base)
        require(base_sha is None or base == base_sha, "Fetched base differs from current PR base")
        self.git("merge-base", "--is-ancestor", base, head)
        require(self.git("diff", "--name-only", base + "..." + head), "No PR changes")
        self.run("bash", str(ROOT / "scripts/check-documentation-impact.sh"),
                 "--base", base, "--head", head, "--pr-body-file", str(body_file), "--require-pr-body")
        self.clean_head(head)
        return head

    def read_pr(self, number):
        pr = self.api(f"repos/{self.repo}/pulls/{number}")
        require(pr["state"] == "open" and pr["base"]["ref"] == self.base, "PR must be open against " + self.base)
        require(pr["head"]["repo"]["full_name"] == self.repo
                and pr["base"]["repo"]["full_name"] == self.repo, "Unexpected PR repository")
        require(pr["base"]["sha"] == self.api(f"repos/{self.repo}/commits/{self.base}")["sha"],
                "Default branch advanced beyond the PR base")
        self.clean_head(pr["head"]["sha"])
        return pr

    @staticmethod
    def inputs(pr):
        return (pr["head"]["sha"], pr["base"]["sha"], pr.get("body") or "")

    def unchanged(self, number, original):
        current = self.read_pr(number)
        require(self.inputs(current) == self.inputs(original), "PR head/base/body changed; validate and review again")
        return current

    def status(self, number, head, state, description):
        self.api(f"repos/{self.repo}/statuses/{head}", "--method", "POST",
                 "-f", "state=" + state, "-f", "context=documentation-local",
                 "-f", "description=" + description,
                 "-f", f"target_url=https://github.com/{self.repo}/pull/{number}")

    def attest(self, number, expected=None, expected_body=None, dry_run=False):
        self.identity()
        pr = self.read_pr(number)
        head = pr["head"]["sha"]
        require(expected is None or head == expected, "PR head differs from reviewed head")
        require(expected_body is None or (pr.get("body") or "") == expected_body,
                "Remote PR body differs from the exact validated body")
        description = "Validated documentation " + hashlib.sha256(
            json.dumps(self.inputs(pr), ensure_ascii=False).encode()).hexdigest()[:20]
        try:
            if not dry_run:
                self.status(number, head, "pending", "Validating current PR head, base and body")
            with tempfile.TemporaryDirectory(prefix="pr-documentation-") as temp:
                body_file = Path(temp) / "body.md"
                body_file.write_text(pr.get("body") or "")
                self.check(body_file, base_sha=pr["base"]["sha"])
            self.unchanged(number, pr)
            if not dry_run:
                self.status(number, head, "success", description)
                # Read our actual stored status, not just the POST response.
                latest = self.api(f"repos/{self.repo}/commits/{head}/status")
                status = next((s for s in latest["statuses"] if s["context"] == "documentation-local"), {})
                require(status.get("state") == "success" and status.get("description") == description,
                        "Successful documentation status could not be read back")
                self.unchanged(number, pr)
        except Exception:
            if not dry_run:
                try:
                    self.status(number, head, "failure", "Documentation validation or input verification failed")
                except Exception:
                    pass  # Preserve the original failure; never continue toward merge.
            raise
        print(json.dumps({"pr": number, "head": head, "base": pr["base"]["sha"],
                          "documentation": "validated" if dry_run else "success/readback", "dry_run": dry_run}))
        return pr

    def publish(self, body_file, number=None, title=None, draft=False):
        # Snapshot the body before any GitHub or push mutation. Use this same file throughout.
        body = Path(body_file).read_text()
        with tempfile.TemporaryDirectory(prefix="pr-publish-") as temp:
            saved = Path(temp) / "body.md"
            saved.write_text(body)
            head = self.check(saved)
            self.identity()
            branch = self.git("branch", "--show-current")
            if number:
                pr = self.api(f"repos/{self.repo}/pulls/{number}")
                require(pr["state"] == "open" and pr["base"]["ref"] == self.base
                        and pr["head"]["ref"] == branch
                        and pr["head"]["repo"]["full_name"] == self.repo
                        and pr["base"]["repo"]["full_name"] == self.repo, "PR does not match this task branch")
                # A changed body does not invalidate a commit status automatically.
                self.status(number, pr["head"]["sha"], "pending", "PR publication update requires revalidation")
            self.clean_head(head)
            # Refuse non-fast-forward changes; no automatic force push or rebase.
            self.git("push", "origin", head + ":refs/heads/" + branch)
            if number:
                command = ["gh", "pr", "edit", str(number), "--repo", self.repo, "--body-file", str(saved)]
                if title:
                    command += ["--title", title]
                self.run(*command)
            else:
                title = title or self.git("log", "-1", "--format=%s")
                command = ["gh", "pr", "create", "--repo", self.repo, "--base", self.base,
                           "--head", branch, "--title", title, "--body-file", str(saved)]
                if draft:
                    command += ["--draft"]
                url = self.run(*command).strip()
                require(re.fullmatch(r"https://github.com/" + re.escape(self.repo) + r"/pull/[1-9][0-9]*", url),
                        "Unexpected PR creation response; inspect GitHub before retrying")
                number = int(url.rsplit("/", 1)[1])
            self.attest(number, head, body)
        print(f"https://github.com/{self.repo}/pull/{number}")

    def merge(self, number, expected, dry_run=False):
        pr = self.attest(number, expected, dry_run=dry_run)
        require(not pr.get("draft"), "Draft PR cannot be merged")
        self.unchanged(number, pr)
        if dry_run:
            return
        result = self.api(f"repos/{self.repo}/pulls/{number}/merge", "--method", "PUT",
                          "-f", "sha=" + expected, "-f", "merge_method=merge")
        require(result.get("merged") is True, "GitHub did not confirm merge: " + result.get("message", ""))
        print(json.dumps(result))


def positive_number(value):
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("Expected a positive PR number")
    return int(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--pr-body-file", required=True)
    check.add_argument("--no-fetch", action="store_true")
    publish = commands.add_parser("publish")
    publish.add_argument("--body-file", required=True)
    publish.add_argument("--pr", type=positive_number)
    publish.add_argument("--title")
    publish.add_argument("--draft", action="store_true")
    for name in ["attest", "merge"]:
        command = commands.add_parser(name)
        command.add_argument("number", type=positive_number)
        command.add_argument("--expected-head", required=(name == "merge"))
        command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if getattr(args, "expected_head", None) is not None:
        require(re.fullmatch(r"[0-9a-f]{40}", args.expected_head), "Expected a full reviewed commit SHA")
    workflow = Workflow()
    if args.command == "check":
        workflow.check(args.pr_body_file, args.no_fetch)
        print("PR preflight passed: clean current branch and exact body")
    elif args.command == "publish":
        workflow.publish(args.body_file, args.pr, args.title, args.draft)
    elif args.command == "attest":
        workflow.attest(args.number, args.expected_head, dry_run=args.dry_run)
    else:
        workflow.merge(args.number, args.expected_head, args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError, StopIteration) as error:
        print("PR workflow stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
