"""SHD1 locked tests: mr.shadow seam, route record shadow fields, log schema roles, run task record."""
import dataclasses
import hashlib
import json
import unittest

from mr import prepass, router
from mr.config import from_dict
from mr.defaults import defaults
from mr.prepass import Features
from mr.task import Params, Task
from tests import log_schema_v2
from tests.test_run_once import RunCase

FIELDS = {"source", "mode", "status", "ref", "consulted", "route", "type", "difficulty", "risk", "risk_tags",
          "confidence", "first_rung", "unsure_fields", "latency_ms"}
# Split so the public-tree gate never sees a key-like literal.
FAKE_KEY = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_COMMON = {"ts": "2026-11-01T10:00:00+05:30", "v": 2, "mr_version": "0.1.0", "task": "t-00000000"}
ROUTE = {**_COMMON, "role": "route", "fingerprint": None, "rules": {"route": "cascade"},
         "applied": {"route": "cascade", "ladder": ["G-l"]}, "sources": {"route": "default"},
         "invariant_violation": False, "judge": None, "classifier": None}
TASK = {**_COMMON, "role": "task", "started_at": "2026-11-01T10:00:00+05:30",
        "ended_at": "2026-11-01T10:05:00+05:30", "wall_s": 300, "repo": "example-app", "base": "abc123",
        "route": "cascade", "ladder": ["G-l"], "attempts": {"impl": 1, "review": 0, "plan": 0, "classifier": 0},
        "outcome": "accepted", "exit": 0, "verified": False, "final_signals": {"H": True},
        "cost": {"claude_pts": 0, "usd_equiv": 0.0, "units": 1.0},
        "shadow": {"judge": None, "classifier": None}, "text_redacted": "fix typo",
        "feature_hash": "0123456789abcdef"}
JUDGE = {**_COMMON, "role": "judge", "ref": "j-t-00000000-1", "status": "ok", "mode": "shadow",
         "latency_ms": 850, "use": "route", "qset": "route.v1", "model_req": "claude-haiku-5-5"}
CLF = {**_COMMON, "role": "classifier", "ref": "c-t-00000000-1", "status": "timeout", "mode": "shadow",
       "latency_ms": None, "model": "grok-4.7-low"}


def _shadow(case):
    try:
        from mr import shadow
    except ImportError as exc:
        case.fail(f"mr.shadow is missing: {exc}")
    return shadow


def _without(rec, key):
    return {k: v for k, v in rec.items() if k != key}


def _fp(hit, n=3):
    for i in range(10000):
        fp = f"fp-{i}"
        if (int(hashlib.sha256(fp.encode()).hexdigest()[:8], 16) % n == 0) == hit:
            return fp
    raise AssertionError("no fingerprint found")


def _ans(shadow, **kw):
    base = dict(source="judge", mode="shadow", status="ok", ref="j-t-00000000-1", consulted=True,
                route="cascade", type="docs", difficulty="easy", risk="low", risk_tags=("deps",),
                confidence=None, first_rung="G-m", unsure_fields=("risk",), latency_ms=900)
    base.update(kw)
    return shadow.ShadowAnswer(**base)


def _task():
    return Task(id="t-00000000", text="task", repo="/work/app", base="a" * 40, params=Params(),
                files_named=(), source="cli")


def _features(**kw):
    base = dict(type_hits={}, files=(), grep_hit_files=0, risk_tags=frozenset(), risk_floor="low",
                difficulty_floor="trivial", difficulty_est="trivial", cross_layer=0, public_interface=False,
                new_module=False, ambiguity=0, judgment_keywords=False, confident=True, type="code", n_files=0)
    base.update(kw)
    return Features(**base)


class ShouldRunTest(unittest.TestCase):
    def test_unsure_task_always_runs_consulted(self):
        s = _shadow(self)
        for fp in (_fp(True), _fp(False)):
            self.assertEqual(s.should_run(Params(), False, fp), (True, True), fp)

    def test_confident_task_follows_hash_sample(self):
        s = _shadow(self)
        self.assertEqual(s.should_run(Params(), True, _fp(True)), (True, False))
        self.assertEqual(s.should_run(Params(), True, _fp(False)), (False, False))
        self.assertEqual(s.should_run(Params(), True, _fp(True, 5), sample_n=5), (True, False))
        self.assertEqual(s.should_run(Params(), True, _fp(False, 5), sample_n=5), (False, False))

    def test_full_params_count_as_confident(self):
        s = _shadow(self)
        full = Params(type="docs", difficulty="trivial", route="cascade")
        self.assertEqual(s.should_run(full, False, _fp(False)), (False, False))
        self.assertEqual(s.should_run(full, False, _fp(True)), (True, False))
        partial = Params(type="docs", difficulty="trivial")
        self.assertEqual(s.should_run(partial, False, _fp(False)), (True, True))

    def test_sample_zero_never_samples(self):
        s = _shadow(self)
        hit = _fp(True)
        self.assertEqual(s.should_run(Params(), True, hit, sample_n=0), (False, False))
        self.assertEqual(s.should_run(Params(), True, hit, sample_n=-1), (False, False))
        self.assertEqual(s.should_run(Params(), False, hit, sample_n=0), (True, True))


