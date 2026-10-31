"""Self-tests for the test fakes and git fixtures (mr-spec 14.2)."""

import json
import os
import subprocess
import tempfile
import time
import unittest

from tests import gitfix

CHECK_DEFAULT = {
    "hour_usage": 10,
    "limit": 40,
    "unit": "points",
    "exceeded": False,
    "five_hour_pct": 30.0,
    "five_hour_resets_at": "2026-10-30T05:00:00Z",
    "seven_day_pct": 12.0,
    "sample_age_s": 30,
    "samples_in_hour": 3,
    "has_baseline": True,
    "checked_at": "2026-10-30T00:00:00Z",
}


class FakeCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = self.td.name
        self.repo = gitfix.make_repo(os.path.join(self.dir, "repo"))

    def setup_env(self, scenario, check=None, scale=None):
        sp = gitfix.write_scenario(self.dir, scenario)
        cp = gitfix.write_check(self.dir, check or {"default": dict(CHECK_DEFAULT)})
        env = gitfix.fake_env(sp, cp)
        if scale is not None:
            env["MR_TEST_TIME_SCALE"] = scale
        return env, sp, cp

    def run_fake(self, exe, args, env, cwd=None, stdin=None):
        return subprocess.run(
            [exe, *args],
            cwd=cwd or self.repo,
            env=env,
            capture_output=True,
            text=True,
            input=stdin,
        )

    def run_cursor(self, env, model="grok-4.7-high", extra=(), prompt="do the task", **kw):
        args = [
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            "--trust",
            "--force",
            "--workspace",
            self.repo,
            *extra,
        ]
        if prompt is not None:
            args.append(prompt)
        return self.run_fake(gitfix.FAKE_CURSOR, args, env, **kw)

    def run_claude(self, env, model="claude-sonnet-5-5", extra=(), prompt="do the task", **kw):
        args = [
            "--output-format",
            "json",
            "--model",
            model,
            "--effort",
            "high",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Read Grep Glob Edit Write",
            "--disallowedTools",
            "Bash(git push *)",
            "--no-session-persistence",
            *extra,
        ]
        if prompt is not None:
            args = ["-p", prompt, *args]
        return self.run_fake(gitfix.FAKE_CLAUDE, args, env, **kw)


