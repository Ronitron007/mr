"""`mr run --rung X --once` (L0 single-rung pipeline) against the fakes (mr-spec §14.3 S1, S7, S8, S9)."""
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from mr import cli, config, state
from mr.commands import run as run_cmd  # noqa: F401  (import check)
from mr.isolation import WriterLock
from tests import gitfix, log_schema_v2
from tests.gitfix import git

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GL = "grok-4.7-low"
TASK = "fix typo in README.md"
CHECK_DEFAULT = {
    "hour_usage": 10, "limit": 40, "unit": "points", "exceeded": False, "five_hour_pct": 30.0,
    "five_hour_resets_at": "2026-10-30T05:00:00Z", "seven_day_pct": 12.0, "sample_age_s": 30,
    "samples_in_hour": 3, "has_baseline": True, "checked_at": "2026-10-30T00:00:00Z",
}


def _wait(pred, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class RunCase(unittest.TestCase):
    branch = "feature/x"
    repo_files = None
    hygiene_cmds = None

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="mr-run-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = os.path.join(self.dir, "repo")
        self.home = os.path.join(self.dir, "home")
        gitfix.make_repo(self.repo, self.repo_files, branch=self.branch)
        self.cfgpath = os.path.join(self.dir, "routes.yaml")
        self.set_scenario({})
        self.write_cfg()

    # -- fixtures -------------------------------------------------------------
    def write_cfg(self):
        raw = {"paths": {"home": self.home, "cursor_agent": gitfix.FAKE_CURSOR,
                         "claude": gitfix.FAKE_CLAUDE, "check": gitfix.FAKE_CHECK}}
        if self.hygiene_cmds:
            raw["hygiene"] = {"commands": {"repo": self.hygiene_cmds}}
        with open(self.cfgpath, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)  # JSON is YAML

    def set_scenario(self, scenario, check=None):
        self.scenario = gitfix.write_scenario(self.dir, scenario)
        self.check = gitfix.write_check(self.dir, check or {"default": dict(CHECK_DEFAULT)})
        self.env = gitfix.fake_env(self.scenario, self.check)

    def cursor(self, *steps, model=GL):
        self.set_scenario({"cursor": {model: list(steps)}})

    def calls(self):
        path = self.scenario + ".calls.jsonl"
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]

    def argv(self, *extra, text=TASK, rung="G-l", json_out=False):
        return (["--config", self.cfgpath] + (["--json"] if json_out else []) +
                ["run", text, "--repo", self.repo, "--rung", rung, "--once", *extra])

    def mr(self, *extra, **kw):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(self.argv(*extra, **kw))
        return code, out.getvalue(), err.getvalue()

    def cfg(self):
        with patch.dict(os.environ, self.env):
            return config.load(self.cfgpath)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def log(self):
        path = os.path.join(self.home, "log", "runs.jsonl")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]

    def records(self, role):
        return [r for r in self.log() if r["role"] == role]

    def state_of(self, task_id):
        return state.read_state(self.home, task_id)

    def only_task(self):
        tasks = self.records("task")
        self.assertEqual(len(tasks), 1, tasks)
        return tasks[0]

    def lock_path(self):
        return WriterLock(self.cfg(), self.repo, "x").path

    def assert_schema(self):
        for rec in self.log():
            self.assertEqual(log_schema_v2.check(rec), [], rec)


