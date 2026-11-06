import itertools
import json
import multiprocessing
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mr import isolation as iso
from mr import pipeline
from mr.log import Log
from mr.prepass import Features, confident, floors
from mr.router import Decision
from mr.task import Params, Task
from tests import gitfix

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "p0", "isolation_table.json")
KEYS = ["RO", "OTHER_WRITER", "DIRTY_FOREIGN", "PARALLEL", "EXPERIMENT", "HIGH_RISK", "DEPS", "PROTECTED", "OWNED_BRANCH"]


def _cfg(home):
    return SimpleNamespace(paths=SimpleNamespace(home=home),
                           worktrees={"protected": ["main", "master", "release/*", "production"]})


def _hold(cfg, repo, ready, stop):
    lock = iso.WriterLock(cfg, repo, "t-child")
    if lock.acquire():
        ready.set()
        stop.wait(20)
        lock.release()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mr-iso-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = gitfix.make_repo(os.path.join(self.tmp, "repo"))
        self.home = os.path.join(self.tmp, "home")
        self.cfg = _cfg(self.home)


class TestTable(unittest.TestCase):
    def test_table(self):
        with open(FIXTURE, encoding="utf-8") as f:
            rows = json.load(f)
        self.assertEqual(len(rows), 16)
        for row in rows:
            with self.subTest(row["id"]):
                got = iso.decide_isolation(iso.IsoInputs(**row["inputs"]), high_isolation="W4")  # the old table
                self.assertEqual(got, row["expect"], row["note"])

    def test_table_property(self):
        order = [("W0", lambda v: v["RO"]), ("W1", lambda v: v["PARALLEL"] or v["OTHER_WRITER"]),
                 ("W2", lambda v: v["DIRTY_FOREIGN"]), ("W3", lambda v: v["EXPERIMENT"]),
                 ("W5", lambda v: v["HIGH_RISK"] or v["DEPS"]), ("W5", lambda v: v["PROTECTED"]),
                 ("W6", lambda v: v["OWNED_BRANCH"]), ("W7", lambda v: True)]
        count = 0
        for combo in itertools.product([False, True], repeat=9):
            vals = dict(zip(KEYS, combo))
            want = next(code for code, cond in order if cond(vals))
            self.assertEqual(iso.decide_isolation(iso.IsoInputs(**vals)), want, vals)
            count += 1
        self.assertEqual(count, 512)

    def test_unsupported_reason(self):
        for code in ("W0", "W5", "W6", "W7"):
            self.assertIsNone(iso.unsupported_reason(code))
        self.assertEqual(iso.unsupported_reason("W2"),
                         "W2 (foreign dirty tree) needs a worktree; worktrees are not built in v1")
        for code in ("W1", "W3", "W4"):
            self.assertIn("not built in v1", iso.unsupported_reason(code))


class TestSnapshot(Base):
    def snap(self):
        return iso.snapshot(self.repo)

    def test_ref_snapshot(self):
        g = lambda *a: gitfix.git(self.repo, *a)
        cases = {
            "new branch": lambda: g("branch", "other"),
            "tag": lambda: g("tag", "v1"),
            "remote ref": lambda: g("update-ref", "refs/remotes/origin/x", "HEAD"),
            "stash": lambda: (gitfix.make_foreign_dirty(self.repo, tracked=True), g("stash")),
            "worktree": lambda: gitfix.add_worktree(self.repo, os.path.join(self.tmp, "wt"), "wtb"),
            "switch": lambda: (g("branch", "sw"), g("switch", "sw")),
            "commit": lambda: gitfix.commit(self.repo, {"a.txt": "a\n"}, "more"),
        }
        for name, act in cases.items():
            with self.subTest(name):
                before = self.snap()
                act()
                self.assertIsNotNone(iso.violation(before, self.snap()))
        # a fresh repo: file edits and untracked files are not violations
        self.repo = gitfix.make_repo(os.path.join(self.tmp, "repo2"))
        before = self.snap()
        with open(os.path.join(self.repo, "README.md"), "a") as f:
            f.write("edit\n")
        gitfix.make_foreign_dirty(self.repo)
        self.assertIsNone(iso.violation(before, self.snap()))

    def test_allow_new_branch(self):
        before = self.snap()
        gitfix.git(self.repo, "branch", "mr/ok")
        gitfix.git(self.repo, "branch", "mr/bad")
        after = self.snap()
        self.assertIsNotNone(iso.violation(before, after, allow_new_branch="mr/ok"))
        gitfix.git(self.repo, "branch", "-D", "mr/bad")
        self.assertIsNone(iso.violation(before, self.snap(), allow_new_branch="mr/ok"))
        self.assertIsNotNone(iso.violation(before, self.snap()))

    def test_snapshot_fields(self):
        gitfix.make_foreign_dirty(self.repo)
        s = self.snap()
        self.assertEqual(s.branch, "main")
        self.assertEqual(s.head, gitfix.head(self.repo))
        self.assertIn("?? foreign.txt", s.status)
        self.assertEqual(s.refs, ("refs/heads/main",))
        self.assertEqual(len(s.worktrees), 1)
        gitfix.git(self.repo, "checkout", "--detach")
        self.assertIsNone(self.snap().branch)


