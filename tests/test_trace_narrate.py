import unittest

from mr.trace.narrate import FAILURE_MEANING, fmt_duration, headline, step_line


def S(role="implementer", rung="G-l", model="grok-4.7-low", ok=True, **kw):
    return dict(role=role, rung=rung, model=model, ok=ok, **kw)


class Narrate(unittest.TestCase):
    def test_meanings(self):
        self.assertEqual(set(FAILURE_MEANING), {"skipped", "tried_wrong", "spec_suspect", "off_track", "invalid_plan", "infra"})

    def test_duration(self):
        self.assertEqual(fmt_duration(0), "0s")
        self.assertEqual(fmt_duration(37), "37s")
        self.assertEqual(fmt_duration(361), "6m 01s")
        self.assertEqual(fmt_duration(3900), "1h 05m")
        self.assertEqual(fmt_duration(None), "–")

    def test_accepted_first_try(self):
        t = {"status": "accepted", "wall_s": 37, "cost": {"units": 1.5}, "steps": [S("planner"), S()]}
        self.assertEqual(headline(t), "Accepted on G-l (grok-4.7-low) after first try · 37s · 1.5 units")

    def test_accepted_escalations(self):
        t = {"status": "accepted", "wall_s": 361, "cost": {"units": 2.0},
             "steps": [S(ok=False), S(rung="G-h", model="m2")]}
        self.assertEqual(headline(t), "Accepted on G-h (m2) after 1 escalation · 6m 01s · 2 units")
        t["steps"].append(S(rung="S", model="m3"))
        t["cost"]["units"] = 0.5
        self.assertEqual(headline(t), "Accepted on S (m3) after 2 escalations · 6m 01s · 0.5 units")

    def test_needs_human(self):
        t = {"status": "needs_human", "wall_s": 3900,
             "steps": [S(ok=False, failure_class="skipped"), S(ok=False, failure_class="spec_suspect")]}
        self.assertEqual(headline(t), "Needs a human: " + FAILURE_MEANING["spec_suspect"] + " · 1h 05m")

    def test_running(self):
        self.assertEqual(headline({"status": "running"}), "Running: routing")
        self.assertEqual(headline({"status": "running", "steps": [S("planner", "G-h", "x")]}), "Running: planner on G-h (x)")

    def test_other(self):
        self.assertEqual(headline({"status": "no_end_record", "wall_s": 5}), "No end record · 5s")

    def test_none_inputs(self):
        for t in (None, {}, {"status": None, "steps": None, "cost": None}):
            headline(t)
        for st in ("accepted", "needs_human", "running"):
            headline({"status": st, "steps": [None, {}], "cost": {"units": None}})
        step_line(None)
        step_line({"diff": None, "wall_s": None})

    def test_step_line(self):
        self.assertEqual(step_line(S(wall_s=37, ok=False, failure_class="spec_suspect")),
                         "Implement on G-l (grok-4.7-low) · 37s · failed: spec_suspect")
        self.assertEqual(step_line(S("planner", "G-h", "grok-4.7-high", wall_s=128)),
                         "Plan on G-h (grok-4.7-high) · 2m 08s · ok")
        self.assertTrue(step_line(S("reviewer", wall_s=1)).startswith("Review on"))
        self.assertTrue(step_line(S("guard", wall_s=1)).startswith("Guard on"))

    def test_step_extras(self):
        line = step_line(S(wall_s=5, escalated_from="G-l", diff={"files": ["a", "b"], "add": 3, "del": 1}))
        self.assertEqual(line, "Implement on G-l (grok-4.7-low) · 5s · ok · escalated from G-l · +3 -1 in 2 files")
        self.assertTrue(step_line(S(wall_s=5, diff={"files": ["a"], "add": 1, "del": 0})).endswith("in 1 file"))
        self.assertNotIn("file", step_line(S(wall_s=5, diff={"files": [], "add": 0, "del": 0})))


if __name__ == "__main__":
    unittest.main()


class Fallbacks(unittest.TestCase):
    def test_standalone_review_headline_has_no_holes(self):
        t = {"status": "accepted", "wall_s": None, "cost": {}, "steps": [{"role": "reviewer", "ok": True}]}
        self.assertEqual(headline(t), "Review: accepted")
        t["wall_s"] = 37
        self.assertEqual(headline(t), "Review: accepted · 37s")

    def test_missing_units_omitted_zero_kept(self):
        impl = {"role": "implementer", "rung": "S", "model": "m", "ok": True}
        self.assertEqual(headline({"status": "accepted", "wall_s": 22, "cost": {"units": None}, "steps": [impl]}),
                         "Accepted on S (m) after first try · 22s")
        self.assertEqual(headline({"status": "accepted", "wall_s": 22, "cost": {"units": 0}, "steps": [impl]}),
                         "Accepted on S (m) after first try · 22s · 0 units")

    def test_unknown_rung_never_prints_question_marks(self):
        t = {"status": "accepted", "steps": [{"role": "implementer", "ok": True}]}
        self.assertNotIn("?", headline(t))


class Shape(unittest.TestCase):
    def test_tags(self):
        from mr.trace.narrate import shape
        self.assertEqual(shape({"time": {"wall_s": 484, "plan_s": 400}}), "mostly planning")
        self.assertEqual(shape({"time": {"wall_s": 100, "plan_s": 10, "retry_s": 60}}), "mostly retries")
        self.assertEqual(shape({"time": {"wall_s": 100, "mr_s": 70}}), "mostly mr checks")
        self.assertIsNone(shape({"time": {"wall_s": 100, "plan_s": 40, "retry_s": 40}}))
        self.assertIsNone(shape({"time": {"wall_s": 10, "plan_s": 10}}))
        self.assertIsNone(shape(None))

    def test_tag_in_headline(self):
        impl = {"role": "implementer", "rung": "G-h", "model": "g", "ok": True}
        t = {"status": "accepted", "wall_s": 484, "cost": {"units": 1.02}, "steps": [impl], "time": {"wall_s": 484, "plan_s": 400}}
        self.assertEqual(headline(t), "Accepted on G-h (g) after first try · 8m 04s · 1.02 units · mostly planning")
