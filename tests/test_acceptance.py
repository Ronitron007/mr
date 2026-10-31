"""Acceptance signal A (spec §5.9, §7.5a, §10.3; AC-SIG-1 A, AC-A-1, AC-A-2)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import textwrap
import unittest

from mr import acceptance, config
from mr.defaults import defaults
from tests import gitfix

_ADD_PASS = textwrap.dedent(
    """\
    import unittest


    def add(a, b):
        return a + b


    class TestAdd(unittest.TestCase):
        def test_add_basic(self):
            self.assertEqual(add(1, 2), 3)
    """
)

_ADD_FAIL = textwrap.dedent(
    """\
    import unittest


    def add(a, b):
        return a + b


    class TestAdd(unittest.TestCase):
        def test_add_basic(self):
            self.assertEqual(add(1, 2), 4)
    """
)

_ADD_SKIP = textwrap.dedent(
    """\
    import unittest


    class TestAdd(unittest.TestCase):
        def test_add_basic(self):
            self.skipTest("later")
    """
)

_ADD_RENAMED = textwrap.dedent(
    """\
    import unittest


    def add(a, b):
        return a + b


    class TestAdd(unittest.TestCase):
        def test_add_renamed(self):
            self.assertEqual(add(1, 2), 3)
    """
)


def _checklist(path, names, content, create, mapped):
    return {
        "goal": "Add a function.",
        "scope": {"modify": ["README.md"], "create": list(create), "forbidden": []},
        "requirements": [{
            "id": "R1",
            "text": "add SHALL add two numbers",
            "examples": [{"given": "1, 2", "when": "add", "then": "3"}],
        }],
        "acceptance": [{"id": "A1", "covers": ["R1"], "file": path, "tests": list(names)}],
        "est_loc": 5,
        "files_map": {"R1": list(mapped)},
        "tests": [{"path": path, "content": content}],
    }


class AcceptanceCase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = td.name
        self.home = os.path.join(self.dir, "home")
        self.repo = gitfix.make_repo(os.path.join(self.dir, "repo"))
        self.repo_name = os.path.basename(self.repo)
        raw = config._deep_merge(defaults(), {
            "paths": {"home": self.home, "cursor_agent": gitfix.FAKE_CURSOR},
            "acceptance": {"runners": {self.repo_name: "unittest"}, "default": "vitest"},
        })
        self.cfg = config.from_dict(raw)

    def _write(self, rel, content):
        dest = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
        return dest

    def _signal(self, checklist, task_id="t-acc"):
        lockdoc = acceptance.lock(self.repo, checklist, self.cfg, self.repo_name, task_id=task_id)
        return acceptance.acceptance(
            self.repo, checklist, lockdoc, cfg=self.cfg, repo_name=self.repo_name,
        ), lockdoc


class TestUnittestSignal(AcceptanceCase):
    def test_all_pass(self):
        path = "tests/test_add_mr.py"
        self._write(path, _ADD_PASS)
        self._write("package.json", '{"name":"x","scripts":{"test":"vitest"}}\n')
        self._write("vitest.config.ts", "export default { test: { setupFiles: ['./setup.ts'] } }\n")
        checklist = _checklist(path, ["test_add_basic"], _ADD_PASS, [path], ["README.md"])
        sig, lockdoc = self._signal(checklist)
        self.assertTrue(sig.ok, sig.detail)
        self.assertEqual(sig.failed, ())
        self.assertEqual(sig.missing, ())
        self.assertEqual(sig.lock_violations, ())
        self.assertIn((path, "test_add_basic"), sig.passed)
        self.assertEqual(set(lockdoc["files"]), {path})

    def test_one_fail(self):
        path = "tests/test_add_mr.py"
        self._write(path, _ADD_FAIL)
        checklist = _checklist(path, ["test_add_basic"], _ADD_FAIL, [path], ["README.md"])
        sig, _lock = self._signal(checklist, "t-fail")
        self.assertFalse(sig.ok)
        self.assertIn((path, "test_add_basic"), sig.failed)
        self.assertEqual(sig.missing, ())
        self.assertEqual(sig.lock_violations, ())

    def test_skipped_fails_signal(self):
        path = "tests/test_add_mr.py"
        self._write(path, _ADD_SKIP)
        checklist = _checklist(path, ["test_add_basic"], _ADD_SKIP, [path], ["README.md"])
        sig, _lock = self._signal(checklist, "t-skip")
        self.assertFalse(sig.ok)
        self.assertIn((path, "test_add_basic"), sig.failed)
        self.assertNotIn((path, "test_add_basic"), sig.passed)

    def test_renamed_or_missing_fails_signal(self):
        path = "tests/test_add_mr.py"
        self._write(path, _ADD_RENAMED)
        checklist = _checklist(path, ["test_add_basic"], _ADD_RENAMED, [path], ["README.md"])
        sig, _lock = self._signal(checklist, "t-miss")
        self.assertFalse(sig.ok)
        self.assertIn((path, "test_add_basic"), sig.missing)
        self.assertEqual(sig.passed, ())

    def test_tampered_lock_fails_signal(self):
        path = "tests/test_add_mr.py"
        self._write(path, _ADD_PASS)
        checklist = _checklist(path, ["test_add_basic"], _ADD_PASS, [path], ["README.md"])
        lockdoc = acceptance.lock(self.repo, checklist, self.cfg, self.repo_name, task_id="t-tamper")
        self.assertEqual(acceptance.verify_lock(self.repo, lockdoc), [])
        with open(os.path.join(self.repo, path), "a", encoding="utf-8") as fh:
            fh.write("\n# tampered\n")
        self.assertEqual(acceptance.verify_lock(self.repo, lockdoc), [path])
        sig = acceptance.acceptance(
            self.repo, checklist, lockdoc, cfg=self.cfg, repo_name=self.repo_name,
        )
        self.assertFalse(sig.ok)
        self.assertIn(path, sig.lock_violations)


class TestCollectionRed(AcceptanceCase):
    def test_import_of_create_module_counts_as_red(self):
        path = "tests/test_new_mr.py"
        content = textwrap.dedent(
            """\
            import created_mod
            import unittest


            class TestNew(unittest.TestCase):
                def test_new_basic(self):
                    self.assertTrue(created_mod.ok())
            """
        )
        self._write(path, content)
        checklist = _checklist(
            path, ["test_new_basic"], content, ["created_mod.py", path], ["created_mod.py"],
        )
        run = acceptance.run_named(self.repo, [path], "unittest", 60)
        self.assertTrue(any(
            err.kind == "module_not_found" and err.module == "created_mod"
            for err in run.collection_errors
        ))
        self.assertEqual(acceptance.base_errors(checklist, run), [])

    def test_other_import_error_is_not_red(self):
        path = "tests/test_new_mr.py"
        content = textwrap.dedent(
            """\
            import not_in_scope
            import unittest


            class TestNew(unittest.TestCase):
                def test_new_basic(self):
                    self.assertTrue(not_in_scope.ok())
            """
        )
        self._write(path, content)
        checklist = _checklist(
            path, ["test_new_basic"], content, ["created_mod.py", path], ["created_mod.py"],
        )
        run = acceptance.run_named(self.repo, [path], "unittest", 60)
        errors = acceptance.base_errors(checklist, run)
        self.assertTrue(errors, errors)
        self.assertTrue(any("collection" in err for err in errors), errors)

        boom = "tests/test_boom_mr.py"
        boom_src = textwrap.dedent(
            """\
            import badimp
            import unittest


            class TestBoom(unittest.TestCase):
                def test_boom_basic(self):
                    self.assertTrue(True)
            """
        )
        self._write("badimp.py", "raise RuntimeError('boom')\n")
        self._write(boom, boom_src)
        boom_list = _checklist(
            boom, ["test_boom_basic"], boom_src, ["created_mod.py", boom], ["created_mod.py"],
        )
        boom_run = acceptance.run_named(self.repo, [boom], "unittest", 60)
        boom_errors = acceptance.base_errors(boom_list, boom_run)
        self.assertTrue(boom_errors, boom_errors)
        self.assertFalse(any(err.kind == "module_not_found" and err.module == "created_mod"
                             for err in boom_run.collection_errors))


class TestVitestReport(unittest.TestCase):
    def test_skipped_pending_todo_and_missing(self):
        tree = "/repo/app"
        path = "src/__tests__/a.mr.spec.ts"
        checklist = _checklist(path, ["toggles off", "stays on"], "it('toggles off'", [path], [path])
        report = {
            "testResults": [{
                "name": os.path.join(tree, path),
                "assertionResults": [
                    {"title": "toggles off", "status": "passed", "fullName": "suite toggles off"},
                    {"title": "stays on", "status": "skipped"},
                    {"title": "later", "status": "pending"},
                    {"title": "someday", "status": "todo"},
                ],
            }],
        }
        run = acceptance.parse_vitest_report(report, tree=tree)
        self.assertEqual(run[(path, "toggles off")], "passed")
        self.assertEqual(run[(path, "stays on")], "skipped")
        self.assertEqual(run[(path, "later")], "pending")
        self.assertEqual(run[(path, "someday")], "todo")
        sig = acceptance.signal_from_statuses(checklist, run)
        self.assertFalse(sig.ok)
        self.assertIn((path, "toggles off"), sig.passed)
        self.assertIn((path, "stays on"), sig.failed)
        self.assertNotIn((path, "toggles off"), sig.missing)

        renamed = {
            "testResults": [{
                "name": path,
                "assertionResults": [{"title": "toggles", "status": "passed"}],
            }],
        }
        run2 = acceptance.parse_vitest_report(renamed, tree=tree)
        sig2 = acceptance.signal_from_statuses(checklist, run2)
        self.assertFalse(sig2.ok)
        self.assertIn((path, "toggles off"), sig2.missing)
        self.assertIn((path, "stays on"), sig2.missing)

    def test_all_passed_report(self):
        tree = "/repo/app"
        path = "src/__tests__/a.mr.spec.ts"
        checklist = _checklist(path, ["toggles off"], "it('toggles off'", [path], [path])
        report = {
            "testResults": [{
                "name": path,
                "assertionResults": [{"title": "toggles off", "status": "passed"}],
            }],
        }
        run = acceptance.parse_vitest_report(report, tree=tree)
        sig = acceptance.signal_from_statuses(checklist, run)
        self.assertTrue(sig.ok, sig.detail)
        self.assertEqual(sig.failed, ())
        self.assertEqual(sig.missing, ())

    def test_canned_collection_module_not_found(self):
        tree = "/repo/app"
        path = "src/features/chat/__tests__/muteSettings.mr.spec.ts"
        report = {
            "testResults": [{
                "name": os.path.join(tree, path),
                "status": "failed",
                "message": "Cannot find module '../muteSettings' from 'muteSettings.mr.spec.ts'",
                "assertionResults": [],
            }],
        }
        run = acceptance.parse_vitest_report(report, tree=tree)
        self.assertTrue(any(
            err.kind == "module_not_found" and err.module == "muteSettings" and err.file == path
            for err in run.collection_errors
        ))
        checklist = _checklist(
            path, ["mutes an unmuted chat"], "it('mutes",
            ["src/features/chat/muteSettings.ts", path],
            ["src/features/chat/muteSettings.ts"],
        )
        self.assertEqual(acceptance.base_errors(checklist, run), [])

        other = {
            "testResults": [{
                "name": path,
                "message": "Cannot find module 'left-pad'",
                "assertionResults": [],
            }],
        }
        run_other = acceptance.parse_vitest_report(other, tree=tree)
        errors = acceptance.base_errors(checklist, run_other)
        self.assertTrue(any("collection" in err for err in errors), errors)


class TestVitestLock(AcceptanceCase):
    def test_hashes_config_setup_and_scripts_only(self):
        raw = config._deep_merge(defaults(), {
            "paths": {"home": self.home, "cursor_agent": gitfix.FAKE_CURSOR},
            "acceptance": {"runners": {self.repo_name: "vitest"}, "default": "unittest"},
        })
        self.cfg = config.from_dict(raw)
        path = "src/__tests__/a.mr.spec.ts"
        body = "import { it } from 'vitest';\nit('toggles off', () => {});\n"
        self._write(path, body)
        self._write("package.json", json.dumps({
            "name": "app",
            "scripts": {"test": "vitest run"},
        }))
        self._write("vitest.config.ts", "export default { test: { setupFiles: ['./setup.ts'] } }\n")
        self._write("setup.ts", "export {}\n")
        checklist = _checklist(path, ["toggles off"], body, [path], ["README.md"])
        lockdoc = acceptance.lock(self.repo, checklist, self.cfg, self.repo_name, task_id="t-vlock")
        files = lockdoc["files"]
        self.assertEqual(files[path]["sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertIn("vitest.config.ts", files)
        self.assertIn("setup.ts", files)
        self.assertEqual(files["package.json"]["part"], "scripts")
        saved = os.path.join(self.home, "tasks", "t-vlock", "tests.lock")
        with open(saved, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), lockdoc)

        with open(os.path.join(self.repo, "package.json"), encoding="utf-8") as handle:
            pkg = json.load(handle)
        pkg["name"] = "renamed"
        self._write("package.json", json.dumps(pkg))
        self.assertEqual(acceptance.verify_lock(self.repo, lockdoc), [])

        pkg["scripts"]["test"] = "vitest run --update"
        self._write("package.json", json.dumps(pkg))
        self.assertIn("package.json", acceptance.verify_lock(self.repo, lockdoc))

        self._write("setup.ts", "export const n = 1\n")
        violated = acceptance.verify_lock(self.repo, lockdoc)
        self.assertIn("setup.ts", violated)
        self.assertIn("package.json", violated)
