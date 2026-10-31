"""Tests for mr.task (AC-TASK-1, §5.1, §7.1)."""

import hashlib
import json
import os
import re
import tempfile
import unittest

from mr.task import Params, Task, TaskError, fingerprint, load_task, new_task_id, normalise
from tests.gitfix import git, head, make_repo


def _task(text: str, repo: str, base: str) -> Task:
    return Task(
        id="t-00000000",
        text=text,
        repo=repo,
        base=base,
        params=Params(),
        files_named=(),
        source="cli",
    )


class TestTask(unittest.TestCase):
    def test_fingerprint(self) -> None:
        repo = "/work/example-app"
        base = "a" * 40
        stable = fingerprint(_task("Fix the Bug.", repo, base))
        self.assertEqual(stable, fingerprint(_task("  FIX   the   bug!!!  ", repo, base)))
        self.assertEqual(stable, fingerprint(_task("fix the bug", repo, base)))
        self.assertEqual(stable, fingerprint(_task("fix the bug,", repo, base)))
        self.assertEqual(stable, fingerprint(_task("fix the bug?", repo, base)))
        self.assertEqual(normalise("  Hello,   WORLD!!! "), "hello, world")
        self.assertNotEqual(stable, fingerprint(_task("fix the bug", repo, "b" * 40)))
        self.assertNotEqual(
            stable, fingerprint(_task("fix the bug", "/work/other-app", base))
        )
        self.assertEqual(len(stable), 40)
        self.assertRegex(stable, r"^[0-9a-f]{40}$")

    def test_new_task_id(self) -> None:
        text = "Fix the crash"
        ts = 1_700_000_000.25
        with tempfile.TemporaryDirectory() as home:
            first = new_task_id(text, ts, home)
            expect = "t-" + hashlib.sha1((text + repr(ts)).encode()).hexdigest()[:8]
            self.assertEqual(first, expect)
            self.assertRegex(first, r"^t-[0-9a-f]{8}$")
            os.makedirs(f"{home}/patches/{first}")
            second = new_task_id(text, ts, home)
            nudged = ts + 1e-6
            expect2 = "t-" + hashlib.sha1((text + repr(nudged)).encode()).hexdigest()[:8]
            self.assertEqual(second, expect2)
            self.assertRegex(second, r"^t-[0-9a-f]{8}$")
            self.assertNotEqual(first, second)
            self.assertFalse(re.fullmatch(r"t-[0-9a-f]{8}", "t-ABC"))

    def test_text_missing_and_too_long(self) -> None:
        params = Params()
        for text in (None, "", "   ", "\n\t"):
            with self.subTest(text=text):
                with self.assertRaises(TaskError) as caught:
                    load_task(text, None, params, "/tmp/does-not-matter")
                self.assertEqual(str(caught.exception), "task text missing")
        with self.assertRaises(TaskError) as caught:
            load_task("x" * 8001, None, params, "/tmp/does-not-matter")
        self.assertIn("8000", str(caught.exception))

    def test_load_task_json(self) -> None:
        params = Params(difficulty="easy", budget="high")
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            other = os.path.join(tmp, "other")
            home = os.path.join(tmp, "home")
            make_repo(repo)
            make_repo(other)
            payload = {
                "text": "Implement login in src/lib/auth.ts",
                "repo": repo,
                "params": {
                    "type": "bugfix",
                    "difficulty": "hard",
                    "budget": "low",
                    "latency": "fast",
                    "experiment": True,
                    "judge": "off",
                },
                "files_named": ["src/lib/auth.ts", "src/lib/session.ts"],
            }
            path = os.path.join(tmp, "task.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)

            task = load_task(None, path, params, None, home=home)
            top = git(repo, "rev-parse", "--show-toplevel").stdout.strip()
            self.assertEqual(task.text, payload["text"])
            self.assertEqual(task.source, "file")
            self.assertEqual(task.repo, top)
            self.assertEqual(task.base, head(repo))
            self.assertEqual(task.files_named, ("src/lib/auth.ts", "src/lib/session.ts"))
            self.assertEqual(task.params.type, "bugfix")
            self.assertEqual(task.params.difficulty, "easy")
            self.assertEqual(task.params.budget, "high")
            self.assertEqual(task.params.latency, "fast")
            self.assertTrue(task.params.experiment)
            self.assertEqual(task.params.judge, "off")
            self.assertIsNone(task.params.route)
            self.assertFalse(task.params.yes)
            self.assertEqual(task.params.wait_lock_s, 600)
            self.assertRegex(task.id, r"^t-[0-9a-f]{8}$")

            from_cli = load_task("Fix the typo in README.md", path, Params(), repo, home=home)
            self.assertEqual(from_cli.text, "Fix the typo in README.md")
            self.assertEqual(from_cli.source, "cli")
            self.assertEqual(from_cli.files_named, task.files_named)
            self.assertEqual(from_cli.repo, top)

            overridden = load_task(None, path, Params(), other, home=home)
            other_top = git(other, "rev-parse", "--show-toplevel").stdout.strip()
            self.assertEqual(overridden.repo, other_top)
            self.assertEqual(overridden.base, head(other))

            bare = {"text": "Fix src/a.ts and src/a.ts and README.md", "repo": repo}
            bare_path = os.path.join(tmp, "bare.json")
            with open(bare_path, "w", encoding="utf-8") as handle:
                json.dump(bare, handle)
            named = load_task(None, bare_path, Params(), None, home=home)
            self.assertEqual(named.files_named, ("src/a.ts", "README.md"))
            self.assertEqual(named.source, "file")

            null_files = {
                "text": "Fix README.md",
                "repo": repo,
                "files_named": None,
            }
            null_path = os.path.join(tmp, "null.json")
            with open(null_path, "w", encoding="utf-8") as handle:
                json.dump(null_files, handle)
            from_text = load_task(None, null_path, Params(), None, home=home)
            self.assertEqual(from_text.files_named, ("README.md",))

            exact = load_task("y" * 8000, None, Params(), repo, home=home)
            self.assertEqual(len(exact.text), 8000)
            self.assertEqual(exact.source, "cli")
            self.assertEqual(exact.files_named, ())

            with self.assertRaises(TaskError) as caught:
                load_task("", path, Params(), repo, home=home)
            self.assertEqual(str(caught.exception), "task text missing")
