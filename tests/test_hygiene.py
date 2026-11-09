"""Signal H: base-relative hygiene (spec §5.9, §10.3, AC-SIG-4)."""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import unittest

from mr import config, hygiene
from mr.defaults import defaults

_TSC = re.compile(r"^(?P<file>[^\s(]+)\(\d+,\d+\): error (?P<code>TS\d+)(?P<msg>.*)")
_LINT = re.compile(r"^(?P<file>[\w./-]+):(\d+):(\d+)")


def _lint_id(line: str) -> str:
    line = line.strip()
    file = _LINT.match(line).group("file")
    bare = re.sub(r"\d+", "", line)
    return "lint:" + file + ":" + hashlib.sha1(bare.encode()).hexdigest()[:8]


def _tsc_id(line: str) -> str:
    m = _TSC.match(line)
    bare = re.sub(r"\d+", "", m.group("msg"))
    return f"tsc:{m.group('file')}:{m.group('code')}:" + hashlib.sha1(bare.encode()).hexdigest()[:8]


def _cfg(home, commands, hygiene_s=30):
    raw = config._deep_merge(defaults(), {
        "paths": {"home": home},
        "hygiene": {"commands": commands},
        "timeouts": {"hygiene_s": hygiene_s},
    })
    return config.from_dict(raw)