class TestPipeline(RunCase):
    def test_s1_trivial_docs_pass(self):
        self.cursor({"files": {"README.md": "# test repo\nfixed typo\n"}})
        code, out, err = self.mr()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.calls()), 1)
        tid = self.only_task()["task"]
        st = self.state_of(tid)
        self.assertEqual(st["isolation"], "W7")
        self.assertEqual(st["next"], "done")
        self.assertEqual(st["outcome"], "accepted")
        self.assertEqual(len(st["rungs"]), 1)
        self.assertEqual(st["rungs"][0]["rung"], "G-l")
        self.assertIsNone(st["rungs"][0]["class"])
        self.assertEqual(st["ladder"], ["G-l"])
        self.assertTrue(os.path.getsize(st["rungs"][0]["patch"]) > 0)
        self.assertTrue(os.path.isfile(os.path.join(self.home, "patches", tid, f"r1-{GL}.patch")))
        # W7: changes stay uncommitted on the user's branch.
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout.strip(), "M README.md")
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "feature/x")
        self.assertEqual(len(self.records("route")), 1)
        impl = self.records("implementer")
        self.assertEqual(len(impl), 1)
        self.assertEqual(impl[0]["signals"], {"H": True, "D": True, "A": None, "C": None})
        self.assertIsNone(impl[0]["failure_class"])
        self.assertEqual(impl[0]["diff"], {"files": 1, "add": 1, "del": 0})
        self.assertEqual(impl[0]["mr_result"], "done")
        task = self.only_task()
        self.assertEqual((task["outcome"], task["exit"], task["verified"]), ("accepted", 0, False))
        self.assertEqual(task["ladder"], ["G-l"])
        self.assertEqual(task["attempts"]["impl"], 1)
        self.assert_schema()
        self.assertEqual(os.path.getsize(self.lock_path()), 0)
        self.assertIn(tid, out)

    def test_w7_leftover_blocks_next_run(self):
        self.cursor({"files": {"README.md": "# test repo\nfixed typo\n"}})
        code, _, err = self.mr()
        self.assertEqual(code, 0, err)
        first = self.only_task()["task"]
        code, _, err = self.mr()
        self.assertEqual(code, 4)
        self.assertIn(f"commit or discard {first}'s changes first", err)
        self.assertEqual(len(self.calls()), 1)

    def test_s7_branch_switch(self):
        self.cursor({"files": {"README.md": "# test repo\nx\n"}, "violation": "switch"})
        code, out, err = self.mr(json_out=True)
        self.assertEqual(code, 4, err)
        doc = json.loads(out)
        self.assertEqual(doc["outcome"], "unsafe")
        self.assertIn("branch", doc["reason"])
        self.assertIn("branch", err)
        self.assertTrue(os.path.isfile(doc["patch"]))
        self.assertTrue(os.path.isfile(doc["final_md"]))
        self.assertEqual(doc["rungs"][0]["class"], "isolation_violation")
        task = self.only_task()
        self.assertEqual((task["outcome"], task["exit"]), ("unsafe", 4))
        self.assertEqual(self.records("implementer")[0]["failure_class"], "isolation_violation")
        self.assert_schema()
        self.assertEqual(os.path.getsize(self.lock_path()), 0)

    def test_s9_foreign_dirty(self):
        gitfix.make_foreign_dirty(self.repo)
        self.cursor({"files": {"README.md": "# test repo\nx\n"}})
        code, _, err = self.mr()
        self.assertEqual(code, 4)
        self.assertIn("W2", err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.only_task()["outcome"], "unsafe")
        self.assertEqual(os.path.getsize(self.lock_path()), 0)

    def test_d_fail_exit1(self):
        self.cursor({"files": {"other/zzz.txt": "elsewhere\n"}})
        code, out, err = self.mr(json_out=True)
        self.assertEqual(code, 1, err)
        doc = json.loads(out)
        self.assertEqual(doc["outcome"], "exhausted")
        self.assertEqual(doc["rungs"][0]["class"], "off_track")
        self.assertEqual(doc["rungs"][0]["signals"]["D"], False)
        self.assertIsNone(doc["rungs"][0]["signals"]["H"])
        self.assertTrue(os.path.isfile(doc["final_md"]))
        self.assertIn("out of scope", self.read(doc["final_md"]))
        # --once never resets: the stray file is still there.
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "other", "zzz.txt")))
        self.assert_schema()

    def test_empty_diff_is_skipped(self):
        self.cursor({})
        code, out, err = self.mr(json_out=True)
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)["rungs"][0]["class"], "skipped")

    def test_infra_exit1(self):
        self.cursor({"files": {"README.md": "# test repo\nx\n"}, "rc": 3, "no_envelope": True})
        code, out, err = self.mr(json_out=True)
        self.assertEqual(code, 1, err)
        doc = json.loads(out)
        self.assertEqual(doc["rungs"][0]["class"], "infra")
        self.assertIn("infra", self.read(doc["final_md"]))

    def test_json_doc(self):
        self.cursor({"files": {"README.md": "# test repo\nfixed\n"}})
        code, out, err = self.mr(json_out=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.strip().splitlines()), 1)
        doc = json.loads(out)
        self.assertTrue({"task", "exit", "outcome", "route", "ladder", "rungs", "patch",
                         "final_md", "state"} <= set(doc), doc)
        self.assertEqual((doc["exit"], doc["outcome"], doc["route"], doc["ladder"]),
                         (0, "accepted", "cascade", ["G-l"]))
        self.assertEqual(set(doc["rungs"][0]), {"rung", "class", "signals"})
        self.assertTrue(os.path.isfile(doc["patch"]))
        self.assertTrue(os.path.isfile(doc["state"]))
        self.assertIsNone(doc["final_md"])
        self.assertEqual(self.state_of(doc["task"])["task"], doc["task"])

    def test_usage_errors(self):
        for extra, argv in (("no --rung", ["run", TASK, "--repo", self.repo, "--once"]),
                            ("bad rung", ["run", TASK, "--repo", self.repo, "--rung", "Z9", "--once"]),
                            ("dry-run", ["run", TASK, "--repo", self.repo, "--rung", "G-l", "--once",
                                         "--dry-run"])):
            with self.subTest(extra), patch.dict(os.environ, self.env), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["--config", self.cfgpath, *argv]), 2)
        self.assertEqual(self.calls(), [])