class TestCursorFake(FakeCase):
    def test_writes_files_and_envelope(self):
        env, _, _ = self.setup_env(
            {"cursor": {"grok-4.7-high": [{"files": {"src/a.txt": "hello\n"}}]}}
        )
        p = self.run_cursor(env)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(self.repo, "src", "a.txt")) as f:
            self.assertEqual(f.read(), "hello\n")
        env_out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(env_out["type"], "result")
        self.assertFalse(env_out["is_error"])
        self.assertIn("MR_RESULT {", env_out["result"])
        self.assertIn("inputTokens", env_out["usage"])
        self.assertIn("session_id", env_out)

    def test_delete_and_result_override(self):
        env, _, _ = self.setup_env(
            {"cursor": {"grok-4.7-high": [{"delete": ["README.md"], "result": "custom out"}]}}
        )
        p = self.run_cursor(env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "README.md")))
        self.assertEqual(json.loads(p.stdout)["result"], "custom out")

    def test_stream_json_init_then_result(self):
        env, _, _ = self.setup_env({"cursor": {"grok-4.7-high": [{}]}})
        p = self.run_cursor(env, extra=("--output-format", "stream-json"))
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        init = json.loads(lines[0])
        self.assertEqual((init["type"], init["subtype"]), ("system", "init"))
        self.assertEqual(init["model"], "grok-4.7-high")
        self.assertEqual(json.loads(lines[1])["type"], "result")

    def test_mode_ask_makes_no_writes(self):
        env, _, _ = self.setup_env(
            {
                "cursor": {
                    "m-good": [{"files": {"nope.txt": "x"}}],
                    "m-bad": [{"files": {"evil.txt": "x"}, "ignore_mode": True}],
                }
            }
        )
        p = self.run_cursor(env, model="m-good", extra=("--mode", "ask"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "nope.txt")))
        p = self.run_cursor(env, model="m-bad", extra=("--mode", "plan"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "evil.txt")))

    def test_rc_is_error_no_envelope_stderr(self):
        env, _, _ = self.setup_env(
            {
                "cursor": {
                    "m-err": [{"rc": 3, "is_error": True, "stderr": "boom happened"}],
                    "m-raw": [{"no_envelope": True}],
                }
            }
        )
        p = self.run_cursor(env, model="m-err")
        self.assertEqual(p.returncode, 3)
        self.assertIn("boom happened", p.stderr)
        self.assertTrue(json.loads(p.stdout)["is_error"])
        p = self.run_cursor(env, model="m-raw")
        self.assertEqual(p.returncode, 0)
        for line in p.stdout.strip().splitlines():
            with self.assertRaises(ValueError):
                json.loads(line)

    def test_violations_observable_via_git(self):
        kinds = ("branch", "switch", "tag", "stash", "worktree")
        for kind in kinds:
            with self.subTest(kind=kind):
                repo = gitfix.make_repo(os.path.join(self.dir, "repo-" + kind))
                env, _, _ = self.setup_env({"cursor": {"m-v": [{"violation": kind}]}})
                args = [
                    "-p",
                    "--model",
                    "m-v",
                    "--workspace",
                    repo,
                    "task",
                ]
                p = self.run_fake(gitfix.FAKE_CURSOR, args, env, cwd=repo)
                self.assertEqual(p.returncode, 0, p.stderr)
                if kind == "branch":
                    out = gitfix.git(repo, "branch", "--list", "mr-evil").stdout
                    self.assertIn("mr-evil", out)
                elif kind == "switch":
                    out = gitfix.git(repo, "symbolic-ref", "--short", "HEAD").stdout
                    self.assertEqual(out.strip(), "evil-switch")
                elif kind == "tag":
                    out = gitfix.git(repo, "tag", "--list", "evil-tag").stdout
                    self.assertIn("evil-tag", out)
                elif kind == "stash":
                    out = gitfix.git(repo, "stash", "list").stdout
                    self.assertIn("stash", out.lower())
                elif kind == "worktree":
                    out = gitfix.git(repo, "worktree", "list").stdout
                    self.assertIn("mr-evil-wt-", out)


class TestClaudeFake(FakeCase):
    def test_model_usage_and_cost(self):
        env, _, _ = self.setup_env(
            {
                "claude": {
                    "claude-sonnet-5-5": [{"cost": 0.12}],
                    "claude-opus-5-5": [{"model_served": "claude-opus-5-5-thinking"}],
                }
            }
        )
        p = self.run_claude(env)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["type"], "result")
        self.assertIn("claude-sonnet-5-5", out["modelUsage"])
        self.assertEqual(out["modelUsage"]["claude-sonnet-5-5"]["costUSD"], 0.12)
        self.assertEqual(out["total_cost_usd"], 0.12)
        self.assertIn("input_tokens", out["usage"])
        self.assertIn("MR_RESULT {", out["result"])
        p = self.run_claude(env, model="claude-opus-5-5")
        out = json.loads(p.stdout)
        self.assertIn("claude-opus-5-5-thinking", out["modelUsage"])

    def test_usage_override(self):
        env, _, _ = self.setup_env(
            {"claude": {"claude-sonnet-5-5": [{"usage": {"input_tokens": 4242}}]}}
        )
        p = self.run_claude(env)
        out = json.loads(p.stdout)
        self.assertEqual(out["usage"]["input_tokens"], 4242)
        self.assertEqual(out["usage"]["output_tokens"], 50)

    def test_exit_75_applies_nothing(self):
        env, sp, _ = self.setup_env(
            {"claude": {"claude-sonnet-5-5": [{"exit": 75, "files": {"x.txt": "y"}}]}}
        )
        p = self.run_claude(env)
        self.assertEqual(p.returncode, 75)
        self.assertEqual(p.stdout, "")
        self.assertIn("refusing to start - hourly limit reached", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "x.txt")))
        state = {}
        if os.path.exists(sp + ".state.json"):
            with open(sp + ".state.json") as f:
                raw = f.read()
            state = json.loads(raw) if raw.strip() else {}
        self.assertEqual(state.get("counters", {}).get("claude:claude-sonnet-5-5", 0), 0)

    def test_bare_forbidden(self):
        env, _, _ = self.setup_env({"claude": {"claude-sonnet-5-5": [{}]}})
        p = self.run_claude(env, extra=("--bare",))
        self.assertEqual(p.returncode, 99)
        self.assertIn("--bare forbidden", p.stderr)
        self.assertEqual(p.stdout, "")

    def test_hook_block(self):
        env, _, _ = self.setup_env(
            {"claude": {"claude-sonnet-5-5": [{"hook_block": True, "files": {"x.txt": "y"}}]}}
        )
        p = self.run_claude(env)
        self.assertEqual(p.returncode, 2)
        self.assertIn("blocked by hook", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "x.txt")))


