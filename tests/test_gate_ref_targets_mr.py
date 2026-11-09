"""gate.ref_targets: a rung that retargets an existing branch or tag is an isolation violation.

Remote-tracking refs may move (a child `git fetch` is benign). mr's own W5 commit comes after the
last post-rung snapshot, so it never trips the check.
"""
import os
import shutil
import tempfile
import unittest

from mr import isolation as iso
from tests import gitfix
from tests.gitfix import git
from tests.test_loop_run import GL, README, LoopCase
from tests.test_run_once import RunCase


def _rev(repo, ref):
    return git(repo, "rev-parse", ref).stdout.strip()


class TestRefTargets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mr-reftargets-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = gitfix.make_repo(os.path.join(self.tmp, "repo"))
        gitfix.commit(self.repo, {"a.txt": "a\n"}, "second")

    def test_codex_repro_existing_branch_retargeted(self):
        git(self.repo, "branch", "keep-me")
        before = iso.snapshot(self.repo)
        git(self.repo, "update-ref", "refs/heads/keep-me", "HEAD~1")
        after = iso.snapshot(self.repo)
        self.assertEqual(after.head, before.head)
        self.assertEqual(iso.violation(before, after), "ref moved: refs/heads/keep-me")

    def test_tag_retargeted(self):
        git(self.repo, "tag", "v1")
        before = iso.snapshot(self.repo)
        git(self.repo, "tag", "-f", "v1", "HEAD~1")
        self.assertEqual(iso.violation(before, iso.snapshot(self.repo)), "ref moved: refs/tags/v1")

    def test_remote_tracking_ref_moved_is_not_a_violation(self):
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD~1")
        before = iso.snapshot(self.repo)
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertIsNone(iso.violation(before, iso.snapshot(self.repo)))

    def test_unmoved_refs_are_not_a_violation(self):
        git(self.repo, "branch", "keep-me")
        git(self.repo, "tag", "v1")
        before = iso.snapshot(self.repo)
        with open(os.path.join(self.repo, "README.md"), "a", encoding="utf-8") as fh:
            fh.write("edit\n")
        self.assertIsNone(iso.violation(before, iso.snapshot(self.repo)))


def _stage_keep_me(repo):
    """keep-me and origin/main sit one commit behind HEAD, so the fake can move them to HEAD."""
    git(repo, "branch", "keep-me")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    gitfix.commit(repo, {"b.txt": "b\n"}, "ahead")
    return _rev(repo, "keep-me")


class TestRetargetLoop(LoopCase):
    def test_rung_retargets_branch_stops_unsafe(self):
        old = _stage_keep_me(self.repo)
        self.cursor({"files": README, "violation": "retarget"}, model=GL)
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 4, err)
        self.assertEqual(doc["outcome"], "unsafe")
        self.assertEqual(doc["rungs"][0]["class"], "isolation_violation")
        self.assertIn("ref moved: refs/heads/keep-me", doc["reason"])
        self.assertEqual(len(self.calls()), 1)
        self.assertNotEqual(_rev(self.repo, "keep-me"), old)

    def test_rung_moves_remote_ref_is_accepted(self):
        _stage_keep_me(self.repo)
        self.cursor({"files": README, "violation": "remote_move"}, model=GL)
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 0, err)
        self.assertEqual(doc["outcome"], "accepted")


class TestRetargetW5(LoopCase):
    branch = "main"

    def test_w5_commit_with_existing_refs_is_accepted(self):
        old = _stage_keep_me(self.repo)
        git(self.repo, "tag", "v1", "keep-me")
        self.cursor({"files": README}, model=GL)
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 0, err)
        self.assertTrue(doc["branch"].startswith("mr/"), doc["branch"])
        self.assertEqual(self.state_of(doc["task"])["isolation"], "W5")
        self.assertIn(f"Mr-Task: {doc['task']}", git(self.repo, "log", "-1", "--format=%B").stdout)
        self.assertEqual((_rev(self.repo, "keep-me"), _rev(self.repo, "v1")), (old, old))


class TestRetargetOnce(RunCase):
    def test_once_rung_retargets_branch_stops_unsafe(self):
        _stage_keep_me(self.repo)
        self.cursor({"files": {"README.md": "# test repo\nx\n"}, "violation": "retarget"})
        code, out, err = self.mr(json_out=True)
        self.assertEqual(code, 4, err)
        self.assertIn("ref moved: refs/heads/keep-me", out)


if __name__ == "__main__":
    unittest.main()