class TestHygiene(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mr-hyg-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tree = os.path.join(self.tmp, "tree")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.tree)

    def write(self, rel, text):
        path = os.path.join(self.tree, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_base_relative_pass(self):
        # AC-SIG-4: two failures at reset_tree stay ok; a third new id does not.
        lines = ["src/a.ts:1:1: base-one", "src/b.ts:2:2: base-two"]
        third = "src/c.ts:3:3: brand-new"
        self.write("check.py", "from pathlib import Path\n"
                   "print(Path('fails.txt').read_text(), end='')\n"
                   "raise SystemExit(1)\n")
        self.write("fails.txt", "\n".join(lines) + "\n")
        cfg = _cfg(self.home, {"demo": ["python3 check.py"]})
        base = hygiene.base_failures(self.tree, cfg, "demo")
        self.assertEqual(base, frozenset(_lint_id(x) for x in lines))
        self.write("README.md", "rung touched a real file\n")
        same = hygiene.hygiene(self.tree, cfg, base, "demo")
        self.assertTrue(same.ok)
        self.assertFalse(same.skipped)
        self.assertEqual(same.new_failures, ())
        self.assertEqual(same.base_size, 2)
        self.assertEqual(same.failures, base)
        self.write("fails.txt", "\n".join(lines + [third]) + "\n")
        added = hygiene.hygiene(self.tree, cfg, base, "demo")
        self.assertFalse(added.ok)
        self.assertEqual(added.new_failures, (_lint_id(third),))

    def test_tsc_lines(self):
        self.write("emit_tsc.py",
                   "print('src/a.ts(1,1): error TS2322: nope')\n"
                   "print('src/a.ts(4,2): error TS2322: again')\n"
                   "print('src/b.ts(3,1): error TS2304: missing')\n"
                   "print('note: not an error')\n")
        got = hygiene.run_checks(self.tree, ["python3 emit_tsc.py"], 10)
        self.assertEqual(got, frozenset({
            _tsc_id("src/a.ts(1,1): error TS2322: nope"),
            _tsc_id("src/a.ts(4,2): error TS2322: again"),
            _tsc_id("src/b.ts(3,1): error TS2304: missing"),
        }))

    def test_vitest_json(self):
        name = os.path.join(self.tree, "src", "a.test.ts")
        report = {
            "testResults": [
                {
                    "name": name,
                    "assertionResults": [
                        {"fullName": "suite passes", "status": "passed"},
                        {"fullName": "suite fails", "status": "failed"},
                    ],
                },
                {
                    "name": os.path.join(self.tree, "src", "b.test.ts"),
                    "assertionResults": [
                        {"fullName": "only pass", "status": "passed"},
                    ],
                },
            ]
        }
        self.write("emit_vitest.py",
                   "import json,sys\n"
                   "print('noise before')\n"
                   "json.dump(json.load(open('report.json')), sys.stdout)\n"
                   "raise SystemExit(1)\n")
        self.write("report.json", json.dumps(report))
        got = hygiene.run_checks(
            self.tree, ["python3 emit_vitest.py --reporter=json"], 10)
        self.assertEqual(got, frozenset({"vitest:src/a.test.ts::suite fails"}))
        self.write("bad_vitest.py", "print('not json')\nraise SystemExit(1)\n")
        bad = hygiene.run_checks(
            self.tree, ["python3 bad_vitest.py --reporter=json"], 10)
        self.assertEqual(bad, frozenset({
            "vitest:python3 bad_vitest.py --reporter=json:unparsable"}))

    def test_generic_lint(self):
        self.write("emit_lint.py",
                   "print('src/foo.ts:10:2: unexpected any')\n"
                   "print('src/foo.ts:11:4: unused var')\n"
                   "print('not a lint line')\n"
                   "raise SystemExit(1)\n")
        got = hygiene.run_checks(self.tree, ["python3 emit_lint.py"], 10)
        self.assertEqual(got, frozenset({
            _lint_id("src/foo.ts:10:2: unexpected any"),
            _lint_id("src/foo.ts:11:4: unused var"),
        }))
        self.write("exit3.py", "print('hello')\nraise SystemExit(3)\n")
        bare = hygiene.run_checks(self.tree, ["python3 exit3.py"], 10)
        self.assertEqual(bare, frozenset({"cmd:python3 exit3.py:exit3"}))

    def test_timeout_id(self):
        start = time.monotonic()
        got = hygiene.run_checks(
            self.tree, ["python3 -c 'import time; time.sleep(30)'"], 0.2)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(got, frozenset({
            "cmd:python3 -c 'import time; time.sleep(30)':timeout"}))

    def test_base_measured_on_each_call(self):
        self.write("check.py", "from pathlib import Path\n"
                   "print(Path('fails.txt').read_text(), end='')\n"
                   "raise SystemExit(1)\n")
        cfg = _cfg(self.home, {"demo": ["python3 check.py"]})
        self.write("fails.txt", "src/a.ts:1:1: boom\n")
        first = hygiene.base_failures(self.tree, cfg, "demo")
        self.write("fails.txt", "src/b.ts:1:1: boom\n")
        second = hygiene.base_failures(self.tree, cfg, "demo")
        self.assertEqual(first, frozenset({_lint_id("src/a.ts:1:1: boom")}))
        self.assertEqual(second, frozenset({_lint_id("src/b.ts:1:1: boom")}))

    def test_locked_ids_dropped(self):
        self.write("emit_tsc.py",
                   "print('src/locked.test.ts(1,1): error TS2322: locked')\n"
                   "print('src/app.ts(2,1): error TS2304: real')\n"
                   "raise SystemExit(1)\n")
        cfg = _cfg(self.home, {"demo": ["python3 emit_tsc.py"]})
        sig = hygiene.hygiene(
            self.tree, cfg, frozenset(), "demo",
            locked_ids=frozenset({"src/locked.test.ts"}))
        real = _tsc_id("src/app.ts(2,1): error TS2304: real")
        self.assertEqual(sig.failures, frozenset({real}))
        self.assertEqual(sig.new_failures, (real,))

    def test_no_commands_skipped(self):
        cfg = _cfg(self.home, {"demo": ["python3 check.py"]})
        sig = hygiene.hygiene(self.tree, cfg, frozenset({"x"}), "nope")
        self.assertTrue(sig.ok)
        self.assertTrue(sig.skipped)
        self.assertEqual(sig.new_failures, ())
        self.assertEqual(sig.detail, "no hygiene commands for nope")
        self.assertEqual(sig.base_size, 1)
