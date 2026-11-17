"""codex.runner: the codex exec runner, its env, the login preflight and the failure classes (docs/codex/SPEC.md)."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from mr import config, review, runners
from mr.defaults import defaults
from tests import gitfix

MODEL = "gpt-6.1-sol"
REVIEW_OK = 'looks fine\nMR_REVIEW {"items":[{"id":"R1","verdict":"pass","evidence":"calc.py:4"}],"overall":"pass"}'


class CodexCase(unittest.TestCase):
    time_scale = "1"
    reviewer_s = 600

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = td.name
        self.repo = gitfix.make_repo(os.path.join(self.dir, "repo"))

    def setup(self, steps, login="chatgpt"):
        self.sp = gitfix.write_scenario(self.dir, {"codex": {"login": [{"login": login}], MODEL: steps}})
        cp = gitfix.write_check(self.dir, {})
        env = dict(gitfix.fake_env(self.sp, cp), MR_TEST_TIME_SCALE=self.time_scale,
                   OPENAI_API_KEY="sk-openai-secret", CODEX_API_KEY="sk-codex-secret")
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        raw = config._deep_merge(defaults(), {
            "paths": {"home": os.path.join(self.dir, "home"), "codex": gitfix.FAKE_CODEX},
            "timeouts": {"reviewer_s": self.reviewer_s},
            "review": {"codex": {"authors": ["claude"]}}})
        self.cfg = config.from_dict(raw)

    def run_codex(self):
        return runners.run_codex(self.cfg, "review this", self.repo, self.cfg.timeouts.reviewer_s,
                                 task="t1", attempt=1)

    def calls(self):
        path = self.sp + ".calls.jsonl"
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh]

    def exec_calls(self):
        return [c for c in self.calls() if c["argv"][:1] == ["exec"]]


class TestCodexRun(CodexCase):
    def test_argv_has_each_flag(self):
        self.setup([{"result": REVIEW_OK}])
        res = self.run_codex()
        self.assertFalse(res.is_error, res.stderr_tail)
        [call] = self.exec_calls()
        last = res.raw_path + ".last"
        self.assertEqual(call["argv"], [
            "exec", "--json", "--color", "never", "-o", last,
            "-m", MODEL, "-c", 'model_reasoning_effort="high"',
            "-s", "read-only", "-c", 'approval_policy="never"',
            "-c", 'web_search="disabled"', "-c", "features.hooks=false",
            "--ignore-user-config", "--ignore-rules", "--ephemeral",
            "-C", self.repo, "review this"])
        self.assertNotIn("--skip-git-repo-check", call["argv"])
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.repo))
        self.assertEqual(runners.codex_argv(self.cfg, "p", "/t", "/b.last")[0], gitfix.FAKE_CODEX)

    def test_no_api_key_reaches_the_child(self):
        self.setup([{"result": REVIEW_OK}])
        self.run_codex()
        calls = self.calls()
        self.assertEqual([c["argv"][:2] for c in calls], [["login", "status"], ["exec", "--json"]])
        for call in calls:
            self.assertNotIn("OPENAI_API_KEY", call["env"]["env_keys"])
            self.assertNotIn("CODEX_API_KEY", call["env"]["env_keys"])
            self.assertEqual(call["env"]["GIT_CONFIG_KEY_0"], "url.file:///nonexistent/.pushInsteadOf")
        env = runners.codex_child_env()
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")

    def test_text_comes_from_the_o_file(self):
        self.setup([{"result": REVIEW_OK, "event_text": "event text, not the review"}])
        res = self.run_codex()
        self.assertEqual(res.result_text, REVIEW_OK)
        self.assertEqual(res.backend, "codex")
        self.assertEqual(res.model_used, MODEL)
        self.assertIsNone(res.codex_failure)
        self.assertEqual(res.usage["input_tokens"], 100)
        self.assertEqual(review.verdict(review.parse_review(res.result_text), ["R1"]), "pass")

    def test_empty_o_file_falls_back_to_the_last_agent_message(self):
        self.setup([{"result": REVIEW_OK, "no_last": True}])
        res = self.run_codex()
        self.assertFalse(res.is_error)
        self.assertEqual(res.result_text, REVIEW_OK)

    def test_stale_o_file_is_not_read(self):
        self.setup([{"result": REVIEW_OK}, {"error": "boom", "no_last": True}])
        self.assertFalse(self.run_codex().is_error)
        res = self.run_codex()
        self.assertTrue(res.is_error)
        self.assertEqual(res.result_text, "")

    def test_read_only_run_writes_no_file(self):
        self.setup([{"result": REVIEW_OK, "files": {"evil.txt": "x"}}])
        self.run_codex()
        self.assertFalse(os.path.exists(os.path.join(self.repo, "evil.txt")))


class TestCodexFailure(CodexCase):
    def test_classes(self):
        cases = [
            ("auth", "none", {}),
            ("auth_api_key", "api_key", {}),
            ("auth", "chatgpt", {"error": "unexpected status 401 Unauthorized"}),
            ("usage_limit", "chatgpt", {"error": "You've hit your usage limit. Try again later."}),
            ("usage_limit", "chatgpt", {"error": "429 Too Many Requests"}),
            ("crash", "chatgpt", {"error": "stream disconnected before completion"}),
            ("crash", "chatgpt", {"result": "", "rc": 0}),
        ]
        for want, login, step in cases:
            with self.subTest(want=want, step=step):
                self.setUp()
                self.setup([step], login=login)
                res = self.run_codex()
                self.assertTrue(res.is_error)
                self.assertEqual(res.codex_failure, want)
                if login != "chatgpt":
                    self.assertEqual(self.exec_calls(), [], "no codex exec after an auth failure")

    def test_contract_is_a_run_that_worked_with_no_verdict(self):
        self.setup([{"result": "I reviewed it and it looks fine."}])
        res = self.run_codex()
        self.assertFalse(res.is_error)
        self.assertIsNone(res.codex_failure)
        self.assertIsNone(review.verdict(review.parse_review(res.result_text), ["R1"]))

    def test_classify_text(self):
        self.assertEqual(runners.codex_failure("", "", True), "timeout")
        self.assertEqual(runners.codex_failure("Rate limit reached", "", False), "usage_limit")
        self.assertEqual(runners.codex_failure("", "Please login again", False), "auth")
        self.assertEqual(runners.codex_failure("", "", False), "crash")


class TestCodexTimeout(CodexCase):
    time_scale = "0.1"  # the login preflight gets 3 s, the review run 1 s
    reviewer_s = 10

    def test_sleep_times_out_at_scaled_reviewer_s(self):
        self.setup([{"sleep_s": 100000, "result": REVIEW_OK}])
        t0 = time.monotonic()
        res = self.run_codex()
        self.assertLess(time.monotonic() - t0, 15)
        self.assertTrue(res.timed_out)
        self.assertTrue(res.is_error)
        self.assertEqual(res.codex_failure, "timeout")
        self.assertGreaterEqual(res.wall_s, 1.0)
        self.assertFalse(runners._children)


if __name__ == "__main__":
    unittest.main()