class TestW5(RunCase):
    branch = "main"

    def test_s8_on_main_w5(self):
        self.cursor({"files": {"README.md": "# test repo\nfixed typo\n"}})
        code, out, err = self.mr(json_out=True)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        now = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertTrue(now.startswith("mr/"), now)
        self.assertEqual(doc["branch"], now)
        msg = git(self.repo, "log", "-1", "--format=%B").stdout
        self.assertIn(f"Mr-Task: {doc['task']}", msg)
        self.assertTrue(msg.startswith(TASK))
        self.assertTrue(doc["task"].startswith("t-"))
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout.strip(), "")
        self.assertIn("original branch: main", err)
        self.assertEqual(self.state_of(doc["task"])["isolation"], "W5")
        self.assertEqual(self.only_task()["commit"], git(self.repo, "rev-parse", "HEAD").stdout.strip()[:12])
        # main itself is untouched.
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", "main").stdout.strip(), "initial")
        self.assert_schema()


class TestHygiene(RunCase):
    repo_files = {"check.py": (
        "import sys\n"
        "if 'BROKEN' in open('README.md').read():\n"
        "    print('README.md:1:1: broken marker')\n"
        "    sys.exit(1)\n")}

    def setUp(self):
        self.hygiene_cmds = [f"{sys.executable} check.py"]
        super().setUp()

    def test_h_fail_exit1(self):
        self.cursor({"files": {"README.md": "# test repo\nBROKEN\n"}})
        code, out, err = self.mr(json_out=True)
        self.assertEqual(code, 1, err)
        doc = json.loads(out)
        self.assertEqual(doc["rungs"][0]["class"], "skipped")
        self.assertEqual(doc["rungs"][0]["signals"]["D"], True)
        self.assertEqual(doc["rungs"][0]["signals"]["H"], False)
        self.assertTrue(os.path.isfile(doc["final_md"]))
        self.assertEqual(self.records("implementer")[0]["signals"]["H"], False)

    def test_h_pass_when_base_relative(self):
        self.cursor({"files": {"README.md": "# test repo\nfine\n"}})
        code, _, err = self.mr()
        self.assertEqual(code, 0, err)
        cache = [n for n in os.listdir(os.path.join(self.home, "state")) if n.startswith("hygiene-repo-")]
        self.assertEqual(len(cache), 1)  # base set computed once at reset_tree and cached