class TestInputs(Base):
    def inputs(self, **kw):
        base = dict(ro=False, risk="low", difficulty="easy", risk_tags=(), experiment=False,
                    other_writer=False, cfg=self.cfg)
        base.update(kw)
        return iso.compute_inputs(self.repo, **base)

    def test_dirty_foreign_untracked(self):
        self.assertFalse(self.inputs().DIRTY_FOREIGN)
        os.makedirs(os.path.join(self.repo, "build"))
        with open(os.path.join(self.repo, "build", "x.o"), "w") as f:
            f.write("ignored\n")
        self.assertFalse(self.inputs().DIRTY_FOREIGN)
        gitfix.make_foreign_dirty(self.repo)
        self.assertTrue(self.inputs().DIRTY_FOREIGN)

    def test_high_risk_and_deps(self):
        self.assertFalse(self.inputs().HIGH_RISK)
        self.assertFalse(self.inputs(risk="high").HIGH_RISK)
        self.assertFalse(self.inputs(difficulty="critical").HIGH_RISK)
        self.assertFalse(self.inputs(est_files=8).HIGH_RISK)
        self.assertFalse(self.inputs(est_files=20).HIGH_RISK)
        self.assertFalse(self.inputs(est_files=7, est_loc=399).HIGH_RISK)
        self.assertFalse(self.inputs(est_loc=400).HIGH_RISK)
        for tag in ("auth", "payments", "migration", "data-deletion", "deps", "native-config"):
            self.assertFalse(self.inputs(risk="high", risk_tags=[tag]).HIGH_RISK, "text tag " + tag)
            self.assertTrue(self.inputs(path_tags=[tag]).HIGH_RISK, "path tag " + tag)
        self.assertFalse(self.inputs(path_tags=["secrets"]).HIGH_RISK)
        self.assertTrue(self.inputs(path_tags=["deps"]).DEPS)
        self.assertFalse(self.inputs(risk_tags=["deps"]).DEPS)
        self.assertFalse(self.inputs(path_tags=["migration"]).DEPS)
        self.assertTrue(self.inputs(difficulty="critical").NEEDS_BRANCH)
        self.assertTrue(self.inputs(est_loc=400).NEEDS_BRANCH)
        self.assertFalse(self.inputs(est_files=20, est_loc=399).NEEDS_BRANCH)
        self.assertEqual(floors(frozenset(), 8), ("low", "hard"))
        self.assertEqual(floors(frozenset(), 20), ("low", "hard"))
        self.assertEqual(floors(frozenset(), 7), ("low", "trivial"))
        self.assertTrue(confident({"refactor": 1}, frozenset(), 8, 1))
        gitfix.git(self.repo, "switch", "-c", "feature/plain")
        for count in (8, 20):
            got = self.inputs(est_files=count)
            self.assertFalse(got.HIGH_RISK, count)
            self.assertFalse(got.DEPS, count)
            self.assertEqual(iso.decide_isolation(got), "W7", count)
            self.assertIsNone(iso.unsupported_reason("W7"))
        wide = self.inputs(est_loc=400)
        self.assertFalse(wide.HIGH_RISK)
        self.assertEqual(iso.decide_isolation(wide), "W5")

    def test_nine_files_pipeline_is_w7(self):
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
            files=named,
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
            files={"change": [{"path": p, "src": "named"} for p in named], "new": [], "read": [], "partial": True},
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
        self.assertNotEqual(ctx.exit, 4)

    def test_protected_and_owned(self):
        self.assertTrue(self.inputs().PROTECTED)
        self.assertFalse(self.inputs(owned_branch="mr/x").OWNED_BRANCH)
        gitfix.git(self.repo, "switch", "-c", "mr/x")
        i = self.inputs(owned_branch="mr/x")
        self.assertFalse(i.PROTECTED)
        self.assertTrue(i.OWNED_BRANCH)
        self.assertFalse(self.inputs(owned_branch="mr/other").OWNED_BRANCH)
        gitfix.git(self.repo, "switch", "-c", "feature/y")
        self.assertFalse(self.inputs(owned_branch="feature/y").OWNED_BRANCH)

    def test_protected(self):
        pats = self.cfg.worktrees["protected"]
        for b in ("main", "master", "release/1.2", "production", None):
            self.assertTrue(iso.protected(b, pats), b)
        for b in ("mr/x", "feature/y"):
            self.assertFalse(iso.protected(b, pats), b)


