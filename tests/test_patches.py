import glob
import hashlib
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from mr import patches as P
from tests import gitfix

LOCKED = {
    "src/__tests__/a.mr.spec.ts": "it('a', () => expect(1).toBe(2))\n",
    "src/__tests__/b.mr.spec.ts": "it('b', () => expect(2).toBe(3))\n",
}


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _write(root, rel, data):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


def _snapshot(root):
    """{relpath: (sha256, mode)} for every file outside .git."""
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        for name in files:
            p = os.path.join(d, name)
            out[os.path.relpath(p, root)] = (_sha(p), stat.S_IMODE(os.stat(p).st_mode))
    return out


class Base(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {k: v for k, v in gitfix._GIT_ENV.items()})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.mkdtemp(prefix="mr-patch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.repo = gitfix.make_repo(os.path.join(self.tmp, "repo"), {
            "src/app.ts": "export const x = 1;\n",
            "src/old.ts": "export const old = 1;\n",
            "scripts/run.sh": "echo hi\n",
        })
        self.index = os.path.join(self.repo, ".git", "index")

    def index_state(self):
        st = os.stat(self.index)
        return (_sha(self.index), st.st_mtime_ns)

    def rung_edits(self):
        _write(self.repo, "src/app.ts", "export const x = 2;\n")
        _write(self.repo, "src/new.ts", "export const n = 1;\n")
        _write(self.repo, "assets/logo.bin", bytes(range(256)) * 4 + b"\0\xff")
        os.remove(os.path.join(self.repo, "src/old.ts"))
        os.chmod(os.path.join(self.repo, "scripts/run.sh"), 0o755)


class TestPatch(Base):
    def test_new_file_in_patch(self):
        head = gitfix.head(self.repo)
        idx = self.index_state()
        reset_tree = P.take_reset_tree(self.repo)
        self.rung_edits()
        data = P.cumulative_diff(self.repo, reset_tree)
        for needle in (b"b/src/app.ts", b"new file mode", b"b/src/new.ts", b"GIT binary patch",
                       b"b/assets/logo.bin", b"deleted file mode", b"a/src/old.ts", b"new mode 100755"):
            self.assertIn(needle, data)
        path = P.save_patch(self.home, "t-1", 1, "grok-4.7", data)
        self.assertEqual(path, os.path.join(self.home, "patches", "t-1", "r1-grok-4.7.patch"))
        self.assertEqual(P.apply_to_tree(self.repo, reset_tree, path), P.take_reset_tree(self.repo))
        self.assertEqual(self.index_state(), idx)
        self.assertEqual(gitfix.head(self.repo), head)
        self.assertEqual(gitfix.git(self.repo, "diff", "--cached", "--quiet", check=False).returncode, 0)

    def test_gitignored_excluded(self):
        reset_tree = P.take_reset_tree(self.repo)
        _write(self.repo, "build/out.js", "artifact\n")
        _write(self.repo, "src/app.ts", "export const x = 3;\n")
        data = P.cumulative_diff(self.repo, reset_tree)
        self.assertIn(b"src/app.ts", data)
        self.assertNotIn(b"build/", data)
        ls = gitfix.git(self.repo, "ls-tree", "-r", "--name-only", P.take_reset_tree(self.repo)).stdout
        self.assertNotIn("build/out.js", ls)

    def test_empty(self):
        reset_tree = P.take_reset_tree(self.repo)
        self.assertEqual(P.cumulative_diff(self.repo, reset_tree), b"")
        r = P.reset(self.repo, reset_tree, None, self.home, "t-1")
        self.assertEqual((r.ok, r.reason), (True, "nothing to reset"))

    def test_numstat(self):
        reset_tree = P.take_reset_tree(self.repo)
        self.rung_edits()
        ns = P.numstat(P.cumulative_diff(self.repo, reset_tree))
        # app.ts +1/-1, new.ts +1, logo.bin binary 0/0, old.ts -1, run.sh mode only 0/0
        self.assertEqual(ns, {"files": 5, "add": 2, "del": 2})
        self.assertEqual(P.numstat(b""), {"files": 0, "add": 0, "del": 0})

    def test_save_patch_rejects_unsafe_names(self):
        with self.assertRaises(ValueError):
            P.save_patch(self.home, "../x", 1, "m", b"")
        with self.assertRaises(ValueError):
            P.save_patch(self.home, "t-1", 1, "a/b", b"")


