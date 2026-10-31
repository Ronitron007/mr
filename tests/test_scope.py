"""Signal D: diff scope (spec §5.9, §10.6, AC-SIG-1 D half, AC-SIG-5)."""
import os
import shutil
import tempfile
import unittest

from mr import patches, scope
from tests import gitfix


def _write(root, rel, text):
    path = os.path.join(root, rel)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mr-scope-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.n = 0

    def patch(self, before, after) -> bytes:
        self.n += 1
        repo = gitfix.make_repo(os.path.join(self.tmp, f"r{self.n}"), before)
        reset = patches.take_reset_tree(repo)
        for rel, text in after.items():
            _write(repo, rel, text)
        return patches.cumulative_diff(repo, reset)

    def test_locked_test_edit(self):
        # AC-SIG-1, D half: a locked test in the patch always fails D.
        diff = self.patch(
            {"src/locked.test.ts": "it('a', () => expect(1).toBe(2))\n"},
            {"src/locked.test.ts": "it('a', () => expect(1).toBe(1))\n"},
        )
        sig = scope.scope(
            diff, checklist=None, difficulty="trivial",
            files_named=("src/locked.test.ts",),
            locked_files=("src/locked.test.ts",),
        )
        self.assertFalse(sig.ok)
        self.assertEqual(sig.reasons, ("locked test edited: src/locked.test.ts",))
        self.assertIn("src/locked.test.ts", sig.files)

    def test_out_of_scope_checklist(self):
        diff = self.patch(
            {"src/a.ts": "export const a = 1\n"},
            {"src/c.ts": "export const c = 1\n"},
        )
        checklist = {
            "est_loc": 100,
            "scope": {"modify": ["src/a.ts"], "create": ["src/b.ts"], "forbidden": []},
        }
        sig = scope.scope(
            diff, checklist=checklist, difficulty="medium", files_named=(),
        )
        self.assertFalse(sig.ok)
        self.assertIn("out of scope: src/c.ts", sig.reasons)
        self.assertEqual(sig.est_loc, 100)

    def test_sibling_dir(self):
        diff = self.patch(
            {"src/a.ts": "export const a = 1\n"},
            {"src/b.ts": "export const b = 1\n", "lib/c.ts": "export const c = 1\n"},
        )
        sig = scope.scope(
            diff, checklist=None, difficulty="easy", files_named=("src/a.ts",),
        )
        self.assertFalse(sig.ok)
        self.assertIn("src/b.ts", sig.files)
        self.assertIn("lib/c.ts", sig.files)
        self.assertIn("out of scope: lib/c.ts", sig.reasons)
        self.assertFalse(any("src/b.ts" in r for r in sig.reasons))

    def test_no_files_named(self):
        diff = self.patch(
            {},
            {"src/ok.ts": "export const ok = 1\n", "package.json": "{}\n"},
        )
        sig = scope.scope(
            diff, checklist=None, difficulty="trivial", files_named=(),
        )
        self.assertFalse(sig.ok)
        self.assertIn("src/ok.ts", sig.files)
        self.assertIn("package.json", sig.files)
        self.assertIn("dependency/config file: package.json", sig.reasons)
        self.assertFalse(any("src/ok.ts" in r for r in sig.reasons))

    def test_too_many_files(self):
        diff = self.patch({}, {
            "src/a.ts": "export const a = 1\n",
            "src/b.ts": "export const b = 1\n",
            "src/c.ts": "export const c = 1\n",
        })
        sig = scope.scope(
            diff, checklist=None, difficulty="trivial", files_named=(),
        )
        self.assertFalse(sig.ok)
        self.assertIn("too many files: 3 > 2", sig.reasons)
        self.assertEqual(sig.est_loc, 40)

    def test_too_large(self):
        body = "".join(f"export const x{i} = {i}\n" for i in range(81))
        diff = self.patch({}, {"src/big.ts": body})
        sig = scope.scope(
            diff, checklist=None, difficulty="trivial", files_named=("src/big.ts",),
        )
        self.assertFalse(sig.ok)
        self.assertGreater(sig.loc, 2 * sig.est_loc)
        self.assertIn("too large: loc > 2×est", sig.reasons)
        self.assertEqual(sig.est_loc, 40)

    def test_forbidden_glob(self):
        diff = self.patch({}, {"secret/nested/x.txt": "x\n"})
        sig = scope.scope(
            diff, checklist=None, difficulty="trivial",
            files_named=("secret/nested/x.txt",),
            forbidden=("secret/**",),
        )
        self.assertFalse(sig.ok)
        self.assertEqual(sig.reasons, ("forbidden: secret/nested/x.txt",))

    def test_build_artifacts_ignored(self):
        # AC-SIG-5: gitignored build output is absent from the cumulative patch.
        repo = gitfix.make_repo(os.path.join(self.tmp, "art"), {"src/app.ts": "export const x = 1\n"})
        reset = patches.take_reset_tree(repo)
        _write(repo, "build/out.js", "artifact\n")
        _write(repo, "src/app.ts", "export const x = 2\n")
        diff = patches.cumulative_diff(repo, reset)
        sig = scope.scope(
            diff, checklist=None, difficulty="trivial", files_named=("src/app.ts",),
        )
        self.assertTrue(sig.ok)
        self.assertEqual(sig.reasons, ())
        self.assertIn("src/app.ts", sig.files)
        self.assertNotIn("build/out.js", sig.files)
