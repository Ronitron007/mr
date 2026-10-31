"""Tests for mr.log (mr-spec §4.4, §14.1)."""

import contextlib
import json
import multiprocessing as mp
import os
import re
import tempfile
import unittest
from io import StringIO

from mr.log import Log, append, now_ist, read
from tests.log_schema_v2 import check

_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+05:30$")


def _common(**overrides):
    base = {
        "ts": "2026-10-30T12:00:00+05:30",
        "v": 2,
        "task": "t-fixture",
        "mr_version": "0.1.0",
    }
    base.update(overrides)
    return base


def _attempt(role: str) -> dict:
    return _common(
        role=role,
        rung="G-h",
        model="grok-4.7-high",
        path="cursor",
        attempt=1,
        started_at="2026-10-30T11:00:00+05:30",
        rc=0,
        is_error=False,
        wall_s=12.5,
        signals={"H": True},
        decision="accept",
        failure_class=None,
        isolation="W7",
        patch=None,
        diff=None,
        mr_result=None,
        raw=None,
        pts_pre=None,
        pts_post=None,
        claude_pts=None,
        usd_equiv=None,
        model_served=None,
        item=None,
    )


FIXTURES = {
    "implementer": _attempt("implementer"),
    "planner": _attempt("planner"),
    "reviewer": _attempt("reviewer"),
    "route": _common(
        role="route",
        fingerprint=None,
        rules={"route": "cascade"},
        applied={"route": "cascade", "ladder": ["G-x"]},
        sources={"route": "default"},
        invariant_violation=False,
    ),
    "task": _common(
        role="task",
        started_at="2026-10-30T10:00:00+05:30",
        ended_at="2026-10-30T11:00:00+05:30",
        wall_s=3600,
        repo="example-app",
        base="abc123",
        route="cascade",
        ladder=["G-x"],
        attempts={"impl": 1, "review": 0, "plan": 0, "classifier": 0},
        outcome="accepted",
        exit=0,
        verified=True,
        final_signals={"H": True},
        cost={"claude_pts": 0, "usd_equiv": 0.0, "units": 1.0},
    ),
    "guard": {
        **_common(task=None, role="guard"),
        "rung": "S",
        "allow": True,
        "reason": "ok",
        "hour": 3.0,
        "reserved": 0,
        "est": 1.5,
        "five_hour": 40.0,
    },
    "isolation": _common(role="isolation", code="W7", inputs={"repo": "/tmp/x"}),
}

BROKEN_FIXTURES = [
    ("implementer", {**_attempt("implementer"), "attempt": "one"}),
    ("planner", {k: v for k, v in _attempt("planner").items() if k != "rung"}),
    ("reviewer", {**_attempt("reviewer"), "path": "azure"}),
    ("route", {**FIXTURES["route"], "applied": {"route": "cascade"}}),
    ("task", {**FIXTURES["task"], "outcome": "maybe"}),
    ("guard", {**FIXTURES["guard"], "allow": "yes"}),
    ("isolation", {**FIXTURES["isolation"], "code": "W9"}),
]


def _worker(home_n: tuple[str, int]) -> None:
    home, n = home_n
    blob = "x" * 1800
    append(
        {
            "role": "guard",
            "task": f"conc-{n}",
            "rung": "S",
            "allow": True,
            "reason": "test",
            "reserved": 0,
            "est": 1.0,
            "blob": blob,
        },
        home=home,
    )


class TestLogConcurrentAppend(unittest.TestCase):
    def test_concurrent_append(self):
        with tempfile.TemporaryDirectory() as home:
            ctx = mp.get_context("fork" if os.name != "nt" else "spawn")
            with ctx.Pool(50) as pool:
                pool.map(_worker, [(home, i) for i in range(50)])
            path = os.path.join(home, "log", "runs.jsonl")
            with open(path, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 50)
            for line in lines:
                json.loads(line)


class TestLogSchema(unittest.TestCase):
    def test_schema_good_fixtures(self):
        for name, record in FIXTURES.items():
            with self.subTest(name=name):
                self.assertEqual(check(record), [])

    def test_schema_broken_fixtures(self):
        for name, broken in BROKEN_FIXTURES:
            with self.subTest(name=name):
                self.assertNotEqual(check(broken), [])

    def test_schema_all_roles(self):
        for name, rec in FIXTURES.items():
            with self.subTest(name=name):
                self.assertEqual(check(rec), [], name)
        broken = {**FIXTURES["implementer"], "rc": "bad"}
        self.assertTrue(check(broken))


class TestLogTsIst(unittest.TestCase):
    def test_ts_ist(self):
        ts = now_ist()
        self.assertRegex(ts, _TS)
        self.assertTrue(ts.endswith("+05:30"))
        with tempfile.TemporaryDirectory() as home:
            append(
                {
                    "role": "guard",
                    "task": "t1",
                    "rung": "S",
                    "allow": True,
                    "reason": "x",
                    "reserved": 0,
                    "est": 0.0,
                },
                home=home,
            )
            rows = read(home)
            self.assertEqual(len(rows), 1)
            self.assertRegex(rows[0]["ts"], _TS)


class TestLogNeverRaises(unittest.TestCase):
    def test_never_raises(self):
        with tempfile.TemporaryDirectory() as td:
            bad_home = os.path.join(td, "not-a-dir")
            with open(bad_home, "w", encoding="utf-8") as f:
                f.write("file")
            err = StringIO()
            with contextlib.redirect_stderr(err):
                append(
                    {
                        "role": "guard",
                        "task": "t",
                        "rung": "S",
                        "allow": False,
                        "reason": "nope",
                        "reserved": 0,
                        "est": 0.0,
                    },
                    home=bad_home,
                )
            self.assertIn("mr log:", err.getvalue())


class TestLogRedactedKeys(unittest.TestCase):
    def test_redacted_keys(self):
        with tempfile.TemporaryDirectory() as home:
            append(
                {
                    "role": "guard",
                    "task": "t-redact",
                    "rung": "S",
                    "allow": True,
                    "reason": "ok",
                    "reserved": 0,
                    "est": 0.0,
                    "prompt": "CANARY-123",
                    "env": {"API_KEY": "secret"},
                },
                home=home,
            )
            with open(
                os.path.join(home, "log", "runs.jsonl"), encoding="utf-8"
            ) as f:
                raw = f.read()
            self.assertNotIn("CANARY-123", raw)
            self.assertNotIn("secret", raw)
            row = json.loads(raw.strip())
            self.assertGreaterEqual(set(row.get("redacted_keys", [])), {"prompt", "env"})


class TestLogRead(unittest.TestCase):
    def test_read_filters_and_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as home:
            log = Log(home)
            path = log.path
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"task":"a","role":"guard"}\n')
                f.write("not json\n")
                f.write('{"task":"b","role":"route"}\n')
            self.assertEqual(len(read(home)), 2)
            self.assertEqual(len(read(home, task="a")), 1)
            self.assertEqual(len(read(home, role="route")), 1)


if __name__ == "__main__":
    unittest.main()