class TestLockAndGuard(RunCase):
    def test_stale_lock_exit4(self):
        lock = self.lock_path()
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w", encoding="utf-8") as fh:
            json.dump({"pid": 999999, "task": "t-deadbeef"}, fh)
        before = self.read(lock)
        self.cursor({"files": {"README.md": "# test repo\nx\n"}})
        code, out, err = self.mr(json_out=True)
        self.assertEqual(code, 4)
        self.assertIn("t-deadbeef", err)
        self.assertIn(os.path.join(self.home, "patches", "t-deadbeef"), err)
        self.assertIn(os.path.join(self.home, "tasks", "t-deadbeef", "state.json"), err)
        self.assertIn("t-deadbeef", json.loads(out)["reason"])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.read(lock), before)  # not ours to truncate

    def test_busy_lock_is_w1_exit4(self):
        other = WriterLock(self.cfg(), self.repo, "t-other")
        self.assertTrue(other.acquire())
        self.addCleanup(other.release)
        code, _, err = self.mr()
        self.assertEqual(code, 4)
        self.assertIn("W1", err)

    def test_guard_deny_75(self):
        self.set_scenario({"claude": {"claude-sonnet-5-5": [{}]}},
                          check={"default": dict(CHECK_DEFAULT, hour_usage=39)})
        code, out, err = self.mr(json_out=True, rung="S")
        self.assertEqual(code, 75, err)
        self.assertEqual(self.calls(), [])
        doc = json.loads(out)
        self.assertEqual(doc["outcome"], "deferred")
        self.assertTrue(doc["frees_hint"])
        self.assertEqual(doc["rungs"], [])
        task = self.only_task()
        self.assertEqual((task["outcome"], task["exit"]), ("deferred", 75))
        self.assertTrue(any(r["allow"] is False for r in self.records("guard")))
        self.assert_schema()
        self.assertEqual(os.path.getsize(self.lock_path()), 0)

    def test_claude_rung_runs(self):
        self.set_scenario({"claude": {"claude-sonnet-5-5": [{"files": {"README.md": "# test repo\nc\n"}}]}})
        code, out, err = self.mr(json_out=True, rung="S")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.state_of(json.loads(out)["task"])["claude_rungs"], 1)
        self.assert_schema()

    def test_design_route_exit3(self):
        text = "rewrite the payments checkout flow with new auth"
        self.cursor({"files": {"README.md": "# test repo\nx\n"}})
        code, _, err = self.mr(text=text)
        self.assertEqual(code, 3, err)
        self.assertIn("design route not built; re-run with --route cascade or plan by hand", err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(self.records("route")), 1)
        self.assertEqual(self.only_task()["outcome"], "needs_human")
        self.assertEqual(os.path.getsize(self.lock_path()) if os.path.exists(self.lock_path()) else 0, 0)
        code, _, err = self.mr("--route", "cascade", text=text)
        # --route cascade gets past the route; this high-risk text then needs W4 (not built).
        self.assertEqual(code, 4, err)
        self.assertIn("W4", err)


