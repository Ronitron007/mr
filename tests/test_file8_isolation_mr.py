"""FILE8 locked checks: naming 8 or more files is not an isolation refusal."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from mr import isolation, pipeline
from mr.log import Log
from mr.prepass import Features, confident, floors
from mr.router import Decision
from mr.task import Params, Task
from tests import gitfix

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISOLATION_PY = os.path.join(ROOT, "mr", "isolation.py")


def _cfg(home):
    return SimpleNamespace(
        paths=SimpleNamespace(home=home),
        worktrees={"protected": ["main", "master", "release/*", "production"]},
    )


class TestFile8(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mr-file8-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = gitfix.make_repo(os.path.join(self.tmp, "repo"))
        self.home = os.path.join(self.tmp, "home")
        self.cfg = _cfg(self.home)

    def inputs(self, **kw):
        base = dict(
            ro=False,
            risk="low",
            difficulty="easy",
            risk_tags=(),
            experiment=False,
            other_writer=False,
            cfg=self.cfg,
        )
        base.update(kw)
        return isolation.compute_inputs(self.repo, **base)

    def test_est_files_does_not_raise_high_risk(self):
        self.assertEqual(floors(frozenset(), 8), ("low", "hard"))
        self.assertEqual(floors(frozenset(), 20), ("low", "hard"))
        self.assertEqual(floors(frozenset(), 7), ("low", "trivial"))
        self.assertEqual(floors(frozenset({"auth"}), 8), ("high", "hard"))
        self.assertTrue(confident({"refactor": 1}, frozenset(), 8, 1))
        self.assertFalse(confident({"refactor": 1}, frozenset(), 7, 0))
        self.assertFalse(self.inputs(est_files=7, est_loc=399).NEEDS_BRANCH)
        self.assertTrue(self.inputs(est_loc=400).NEEDS_BRANCH)
        self.assertFalse(self.inputs(risk="high", est_files=1).HIGH_RISK)
        self.assertTrue(self.inputs(difficulty="critical", est_files=1).NEEDS_BRANCH)
        for tag in ("deps", "migration", "native-config", "auth"):
            self.assertTrue(self.inputs(path_tags=[tag], est_files=1).HIGH_RISK, tag)
            self.assertFalse(self.inputs(risk_tags=[tag], est_files=1).HIGH_RISK, tag)
        self.assertFalse(self.inputs(est_files=8).HIGH_RISK)
        self.assertFalse(self.inputs(est_files=20).HIGH_RISK)
        self.assertFalse(self.inputs(est_files=8, est_loc=399).NEEDS_BRANCH)
        with open(ISOLATION_PY, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("est_loc >= 400", source)
        self.assertIn("est_files", source)
        self.assertNotIn("est_files >= 8", source)

    def test_many_files_on_plain_branch_is_w7(self):
        gitfix.git(self.repo, "switch", "-c", "feature/plain")
        kept = self.inputs(est_loc=400)
        self.assertTrue(kept.NEEDS_BRANCH)
        self.assertFalse(kept.HIGH_RISK)
        self.assertFalse(kept.PROTECTED)
        self.assertFalse(kept.DIRTY_FOREIGN)
        self.assertEqual(isolation.decide_isolation(kept), "W5")
        self.assertIn("not built in v1", isolation.unsupported_reason("W4"))
        for count in (8, 20):
            got = self.inputs(est_files=count)
            self.assertFalse(got.HIGH_RISK, count)
            self.assertFalse(got.PROTECTED, count)
            self.assertFalse(got.OWNED_BRANCH, count)
            self.assertFalse(got.DEPS, count)
            self.assertFalse(got.DIRTY_FOREIGN, count)
            self.assertEqual(isolation.decide_isolation(got), "W7", count)
            self.assertIsNone(isolation.unsupported_reason("W7"))

    def test_pipeline_nine_files_not_refused(self):
        gitfix.git(self.repo, "switch", "-c", "feature/plain")
        named = tuple("src/m%d.py" % i for i in range(9))
        task = Task(
            id="t-file8001",
            text="Rename helpers in nine plain modules",
            repo=self.repo,
            base=gitfix.head(self.repo),
            params=Params(),
            files_named=named,
            source="cli",
        )
        feats = Features(
            type_hits={"refactor": 1},
            files_named=named,
            grep_hit_files=0,
            risk_tags=frozenset(),
            risk_floor="low",
            difficulty_floor="hard",
            difficulty_est="hard",
            cross_layer=0,
            public_interface=False,
            new_module=False,
            ambiguity=0,
            judgment_keywords=False,
            confident=True,
            type="refactor",
            n_files=9,
        )
        decision = Decision(
            route="cascade",
            type="refactor",
            difficulty="hard",
            risk="low",
            risk_tags=frozenset(),
            load_bearing=False,
            high_judgment=False,
            needs_review=True,
            isolate_hint=None,
            needs_clarification=False,
            ladder=("G-l",),
            reasons=(),
            sources={
                "route": "default",
                "type": "rule",
                "difficulty": "rule",
                "risk": "rule",
                "ladder": "rule",
            },
        )
        ctx = SimpleNamespace(
            args=SimpleNamespace(experiment=False, route="cascade"),
            cfg=self.cfg,
            task=task,
            feats=feats,
            d=decision,
            log=Log(self.home),
            iso=None,
            lock=None,
            exit=None,
            reason="",
        )

        def stop(code, reason=""):
            ctx.exit = code
            ctx.reason = reason
            return code

        ctx.stop = stop
        code = pipeline.start(ctx)
        self.addCleanup(lambda: ctx.lock.release() if ctx.lock is not None else None)
        self.assertIsNone(code)
        self.assertEqual(ctx.iso, "W7")
        self.assertNotEqual(getattr(ctx, "exit", None), 4)

if __name__ == "__main__":
    unittest.main()
