#!/usr/bin/env python3
"""Offline contract and failure-order tests. Never contacts GitHub or merges PRs."""
import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from documentation_gate import ROOT, policy, validate
from pr_workflow import Workflow

HEAD = "a" * 40
BASE = "b" * 40
FILES = ["src/runtime.py", "docs/runbook.md", "docs/changes/delivery.md"]
BODY = "## Documentation impact\n- Documentation added: `docs/runbook.md`\n- Delivery record: `docs/changes/delivery.md`\n"
EXEMPTION = "## Documentation impact\n- Documentation added: N/A\n- Delivery record: N/A\n- Documentation not required reason: test-only change; no runtime or operational behavior changed.\n"


class ValidatorTests(unittest.TestCase):
    def test_valid_functional(self):
        self.assertEqual(validate(FILES, BODY, policy()), [])

    def test_missing_or_fake_docs(self):
        for body in ["", BODY.replace("docs/runbook.md", "docs/other.md"),
                     BODY.replace("docs/runbook.md", "<canonical path>"),
                     BODY + BODY, BODY.replace("## Documentation impact", "## Other")]:
            with self.subTest(body=body):
                self.assertTrue(validate(FILES, body, policy()))

    def test_runtime_and_configuration_cannot_exempt(self):
        for path in ["components/widget.tsx", "scripts/release.sh", ".github/workflows/build.yml",
                     "migrations/001.sql", "config/runtime.json", "package.json", "Dockerfile",
                     "render.yaml", "new-runtime/main.go", "requirements.txt", "openclaw/skills/tool/SKILL.md"]:
            with self.subTest(path=path):
                self.assertTrue(validate([path], EXEMPTION, policy()))

    def test_functional_requires_separate_runbook_and_record(self):
        for paths in [["src/runtime.py"], ["src/runtime.py", "docs/changes/delivery.md"],
                      ["src/runtime.py", "docs/runbook.md"]]:
            self.assertTrue(validate(paths, BODY, policy()))

    def test_deleted_docs_and_symlinks_not_accepted(self):
        self.assertTrue(validate(FILES, BODY, policy(), {"src/runtime.py"}))

    def test_legacy_scout_formats(self):
        legacy = "## Documentation impact\nDocumentation added: `docs/runbook.md`\nDelivery record: `docs/changes/delivery.md`\n"
        self.assertEqual(validate(FILES, legacy, policy()), [])
        legacy = "## Documentation impact\nDocumentation not required: test-only change with no operational impact.\n"
        self.assertEqual(validate(["tests/test_example.py"], legacy, policy()), [])
        self.assertTrue(validate(["src/runtime.py"], legacy, policy()))

    def test_multiple_scout_paths(self):
        body = BODY.replace("`docs/runbook.md`", "`docs/runbook.md`, `docs/other.md`")
        self.assertEqual(validate(FILES + ["docs/other.md"], body, policy()), [])

    def test_exemptions_require_reason_and_safe_paths(self):
        for path in ["tests/test_example.py", "lib/example.test.ts", "openclaw/context/runtime.hashes.json"]:
            self.assertEqual(validate([path], EXEMPTION, policy()), [])
            self.assertTrue(validate([path], EXEMPTION.split("Documentation not required reason:")[0], policy()))

    def test_declarations_outside_section_do_not_count(self):
        body = "## Documentation impact\n## Other\n" + BODY.split("\n", 1)[1]
        self.assertTrue(validate(FILES, body, policy()))

    def test_duplicate_and_conflicting_declarations_rejected(self):
        self.assertTrue(validate(FILES, BODY + "Documentation added: `docs/runbook.md`\n", policy()))
        self.assertTrue(validate(FILES, BODY + "Documentation not required: no operational change at all\n", policy()))

    def test_footer_outside_section_is_ignored(self):
        for footer in ["\n## Rollout\n<a href='example'>link</a>", "\n<!-- CURSOR_AGENT_PR_BODY_END -->\n<div>footer</div>"]:
            self.assertEqual(validate(FILES, BODY + footer, policy()), [])

    def test_delivery_cannot_double_as_runbook(self):
        self.assertTrue(validate(FILES, BODY.replace("docs/runbook.md", "docs/changes/delivery.md"), policy()))


