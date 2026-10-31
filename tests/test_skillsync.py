"""mr/skillsync.py: the skills sync hand-off is off in tests, detached, and never raises."""
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from mr import skillsync


class SkillSyncCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mr-skillsync-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.out = os.path.join(self.dir, "out")
        self.hook = os.path.join(self.dir, "hook.sh")
        with open(self.hook, "w") as fh:
            fh.write('#!/bin/sh\n{ echo "$1"; env; } > "%s.tmp"; mv "%s.tmp" "%s"\n'
                     % (self.out, self.out, self.out))
        os.chmod(self.hook, 0o755)

    def env(self, **kw):
        base = {"MR_SKILLSYNC": "1", "MR_SKILLSYNC_HOOK": self.hook}
        base.update(kw)
        return patch.dict(os.environ, base)

    def wait_out(self):
        end = time.monotonic() + 10
        while time.monotonic() < end and not os.path.exists(self.out):
            time.sleep(0.02)
        with open(self.out) as fh:
            return fh.read().splitlines()

    def test_off_in_test_suite(self):
        self.assertEqual(os.environ.get("MR_SKILLSYNC"), "0")  # tests/__init__.py
        with patch.dict(os.environ, {"MR_SKILLSYNC_HOOK": self.hook}), \
                patch("subprocess.Popen") as p:
            self.assertFalse(skillsync.notify("run-end"))
        p.assert_not_called()

    def test_missing_hook_is_a_no_op(self):
        with self.env(MR_SKILLSYNC_HOOK=os.path.join(self.dir, "nope")), patch("subprocess.Popen") as p:
            self.assertFalse(skillsync.notify("run-end"))
        p.assert_not_called()

    def test_runs_detached_with_env_passed_through(self):
        with self.env(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="url.x.pushInsteadOf",
                      GIT_CONFIG_VALUE_0="https://", GIT_DIR="/elsewhere/.git",
                      GIT_WORK_TREE="/elsewhere", GIT_INDEX_FILE="/elsewhere/idx"), \
                patch("subprocess.Popen", wraps=skillsync.subprocess.Popen) as p:
            self.assertTrue(skillsync.notify("run-end"))
        kw = p.call_args.kwargs
        self.assertTrue(kw["start_new_session"])
        for k in ("stdin", "stdout", "stderr"):
            self.assertEqual(kw[k], skillsync.subprocess.DEVNULL)
        skillsync._started[-1].wait(10)
        lines = self.wait_out()
        self.assertEqual(lines[0], "mr-run-end")
        env = dict(ln.split("=", 1) for ln in lines[1:] if "=" in ln)
        self.assertEqual(env.get("GIT_CONFIG_KEY_0"), "url.x.pushInsteadOf")  # nested mr keeps its guard
        for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.assertNotIn(k, env)

    def test_never_raises(self):
        for exc in (OSError("boom"), PermissionError("no"), ValueError("bad")):
            with self.env(), patch("subprocess.Popen", side_effect=exc):
                self.assertFalse(skillsync.notify("run-end"))

    def test_run_calls_notify_once_after_finalize(self):
        import inspect
        from mr.commands import run as run_cmd
        src = inspect.getsource(run_cmd.run)
        self.assertEqual(src.count("skillsync.notify("), 1)
        self.assertLess(src.index("_finalize(c)"), src.index('skillsync.notify("run-end")'))


if __name__ == "__main__":
    unittest.main()
