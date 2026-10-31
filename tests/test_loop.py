"""T16: loop.next_step, counters (AC-ESC-1/2, AC-CAP-1, §0.3). AC-PAID-1 is retired: no paid ledger."""
import itertools
import json
import os
import unittest
from dataclasses import fields, replace

from mr import config, exitcodes, loop
from mr.defaults import defaults

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "p0", "escalation_table.json")
_FIELDS = {f.name for f in fields(loop.LoopState)}


def _state(row: dict) -> loop.LoopState:
    return loop.LoopState(**{k: tuple(v) if isinstance(v, list) else v
                             for k, v in row.items() if k in _FIELDS})


def _rows():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


class TestEscalationTable(unittest.TestCase):
    def test_fixture_rows(self):  # AC-ESC-1
        rows = _rows()
        self.assertEqual(len(rows), 25)
        for row in rows:
            with self.subTest(row["id"], note=row["note"]):
                st = loop.next_step(_state(row))
                got = {"action": st.action, "rung": st.rung, "exit": st.exit,
                       "reset": st.reset, "effort_up": st.effort_up}
                self.assertEqual(got, row["expect"])

    def test_no_repeat(self):  # AC-ESC-2
        base_row = _rows()[0]
        for cur, eu, used_extra in itertools.product(
                ["G-m", "G-h", "G-x", "C25", "K-h", "K-x", "S", "S+"],
                [(), ("grok-4.7",), ("kimi-k3",), ("claude-sonnet-5-5",)],
                [(), ("G-x",), ("K-x",), ("S+",)]):
            s = replace(_state(base_row), ladder=("G-m", "G-h", "G-x", "S"),
                        difficulty="medium", current_rung=cur, cls="skipped", attempts=1,
                        used_rungs=tuple(dict.fromkeys(("G-m", cur) + used_extra)),
                        effort_up_used=eu, claude_rungs=int(cur in loop.CLAUDE))
            with self.subTest(cur=cur, eu=eu, used=s.used_rungs):
                st = loop.next_step(s)
                if st.action == "rung":
                    self.assertNotEqual(st.rung, cur)
                    self.assertNotIn(st.rung, s.used_rungs)
                    if st.effort_up:
                        self.assertEqual(st.rung, loop.UP[cur])
                        self.assertNotIn(loop.base(cur), eu)


class TestHardRule(unittest.TestCase):
    def test_guard_deny_never_claude(self):
        base_row = _rows()[0]
        ladders = [("G-h", "S"), ("S", "O"), ("S+", "O+"), ("G-x", "S+", "O")]
        for ladder, cls, used, infra in itertools.product(
                ladders, ["tried_wrong", "skipped", "off_track", "infra", "guard_deny"],
                [(), ("G-x",)], [0, 1]):
            cur = ladder[0]
            s = replace(_state(base_row), ladder=ladder, difficulty="hard", current_rung=cur,
                        cls=cls, used_rungs=(cur,) + used, attempts=1, guard_allows=False,
                        claude_rungs=int(cur in loop.CLAUDE), opus_rungs=0,
                        infra_retries_this_rung=infra)
            with self.subTest(ladder=ladder, cls=cls, used=used, infra=infra):
                st = loop.next_step(s)
                if st.action == "rung":
                    if st.rung in loop.CLAUDE:
                        self.fail(f"Claude rung {st.rung} under guard deny")
                    if st.rung != cur and cur not in loop.CLAUDE:
                        continue  # a non-Claude ladder/effort-up rung needs no guard
                    if st.rung != cur:
                        self.assertEqual(st.rung, "G-x")
                else:
                    self.assertIn(st.exit, (exitcodes.DEFERRED, exitcodes.NOT_ACCEPTED,
                                            exitcodes.HUMAN))
                    if cls == "guard_deny":
                        self.assertEqual(st.exit, exitcodes.DEFERRED)

    def test_claude_fallback_rejected(self):
        s = _state(_rows()[11])  # E12: guard denies S
        with self.assertRaises(ValueError):
            loop.next_step(s, fallback=("S", "G-x"))

    def test_cfg_fallback_non_anthropic(self):
        cfg = config.from_dict(defaults())
        self.assertEqual(cfg.fallback_claude_denied, ("G-x",))
        for r in cfg.fallback_claude_denied:
            self.assertFalse(config.is_anthropic(cfg.rungs[r].model))


class TestCounting(unittest.TestCase):  # AC-CAP-1
    Z = {"attempts": 0, "claude_rungs": 0, "opus_rungs": 0}

    def test_fixtures(self):
        cases = [
            ("G-x", "effort_up", (1, 0, 0)),
            ("S+", "effort_up", (1, 1, 0)),
            ("G-x", "fallback", (1, 0, 0)),
            ("G-h", "implementer", (1, 0, 0)),
            ("O", "implementer", (1, 1, 1)),
            ("S", "infra_retry", (0, 0, 0)),
            ("G-h", "planner", (0, 0, 0)),
            ("S", "reviewer", (0, 0, 0)),
            ("O", "reviewer", (0, 0, 0)),
            ("G-x", "reviewer", (0, 0, 0)),
        ]
        for rung, kind, (a, c, o) in cases:
            with self.subTest(rung=rung, kind=kind):
                self.assertEqual(loop.count_after(self.Z, rung, kind=kind),
                                 {"attempts": a, "claude_rungs": c, "opus_rungs": o})

    def test_sequence_and_purity(self):
        c0 = dict(self.Z)
        c = loop.count_after(c0, "G-h", kind="implementer")
        c = loop.count_after(c, "G-h", kind="infra_retry")
        c = loop.count_after(c, "G-x", kind="effort_up")
        c = loop.count_after(c, "S", kind="reviewer")
        c = loop.count_after(c, "S+", kind="implementer")
        self.assertEqual(c, {"attempts": 3, "claude_rungs": 1, "opus_rungs": 0})
        self.assertEqual(c0, self.Z)
        with self.assertRaises(ValueError):
            loop.count_after(c0, "G-h", kind="bogus")


class TestPaidRetired(unittest.TestCase):  # AC-PAID-1 retired (30 Oct 2026, model roles)
    def test_no_paid_ledger_or_state(self):
        self.assertFalse(hasattr(loop, "PaidLedger"))
        self.assertFalse(hasattr(loop, "PAID"))
        self.assertNotIn("cursor_paid_left", _FIELDS)

    def test_grok_author_reviewer_is_sonnet(self):
        self.assertEqual(loop.reviewer_for_grok(config.from_dict(defaults())), "S")

    def test_kimi_rungs_are_not_special(self):
        # a user may still opt in to a K-* ladder: no cap, same rules as any cursor rung
        s = replace(_state(_rows()[2]), ladder=("G-h", "K-h", "S"), used_rungs=("G-h",),
                    current_rung="G-h", cls="tried_wrong", attempts=1)
        self.assertEqual(loop.next_step(s).rung, "K-h")


if __name__ == "__main__":
    unittest.main()