class FakeWorkflow(Workflow):
    def __init__(self):
        super().__init__()
        self.events = []
        self.pr = {"state": "open", "draft": False, "body": BODY,
                   "head": {"sha": HEAD, "ref": "codex/test", "repo": {"full_name": self.repo}},
                   "base": {"sha": BASE, "ref": "main", "repo": {"full_name": self.repo}}}
        self.main = BASE
        self.local = HEAD
        self.dirty = False
        self.status_value = None
        self.fail_validator = False
        self.fail_status = None
        self.missing_readback = False
        self.drift = None
        self.reads = 0
        self.seen_body = None
        self.hook = None

    def api(self, endpoint, *args):
        self.events.append(("api", endpoint, *args))
        if endpoint == "user":
            return {"login": "warmr-dev"}
        if endpoint.endswith("/commits/main"):
            return {"sha": self.main}
        if endpoint.endswith("/pulls/1"):
            self.reads += 1
            if self.reads > 1 and self.drift:
                if self.drift == "body":
                    self.pr["body"] += "changed"
                elif self.drift == "base":
                    self.pr["base"]["sha"] = "c" * 40
                elif self.drift == "head":
                    self.pr["head"]["sha"] = "d" * 40
            return copy.deepcopy(self.pr)
        if "/statuses/" in endpoint:
            values = dict(args[i + 1].split("=", 1) for i, item in enumerate(args) if item == "-f")
            if values["state"] == self.fail_status:
                raise ValueError("Status API failed")
            self.status_value = values
            return values
        if endpoint.endswith("/status"):
            return {"statuses": [] if self.missing_readback else [self.status_value]}
        if endpoint.endswith("/merge"):
            return {"merged": True, "sha": "e" * 40}
        raise AssertionError(endpoint)

    def git(self, *args):
        self.events.append(("git", *args))
        if args == ("status", "--porcelain"):
            return " M runtime.py" if self.dirty else ""
        if args == ("branch", "--show-current"):
            return "codex/test"
        if args == ("rev-parse", "HEAD"):
            return self.local
        if args == ("rev-parse", "origin/main"):
            return self.main
        if args[:2] == ("diff", "--name-only"):
            return "\n".join(FILES)
        if args[:3] == ("remote", "get-url", "origin"):
            return "https://warmr-dev@github.com/" + self.repo + ".git"
        if args[-1:] == ("user.name",):
            return "warmr-dev"
        if args[-1:] == ("user.email",):
            return "warmr@rapidevelopers.com"
        if args[:1] in [("fetch",), ("merge-base",), ("push",)]:
            return ""
        raise AssertionError(args)

    def run(self, *args):
        self.events.append(tuple(args))
        if args[0] == "bash":
            self.seen_body = Path(args[args.index("--pr-body-file") + 1]).read_text()
            if self.hook:
                self.hook()
            if self.fail_validator or validate(FILES, self.seen_body, policy()):
                raise ValueError("Validator failed")
            return "OK"
        if args[:3] == ("gh", "pr", "create"):
            return f"https://github.com/{self.repo}/pull/1\n"
        if args[:3] == ("gh", "pr", "edit"):
            return "OK"
        raise AssertionError(args)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.flow = FakeWorkflow()

    def merged(self):
        return any(str(e[1]).endswith("/merge") for e in self.flow.events if len(e) > 1)

    def states(self):
        return [e[e.index("-f") + 1] for e in self.flow.events if e[0] == "api" and "/statuses/" in e[1]]

    def test_success_rechecks_and_passes_exact_head_to_merge(self):
        self.flow.merge(1, HEAD)
        self.assertTrue(self.merged())
        self.assertGreaterEqual(self.flow.reads, 4)
        self.assertEqual(self.states(), ["state=pending", "state=success"])
        self.assertIn("sha=" + HEAD, self.flow.events[-1])

    def test_merge_fails_closed_for_validation_or_status_failure(self):
        for attr, value in [("fail_validator", True), ("fail_status", "success"),
                            ("fail_status", "pending"), ("missing_readback", True), ("dirty", True),
                            ("local", "f" * 40), ("main", "c" * 40)]:
            with self.subTest(attr=attr):
                self.flow = FakeWorkflow()
                setattr(self.flow, attr, value)
                with self.assertRaises(ValueError):
                    self.flow.merge(1, HEAD)
                self.assertFalse(self.merged())

    def test_all_input_drift_stops_merge(self):
        for drift in ["head", "base", "body"]:
            self.flow = FakeWorkflow()
            self.flow.drift = drift
            with self.assertRaises(ValueError):
                self.flow.merge(1, HEAD)
            self.assertFalse(self.merged())
            self.assertNotIn("state=success", self.states())

    def test_wrong_reviewed_head_never_publishes_success(self):
        with self.assertRaises(ValueError):
            self.flow.merge(1, "f" * 40)
        self.assertEqual(self.states(), [])
        self.assertFalse(self.merged())

    def test_dry_run_never_writes_status_or_merges(self):
        self.flow.merge(1, HEAD, dry_run=True)
        self.assertFalse(self.merged())
        self.assertEqual(self.states(), [])
        self.assertEqual(self.flow.seen_body, BODY)

    def test_missing_status_readback_marks_failure(self):
        self.flow.missing_readback = True
        with self.assertRaises(ValueError):
            self.flow.attest(1)
        self.assertEqual(self.states()[-1], "state=failure")

    def test_invalid_publication_has_no_push_or_github_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "body"
            file.write_text("## No documentation declaration")
            with self.assertRaises(ValueError):
                self.flow.publish(file, title="Test")
        self.assertFalse(any(e[:2] == ("git", "push") or e[0] == "api" for e in self.flow.events))

    def test_publication_uses_immutable_body_snapshot_and_attests(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "body"
            file.write_text(BODY)
            self.flow.hook = lambda: file.write_text("Changed original file")
            self.flow.publish(file, title="Test")
        self.assertEqual(self.flow.seen_body, BODY)
        self.assertIn("state=success", self.states())

    def test_remote_body_mismatch_stops_attestation(self):
        with self.assertRaises(ValueError):
            self.flow.attest(1, HEAD, "Another body")
        self.assertNotIn("state=success", self.states())

    def test_update_invalidates_old_status_before_push(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "body"
            file.write_text(BODY)
            self.flow.publish(file, 1)
        pending = next(i for i, e in enumerate(self.flow.events) if "state=pending" in e)
        push = next(i for i, e in enumerate(self.flow.events) if e[:2] == ("git", "push"))
        self.assertLess(pending, push)


class RealGitTests(unittest.TestCase):
    def test_actual_cli_blocks_bad_body_before_push_and_github(self):
        with tempfile.TemporaryDirectory(prefix="pr-gate-test-") as temp:
            root = Path(temp)
            repo = root / "repo"
            origin = root / "origin.git"
            subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
            subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=repo, stderr=subprocess.DEVNULL).decode().strip()
            git("config", "user.name", "Gate Test")
            git("config", "user.email", "gate@example.invalid")
            (repo / "README.md").write_text("baseline")
            git("add", ".")
            git("commit", "-qm", "baseline")
            git("branch", "-M", "main")
            git("push", "-q", "origin", "main")
            git("switch", "-qc", "codex/test")
            scripts = repo / "scripts"
            scripts.mkdir()
            for name in ["pr_workflow.py", "documentation_gate.py", "pr-policy.json", "pr-publish.sh", "check-documentation-impact.sh"]:
                shutil.copyfile(ROOT / "scripts" / name, scripts / name)
            (repo / "runtime.py").write_text("pass\n")
            (repo / ".gitignore").write_text("__pycache__/\n")
            git("add", ".")
            git("commit", "-qm", "runtime without docs")
            body = root / "body.md"
            body.write_text(EXEMPTION)
            result = subprocess.run(["bash", "scripts/pr-publish.sh", "--body-file", str(body)], cwd=repo,
                                    capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PR workflow stopped", result.stderr)
            self.assertEqual(git("ls-remote", "--heads", "origin", "codex/test"), "")
            self.assertNotIn("Wrong", result.stderr)  # Rejected by validator before identity/GitHub.


if __name__ == "__main__":
    unittest.main(verbosity=2)
