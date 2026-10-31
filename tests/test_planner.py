"""Planner checklist validation and plan() (spec §7.5a, §10.3)."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import textwrap
import unittest
from unittest import mock

from mr import config, planner
from mr.config import ConfigError
from mr.defaults import defaults
from mr.task import Params, make_task
from tests import gitfix

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "p0", "checklist.example.json")
_SPEC = "src/features/chat/__tests__/muteSettings.mr.spec.ts"

_OPUS = "claude-opus-5-5"
_CHECK_OK = {
    "hour_usage": 10, "limit": 40, "unit": "points", "exceeded": False,
    "five_hour_pct": 30.0, "five_hour_resets_at": "2026-10-30T05:00:00Z",
    "seven_day_pct": 12.0, "sample_age_s": 30, "samples_in_hour": 3,
    "has_baseline": True, "checked_at": "2026-10-30T00:00:00Z",
}

_PASSING = textwrap.dedent(
    """\
    import unittest


    class TestMul(unittest.TestCase):
        def test_mul_basic(self):
            self.assertEqual(1, 1)
    """
)

_RED = textwrap.dedent(
    """\
    import mul
    import unittest


    class TestMul(unittest.TestCase):
        def test_mul_basic(self):
            self.assertEqual(mul.mul(2, 3), 6)
    """
)


def _unit_doc(content, create):
    path = "tests/test_mul_mr.py"
    return {
        "goal": "Add mul.",
        "scope": {
            "modify": ["README.md"],
            "create": list(create),
            "forbidden": ["package.json"],
        },
        "requirements": [{
            "id": "R1",
            "text": "mul SHALL multiply",
            "examples": [{"given": "2, 3", "when": "mul", "then": "6"}],
        }],
        "acceptance": [{
            "id": "A1",
            "covers": ["R1"],
            "file": path,
            "tests": ["test_mul_basic"],
        }],
        "est_loc": 10,
        "files_map": {"R1": [create[0] if create[0] != path else "README.md"]},
        "tests": [{"path": path, "content": content}],
    }


class TestValidate(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.repo = gitfix.make_repo(
            os.path.join(td.name, "repo"),
            {"src/features/chat/index.ts": "export {}\n"},
        )
        with open(_FIXTURE, encoding="utf-8") as fh:
            self.doc = json.load(fh)

    def _errors(self, doc):
        return planner.validate(doc, repo_root=self.repo, runner="vitest")

    def _retarget(self, doc, path):
        for item in doc["scope"]["create"]:
            pass
        doc["scope"]["create"] = [
            path if item == _SPEC else item for item in doc["scope"]["create"]
        ]
        for item in doc["acceptance"]:
            if item["file"] == _SPEC:
                item["file"] = path
        for item in doc["tests"]:
            if item["path"] == _SPEC:
                item["path"] = path

    def test_example_checklist_valid(self):
        self.assertEqual(self._errors(self.doc), [])

    def test_rule_keys(self):
        doc = copy.deepcopy(self.doc)
        doc["extra"] = True
        errors = self._errors(doc)
        self.assertTrue(any("unexpected" in err for err in errors), errors)

    def test_rule_ids(self):
        doc = copy.deepcopy(self.doc)
        doc["requirements"][0]["id"] = "REQ1"
        doc["acceptance"][0]["covers"] = ["REQ1"]
        doc["files_map"] = {"REQ1": doc["files_map"].pop("R1"), **{
            k: v for k, v in doc["files_map"].items() if k != "R1"
        }}
        errors = self._errors(doc)
        self.assertTrue(any(err.startswith("id:") for err in errors), errors)

    def test_rule_coverage(self):
        doc = copy.deepcopy(self.doc)
        doc["acceptance"][2]["covers"] = ["R1"]
        errors = self._errors(doc)
        self.assertTrue(any("not covered" in err for err in errors), errors)

    def test_rule_acceptance_file_in_tests(self):
        doc = copy.deepcopy(self.doc)
        other = "src/features/chat/__tests__/other.mr.spec.ts"
        doc["scope"]["create"].append(other)
        doc["acceptance"][0]["file"] = other
        errors = self._errors(doc)
        self.assertTrue(any("not in tests" in err for err in errors), errors)

    def test_rule_acceptance_file_in_create(self):
        doc = copy.deepcopy(self.doc)
        doc["scope"]["create"] = [p for p in doc["scope"]["create"] if p != _SPEC]
        errors = self._errors(doc)
        self.assertTrue(any("not in scope.create" in err for err in errors), errors)

    def test_rule_files_map(self):
        doc = copy.deepcopy(self.doc)
        doc["files_map"]["R1"].append("src/no/such.ts")
        errors = self._errors(doc)
        self.assertTrue(any("outside scope" in err for err in errors), errors)

    def test_rule_modify_exists(self):
        doc = copy.deepcopy(self.doc)
        doc["scope"]["modify"] = ["src/features/chat/missing.ts"]
        doc["files_map"]["R3"] = ["src/features/chat/muteSettings.ts"]
        errors = self._errors(doc)
        self.assertTrue(any("does not exist" in err for err in errors), errors)

    def test_rule_create_absent(self):
        doc = copy.deepcopy(self.doc)
        dest = os.path.join(self.repo, "src/features/chat/muteSettings.ts")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write("export {}\n")
        errors = self._errors(doc)
        self.assertTrue(any("already exists" in err for err in errors), errors)

    def test_rule_test_pattern(self):
        doc = copy.deepcopy(self.doc)
        self._retarget(doc, "src/features/chat/__tests__/muteSettings.spec.ts")
        errors = self._errors(doc)
        self.assertTrue(any("does not match" in err for err in errors), errors)

    def test_rule_test_name_in_content(self):
        doc = copy.deepcopy(self.doc)
        doc["acceptance"][0]["tests"][0] = "not a real title"
        errors = self._errors(doc)
        self.assertTrue(any("not found in" in err for err in errors), errors)

    def test_rule_path_inside_repo(self):
        for label, bad in (("dotdot", "../mute.mr.spec.ts"), ("absolute", "/tmp/mute.mr.spec.ts")):
            with self.subTest(label=label):
                doc = copy.deepcopy(self.doc)
                self._retarget(doc, bad)
                errors = self._errors(doc)
                self.assertTrue(any("outside the repo" in err for err in errors), errors)


class TestPromptAndParse(unittest.TestCase):
    def test_build_prompt_selects_runner_template(self):
        vitest = planner.build_prompt("add mute", "src/a.ts", "src/a.ts", "t-1", "vitest")
        self.assertIn("add mute", vitest)
        self.assertIn("t-1", vitest)
        self.assertIn("src/a.ts", vitest)
        self.assertIn("it()", vitest)
        self.assertIn("from 'vitest'", vitest)

        unit = planner.build_prompt("add mul", "mul.py", "README.md", "t-2", "unittest")
        self.assertIn("add mul", unit)
        self.assertIn("t-2", unit)
        self.assertIn("unittest", unit)
        self.assertIn("test_mul_basic", unit)
        self.assertIn("tests/test_*_mr.py", unit)
        self.assertNotIn("from 'vitest'", unit)

    def test_parse_strips_one_fence_and_rejects_extra(self):
        doc = {"goal": "x"}
        text = "```json\n" + json.dumps(doc) + "\n```"
        self.assertEqual(planner.parse_document(text), doc)
        self.assertEqual(planner.parse_document("here you go\n" + json.dumps(doc)), doc)
        self.assertIsNone(planner.parse_document(json.dumps(doc) + "\n" + json.dumps(doc)))
        self.assertIsNone(planner.parse_document("```json\n" + json.dumps(doc) + "\n```\ntrailing"))


class TestParseLive(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.repo = gitfix.make_repo(
            os.path.join(td.name, "repo"),
            {
                "src/__init__.py": "",
                "src/calc.py": "",
                "tests/__init__.py": "",
            },
        )
        self.live = os.path.join(os.path.dirname(__file__), "fixtures", "planner_live")

    def _read(self, name):
        with open(os.path.join(self.live, name), encoding="utf-8") as handle:
            return handle.read()

    def test_live_fixtures_parse_and_validate(self):
        for name in ("planner-a1.txt", "planner-a2.txt"):
            with self.subTest(name=name):
                doc = planner.parse_document(self._read(name))
                self.assertIsInstance(doc, dict)
                errors = planner.validate(doc, repo_root=self.repo, runner="unittest")
                self.assertEqual(errors, [])

    def test_prose_around_one_object(self):
        doc = {"goal": "add evaluate"}
        text = "I'll read the code first.\n" + json.dumps(doc) + "\nThat is the checklist."
        self.assertEqual(planner.parse_document(text), doc)

    def test_two_checklists_rejected(self):
        first = json.dumps({"goal": "one"})
        second = json.dumps({"goal": "two"})
        self.assertIsNone(planner.parse_document(f"draft {first}\nfinal {second}"))

    def test_fenced_still_works(self):
        doc = {"goal": "fenced"}
        text = "```json\n" + json.dumps(doc) + "\n```"
        self.assertEqual(planner.parse_document(text), doc)

    def test_no_json(self):
        self.assertIsNone(planner.parse_document("the evaluator is not in the tree yet"))


class TestPlan(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = td.name
        self.home = os.path.join(self.dir, "home")
        self.repo = gitfix.make_repo(os.path.join(self.dir, "repo"))
        self.task_id = "t-plan1"
        raw = config._deep_merge(defaults(), {
            "paths": {"home": self.home, "cursor_agent": gitfix.FAKE_CURSOR,
                      "claude": gitfix.FAKE_CLAUDE, "check": gitfix.FAKE_CHECK},
            "acceptance": {"default": "unittest"},
        })
        with mock.patch.dict(os.environ, {"MR_TEST_ALLOW_FAKE_PATHS": "1"}):  # fake claude/check paths
            self.cfg = config.from_dict(raw)
        self.task = make_task(
            "add mul", self.repo, gitfix.head(self.repo), Params(), files_named=("README.md",),
        )

    def _scenario(self, steps, claude=None, check=None):
        scenario = {"cursor": {"grok-4.7-high": steps}}
        if claude is not None:
            scenario["claude"] = {_OPUS: claude}
        path = gitfix.write_scenario(self.dir, scenario)
        check = gitfix.write_check(self.dir, {"default": dict(_CHECK_OK, **(check or {}))})
        patcher = mock.patch.dict(os.environ, gitfix.fake_env(path, check))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.scenario = path
        return path

    def _plan(self, difficulty=None):
        return planner.plan(
            self.task, cfg=self.cfg, tree=self.repo, task_id=self.task_id, attempt=1,
            difficulty=difficulty,
        )

    def _calls(self):
        try:
            with open(self.scenario + ".calls.jsonl", encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            return []

    def test_plan_valid_unittest(self):
        doc = _unit_doc(_RED, ["mul.py", "tests/test_mul_mr.py"])
        self._scenario([{"result": "```json\n" + json.dumps(doc) + "\n```"}])
        result = self._plan()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.checklist["goal"], "Add mul.")
        self.assertRegex(result.reset_tree, r"^[0-9a-f]{40}$")
        written = os.path.join(self.repo, "tests/test_mul_mr.py")
        self.assertTrue(os.path.isfile(written))
        with open(written, encoding="utf-8") as handle:
            self.assertIn("import mul", handle.read())
        task_dir = os.path.join(self.home, "tasks", self.task_id)
        self.assertTrue(os.path.isfile(os.path.join(task_dir, "checklist.json")))
        lock_path = os.path.join(task_dir, "tests.lock")
        with open(lock_path, encoding="utf-8") as handle:
            lockdoc = json.load(handle)
        self.assertIn("tests/test_mul_mr.py", lockdoc["files"])
        listed = gitfix.git(self.repo, "ls-tree", "-r", "--name-only", result.reset_tree)
        self.assertIn("tests/test_mul_mr.py", listed.stdout.splitlines())
        argv = self._calls()[0]["argv"]
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "ask")
        self.assertIn("grok-4.7-high", argv)
        self.assertEqual(result.rung, "G-h")
        self.assertEqual(result.spend, ())

    def test_plan_pass_on_base_then_retry(self):
        bad = _unit_doc(_PASSING, ["tests/test_mul_mr.py"])
        good = _unit_doc(_RED, ["mul.py", "tests/test_mul_mr.py"])
        self._scenario([
            {"result": json.dumps(bad)},
            {"result": json.dumps(good)},
        ])
        result = self._plan()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(result.raw_outputs), 2)
        calls = self._calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("passed on base", calls[1]["prompt"])
        with open(os.path.join(self.repo, "tests/test_mul_mr.py"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("import mul", text)
        self.assertNotIn("assertEqual(1, 1)", text)

    def test_plan_invalid_twice(self):
        first = _unit_doc(_PASSING, ["tests/test_mul_mr.py"])
        second = _unit_doc(_PASSING.replace("assertEqual(1, 1)", "assertEqual(2, 2)"),
                           ["tests/test_mul_mr.py"])
        self._scenario([
            {"result": json.dumps(first)},
            {"result": json.dumps(second)},
        ])
        result = self._plan()
        self.assertFalse(result.ok)
        self.assertIsNone(result.checklist)
        self.assertIsNone(result.reset_tree)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(result.raw_outputs), 2)
        self.assertTrue(result.errors)
        task_dir = os.path.join(self.home, "tasks", self.task_id)
        for name in ("planner-a1.txt", "planner-a2.txt"):
            with open(os.path.join(task_dir, name), encoding="utf-8") as handle:
                body = handle.read()
            self.assertIn("test_mul_basic", body)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "tests/test_mul_mr.py")))
        self.assertFalse(os.path.exists(os.path.join(task_dir, "checklist.json")))
        self.assertFalse(os.path.exists(os.path.join(task_dir, "tests.lock")))

    def _valid_json(self):
        return json.dumps(_unit_doc(_RED, ["mul.py", "tests/test_mul_mr.py"]))

    def test_difficulty_picks_planner_rung(self):
        for difficulty in ("trivial", "easy", "medium", None):
            with self.subTest(difficulty=difficulty):
                self.assertEqual(
                    self.cfg.planner.rungs[difficulty or "medium"], "G-h")
        self.assertEqual(self.cfg.planner.rungs["hard"], "O")
        self.assertEqual(self.cfg.planner.rungs["critical"], "O")

    def test_medium_plans_on_g_h_via_cursor(self):
        self._scenario([{"result": self._valid_json()}])
        result = self._plan("medium")
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.rung, "G-h")
        self.assertEqual([c["cli"] for c in self._calls()], ["cursor"])

    def test_hard_plans_on_opus_via_claude_cli(self):
        self._scenario([], claude=[{"result": self._valid_json()}])
        result = self._plan("hard")
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.rung, "O")
        self.assertEqual(result.notes, ())
        self.assertEqual(len(result.spend), 1)  # the caller adds this to the task cost
        calls = self._calls()
        self.assertEqual([c["cli"] for c in calls], ["claude"])
        self.assertEqual(calls[0]["argv"][calls[0]["argv"].index("--permission-mode") + 1], "plan")
        self.assertIn(_OPUS, calls[0]["argv"])

    def test_opus_denied_falls_back_to_g_h(self):
        self._scenario([{"result": self._valid_json()}], claude=[{}], check={"hour_usage": 39})
        result = self._plan("hard")
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.rung, "G-h")
        self.assertEqual(result.spend, ())
        self.assertEqual(len(result.notes), 1)
        self.assertIn("planner O denied", result.notes[0])
        self.assertEqual([c["cli"] for c in self._calls()], ["cursor"])

    def test_opus_is_never_run_via_cursor(self):  # hard rule §0.3
        import dataclasses
        self._scenario([{"result": self._valid_json()}])
        self.cfg.rungs["O"] = dataclasses.replace(self.cfg.rungs["O"], path="cursor")
        with self.assertRaises(RuntimeError):
            self._plan("hard")
        self.assertEqual(self._calls(), [])

    def test_opus_timeout_reports_timeout(self):
        self._scenario([], claude=[{"sleep_s": 100000}, {"sleep_s": 100000}])
        with mock.patch.dict(os.environ, {"MR_TEST_TIME_SCALE": "0.0005"}):
            result = self._plan("hard")
        self.assertFalse(result.ok)
        self.assertEqual(result.errors, ["planner timed out after 600s"])
        self.assertEqual(result.rung, "O")
        self.assertEqual(len(result.spend), 2)

    def test_planner_timeout_message(self):
        self._scenario([{"sleep_s": 100000}, {"sleep_s": 100000}])
        with mock.patch.dict(os.environ, {"MR_TEST_TIME_SCALE": "0.0005"}):
            result = self._plan("medium")
        self.assertFalse(result.ok)
        self.assertEqual(result.errors, ["planner timed out after 600s"])
        self.assertNotIn("not one JSON document", " ".join(result.errors))
        self.assertEqual(result.attempts, 2)


class TestAcceptanceConfig(unittest.TestCase):
    def test_absent_block_defaults(self):
        cfg = config.from_dict(defaults())
        self.assertEqual(cfg.acceptance, {"runners": {}, "default": "vitest"})

    def test_runners_and_rejects_unknown(self):
        raw = copy.deepcopy(defaults())
        raw["acceptance"] = {"runners": {"example-app": "unittest"}, "default": "vitest"}
        cfg = config.from_dict(raw)
        self.assertEqual(cfg.acceptance["runners"]["example-app"], "unittest")
        self.assertEqual(cfg.acceptance["default"], "vitest")

        raw["acceptance"] = {"runners": {"example-app": "pytest"}, "default": "vitest"}
        with self.assertRaises(ConfigError) as caught:
            config.from_dict(raw)
        self.assertIn("acceptance", str(caught.exception))

        raw["acceptance"] = {"default": "nose"}
        with self.assertRaises(ConfigError) as caught:
            config.from_dict(raw)
        self.assertIn("acceptance.default", str(caught.exception))