class ShadowAnswerTest(unittest.TestCase):
    def test_shadow_answer_fields_frozen(self):
        s = _shadow(self)
        self.assertEqual({f.name for f in dataclasses.fields(s.ShadowAnswer)}, FIELDS)
        ans = _ans(s)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ans.status = "timeout"

    def test_route_summary_judge(self):
        s = _shadow(self)
        got = json.loads(json.dumps(s.route_summary(_ans(s))))
        self.assertEqual(got, {"mode": "shadow", "ref": "j-t-00000000-1", "status": "ok", "consulted": True,
                               "route": "cascade", "type": "docs", "difficulty": "easy", "risk": "low",
                               "risk_tags": ["deps"], "acted_fields": [], "unsure_fields": ["risk"]})
        self.assertIsNone(s.route_summary(None))

    def test_route_summary_classifier_adds_confidence(self):
        s = _shadow(self)
        ans = _ans(s, source="classifier", ref="c-t-00000000-1", status="ok_partial", consulted=False,
                   route="design", confidence=0.8, first_rung=None, unsure_fields=())
        got = json.loads(json.dumps(s.route_summary(ans)))
        self.assertEqual(got, {"mode": "shadow", "ref": "c-t-00000000-1", "status": "ok_partial",
                               "consulted": False, "route": "design", "type": "docs", "difficulty": "easy",
                               "risk": "low", "risk_tags": ["deps"], "acted_fields": [], "unsure_fields": [],
                               "confidence": 0.8})

    def test_task_summary(self):
        s = _shadow(self)
        self.assertEqual(s.task_summary(_ans(s)), {"route": "cascade", "first_rung": "G-m"})
        self.assertEqual(s.task_summary(_ans(s, status="cache_hit")), {"route": "cascade", "first_rung": "G-m"})
        clf = _ans(s, source="classifier", ref="c-t-00000000-1", status="ok_partial", confidence=0.6,
                   first_rung=None)
        self.assertEqual(s.task_summary(clf), {"route": "cascade"})
        for status in ("timeout", "skipped_confident", "budget_denied"):
            self.assertIsNone(s.task_summary(_ans(s, status=status)), status)
        self.assertIsNone(s.task_summary(None))


class RouteRecordTest(unittest.TestCase):
    def setUp(self):
        self.cfg = from_dict(defaults())
        self.task = _task()
        self.f = _features(type="bugfix", type_hits={"bugfix": 1}, confident=False)
        self.d = router.decide(self.task, self.f, self.cfg)

    def _record(self, **kw):
        try:
            return router.route_record(self.task, self.f, self.d, **kw)
        except TypeError as exc:
            self.fail(f"route_record does not take judge/clf: {exc}")

    def test_route_record_none_args_byte_identical(self):
        plain = router.route_record(self.task, self.f, self.d)
        got = self._record(judge=None, clf=None)
        self.assertEqual(json.dumps(got), json.dumps(plain))

    def test_route_record_carries_shadow_summaries(self):
        s = _shadow(self)
        judge = _ans(s, route="design", difficulty="hard")
        clf = _ans(s, source="classifier", ref="c-t-00000000-1", route="design", confidence=0.9, first_rung=None)
        self.assertEqual(router.decide(self.task, self.f, self.cfg, judge=judge, clf=clf), self.d)
        plain = router.route_record(self.task, self.f, self.d)
        rec = self._record(judge=judge, clf=clf)
        self.assertEqual(rec["judge"], s.route_summary(judge))
        self.assertEqual(rec["classifier"], s.route_summary(clf))
        self.assertEqual(rec["applied"], plain["applied"])
        self.assertEqual(rec["rules"], plain["rules"])
        self.assertEqual(rec["applied"]["route"], self.d.route)


