"""T19: mr log, mr unlock --repo, mr gc --dry-run, --prefer anthropic via cursor (AC-ISO-2, AC-STATE-2)."""

import contextlib
import io
import json
import multiprocessing
import os
import shutil
import tempfile
import unittest

from mr import exitcodes, isolation
from mr.__main__ import main
from mr.log import append
from tests import gitfix


@contextlib.contextmanager
def _allow_fake_paths():
    old = os.environ.get("MR_TEST_ALLOW_FAKE_PATHS")
    os.environ["MR_TEST_ALLOW_FAKE_PATHS"] = "1"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("MR_TEST_ALLOW_FAKE_PATHS", None)
        else:
            os.environ["MR_TEST_ALLOW_FAKE_PATHS"] = old


def _run(argv):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), _allow_fake_paths():
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _write_cfg(tmp, home):
    path = os.path.join(tmp, "routes.yaml")
    raw = {
        "paths": {
            "home": home,
            "cursor_agent": gitfix.FAKE_CURSOR,
            "claude": gitfix.FAKE_CLAUDE,
            "check": gitfix.FAKE_CHECK,
        }
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)
    return path


def _hold(cfg, repo, ready, stop):
    lock = isolation.WriterLock(cfg, repo, "t-busy")
    if lock.acquire():
        ready.set()
        stop.wait(20)
        lock.release()


class TestMrLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mr-t19-log-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.cfg = _write_cfg(self.tmp, self.home)

    def test_missing_file(self):
        code, out, err = _run(["--config", self.cfg, "log"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_filter_tail_json_and_malformed(self):
        append({"task": "t-a", "role": "guard", "rung": "S", "model": "claude", "decision": "allow"}, home=self.home)
        append({"task": "t-b", "role": "route", "rung": "G-x", "decision": "cascade"}, home=self.home)
        append({"task": "t-a", "role": "task", "outcome": "accepted", "exit": 0}, home=self.home)
        log_path = os.path.join(self.home, "log", "runs.jsonl")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("{bad json\n")

        code, out, err = _run(["--config", self.cfg, "log", "--task", "t-a"])
        self.assertEqual(code, 0)
        self.assertIn("skipping malformed", err)
        lines = [ln for ln in out.strip().splitlines() if ln]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(" t-a " in ln for ln in lines))

        code, out, _ = _run(["--config", self.cfg, "log", "--role", "guard"])
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("guard", out)

        code, out, _ = _run(["--config", self.cfg, "log", "--task", "t-a", "--tail", "1"])
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("task", out)

        code, out, _ = _run(["--config", self.cfg, "--json", "log", "--task", "t-a"])
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["role"] for r in rows}, {"guard", "task"})


class TestMrUnlock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mr-t19-unlock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.repo = gitfix.make_repo(os.path.join(self.tmp, "repo"))
        self.cfg = _write_cfg(self.tmp, self.home)
        from types import SimpleNamespace

        self.ns_cfg = SimpleNamespace(paths=SimpleNamespace(home=self.home))

    def test_no_lock_and_empty(self):
        code, out, _ = _run(["--config", self.cfg, "unlock", "--repo", self.repo])
        self.assertEqual(code, 0)
        self.assertIn("no stale lock", out)

        path = isolation.lock_path(self.ns_cfg, self.repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        code, out, _ = _run(["--config", self.cfg, "unlock", "--repo", self.repo])
        self.assertEqual(code, 0)
        self.assertIn("no stale lock", out)
        self.assertTrue(os.path.isfile(path))

    def test_crashed_lock_truncates(self):
        task = "t-crashed"
        patch = os.path.join(self.home, "patches", task, "r1.patch")
        os.makedirs(os.path.dirname(patch), exist_ok=True)
        with open(patch, "w", encoding="utf-8") as fh:
            fh.write("diff\n")
        state_dir = os.path.join(self.home, "tasks", task)
        os.makedirs(state_dir, exist_ok=True)
        state_path = os.path.join(state_dir, "state.json")
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump({"patch": patch}, fh)

        path = isolation.lock_path(self.ns_cfg, self.repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pid": 99999, "task": task}, fh)

        code, out, err = _run(["--config", self.cfg, "unlock", "--repo", self.repo])
        self.assertEqual(code, 0, err)
        self.assertIn(task, out)
        self.assertIn(state_path, out)
        self.assertIn(patch, out)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.getsize(path), 0)

        lock = isolation.WriterLock(self.ns_cfg, self.repo, "t-next")
        self.assertTrue(lock.acquire())
        lock.release()

    def test_busy_lock_unchanged(self):
        ctx = multiprocessing.get_context("fork")
        ready, stop = ctx.Event(), ctx.Event()
        proc = ctx.Process(
            target=_hold, args=(self.ns_cfg, self.repo, ready, stop), daemon=True
        )
        proc.start()
        self.addCleanup(lambda: (stop.set(), proc.join(10)))
        self.assertTrue(ready.wait(10))
        path = isolation.lock_path(self.ns_cfg, self.repo)

        code, out, err = _run(["--config", self.cfg, "unlock", "--repo", self.repo])
        self.assertEqual(code, exitcodes.UNSAFE_GIT)
        self.assertIn("lock busy", err)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["task"], "t-busy")


class TestMrGcDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mr-t19-gc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.repo = gitfix.make_repo(os.path.join(self.tmp, "repo"))
        self.cfg = _write_cfg(self.tmp, self.home)
        g = lambda *a: gitfix.git(self.repo, *a)

        g("branch", "mr/a")
        g("checkout", "mr/a")
        gitfix.commit(self.repo, {"a.txt": "a\n"}, "mr/a work")
        g("checkout", "main")
        g("merge", "mr/a", "--no-edit")

        g("branch", "mr/b")
        g("checkout", "mr/b")
        gitfix.commit(self.repo, {"b.txt": "b\n"}, "mr/b work")
        g("checkout", "main")

        g("branch", "alice/x")
        g("checkout", "alice/x")
        gitfix.commit(self.repo, {"r.txt": "r\n"}, "alice work")
        g("checkout", "main")
        g("merge", "alice/x", "--no-edit")

        gitfix.add_decoys(self.repo, self.home)

    def test_scope_merged_branches_only(self):
        before = set(gitfix.git(self.repo, "branch", "--list").stdout.split())
        code, out, err = _run(
            [
                "--config",
                self.cfg,
                "gc",
                "--dry-run",
                "--repo",
                self.repo,
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("branch mr/a", out)
        self.assertNotIn("mr/b", out)
        self.assertNotIn("alice", out)
        self.assertNotIn(".cursor", out)
        self.assertNotIn(".claude", out)
        after = set(gitfix.git(self.repo, "branch", "--list").stdout.split())
        self.assertEqual(before, after)

    def test_no_dry_run_exits_human(self):
        code, _, err = _run(["--config", self.cfg, "gc"])
        self.assertEqual(code, exitcodes.HUMAN)
        self.assertIn("only --dry-run", err)


class TestPreferAnthropic(unittest.TestCase):
    def test_cursor_sonnet_rejected(self):
        code, _, err = _run(["route", "--prefer", "cursor:sonnet", "--type", "code"])
        self.assertEqual(code, exitcodes.USAGE)
        self.assertIn("--prefer", err)


if __name__ == "__main__":
    unittest.main()