class TestScenarioEngine(FakeCase):
    def test_step_counter_advances_and_last_repeats(self):
        env, sp, _ = self.setup_env(
            {"cursor": {"m-seq": [{"result": "one"}, {"result": "two"}]}}
        )
        got = [
            json.loads(self.run_cursor(env, model="m-seq").stdout)["result"] for _ in range(3)
        ]
        self.assertEqual(got, ["one", "two", "two"])
        with open(sp + ".state.json") as f:
            self.assertEqual(json.load(f)["counters"]["cursor:m-seq"], 3)

    def test_calls_jsonl_records_argv_env_prompt(self):
        env, sp, _ = self.setup_env({"cursor": {"grok-4.7-high": [{}]}})
        p = self.run_cursor(env, prompt="the prompt text")
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(sp + ".calls.jsonl") as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["cli"], "cursor")
        self.assertEqual(rec["model"], "grok-4.7-high")
        self.assertIn("--model", rec["argv"])
        self.assertEqual(rec["prompt"], "the prompt text")
        self.assertEqual(rec["stdin_prompt_len"], 0)
        self.assertEqual(rec["cwd"], self.repo)
        self.assertIn("MR_FAKE_SCENARIO", rec["env"]["env_keys"])
        self.assertNotIn("HOME", rec["env"])  # only the fixed keys + env_keys

    def test_calls_jsonl_stdin_prompt(self):
        env, sp, _ = self.setup_env({"claude": {"claude-sonnet-5-5": [{}]}})
        p = self.run_claude(env, prompt=None, stdin="piped prompt\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(sp + ".calls.jsonl") as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["prompt"], "piped prompt\n")
        self.assertEqual(rec["stdin_prompt_len"], len("piped prompt\n"))

    def test_time_scale_scales_sleep(self):
        env, _, _ = self.setup_env(
            {"cursor": {"m-slow": [{"sleep_s": 10}]}}, scale="0.001"
        )
        t0 = time.monotonic()
        p = self.run_cursor(env, model="m-slow")
        elapsed = time.monotonic() - t0
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertLess(elapsed, 1.0)


class TestFakeCheck(FakeCase):
    def run_check(self, env, cp, *args):
        return subprocess.run(
            [gitfix.FAKE_CHECK, *args],
            env={"MR_FAKE_CHECK": cp, "PATH": env.get("PATH", os.environ["PATH"])},
            capture_output=True,
            text=True,
        )

    def test_modes(self):
        env, _, cp = self.setup_env(
            {},
            check={
                "default": dict(CHECK_DEFAULT),
                "no_probe": "ok",
                "probe": "fail",
                "plain": "junk",
            },
        )
        p = self.run_check(env, cp, "--no-probe")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["hour_usage"], 10)
        p = self.run_check(env, cp)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "not json")
        p = self.run_check(env, cp, "--probe")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout, "")

    def test_exit1_prints_valid_json(self):
        env, _, cp = self.setup_env(
            {}, check={"default": dict(CHECK_DEFAULT), "no_probe": "exit1"}
        )
        p = self.run_check(env, cp, "--no-probe")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(json.loads(p.stdout)["limit"], 40)

    def test_overrides_after_probe_persistence_and_calls(self):
        env, _, cp = self.setup_env(
            {},
            check={
                "default": dict(CHECK_DEFAULT),
                "probe": {"hour_usage": 20},
                "after_probe": {"hour_usage": 25, "sample_age_s": 5},
            },
        )
        p = self.run_check(env, cp, "--no-probe")
        self.assertEqual(json.loads(p.stdout)["hour_usage"], 10)
        p = self.run_check(env, cp, "--probe")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["hour_usage"], 20)
        p = self.run_check(env, cp, "--no-probe")
        out = json.loads(p.stdout)
        self.assertEqual(out["hour_usage"], 25)
        self.assertEqual(out["sample_age_s"], 5)
        with open(cp + ".calls") as f:
            calls = [line.strip() for line in f]
        self.assertEqual(calls, ["--no-probe", "--probe", "--no-probe"])


