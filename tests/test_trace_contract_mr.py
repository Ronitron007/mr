"""Contract for task title, caller, and the trace viewer's log fields."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import mr.commands.run as run_mod
import mr.trace.load as load
from mr.log import Log

ENDED = "2026-10-31T15:05:00+05:30"

EXPECTED_READ_FIELDS = {
    "route": {
        "task": str,
        "ts": str,
        "applied": dict,
        "caller": (str, type(None)),
        "sources": dict,
        "prepass_confident": bool,
    },
    "planner": {
        "task": str,
        "rung": str,
        "model": str,
        "attempt": int,
        "ts": str,
    },
    "implementer": {
        "task": str,
        "rung": str,
        "model": str,
        "attempt": int,
        "ts": str,
    },
    "reviewer": {
        "task": str,
        "rung": str,
        "model": str,
        "attempt": int,
        "ts": str,
    },
    "advisor": {
        "task": str,
        "ts": str,
        "row": (str, type(None)),
        "gate": (str, type(None)),
        "consult_made": int,
    },
    "guard": {
        "task": (str, type(None)),
        "ts": str,
        "rung": str,
        "allow": bool,
        "reason": str,
    },
    "task": {
        "task": str,
        "started_at": str,
        "ended_at": str,
        "title": (str, type(None)),
        "caller": (str, type(None)),
        "outcome": str,
        "ladder": list,
    },
}


def _matches(value, typ):
    types = typ if isinstance(typ, tuple) else (typ,)
    if type(value) is bool and bool not in types:
        return False
    return isinstance(value, types)


def _parent_comm():
    try:
        with open("/proc/%s/comm" % os.getppid(), encoding="utf-8") as fh:
            name = fh.read().strip()
    except OSError:
        return None
    return name[:60] or None


class _Ctx:
    """Minimal stand-in for the attributes _task_record reads."""

    exit = 0
    rung = "G-l"
    n_review = 0
    n_plan = 0
    commit = None
    started_at = "2026-10-31T15:00:00+05:30"
    t0 = 1700000000.0
    counters = {"attempts": 0}
    cost = {"units": 0.0}
    diff = {"files": 0}

    def __init__(self, home, text):
        self.rungs = []
        self.files = ()
        self.ladder = ["G-l"]
        self.d = SimpleNamespace(
            type="bugfix", difficulty="easy", risk="low", risk_tags=[],
            route="cascade", sources={"route": "rule"},
        )
        self.task = SimpleNamespace(
            id="t-516f98c7", repo="/tmp/mr-public", base="0123456789abcdef", text=text,
        )
        self.cfg = SimpleNamespace(paths=SimpleNamespace(home=home))

    @property
    def outcome(self):
        return "accepted"

    @property
    def used(self):
        return []


class TraceContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self._env = os.environ.get("MR_CALLER")

    def tearDown(self):
        if self._env is None:
            os.environ.pop("MR_CALLER", None)
        else:
            os.environ["MR_CALLER"] = self._env
        self.tmp.cleanup()

    def test_title_multiline_and_blank_lines(self):
        fn = getattr(run_mod, "_title", None)
        self.assertTrue(callable(fn))
        self.assertEqual(fn("First line\nSecond line"), "First line")
        self.assertEqual(fn("\n\n   \n\t\n  Ship the title  \nrest"), "Ship the title")
        self.assertEqual(fn("  hello  world  "), "hello  world")

    def test_title_truncates_at_120_and_empty_is_none(self):
        fn = getattr(run_mod, "_title", None)
        self.assertTrue(callable(fn))
        self.assertEqual(fn("m" * 120), "m" * 120)
        self.assertEqual(fn("\n\n" + ("q" * 200) + "\nrest"), "q" * 120)
        self.assertEqual(fn("   " + ("z" * 130)), "z" * 120)
        self.assertIsNone(fn(""))
        self.assertIsNone(fn(" \n\t\n "))

    def test_caller_mr_caller_cut_to_60(self):
        fn = getattr(run_mod, "_caller", None)
        self.assertTrue(callable(fn))
        os.environ["MR_CALLER"] = "n" * 80
        self.assertEqual(fn(), "n" * 60)
        os.environ["MR_CALLER"] = "cursor-agent"
        self.assertEqual(fn(), "cursor-agent")

    def test_caller_parent_comm_when_unset(self):
        fn = getattr(run_mod, "_caller", None)
        self.assertTrue(callable(fn))
        os.environ.pop("MR_CALLER", None)
        expected = _parent_comm()
        self.assertEqual(fn(), expected)
        os.environ["MR_CALLER"] = ""
        self.assertEqual(fn(), expected)
        os.environ.pop("MR_CALLER", None)
        with mock.patch("mr.commands.run.os.getppid", return_value=-1):
            try:
                missing = fn()
            except Exception as exc:
                self.fail("_caller raised %s" % exc)
        self.assertIsNone(missing)

    def test_task_record_sets_title_and_caller(self):
        os.environ["MR_CALLER"] = "planner-bot"
        text = "\n\n  Fix the door  \nmore detail"
        rec = run_mod._task_record(_Ctx(self.home, text), ENDED)
        self.assertEqual(rec["title"], "Fix the door")
        keys = list(rec)
        self.assertEqual(keys[keys.index("title") + 1], "caller")
        self.assertEqual(rec["caller"], "planner-bot")
        Log(self.home).append(rec)
        rows = [r for r in load.read_rows(self.home) if r.get("role") == "task"]
        self.assertTrue(rows)
        self.assertEqual(rows[-1]["title"], "Fix the door")
        self.assertEqual(rows[-1]["caller"], "planner-bot")
        saved = list(rows[-1])
        self.assertEqual(saved[saved.index("title") + 1], "caller")

    def test_read_fields_roundtrip(self):
        os.environ["MR_CALLER"] = "contract-caller"
        task_rec = run_mod._task_record(_Ctx(self.home, "Pin the contract\nbody"), ENDED)
        self.assertEqual(getattr(load, "READ_FIELDS", None), EXPECTED_READ_FIELDS)
        rows_in = [
            {"role": "route", "task": "t-contract", "applied": {"ladder": ["G-l"]},
             "caller": "route-caller", "sources": {"route": "rule"}, "prepass_confident": True},
            {"role": "planner", "task": "t-contract", "rung": "G-l", "model": "grok-4.7", "attempt": 1},
            {"role": "implementer", "task": "t-contract", "rung": "G-h", "model": "grok-4.7-high", "attempt": 2},
            {"role": "reviewer", "task": "t-contract", "rung": "S", "model": "claude-sonnet", "attempt": 3},
            {"role": "advisor", "task": "t-contract", "row": "Y1", "gate": "open", "consult_made": 1},
            {"role": "guard", "task": "t-contract", "rung": "S", "allow": True, "reason": "ok"},
            task_rec,
        ]
        log = Log(self.home)
        for rec in rows_in:
            log.append(rec)
        by_role = {}
        for row in load.read_rows(self.home):
            by_role.setdefault(row.get("role"), []).append(row)
        for role, fields in EXPECTED_READ_FIELDS.items():
            self.assertIn(role, by_role)
            row = by_role[role][-1]
            for name, typ in fields.items():
                self.assertIn(name, row)
                self.assertTrue(_matches(row[name], typ), "%s.%s=%r" % (role, name, row.get(name)))

    def test_v3_flags_newer_log_format(self):
        log = Log(self.home)
        common = {
            "outcome": "accepted", "ladder": [], "title": None, "caller": None,
        }
        log.append({
            "role": "task", "task": "t-v3", "v": 3,
            "started_at": "2026-10-31T12:00:00+05:30",
            "ended_at": "2026-10-31T12:05:00+05:30",
            "outcome": "accepted", "ladder": ["G-l"],
            "title": None, "caller": None, "unknown_field": {"keep": True},
        })
        log.append(dict(common, role="task", task="t-v2",
                        started_at="2026-10-31T11:00:00+05:30",
                        ended_at="2026-10-31T11:05:00+05:30"))
        log.append(dict(common, role="task", task="t-vstr", v="3",
                        started_at="2026-10-31T10:00:00+05:30",
                        ended_at="2026-10-31T10:05:00+05:30"))
        log.append(dict(common, role="task", task="t-vbool", v=True,
                        started_at="2026-10-31T08:00:00+05:30",
                        ended_at="2026-10-31T08:05:00+05:30"))
        path = os.path.join(self.home, "log", "runs.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(
                common, role="task", task="t-nov", extra=1,
                started_at="2026-10-31T09:00:00+05:30",
                ended_at="2026-10-31T09:05:00+05:30",
            )) + "\n")
        with mock.patch.object(load, "running_info", return_value={}):
            traces = {t["task"]: t for t in load.build_traces(self.home, with_transcripts=False)}
        for tid in ("t-v3", "t-v2", "t-vstr", "t-vbool", "t-nov"):
            self.assertIn(tid, traces)
            self.assertIsNone(traces[tid]["title"])
            self.assertIsNone(traces[tid]["caller"])
        for tid in ("t-nov", "t-v2", "t-vstr", "t-vbool"):
            self.assertNotIn("newer_log_format", traces[tid])
        self.assertIs(traces["t-v3"].get("newer_log_format"), True)


if __name__ == "__main__":
    unittest.main()
