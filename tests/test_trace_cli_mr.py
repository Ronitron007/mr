"""CLI tests for mr trace. Stdlib unittest, temp directories, no network."""
import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from mr import cli, exitcodes


TID = "t-c0ffee01"
LEAK_TID = "t-c0ffee02"
LEAK_TITLE = "Ship the billing export"
_STORY = (
    "Accepted on G-l (m-low) after first try \u00b7 2m 00s \u00b7 1.5 units \u00b7 mostly mr checks"
)


def _line(**row):
    return json.dumps(row) + "\n"


def _write_home(root, rows):
    log = os.path.join(root, "log")
    os.makedirs(log, exist_ok=True)
    with open(os.path.join(log, "runs.jsonl"), "w", encoding="utf-8") as fh:
        fh.writelines(rows)
    return root


def _clean_rows():
    return [
        _line(
            role="implementer",
            task=TID,
            rung="G-l",
            model="m-low",
            path="cursor",
            attempt=1,
            started_at="2026-10-30T01:00:00+05:30",
            wall_s=30,
            decision="accept",
        ),
        _line(
            role="task",
            task=TID,
            title="Add a flag",
            type="code",
            difficulty="easy",
            started_at="2026-10-30T01:00:00+05:30",
            ended_at="2026-10-30T01:02:00+05:30",
            wall_s=120,
            outcome="accepted",
            repo="scratch-repo",
            ladder=["G-l"],
            cost={"units": 1.5},
        ),
    ]


def _leak_rows():
    return [
        _line(
            role="implementer",
            task=LEAK_TID,
            rung="G-l",
            model="m-low",
            path="cursor",
            attempt=1,
            started_at="2026-10-30T01:00:00+05:30",
            wall_s=30,
            decision="accept",
        ),
        _line(
            role="task",
            task=LEAK_TID,
            title=LEAK_TITLE,
            difficulty=LEAK_TITLE,
            type="code",
            started_at="2026-10-30T01:00:00+05:30",
            ended_at="2026-10-30T01:02:00+05:30",
            wall_s=120,
            outcome="accepted",
            repo="scratch-repo",
        ),
    ]


class TraceCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        _write_home(self.home, _clean_rows())
        self.leak_home = os.path.join(self.tmp.name, "leak-home")
        _write_home(self.leak_home, _leak_rows())
        self.empty = os.path.join(self.tmp.name, "empty-home")
        os.makedirs(self.empty)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _config(self, home):
        path = os.path.join(self.tmp.name, "routes-%s.yaml" % os.path.basename(home))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('version: 1\npaths:\n  home: "%s"\n' % home)
        return path

    def test_help_lists_trace(self):
        code, out, err = self._run(["--help"])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertIn("trace", out)
        code, out, err = self._run(["plan"])
        self.assertEqual(code, exitcodes.USAGE, err)
        self.assertIn("mr plan: not built in v1 L0", err)

    def test_trace_help_lists_subcommands(self):
        code, out, err = self._run(["trace", "--help"])
        self.assertEqual(code, exitcodes.OK, err)
        for name in ("serve", "build", "demo", "show", "--home"):
            self.assertIn(name, out, out)

    def test_show_prints_story(self):
        code, out, err = self._run(["trace", "--home", self.home, "show", TID])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertIn("t-c0ffee01  Add a flag", out)
        self.assertIn(_STORY, out)
        self.assertIn("1. Implement on G-l (m-low) \u00b7 30s \u00b7 ok", out)

    def test_show_json(self):
        code, out, err = self._run(["--json", "trace", "--home", self.home, "show", TID])
        self.assertEqual(code, exitcodes.OK, err)
        doc = json.loads(out)
        self.assertIsInstance(doc, dict)
        self.assertEqual(doc["task"], TID)
        self.assertEqual(doc["title"], "Add a flag")
        self.assertEqual(doc["status"], "accepted")
        self.assertIn("implementer", [step["role"] for step in doc["steps"]])

    def test_show_unknown_task(self):
        code, out, err = self._run(["trace", "--home", self.home, "show", "t-missing1"])
        self.assertEqual(code, exitcodes.NOT_ACCEPTED, err)
        self.assertIn("no such task", err)
        self.assertEqual(out.strip(), "")

    def test_demo_writes_scrubbed(self):
        html = os.path.join(self.tmp.name, "demo.html")
        data = os.path.join(self.tmp.name, "demo.json")
        with contextlib.chdir(self.tmp.name):
            code, out, err = self._run(
                ["trace", "--home", self.home, "demo", "-o", html, "--data", data]
            )
        self.assertEqual(code, exitcodes.OK, err)
        with open(html, encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("MRTRACE_DATA", page)
        self.assertNotIn("Add a flag", page)
        with open(data, encoding="utf-8") as fh:
            clean = json.loads(fh.read())
        self.assertEqual(clean[0]["title"], "Code task, easy, 1 attempt (demo)")
        self.assertNotIn("Add a flag", json.dumps(clean))
        self.assertNotIn(LEAK_TITLE, json.dumps(clean))

    def test_demo_refuses_surviving_title(self):
        html = os.path.join(self.tmp.name, "leak.html")
        data = os.path.join(self.tmp.name, "leak.json")
        with contextlib.chdir(self.tmp.name):
            code, out, err = self._run(
                ["trace", "--home", self.leak_home, "demo", "-o", html, "--data", data]
            )
        self.assertEqual(code, exitcodes.NOT_ACCEPTED, err)
        self.assertIn("real task titles/labels survived scrubbing", err)
        self.assertNotIn("secret-looking", err)
        self.assertFalse(os.path.exists(html))
        self.assertFalse(os.path.exists(data))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "docs", "index.html")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "docs", "demo-traces.json")))
        self.assertNotIn("wrote", out)

    def test_build_writes_html(self):
        html = os.path.join(self.tmp.name, "snap.html")
        with contextlib.chdir(self.tmp.name):
            code, out, err = self._run(["trace", "--home", self.home, "build", "-o", html])
            self.assertEqual(code, exitcodes.OK, err)
            code2, out2, err2 = self._run(["trace", "--home", self.home, "build"])
        self.assertEqual(code2, exitcodes.OK, err2)
        with open(html, encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("MRTRACE_DATA", page)
        self.assertIn("Add a flag", page)
        default = os.path.join(self.tmp.name, "mr-trace.local.html")
        self.assertTrue(os.path.isfile(default), out2)
        with open(default, encoding="utf-8") as fh:
            self.assertIn("MRTRACE_DATA", fh.read())

    def test_serve_calls_server(self):
        with mock.patch("mr.trace.serve.serve") as srv:
            code, out, err = self._run(["trace", "--home", self.home, "serve"])
        self.assertEqual(code, exitcodes.OK, err)
        srv.assert_called_once()
        args, kwargs = srv.call_args
        self.assertEqual(args[0], self.home)
        host = kwargs.get("host", args[1] if len(args) > 1 else None)
        port = kwargs.get("port", args[2] if len(args) > 2 else None)
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 8765)
        with mock.patch("mr.trace.serve.serve") as srv:
            code, out, err = self._run(
                ["trace", "--home", self.home, "serve", "--host", "0.0.0.0", "--port", "9"]
            )
        self.assertEqual(code, exitcodes.OK, err)
        args, kwargs = srv.call_args
        host = kwargs.get("host", args[1] if len(args) > 1 else None)
        port = kwargs.get("port", args[2] if len(args) > 2 else None)
        self.assertEqual(args[0], self.home)
        self.assertEqual(host, "0.0.0.0")
        self.assertEqual(port, 9)

    def test_unknown_trace_subcommand(self):
        code, out, err = self._run(["trace", "nope"])
        self.assertEqual(code, exitcodes.USAGE, err)
        self.assertIn("nope", err)

    def test_home_flag_over_env(self):
        env = os.environ.copy()
        env["MR_HOME"] = self.empty
        with mock.patch.dict(os.environ, env, clear=True):
            code, out, err = self._run(["trace", "--home", self.home, "show", TID])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertIn("Add a flag", out)

    def test_home_env_over_config(self):
        cfg = self._config(self.empty)
        env = os.environ.copy()
        env["MR_HOME"] = self.home
        with mock.patch.dict(os.environ, env, clear=True):
            code, out, err = self._run(["--config", cfg, "trace", "show", TID])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertIn("Add a flag", out)

    def test_home_from_config(self):
        cfg = self._config(self.home)
        env = os.environ.copy()
        env.pop("MR_HOME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            code, out, err = self._run(["--config", cfg, "trace", "show", TID])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertIn("Add a flag", out)

    def test_route_does_not_import_trace(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = "\n".join(
            [
                "import importlib, sys",
                "from mr.cli import main",
                "rc = main(['route', 'add a flag', '--no-models'])",
                "bad = sorted(n for n in sys.modules if n.startswith('mr.trace'))",
                "sys.stderr.write('ROUTE_RC=%s\\n' % rc)",
                "sys.stderr.write('TRACE_MODS=%s\\n' % ','.join(bad))",
                "importlib.import_module('mr.commands.trace')",
                "bad2 = sorted(n for n in sys.modules if n.startswith('mr.trace'))",
                "sys.stderr.write('AFTER_IMPORT=%s\\n' % ','.join(bad2))",
            ]
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("ROUTE_RC=0\n", proc.stderr)
        self.assertIn("TRACE_MODS=\n", proc.stderr)
        self.assertIn("AFTER_IMPORT=\n", proc.stderr)

    def test_trace_main_removed(self):
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("mr.trace.main")


if __name__ == "__main__":
    unittest.main()
