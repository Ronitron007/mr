"""Callers resolve mr's data directory at call time."""
from __future__ import annotations

import importlib
import inspect
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import mr.cli as cli
import mr.skillsync as skillsync
from mr.log import append
from mr.paths import data_dir
from mr.task import Params, load_task, make_task
from mr.trace import load
from tests.gitfix import make_repo


def _inside_temp(path):
    temp = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(path)
    return real == temp or real.startswith(temp + os.sep)


def _capture_append(env, **extra):
    opened = []

    def fake_open(path, *args, **kwargs):
        opened.append(path)
        raise OSError("captured")

    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch("mr.log.os.makedirs"), mock.patch("mr.log.open", side_effect=fake_open):
            append({"role": "task", "task": "t-paths"}, **extra)
        expected = os.path.join(data_dir(), "log", "runs.jsonl")
    return opened, expected


def _id_paths(env, call):
    seen = []

    def spy(path):
        seen.append(path)
        return False

    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch("mr.task.os.path.exists", side_effect=spy):
            call()
        prefix = data_dir().rstrip("/") + "/patches/"
    return seen, prefix


def _write_row(home, task):
    log_dir = os.path.join(home, "log")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "runs.jsonl"), "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "role": "route",
            "task": task,
            "ts": "2026-01-01T00:00:00+05:30",
        }) + "\n")


def _read_trace(env):
    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch.object(load, "running_info", return_value={}):
            rows = load.read_rows(home=None)
            traces = load.build_traces(home=None, with_transcripts=False)
    return rows, traces


