"""Tests for mr.prepass (AC-PRE-1, AC-PRE-2, §5.2)."""

import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mr.prepass import extract, extract_from
from mr.task import Params, Task
from tests.gitfix import make_repo

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "p0", "prepass_table.json")


def _allowed(argv: list[str]) -> bool:
    if not argv or argv[0] != "git":
        return False
    if len(argv) >= 4 and argv[1] == "-C":
        cmd = argv[3]
        rest = argv[4:]
    elif len(argv) >= 2:
        cmd = argv[1]
        rest = argv[2:]
    else:
        return False
    if cmd in ("ls-files", "rev-parse"):
        return True
    return cmd == "grep" and "-l" in rest


class TestPrepass(unittest.TestCase):
    def test_fixture_table(self) -> None:
        with open(_FIXTURE, encoding="utf-8") as handle:
            rows = json.load(handle)
        self.assertEqual(len(rows), 40)
        for row in rows:
            with self.subTest(id=row["id"]):
                feat = extract_from(row["text"], row["files_named"])
                expect = row["expect"]
                self.assertEqual(feat.type, expect["type"])
                self.assertEqual(sorted(feat.risk_tags), expect["risk_tags"])
                self.assertEqual(feat.risk_floor, expect["risk_floor"])
                self.assertEqual(feat.difficulty_floor, expect["difficulty_floor"])
                self.assertEqual(feat.confident, expect["confident"])
                self.assertEqual(feat.grep_hit_files, 0)
                self.assertEqual(feat.n_files, len(feat.files_named))

    def test_no_content_reads(self) -> None:
        calls: list[list[str]] = []
        runs: list[list[str]] = []

        def _fake_stdout(argv: list[str]) -> str:
            return "README.md\n" if "ls-files" in argv else ""

        def wrap_run(args, *pos, **kwargs):
            argv = list(args)
            self.assertTrue(_allowed(argv), argv)
            runs.append(argv)
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=_fake_stdout(argv), stderr="")

        def wrap_popen(args, *pos, **kwargs):
            argv = list(args)
            self.assertTrue(_allowed(argv), argv)
            calls.append(argv)
            raise AssertionError("extract must call subprocess.run, not Popen")

        def boom(*_args, **_kwargs):
            raise AssertionError("content or network read")

        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            make_repo(repo)
            task = Task(
                id="t-0123abcd",
                text="Fix the crash in README.md",
                repo=repo,
                base="abc",
                params=Params(),
                files_named=("README.md", "missing.ts"),
                source="cli",
            )
            with (
                patch("builtins.open", side_effect=boom),
                patch("io.open", side_effect=boom),
                patch("os.open", side_effect=boom),
                patch("pathlib.Path.read_text", side_effect=boom),
                patch("pathlib.Path.read_bytes", side_effect=boom),
                patch("urllib.request.urlopen", side_effect=boom),
                patch("socket.socket.connect", side_effect=boom),
                patch("subprocess.run", wrap_run),
                patch("subprocess.Popen", wrap_popen),
            ):
                feat = extract(task, repo)
            self.assertEqual(feat.grep_hit_files, 1)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0][0], "git")
            self.assertEqual(runs[0][1], "-C")
            self.assertEqual(runs[0][3], "ls-files")
            self.assertIn("--", runs[0])
            self.assertTrue(calls)

            empty = Task(
                id="t-0123abcd",
                text="Fix the crash",
                repo=repo,
                base="abc",
                params=Params(),
                files_named=(),
                source="cli",
            )
            none_files = Task(
                id="t-0123abcd",
                text="Fix the crash in README.md",
                repo=repo,
                base="abc",
                params=Params(),
                files_named=("README.md",),
                source="cli",
            )

            def refuse(*_args, **_kwargs):
                raise AssertionError("unexpected subprocess")

            with patch("subprocess.run", side_effect=refuse), patch(
                "subprocess.Popen", side_effect=refuse
            ):
                self.assertEqual(extract(empty, repo).grep_hit_files, 0)
                self.assertEqual(extract(none_files, None).grep_hit_files, 0)

    def test_soft_fields(self) -> None:
        layered = extract_from(
            "Create a new module and polish the animation. Export the public API.",
            (
                "supabase/migrations/a.sql",
                "src/navigation/Root.tsx",
                "ios/App/Info.plist",
                "src/store/user.ts",
            ),
        )
        self.assertEqual(layered.cross_layer, 4)
        self.assertTrue(layered.new_module)
        self.assertTrue(layered.judgment_keywords)
        self.assertTrue(layered.public_interface)
        self.assertEqual(layered.n_files, 4)

        both = extract_from("Tidy this", ("app/store/x.ts",))
        self.assertEqual(both.cross_layer, 2)

        screen = extract_from("Add a new screen for settings", None)
        self.assertTrue(screen.new_module)
        package = extract_from("create the package for billing helpers", None)
        self.assertTrue(package.new_module)
        self.assertFalse(extract_from("create a screen for settings", None).new_module)

        self.assertTrue(extract_from("Match the look and feel", None).judgment_keywords)
        self.assertTrue(extract_from("Tighten the UX flow", None).judgment_keywords)
        self.assertFalse(extract_from("Fix the crash in auth", None).judgment_keywords)

        two = extract_from("Fix the typo", ("a.ts", "b.ts"))
        self.assertEqual(two.difficulty_floor, "trivial")
        self.assertEqual(two.difficulty_est, "easy")
        self.assertEqual(two.cross_layer, 0)

        wide = extract_from("Fix the badge", ("a.ts", "b.ts", "c.ts"))
        self.assertEqual(wide.difficulty_floor, "trivial")
        self.assertEqual(wide.difficulty_est, "medium")

        auth = extract_from("Fix login", ("src/auth.ts",))
        self.assertEqual(auth.risk_floor, "high")
        self.assertEqual(auth.difficulty_floor, "hard")
        self.assertEqual(auth.difficulty_est, "hard")

    def test_grep_hit_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            make_repo(repo, {"src/a.ts": "export {}\n"})
            task = Task(
                id="t-abcdef01",
                text="Fix src/a.ts",
                repo=repo,
                base="abc",
                params=Params(),
                files_named=("src/a.ts", "nope.ts", "README.md"),
                source="cli",
            )
            self.assertEqual(extract(task, repo).grep_hit_files, 2)
            self.assertEqual(extract(task, None).grep_hit_files, 0)
