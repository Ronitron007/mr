import unittest

from mr.trace.load import _time_split


def st(role, t0, t1, ok=True):
    return {"role": role, "t0": t0, "t1": t1, "ok": ok}


class TimeSplit(unittest.TestCase):
    def test_model_vs_mr(self):
        t = _time_split([st("planner", 0, 40), st("implementer", 50, 90)], 0, 100, None)
        self.assertEqual((t["wall_s"], t["model_s"], t["mr_s"], t["plan_s"], t["retry_s"]), (100, 80, 20, 40, 0))

    def test_thrown_away_attempts_are_retries(self):
        steps = [st("implementer", 0, 30, ok=False), st("reviewer", 30, 40), st("implementer", 40, 60)]
        self.assertEqual(_time_split(steps, 0, 60, None)["retry_s"], 40)

    def test_review_only_is_not_a_retry(self):
        self.assertEqual(_time_split([st("reviewer", 0, 50, ok=False)], 0, 50, None)["retry_s"], 0)

    def test_killed_job_time_is_unlogged_not_mr(self):
        t = _time_split([st("planner", 0, 40)], 0, 100, None, killed=True)
        self.assertEqual((t["mr_s"], t["unlogged_s"]), (0, 60))