class TestPathsCallers(unittest.TestCase):
    def test_log_append_uses_data_dir_at_call_time(self):
        self.assertIsNone(inspect.signature(append).parameters["home"].default)
        with tempfile.TemporaryDirectory() as first:
            with tempfile.TemporaryDirectory() as second:
                opened, expected = _capture_append({"MR_HOME": first})
                self.assertEqual(opened, [expected])
                opened, expected = _capture_append({"MR_HOME": second})
                self.assertEqual(opened, [expected])
        with tempfile.TemporaryDirectory() as xdg:
            opened, expected = _capture_append({"MR_HOME": "", "XDG_DATA_HOME": xdg})
            self.assertEqual(opened, [expected])
            self.assertEqual(expected, os.path.join(xdg, "mr", "log", "runs.jsonl"))
        with tempfile.TemporaryDirectory() as home:
            opened, expected = _capture_append({"MR_HOME": home}, home=None)
            self.assertEqual(opened, [expected])

    def test_task_builders_use_data_dir_at_call_time(self):
        self.assertIsNone(inspect.signature(load_task).parameters["home"].default)
        with tempfile.TemporaryDirectory() as first:
            seen, prefix = _id_paths({"MR_HOME": first}, lambda: make_task(
                "portable callers", "/repo", "abc", Params(), files_named=(),
            ))
            self.assertTrue(seen)
            self.assertTrue(all(path.startswith(prefix) for path in seen), seen)
        with tempfile.TemporaryDirectory() as second:
            seen, prefix = _id_paths({"MR_HOME": second}, lambda: make_task(
                "portable callers two", "/repo", "abc", Params(), files_named=(),
            ))
            self.assertTrue(seen)
            self.assertTrue(all(path.startswith(prefix) for path in seen), seen)
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(os.path.join(tmp, "repo"), {"a.txt": "a\n"})
            home = os.path.join(tmp, "data")
            os.makedirs(home)
            seen, prefix = _id_paths(
                {"MR_HOME": home},
                lambda: load_task("portable callers", None, Params(), repo),
            )
            self.assertTrue(seen)
            self.assertTrue(all(path.startswith(prefix) for path in seen), seen)
            seen, prefix = _id_paths(
                {"MR_HOME": home},
                lambda: load_task("portable callers", None, Params(), repo, home=None),
            )
            self.assertTrue(seen)
            self.assertTrue(all(path.startswith(prefix) for path in seen), seen)
        with tempfile.TemporaryDirectory() as xdg:
            seen, prefix = _id_paths({"MR_HOME": "", "XDG_DATA_HOME": xdg}, lambda: make_task(
                "xdg callers", "/repo", "abc", Params(), files_named=(),
            ))
            self.assertTrue(seen)
            self.assertTrue(all(path.startswith(prefix) for path in seen), seen)
            self.assertEqual(prefix, os.path.join(xdg, "mr").rstrip("/") + "/patches/")

    def test_trace_load_reads_data_dir_at_call_time(self):
        self.assertIsNone(inspect.signature(load.read_rows).parameters["home"].default)
        self.assertIsNone(inspect.signature(load.build_traces).parameters["home"].default)
        self.assertFalse(hasattr(load, "MR_HOME"))
        with tempfile.TemporaryDirectory() as first:
            with tempfile.TemporaryDirectory() as second:
                _write_row(first, "t-aaaa1111")
                _write_row(second, "t-bbbb2222")
                rows_a, traces_a = _read_trace({"MR_HOME": first})
                rows_b, traces_b = _read_trace({"MR_HOME": second})
                self.assertEqual([row.get("task") for row in rows_a], ["t-aaaa1111"])
                self.assertEqual([row.get("task") for row in rows_b], ["t-bbbb2222"])
                self.assertEqual([item.get("task") for item in traces_a], ["t-aaaa1111"])
                self.assertEqual([item.get("task") for item in traces_b], ["t-bbbb2222"])
        with tempfile.TemporaryDirectory() as xdg:
            home = os.path.join(xdg, "mr")
            _write_row(home, "t-xdg3333")
            rows, traces = _read_trace({"MR_HOME": "", "XDG_DATA_HOME": xdg})
            self.assertEqual([row.get("task") for row in rows], ["t-xdg3333"])
            self.assertEqual([item.get("task") for item in traces], ["t-xdg3333"])

    def test_skillsync_notify_false_when_hook_unset(self):
        seen = []

        def access(path, mode):
            seen.append(path)
            return True

        with mock.patch.dict(os.environ, {"MR_SKILLSYNC": "1"}, clear=False):
            os.environ.pop("MR_SKILLSYNC_HOOK", None)
            with mock.patch("mr.skillsync.os.access", side_effect=access), mock.patch(
                "mr.skillsync.subprocess.Popen"
            ) as popen:
                started = skillsync.notify("run-end")
        self.assertFalse(started)
        popen.assert_not_called()
        self.assertEqual(seen, [])
        source = inspect.getsource(skillsync)
        self.assertFalse(hasattr(skillsync, "DEFAULT_HOOK"))
        self.assertNotIn("DEFAULT_HOOK", source)
        self.assertIsNone(re.search(r"/(home|Users)/[A-Za-z]", source))

    def test_cli_has_no_machine_config_constant(self):
        self.assertFalse(hasattr(cli, "_DEFAULT_CONFIG"))
        source = inspect.getsource(cli)
        self.assertNotIn("_DEFAULT_CONFIG", source)
        self.assertIsNone(re.search(r"/(home|Users)/[A-Za-z]", source))

    def test_import_points_homes_at_one_temp_dir(self):
        import tests as tests_pkg
        outside = "/not-a-temp-mr-home/slot"
        env = {
            "MR_HOME": outside,
            "XDG_CONFIG_HOME": outside,
            "XDG_DATA_HOME": outside,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            importlib.reload(tests_pkg)
            first = os.environ["MR_HOME"]
            self.assertTrue(_inside_temp(first), first)
            self.addCleanup(shutil.rmtree, first, True)
            self.assertEqual(os.environ["XDG_CONFIG_HOME"], first)
            self.assertEqual(os.environ["XDG_DATA_HOME"], first)
            self.assertEqual(
                os.path.dirname(os.path.realpath(first)),
                os.path.realpath(tempfile.gettempdir()),
            )
            os.environ["MR_HOME"] = outside
            os.environ["XDG_CONFIG_HOME"] = outside
            os.environ["XDG_DATA_HOME"] = outside
            importlib.reload(tests_pkg)
            second = os.environ["MR_HOME"]
            self.assertTrue(_inside_temp(second), second)
            self.addCleanup(shutil.rmtree, second, True)
            self.assertEqual(os.environ["XDG_CONFIG_HOME"], second)
            self.assertEqual(os.environ["XDG_DATA_HOME"], second)
            self.assertNotEqual(first, second)
            importlib.reload(tests_pkg)
            self.assertEqual(os.environ["MR_HOME"], second)
            self.assertEqual(os.environ["XDG_CONFIG_HOME"], second)
            self.assertEqual(os.environ["XDG_DATA_HOME"], second)
            self.assertEqual(os.environ.get("MR_SKILLSYNC"), "0")

    def test_run_all_uses_portable_python(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_all.sh")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("${PYTHON:-python3}", text)
        self.assertIsNone(re.search(r"/(home|Users)/[A-Za-z]", text))
        self.assertIn('cd "$(dirname "$0")/.."', text)
        self.assertIn("-m unittest discover -s tests -t .", text)
        self.assertIn('"$@"', text)

    def test_cli_route_log_uses_data_dir(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_cli_route.py")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIsNone(re.search(r"/(home|Users)/[A-Za-z]", text))
        self.assertIn("data_dir()", text)


if __name__ == "__main__":
    unittest.main()
