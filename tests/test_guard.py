"""Tests for the Claude usage guard (mr-spec §8, AC-GRD-1..5, -7, -8)."""

import contextlib
import glob
import io
import json
import multiprocessing
import os
import tempfile
import time
import types
import unittest
from unittest import mock

from mr import cli, config, guard
from mr import exitcodes as ec
from mr.defaults import defaults
from tests import gitfix

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHECK_DEFAULT = {
    "hour_usage": 10,
    "limit": 40,
    "unit": "points",
    "exceeded": False,
    "five_hour_pct": 30.0,
    "five_hour_resets_at": "2026-10-30T05:00:00Z",
    "seven_day_pct": 12.0,
    "sample_age_s": 30,
    "samples_in_hour": 3,
    "has_baseline": True,
    "checked_at": "2026-10-30T00:00:00Z",
}


def make_cfg(home, claude_limits=None):
    over = {"paths": {"check": gitfix.FAKE_CHECK, "home": home, "claude": "/nonexistent/fake-claude"}}
    if claude_limits is not None:
        over["claude_limits"] = claude_limits
    return config.from_dict(config._deep_merge(defaults(), over))


def _rc(rc, **kw):
    return types.SimpleNamespace(rc=rc, **kw)


def _slow_exec(log_path, sleep_s):
    def exec_fn(argv):
        start = time.time()
        time.sleep(sleep_s)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps([start, time.time()]) + "\n")
        return _rc(0)

    return exec_fn


def _child_guarded(home, log_path, wait_lock_s, out):
    cfg = make_cfg(home)
    run = guard.run_claude_guarded(
        "S", ["-p", "x"], cfg, task="t", exec_fn=_slow_exec(log_path, 1.0), wait_lock_s=wait_lock_s
    )
    out.put(run.guard.reason)


def _child_reserve(home, n, pts):
    cfg = make_cfg(home)
    for i in range(n):
        guard.reserve(cfg, f"t{os.getpid()}", "S", pts)