class TestBranch(Base):
    def test_branch_name(self):
        now = datetime(2026, 10, 29, 19, 30, tzinfo=timezone.utc)  # 01:00 IST on the 30th
        self.assertEqual(iso.branch_name("t-abcd1234", "Add Mute  Settings!!", now), "mr/20261030-0100-add-mute-settings-1234")
        long = iso.branch_name("t-abcd1234", "implement the extremely long feature name here", now)
        slug = long.split("-", 2)[2].rsplit("-", 1)[0]
        self.assertLessEqual(len(slug), 24)
        self.assertRegex(iso.branch_name("t-00000042", "x"), r"^mr/\d{8}-\d{4}-x-0042$")
        self.assertRegex(iso.branch_name("t-00000042", "!!!"), r"-task-0042$")
        naive = datetime(2026, 1, 2, 3, 4)
        self.assertTrue(iso.branch_name("abcd", "a", naive).startswith("mr/20260102-0304-"))

    def test_create_branch_w5(self):
        before = gitfix.head(self.repo)
        orig, new = iso.create_branch_w5(self.repo, "t-abcd1234", "fix the thing")
        self.assertEqual(orig, "main")
        self.assertRegex(new, r"^mr/\d{8}-\d{4}-fix-the-thing-1234$")
        snap = iso.snapshot(self.repo)
        self.assertEqual(snap.branch, new)
        self.assertEqual(snap.head, before)
        self.assertIsNone(iso.violation(iso.GitSnap(snap.head, new, snap.worktrees, snap.status,
                                                    tuple(r for r in snap.refs if r != f"refs/heads/{new}"),
                                                    snap.stash), snap, allow_new_branch=new))
        gitfix.git(self.repo, "checkout", "--detach")
        orig2, _ = iso.create_branch_w5(self.repo, "t-abcd5678", "again")
        self.assertEqual(orig2, before)


class TestWriterLock(Base):
    def test_writer_lock(self):
        self.assertFalse(iso.is_held(self.cfg, self.repo))
        ctx = multiprocessing.get_context("fork")
        ready, stop = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_hold, args=(self.cfg, self.repo, ready, stop), daemon=True)
        proc.start()
        try:
            self.assertTrue(ready.wait(10))
            self.assertTrue(iso.is_held(self.cfg, self.repo))
            lock = iso.WriterLock(self.cfg, self.repo, "t-parent")
            self.assertFalse(lock.acquire())
            lock.release()  # must not clear the child's content
            with open(lock.path) as f:
                self.assertEqual(json.load(f)["task"], "t-child")
        finally:
            stop.set()
            proc.join(10)
        self.assertEqual(os.path.getsize(lock.path), 0)
        self.assertFalse(iso.is_held(self.cfg, self.repo))
        lock = iso.WriterLock(self.cfg, self.repo, "t-parent")
        self.assertTrue(lock.acquire())
        with open(lock.path) as f:
            self.assertEqual(json.load(f), {"pid": os.getpid(), "task": "t-parent"})
        self.assertTrue(iso.is_held(self.cfg, self.repo))
        lock.release()
        lock.release()
        self.assertEqual(os.path.getsize(lock.path), 0)
        with iso.WriterLock(self.cfg, self.repo, "t-ctx") as again:
            self.assertTrue(again.held)
        self.assertEqual(os.path.getsize(lock.path), 0)

    def test_stale_lock(self):
        lock = iso.WriterLock(self.cfg, self.repo, "t-new")
        os.makedirs(os.path.dirname(lock.path))
        with open(lock.path, "w") as f:
            json.dump({"pid": 424242, "task": "t-crashed"}, f)
        with self.assertRaises(iso.StaleLock) as cm:
            lock.acquire()
        self.assertEqual((cm.exception.task, cm.exception.pid, cm.exception.path), ("t-crashed", 424242, lock.path))
        self.assertFalse(iso.is_held(self.cfg, self.repo))  # flock was dropped
        with open(lock.path) as f:  # content untouched until `mr unlock`
            self.assertEqual(json.load(f)["task"], "t-crashed")

    def test_leftover_w7_placeholder(self):
        self.assertIsNone(iso.leftover_w7(self.cfg, self.repo))


if __name__ == "__main__":
    unittest.main()