class SchemaTest(unittest.TestCase):
    def test_schema_route_requires_judge_and_classifier(self):
        check = log_schema_v2.check
        self.assertNotEqual(check(_without(ROUTE, "judge")), [])
        self.assertNotEqual(check(_without(ROUTE, "classifier")), [])
        self.assertNotEqual(check({**ROUTE, "judge": "yes"}), [])
        self.assertNotEqual(check({**ROUTE, "classifier": [1]}), [])
        self.assertEqual(check({**ROUTE, "judge": {"mode": "shadow"}}), [])
        self.assertEqual(check(ROUTE), [])

    def test_schema_task_shadow_fields(self):
        check = log_schema_v2.check
        self.assertNotEqual(check(_without(TASK, "shadow")), [])
        self.assertNotEqual(check({**TASK, "shadow": None}), [])
        self.assertNotEqual(check({**TASK, "shadow": {"judge": None}}), [])
        self.assertNotEqual(check({**TASK, "shadow": {"judge": "x", "classifier": None}}), [])
        self.assertNotEqual(check(_without(TASK, "text_redacted")), [])
        self.assertNotEqual(check({**TASK, "text_redacted": 5}), [])
        self.assertNotEqual(check(_without(TASK, "feature_hash")), [])
        self.assertNotEqual(check({**TASK, "feature_hash": 12}), [])
        self.assertEqual(check({**TASK, "text_redacted": None, "feature_hash": None}), [])
        self.assertEqual(check({**TASK, "shadow": {"judge": {"route": "cascade"}, "classifier": None}}), [])
        self.assertEqual(check(TASK), [])

    def test_schema_judge_role(self):
        check = log_schema_v2.check
        self.assertEqual(check(JUDGE), [])
        for key in ("task", "ref", "status", "mode", "latency_ms", "use", "qset", "model_req"):
            self.assertNotEqual(check(_without(JUDGE, key)), [], key)
        self.assertNotEqual(check({**JUDGE, "latency_ms": "slow"}), [])
        self.assertNotEqual(check({**JUDGE, "latency_ms": True}), [])
        self.assertNotEqual(check({**JUDGE, "qset": 1}), [])
        self.assertEqual(check({**JUDGE, "latency_ms": None}), [])

    def test_schema_classifier_role(self):
        check = log_schema_v2.check
        self.assertEqual(check(CLF), [])
        for key in ("task", "ref", "status", "mode", "latency_ms", "model"):
            self.assertNotEqual(check(_without(CLF, key)), [], key)
        self.assertNotEqual(check({**CLF, "model": None}), [])
        self.assertEqual(check({**CLF, "latency_ms": 1200}), [])
        self.assertIn("unknown role", check({**CLF, "role": "jury"}))


class RunTaskRecordTest(RunCase):
    def set_scenario(self, scenario, check=None):
        super().set_scenario(scenario, check)
        self.env["MR_HOME"] = self.home

    def _features_hash(self, text):
        task = Task(id="t-00000000", text=text, repo=self.repo, base="a" * 40, params=Params(),
                    files_named=prepass.path_tokens(text), source="cli")
        feats = dataclasses.asdict(prepass.extract(task, self.repo))
        doc = {k: sorted(v) if isinstance(v, (set, frozenset)) else v for k, v in feats.items()}
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]

    def test_task_record_has_redacted_text_and_feature_hash(self):
        text = f"fix typo in README.md (ref {FAKE_KEY})"
        want = self._features_hash(text)
        self.cursor({"files": {"README.md": "# test repo\nfixed typo\n"}})
        self.mr(text=text)
        task = self.only_task()
        self.assertIn("text_redacted", task)
        self.assertIsInstance(task["text_redacted"], str)
        self.assertNotIn(FAKE_KEY, task["text_redacted"])
        self.assertIn("<redacted:token>", task["text_redacted"])
        self.assertTrue(task["text_redacted"].startswith("fix typo in README.md"), task["text_redacted"])
        self.assertEqual(task.get("feature_hash"), want)
        self.assertEqual(task["shadow"], {"judge": None, "classifier": None})
        self.assertEqual(task["attempts"]["classifier"], 0)
        self.assertEqual(log_schema_v2.check(task), [])
        routes = self.records("route")
        self.assertEqual(len(routes), 1)
        self.assertIsNone(routes[0]["judge"])
        self.assertIsNone(routes[0]["classifier"])
        self.assert_schema()

    def test_task_record_text_capped_at_4000(self):
        text = "fix typo in README.md " + "please " * 800
        self.cursor({"files": {"README.md": "# test repo\nfixed typo\n"}})
        self.mr(text=text)
        task = self.only_task()
        self.assertIn("text_redacted", task)
        self.assertEqual(task["text_redacted"], text[:4000])
        self.assertEqual(log_schema_v2.check(task), [])


if __name__ == "__main__":
    unittest.main()