class GuardCase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.home = td.name
        self.set_check({"default": dict(CHECK_DEFAULT)})
        patcher = mock.patch.dict(
            os.environ,
            {"MR_FAKE_CHECK": self.check_path, "MR_TEST_ALLOW_FAKE_PATHS": "1", "MR_TEST_TIME_SCALE": "0.01"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = make_cfg(self.home)

    def set_check(self, cfg, **default):
        if default:
            cfg["default"] = {**cfg["default"], **default}
        self.check_path = gitfix.write_check(self.home, cfg)
        calls = self.check_path + ".calls"
        if os.path.exists(calls):
            os.remove(calls)

    def calls(self):
        with open(self.check_path + ".calls", encoding="utf-8") as f:
            return f.read().splitlines()

    def guarded(self, exec_fn, **kw):
        return guard.run_claude_guarded("S", ["-p", "x"], self.cfg, task="t1", exec_fn=exec_fn, **kw)


class TestCheck(GuardCase):
    def test_fail_closed(self):
        cases = {
            "exit1_no_json": ({"default": dict(CHECK_DEFAULT), "no_probe": "fail", "probe": "fail"}, "guard error"),
            "junk_then_probe_fails": (
                {"default": dict(CHECK_DEFAULT), "no_probe": "junk", "probe": "fail"},
                "guard error",
            ),
            "stale_probe_fails": (
                {"default": {**CHECK_DEFAULT, "sample_age_s": 9999}, "probe": "fail"},
                "stale sample, probe failed",
            ),
            "no_baseline": (
                {"default": {**CHECK_DEFAULT, "five_hour_pct": None, "has_baseline": False}},
                "no 5h baseline",
            ),
        }
        for name, (check_cfg, reason) in cases.items():
            with self.subTest(name):
                self.set_check(check_cfg)
                res = guard.check("S", self.cfg)
                self.assertFalse(res.allow)
                self.assertEqual(res.reason, reason)
                self.assertIsNotNone(res.frees_hint)
                ran = []
                run = self.guarded(lambda argv: ran.append(argv))
                self.assertFalse(run.guard.allow)
                self.assertIsNone(run.result)
                self.assertEqual(ran, [])

    def test_junk_then_probe_ok_allows(self):
        self.set_check({"default": dict(CHECK_DEFAULT), "no_probe": "junk"})
        res = guard.check("S", self.cfg)
        self.assertTrue(res.allow, res)
        self.assertTrue(res.probed)

    def test_exceeded(self):
        self.set_check({"default": {**CHECK_DEFAULT, "exceeded": True, "sample_age_s": 9999}})
        res = guard.check("S", self.cfg)
        self.assertEqual((res.allow, res.reason), (False, "over_limit"))
        self.assertFalse(res.probed)
        self.assertEqual(self.calls(), ["--no-probe"])

    def test_unknown_rung(self):
        res = guard.check("Z", self.cfg)
        self.assertEqual((res.allow, res.reason), (False, "unknown rung"))
        self.assertFalse(os.path.exists(self.check_path + ".calls"))

    def test_boundaries(self):
        cfg = make_cfg(self.home, {"hour_max": 30, "five_hour_max": 85})  # explicit hourly opt-in
        guard.reserve(cfg, "other", "S", 3)
        for hour, allow in ((21, True), (22, False)):
            with self.subTest(hour=hour):
                self.set_check({"default": {**CHECK_DEFAULT, "hour_usage": hour}})
                res = guard.check("S+", cfg)
                self.assertEqual((res.hour, res.reserved, res.est), (hour, 3.0, 6.0))
                self.assertEqual(res.allow, allow, res)
                if not allow:
                    self.assertEqual(res.reason, "budget")
        os.remove(os.path.join(self.home, "state", "reservations.json"))
        for fiveh, allow in ((79, True), (80, False)):
            with self.subTest(fiveh=fiveh):
                self.set_check({"default": {**CHECK_DEFAULT, "five_hour_pct": fiveh}})
                res = guard.check("S+", self.cfg)
                self.assertEqual(res.allow, allow, res)
                if not allow:
                    self.assertEqual(res.reason, "five_hour")

    def test_shared_claude_limits(self):
        """Guard allow/deny and judge_should_yield use one claude_limits block."""
        self._pin_shared(make_cfg(self.home, {"hour_max": 30, "five_hour_max": 85}))
        self._pin_shared(make_cfg(self.home, {"hour_max": 10, "five_hour_max": 40}))

    def _pin_shared(self, cfg):
        lim = cfg.claude_limits
        est = cfg.guard.est_seed["S"]
        hour_ok = lim.hour_max - est
        five_ok = lim.five_hour_max - est
        self.set_check({"default": {**CHECK_DEFAULT, "hour_usage": hour_ok, "five_hour_pct": 1}})
        res = guard.check("S", cfg)
        self.assertEqual((res.allow, res.hour, res.est), (True, hour_ok, est), res)
        self.set_check({"default": {**CHECK_DEFAULT, "hour_usage": hour_ok + 1, "five_hour_pct": 1}})
        res = guard.check("S", cfg)
        self.assertEqual((res.allow, res.reason), (False, "budget"), res)
        self.set_check({"default": {**CHECK_DEFAULT, "hour_usage": 0, "five_hour_pct": five_ok}})
        res = guard.check("S", cfg)
        self.assertTrue(res.allow, res)
        self.set_check({"default": {**CHECK_DEFAULT, "hour_usage": 0, "five_hour_pct": five_ok + 1}})
        res = guard.check("S", cfg)
        self.assertEqual((res.allow, res.reason), (False, "five_hour"), res)
        self.assertFalse(guard.judge_should_yield(cfg, lim.hour_max - 1, 0))
        self.assertTrue(guard.judge_should_yield(cfg, lim.hour_max, 0))
        self.assertFalse(guard.judge_should_yield(cfg, 0, lim.five_hour_max - 1))
        self.assertTrue(guard.judge_should_yield(cfg, 0, lim.five_hour_max))

    def test_frees_hint(self):
        now = 1_793_334_600.0  # 2026-10-30T04:30:00Z
        self.set_check({"default": {**CHECK_DEFAULT, "exceeded": True}})
        res = guard.check("S", self.cfg, now=now)
        self.assertEqual(res.frees_hint, "five-hour window resets at 10:30 IST")
        res = guard.check("S", self.cfg, now=now - 7200)
        self.assertEqual(res.frees_hint, "≈ in 60 min")
        self.set_check({"default": dict(CHECK_DEFAULT)})
        self.assertIsNone(guard.check("S", self.cfg, now=now).frees_hint)

    def test_log_record(self):
        guard.check("S", self.cfg)
        with open(os.path.join(self.home, "log", "runs.jsonl"), encoding="utf-8") as f:
            rec = json.loads(f.read().splitlines()[-1])
        self.assertEqual((rec["role"], rec["task"], rec["rung"], rec["allow"]), ("guard", None, "S", True))
        for key in ("reason", "hour", "reserved", "est", "five_hour", "frees_hint", "probed", "delta", "wrapper_denied"):
            self.assertIn(key, rec)


class TestReservations(GuardCase):
    def test_reservations(self):
        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_child_reserve, args=(self.home, 10, pts)) for pts in (1.0, 2.0)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(20)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(guard.reserved_total(self.cfg), 30.0)
        now = time.time()
        self.assertEqual(guard.reserved_total(self.cfg, now + 7200), 0.0)
        path = os.path.join(self.home, "state", "reservations.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["reservations"].append({"id": "old", "task": "x", "rung": "O", "pts": 100.0, "expires_at": now - 1})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.assertEqual(guard.reserved_total(self.cfg), 30.0)
        rid = guard.reserve(self.cfg, "y", "S", 4)
        with open(path, encoding="utf-8") as f:
            ids = [r["id"] for r in json.load(f)["reservations"]]
        self.assertNotIn("old", ids)
        self.assertEqual(len(ids), 21)
        guard.release(self.cfg, rid)
        self.assertEqual(guard.reserved_total(self.cfg), 30.0)

    def test_corrupt_reservations_fail_closed(self):
        os.makedirs(os.path.join(self.home, "state"))
        with open(os.path.join(self.home, "state", "reservations.json"), "w") as f:
            f.write("{nope")
        res = guard.check("S", self.cfg)
        self.assertEqual((res.allow, res.reason), (False, "guard error"))


class TestGuardedRun(GuardCase):
    def test_serialised(self):
        ctx = multiprocessing.get_context("fork")
        for wait, want in ((None, ["ok", "ok"]), (0, ["claude busy", "ok"])):
            with self.subTest(wait_lock_s=wait):
                log_path = os.path.join(self.home, f"runs-{wait}.jsonl")
                out = ctx.Queue()
                procs = []
                for _ in range(2):
                    p = ctx.Process(target=_child_guarded, args=(self.home, log_path, wait, out))
                    p.start()
                    procs.append(p)
                    time.sleep(0.3)
                for p in procs:
                    p.join(20)
                    self.assertEqual(p.exitcode, 0)
                self.assertEqual(sorted(out.get(timeout=5) for _ in procs), want)
                with open(log_path, encoding="utf-8") as f:
                    spans = sorted(json.loads(line) for line in f)
                self.assertEqual(len(spans), want.count("ok"))
                for (_, end), (start, _) in zip(spans, spans[1:]):
                    self.assertLessEqual(end, start)

    def test_postprobe_fail(self):
        self.set_check({"default": dict(CHECK_DEFAULT), "probe": "fail"})
        run = self.guarded(lambda argv: _rc(0))
        self.assertTrue(run.guard.allow)
        self.assertIsNone(run.delta)
        self.assertEqual(guard.reserved_total(self.cfg), 4.0)

        self.set_check({"default": dict(CHECK_DEFAULT), "probe": {"five_hour_pct": 33.0}})
        os.remove(os.path.join(self.home, "state", "reservations.json"))
        run = self.guarded(lambda argv: _rc(0))
        self.assertEqual(run.delta, 3.0)
        self.assertEqual(guard.reserved_total(self.cfg), 0.0)
        self.assertEqual(self.calls(), ["--no-probe", "--probe"])

    def test_delta_null_on_window_reset(self):
        self.set_check({"default": dict(CHECK_DEFAULT), "probe": {"five_hour_resets_at": "2026-10-30T10:00:00Z"}})
        run = self.guarded(lambda argv: _rc(0))
        self.assertIsNone(run.delta)
        self.assertEqual(guard.reserved_total(self.cfg), 0.0)

    def test_wrapper_75(self):
        seen = []

        def exec_fn(argv):
            seen.append(argv)
            return _rc(75)

        run = self.guarded(exec_fn)
        self.assertTrue(run.wrapper_denied)
        self.assertEqual(seen, [["/nonexistent/fake-claude", "-p", "x"]])
        run = self.guarded(lambda argv: _rc(2, stderr="blocked by usage-limits hook"))
        self.assertTrue(run.wrapper_denied)
        run = self.guarded(lambda argv: _rc(2, stderr="usage error"))
        self.assertFalse(run.wrapper_denied)


class TestStatic(unittest.TestCase):
    def test_single_entry(self):
        for path in glob.glob(os.path.join(ROOT, "mr", "**", "*.py"), recursive=True):
            rel = os.path.relpath(path, ROOT)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            if rel not in ("mr/guard.py", "mr/config.py"):
                self.assertNotIn("paths.claude", text, rel)
            if rel not in ("mr/guard.py", "mr/config.py", "mr/defaults.py"):
                self.assertNotIn("usage-limits/bin/claude", text, rel)


class TestCli(GuardCase):
    def run_cli(self):
        routes = os.path.join(self.home, "routes.yaml")
        with open(routes, "w", encoding="utf-8") as f:
            f.write(
                f"paths:\n  check: {gitfix.FAKE_CHECK}\n  home: {self.home}\n  claude: /nonexistent/fake-claude\n"
            )
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--config", routes, "guard", "--rung", "S"])
        return code, out.getvalue()

    def test_mr_guard_cli(self):
        code, out = self.run_cli()
        self.assertEqual(code, ec.OK)
        doc = json.loads(out)
        self.assertTrue(doc["allow"])
        self.assertNotIn("sample", doc)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.set_check({"default": {**CHECK_DEFAULT, "exceeded": True}})
        code, out = self.run_cli()
        self.assertEqual(code, ec.GUARD_DENY)
        self.assertEqual(json.loads(out)["reason"], "over_limit")


if __name__ == "__main__":
    unittest.main()