class TestReset(Base):
    def _locked(self):
        for rel, body in LOCKED.items():
            _write(self.repo, rel, body)
        return {rel: _sha(os.path.join(self.repo, rel)) for rel in LOCKED}

    def _check_reset_keeps_locked(self, commit_locked):
        hashes = self._locked()
        if commit_locked:
            gitfix.git(self.repo, "add", "-A")
            gitfix.git(self.repo, "commit", "-m", "locked tests")
        head = gitfix.head(self.repo)
        reset_tree = P.take_reset_tree(self.repo)
        before = _snapshot(self.repo)
        idx = self.index_state()
        self.rung_edits()
        _write(self.repo, "src/__tests__/helper.ts", "export {}\n")
        path = P.save_patch(self.home, "t-1", 1, "grok-4.7", P.cumulative_diff(self.repo, reset_tree))
        self.assertNotIn(b"a.mr.spec.ts", open(path, "rb").read())
        r = P.reset(self.repo, reset_tree, path, self.home, "t-1")
        self.assertTrue(r.ok, r.reason)
        for rel, h in hashes.items():
            self.assertEqual(_sha(os.path.join(self.repo, rel)), h)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "src/new.ts")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "assets/logo.bin")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "src/__tests__/helper.ts")))
        self.assertEqual(_snapshot(self.repo), before)
        self.assertEqual(P.cumulative_diff(self.repo, reset_tree), b"")
        self.assertEqual(self.index_state(), idx)
        self.assertEqual(gitfix.head(self.repo), head)

    def test_reset_keeps_locked_tests_w7(self):
        self._check_reset_keeps_locked(commit_locked=False)

    def test_reset_keeps_locked_tests_w5(self):
        gitfix.add_branch(self.repo, "mr/t-1-demo", checkout=True)
        self._check_reset_keeps_locked(commit_locked=True)
        self.assertEqual(gitfix.git(self.repo, "symbolic-ref", "--short", "HEAD").stdout.strip(), "mr/t-1-demo")

    def test_human_edit_exit4(self):
        self._locked()
        reset_tree = P.take_reset_tree(self.repo)
        self.rung_edits()
        path = P.save_patch(self.home, "t-1", 1, "grok-4.7", P.cumulative_diff(self.repo, reset_tree))
        _write(self.repo, "README.md", "# edited by Alice\n")
        before = _snapshot(self.repo)
        r = P.reset(self.repo, reset_tree, path, self.home, "t-1")
        self.assertFalse(r.ok)
        self.assertIn("human edit", r.reason)
        self.assertEqual(_snapshot(self.repo), before)
        human = glob.glob(os.path.join(self.home, "patches", "t-1", "r-human-*.patch"))
        self.assertEqual(len(human), 1)
        self.assertIn(human[0], r.saved)
        self.assertIn(path, r.saved)
        self.assertIn(b"edited by Alice", open(human[0], "rb").read())

    def test_reset_missing_last_patch(self):
        reset_tree = P.take_reset_tree(self.repo)
        self.rung_edits()
        before = _snapshot(self.repo)
        missing = os.path.join(self.home, "patches", "t-1", "r9-none.patch")
        for last in (None, missing):
            r = P.reset(self.repo, reset_tree, last, self.home, "t-1")
            self.assertFalse(r.ok)
            self.assertEqual(_snapshot(self.repo), before)

    def test_reset_check_fails(self):
        # Equal bytes but a patch that cannot reverse-apply: build one against a different base.
        reset_tree = P.take_reset_tree(self.repo)
        _write(self.repo, "src/app.ts", "export const x = 2;\n")
        data = P.cumulative_diff(self.repo, reset_tree)
        path = P.save_patch(self.home, "t-1", 1, "m", data)
        with mock.patch.object(P, "cumulative_diff", return_value=data):
            _write(self.repo, "src/app.ts", "totally different\n")
            before = _snapshot(self.repo)
            r = P.reset(self.repo, reset_tree, path, self.home, "t-1")
        self.assertFalse(r.ok)
        self.assertIn("check failed", r.reason)
        self.assertEqual(_snapshot(self.repo), before)


class TestFinal(Base):
    def test_final_md(self):
        p = P.write_final_md(self.home, "t-1", outcome="not accepted", exit_code=1, reason="ladder exhausted",
                             rungs=[{"rung": 1, "model": "grok-4.7", "class": "tried_wrong",
                                     "signals": {"A": "fail", "H": "pass"}, "patch": "/x/r1-grok-4.7.patch"},
                                    {"rung": 2, "model": "kimi-k3", "class": "off_track", "signals": "D|fail",
                                     "patch": "/x/r2-kimi-k3.patch"}],
                             patch="/x/r2-kimi-k3.patch", reset_tree="abc123", base="def456")
        self.assertEqual(p, os.path.join(self.home, "patches", "t-1", "FINAL.md"))
        text = open(p, encoding="utf-8").read()
        self.assertTrue(text.startswith("# mr task t-1"))
        for s in ("not accepted (exit 1)", "ladder exhausted", "| rung | model | class | signals | patch |",
                  "| 1 | grok-4.7 | tried_wrong | A=fail, H=pass | /x/r1-grok-4.7.patch |",
                  "D\\|fail", "git apply /x/r2-kimi-k3.patch", "abc123", "def456"):
            self.assertIn(s, text)


if __name__ == "__main__":
    unittest.main()
