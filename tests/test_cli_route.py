"""`mr route --explain --no-models` stays off the network and off git (AC-CLI-2)."""
import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
import urllib.request
from contextlib import ExitStack
from unittest.mock import patch

from mr import cli
from mr.gitmeta import read_head
from mr.paths import data_dir
from mr.task import TaskError
from tests.gitfix import git, head, make_repo

_BLOCKED = (
    "subprocess.Popen",
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_output",
    "os.system",
    "os.posix_spawn",
    "urllib.request.urlopen",
    "socket.socket.connect",
)
_DESIGN = "design route not built; re-run with --route cascade or plan by hand"


def _boom(*_args, **_kwargs):
    raise AssertionError("subprocess or network")


class TestCliRoute(unittest.TestCase):
    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            for name in _BLOCKED:
                stack.enter_context(patch(name, side_effect=_boom))
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_route_no_models_is_pure(self) -> None:
        self.assertTrue(callable(socket.socket.connect))
        self.assertTrue(callable(urllib.request.urlopen))
        log_path = os.path.join(data_dir(), "log", "runs.jsonl")
        before = os.path.getsize(log_path) if os.path.isfile(log_path) else None
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            make_repo(repo, {"src/a.ts": "export const a = 1\n"})
            sha = head(repo)
            git(repo, "checkout", "--detach")
            self.assertEqual(read_head(repo), sha)
            git(repo, "checkout", "main")
            git(repo, "branch", "wt-branch")
            wt = os.path.join(tmp, "wt")
            git(repo, "worktree", "add", wt, "wt-branch")
            self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))
            self.assertEqual(read_head(wt), sha)
            with self.assertRaises(TaskError):
                read_head(tmp)

            code, out, err = self._main(
                [
                    "--json",
                    "route",
                    "fix the crash in src/a.ts",
                    "--repo",
                    repo,
                    "--explain",
                    "--no-models",
                ]
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(err, "")
            doc = json.loads(out)
            self.assertEqual(
                set(doc),
                {"task_id", "fingerprint", "features", "decision", "reasons", "exit", "facts", "risk_evidence",
                 "roles"},
            )
            self.assertEqual(doc["exit"], 0)
            self.assertIsInstance(doc["features"]["risk_tags"], list)
            self.assertEqual(doc["decision"]["route"], "cascade")
            self.assertEqual(doc["decision"]["type"], "bugfix")

            code, out, err = self._main(
                [
                    "--json",
                    "route",
                    "add login with oauth",
                    "--repo",
                    repo,
                    "--explain",
                    "--no-models",
                ]
            )
            self.assertEqual(code, 0, err)  # a text-only risk word is not design evidence
            self.assertNotIn(_DESIGN, err)
            doc = json.loads(out)
            self.assertEqual(doc["exit"], 0)
            self.assertEqual(doc["decision"]["route"], "cascade")

            code, out, err = self._main(
                [
                    "--json",
                    "route",
                    "add login with oauth",
                    "--repo",
                    repo,
                    "--type",
                    "code",
                    "--difficulty",
                    "critical",
                    "--route",
                    "cascade",
                    "--explain",
                    "--no-models",
                ]
            )
            self.assertEqual(code, 0, err)
            self.assertNotIn(_DESIGN, err)
            doc = json.loads(out)
            self.assertEqual(doc["exit"], 0)
            self.assertEqual(doc["decision"]["route"], "cascade")
            self.assertIn("param_overrode_trigger", doc["reasons"])

            code, text, err = self._main(
                ["route", "fix the crash in src/a.ts", "--repo", repo, "--explain", "--no-models"]
            )
            self.assertEqual(code, 0, err)
            self.assertIn("\n", text)
            self.assertIn("route: cascade", text)
            self.assertEqual(git(repo, "status", "--porcelain").stdout, "")
        after = os.path.getsize(log_path) if os.path.isfile(log_path) else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
