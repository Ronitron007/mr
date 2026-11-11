"""Hygiene and acceptance runs never import stale bytecode.

CPython reuses a ``__pycache__`` pyc when the source has the same size and the same whole-second mtime. An edit
from ``return 1`` to ``return 9`` in that second imports the old code, so a regression passes. Each test forces
that edit with bytecode writing on.
"""
import os
import py_compile
import shlex
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from mr import acceptance, hygiene
from tests.gitfix import git

_OLD = "def f():\n    return 1\n"
_NEW = "def f():\n    return 9\n"
_TEST = textwrap.dedent(
    """\
    import unittest

    import calc


    class TestCalc(unittest.TestCase):
        def test_f(self):
            self.assertEqual(calc.f(), 1)
    """
)
_CMD = f"{shlex.quote(sys.executable)} -m unittest test_calc"
# These commands drop PYTHONPYCACHEPREFIX, so only the purge of the tree's pycs protects them.
_NO_ENV_CMDS = (f"{shlex.quote(sys.executable)} -E -m unittest test_calc",
                f"env -i {shlex.quote(sys.executable)} -m unittest test_calc")


class TestStaleBytecode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mr-pyc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tree = os.path.join(self.tmp, "tree")
        os.makedirs(self.tree)
        self.calc = os.path.join(self.tree, "calc.py")
        self._write("calc.py", _OLD)
        self._write("test_calc.py", _TEST)
        git(self.tree, "init", "-q")
        env = mock.patch.dict(os.environ)  # bytecode writing on, whatever the shell sets
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        os.environ.pop("PYTHONPYCACHEPREFIX", None)

    def _write(self, rel, text):
        with open(os.path.join(self.tree, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def _cache_old_in_tree(self):
        """The coding agent imported calc.py, so the tree holds a pyc of the old source."""
        cfile = os.path.join(self.tree, "__pycache__", f"calc.{sys.implementation.cache_tag}.pyc")
        py_compile.compile(self.calc, cfile=cfile, doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
        return cfile

    def _regress_in_same_second(self):
        stat = os.stat(self.calc)
        self._write("calc.py", _NEW)
        os.utime(self.calc, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(os.path.getsize(self.calc), stat.st_size)

    def test_hygiene_ignores_a_stale_pyc_in_the_tree(self):
        self._cache_old_in_tree()
        self._regress_in_same_second()
        self.assertTrue(hygiene.run_checks(self.tree, [_CMD], 60))

    def test_hygiene_command_without_the_environment_ignores_a_stale_pyc(self):
        for cmd in _NO_ENV_CMDS:
            with self.subTest(cmd=cmd):
                self._write("calc.py", _OLD)
                self._cache_old_in_tree()
                self._regress_in_same_second()
                self.assertTrue(hygiene.run_checks(self.tree, [cmd], 60))

    def test_purge_keeps_tracked_files(self):
        cfile = self._cache_old_in_tree()
        note = os.path.join(self.tree, "__pycache__", "note.txt")
        self._write(os.path.join("__pycache__", "note.txt"), "untracked, not a pyc\n")
        git(self.tree, "add", "-f", os.path.relpath(cfile, self.tree))
        git(self.tree, "commit", "-q", "-m", "track a pyc")
        stray = os.path.join(self.tree, "pkg", "__pycache__", "stray.cpython-0.pyc")
        os.makedirs(os.path.dirname(stray))
        self._write(os.path.relpath(stray, self.tree), "")
        acceptance.purge_bytecode(self.tree)
        self.assertTrue(os.path.exists(cfile))
        self.assertTrue(os.path.exists(note))
        self.assertFalse(os.path.exists(stray))

    def test_hygiene_ignores_the_pyc_of_its_own_earlier_run(self):
        self.assertEqual(hygiene.run_checks(self.tree, [_CMD], 60), frozenset())
        self._regress_in_same_second()
        self.assertTrue(hygiene.run_checks(self.tree, [_CMD], 60))

    def test_acceptance_ignores_a_stale_pyc_in_the_tree(self):
        self._cache_old_in_tree()
        self._regress_in_same_second()
        run = acceptance.run_named(self.tree, ["test_calc.py"], "unittest", 60)
        self.assertEqual(run.get(("test_calc.py", "test_f")), "failed")

    def test_acceptance_ignores_the_pyc_of_its_own_earlier_run(self):
        first = acceptance.run_named(self.tree, ["test_calc.py"], "unittest", 60)
        self.assertEqual(first.get(("test_calc.py", "test_f")), "passed")
        self._regress_in_same_second()
        run = acceptance.run_named(self.tree, ["test_calc.py"], "unittest", 60)
        self.assertEqual(run.get(("test_calc.py", "test_f")), "failed")

    def test_runs_leave_no_bytecode_behind(self):
        scratch = os.path.join(self.tmp, "scratch")
        os.makedirs(scratch)
        with mock.patch.object(tempfile, "tempdir", scratch):
            hygiene.run_checks(self.tree, [_CMD], 60)
            acceptance.run_named(self.tree, ["test_calc.py"], "unittest", 60)
        self.assertFalse(os.path.exists(os.path.join(self.tree, "__pycache__")))
        self.assertEqual(os.listdir(scratch), [])


if __name__ == "__main__":
    unittest.main()