class TestSubprocess(RunCase):
    """Real mr processes: signals and SIGKILL."""

    def spawn(self, text, scale="1", **extra):
        env = dict(self.env, MR_TEST_TIME_SCALE=scale)
        return subprocess.Popen(
            [sys.executable, "-m", "mr", *self.argv(text=text, **extra)], cwd=SRC, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def cmdlines_with(self, marker):
        hits = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == os.getpid():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    if marker.encode() in fh.read():
                        hits.append(pid)
            except OSError:
                pass
        return hits

    def test_sigint_130(self):
        marker = "ZZSIGINTMARK7731"
        self.cursor({"files": {"README.md": "# test repo\nx\n"}, "sleep_s": 120})
        proc = self.spawn(f"{TASK} {marker}", json_out=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(_wait(lambda: bool(self.calls())), "fake cursor never started")
        time.sleep(0.5)
        t0 = time.monotonic()
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 130, err)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(self.cmdlines_with(marker), [], "fake process group still alive")
        doc = json.loads(out)
        self.assertEqual((doc["exit"], doc["outcome"]), (130, "interrupted"))
        task = self.only_task()
        self.assertEqual((task["outcome"], task["exit"]), ("interrupted", 130))
        self.assertEqual(self.state_of(task["task"])["next"], "done")
        self.assertTrue(os.path.isfile(doc["patch"]))  # saved from the handler path
        self.assertEqual(os.path.getsize(self.lock_path()), 0)
        self.assert_schema()

    def test_sigterm_143(self):
        self.cursor({"sleep_s": 120})
        proc = self.spawn(TASK, json_out=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(_wait(lambda: bool(self.calls())))
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 143, err)
        self.assertEqual(self.only_task()["outcome"], "interrupted")
        self.assertEqual(os.path.getsize(self.lock_path()), 0)

    def slow_hygiene(self):
        """Hygiene that sleeps only after the rung (README contains SLOW). Returns the pid file."""
        marker = os.path.join(self.dir, "hyg.pid")
        gitfix.commit(self.repo, {"check.py": (
            "import os, time\n"
            "if 'SLOW' in open('README.md').read():\n"
            f"    open({marker!r}, 'w').write(str(os.getpid()))\n"
            "    time.sleep(120)\n")}, "add check")
        self.hygiene_cmds = [f"{sys.executable} check.py"]
        self.write_cfg()
        self.cursor({"files": {"README.md": "# test repo\nSLOW\n"}})

        def reap():
            try:
                os.kill(int(self.read(marker)), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.addCleanup(reap)
        return marker

    def hyg_alive(self, marker):
        try:
            os.kill(int(self.read(marker)), 0)
            with open(f"/proc/{int(self.read(marker))}/stat", encoding="utf-8") as fh:
                return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, ValueError):
            return False

    def test_sigint_during_hygiene(self):
        marker = self.slow_hygiene()
        proc = self.spawn(TASK, scale="0.01", json_out=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(_wait(lambda: os.path.isfile(marker)))
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 130, err)
        self.assertFalse(self.hyg_alive(marker), "hygiene process group survived SIGINT")
        doc = json.loads(out)
        self.assertEqual(doc["outcome"], "interrupted")
        self.assertIn("README.md", self.read(doc["patch"]))
        self.assertEqual(self.only_task()["outcome"], "interrupted")
        self.assertEqual(os.path.getsize(self.lock_path()), 0)

    def test_kill_state_matches_log(self):
        # AC-STATE-1, L0 form: SIGKILL during hygiene, after the rung.
        marker = self.slow_hygiene()
        proc = self.spawn(TASK, scale="0.01")
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(_wait(lambda: os.path.isfile(marker)), "hygiene never started after the rung")
        proc.kill()
        proc.communicate(timeout=30)
        tasks = os.listdir(os.path.join(self.home, "tasks"))
        self.assertEqual(len(tasks), 1)
        st = self.state_of(tasks[0])
        self.assertEqual((st["next"], st["rungs"], st["attempts"]), ("rung", [], 0))
        self.assertTrue(st["reset_point"])
        self.assertEqual([r["role"] for r in self.log()], ["route"])  # no attempt, no task record
        self.assertEqual(self.log()[0]["task"], tasks[0])
        # The next run on the same repo sees the stale lock (mr unlock is L1).
        ncalls = len(self.calls())
        code, _, err = self.mr()
        self.assertEqual(code, 4)
        self.assertIn(tasks[0], err)
        self.assertEqual(len(self.calls()), ncalls)


class TestState(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="mr-state-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, "home")

    def _w7(self, tid, repo, patch_bytes=b"diff\n", **kw):
        pdir = os.path.join(self.home, "patches", tid)
        os.makedirs(pdir)
        pfile = os.path.join(pdir, "r1-m.patch")
        with open(pfile, "wb") as fh:
            fh.write(patch_bytes)
        st = {"task": tid, "repo": repo, "isolation": "W7", "outcome": "accepted", "patch": pfile}
        st.update(kw)
        state.write_state(self.home, tid, st)

    def test_roundtrip_and_missing(self):
        self.assertIsNone(state.read_state(self.home, "t-nope"))
        state.write_state(self.home, "t-1", {"a": 1})
        self.assertEqual(state.read_state(self.home, "t-1"), {"a": 1})
        state.write_state(self.home, "t-1", {"a": 2})
        self.assertEqual(state.read_state(self.home, "t-1"), {"a": 2})
        self.assertEqual(os.listdir(state.task_dir(self.home, "t-1")), ["state.json"])
        with self.assertRaises(ValueError):
            state.task_dir(self.home, "../x")

    def test_find_w7_leftover(self):
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        self.assertIsNone(state.find_w7_leftover(self.home, repo))
        self._w7("t-old", repo)
        time.sleep(0.02)
        self._w7("t-new", repo)
        self._w7("t-empty", repo, patch_bytes=b"")
        self._w7("t-other", os.path.join(self.dir, "elsewhere"))
        self._w7("t-w6", repo, isolation="W6")
        self._w7("t-fail", repo, outcome="exhausted")
        self.assertEqual(state.find_w7_leftover(self.home, repo)["task"], "t-new")
        link = os.path.join(self.dir, "link")
        os.symlink(repo, link)
        self.assertEqual(state.find_w7_leftover(self.home, link)["task"], "t-new")


if __name__ == "__main__":
    unittest.main()