class TestGitFix(FakeCase):
    def test_make_repo_and_helpers(self):
        repo = gitfix.make_repo(os.path.join(self.dir, "r2"), files={"a.py": "x = 1\n"})
        with open(os.path.join(repo, ".gitignore")) as f:
            self.assertEqual(f.read(), "build/\nnode_modules/\n")
        self.assertEqual(
            gitfix.git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip(), "main"
        )
        sha0 = gitfix.head(repo)
        self.assertEqual(len(sha0), 40)
        sha1 = gitfix.commit(repo, {"b.py": "y = 2\n"}, "second")
        self.assertNotEqual(sha0, sha1)
        self.assertEqual(gitfix.head(repo), sha1)

        gitfix.add_branch(repo, "mr/t1", checkout=True)
        self.assertEqual(
            gitfix.git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip(), "mr/t1"
        )

        gitfix.make_foreign_dirty(repo)
        self.assertIn("foreign.txt", gitfix.git(repo, "status", "--porcelain").stdout)
        gitfix.make_foreign_dirty(repo, path="tracked.txt", tracked=True)
        status = gitfix.git(repo, "status", "--porcelain").stdout
        self.assertIn(" M tracked.txt", status)

        wt = os.path.join(self.dir, "wt1")
        gitfix.add_worktree(repo, wt, "mr/wt")
        self.assertIn(wt, gitfix.git(repo, "worktree", "list").stdout)

        home = os.path.join(self.dir, "home")
        gitfix.add_decoys(repo, home)
        self.assertIn("alice/wip", gitfix.git(repo, "branch", "--list").stdout)
        self.assertTrue(os.path.isdir(os.path.join(repo, ".claude", "worktrees", "x")))
        self.assertTrue(
            os.path.isdir(
                os.path.join(home, ".cursor", "worktrees", os.path.basename(repo), "y")
            )
        )

    def test_fake_env(self):
        env, sp, cp = self.setup_env({})
        self.assertEqual(env["MR_FAKE_SCENARIO"], sp)
        self.assertEqual(env["MR_FAKE_CHECK"], cp)
        self.assertEqual(env["MR_TEST_TIME_SCALE"], "0.01")
        self.assertEqual(env["MR_TEST_ALLOW_FAKE_PATHS"], "1")
        for path in (gitfix.FAKE_CURSOR, gitfix.FAKE_CLAUDE, gitfix.FAKE_CHECK):
            self.assertTrue(os.path.isfile(path), path)
            self.assertTrue(os.access(path, os.X_OK), path)


if __name__ == "__main__":
    unittest.main()
