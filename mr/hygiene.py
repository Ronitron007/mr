"""Hygiene signal H, base-relative (spec §5.9, §10.3)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass

_TSC = re.compile(r"^(?P<file>[^\s(]+)\(\d+,\d+\): error (?P<code>TS\d+)")
_LINT = re.compile(r"^(?P<file>[\w./-]+):(\d+):(\d+)")


@dataclass(frozen=True)
class SignalH:
    ok: bool
    failures: frozenset[str]
    new_failures: tuple[str, ...]
    base_size: int
    skipped: bool
    detail: str


def _scale() -> float:
    try:
        return float(os.environ.get("MR_TEST_TIME_SCALE") or 1)
    except ValueError:
        return 1.0


def _run(tree: str, cmd: str, timeout_s) -> tuple[int, str, str, bool]:
    proc = subprocess.Popen(
        cmd, cwd=tree, shell=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out_b, err_b = proc.communicate(timeout=float(timeout_s) * _scale())
        timed = False
    except subprocess.TimeoutExpired:
        timed = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        out_b, err_b = proc.communicate()
    out = (out_b or b"").decode("utf-8", "replace")
    err = (err_b or b"").decode("utf-8", "replace")
    return proc.returncode or 0, out, err, timed


def _parse(tree: str, cmd: str, rc: int, out: str, err: str) -> set[str]:
    text = f"{out}\n{err}"
    if "tsc" in cmd:
        return {f"tsc:{m.group('file')}:{m.group('code')}"
                for line in text.splitlines() if (m := _TSC.match(line.strip()))}
    if "vitest" in cmd and "--reporter=json" in cmd:
        start, end = out.find("{"), out.rfind("}")
        try:
            if start < 0 or end < start:
                raise ValueError("no json")
            data = json.loads(out[start:end + 1])
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (ValueError, json.JSONDecodeError):
            return {f"vitest:{cmd}:unparsable"} if rc != 0 else set()
        ids: set[str] = set()
        for result in data.get("testResults") or []:
            if not isinstance(result, dict):
                continue
            name = str(result.get("name") or "").replace("\\", "/")
            rel = os.path.relpath(name, tree) if os.path.isabs(name) else name
            rel = rel[2:] if rel.startswith("./") else rel
            for item in result.get("assertionResults") or []:
                if isinstance(item, dict) and item.get("status") == "failed":
                    ids.add(f"vitest:{rel}::{item.get('fullName') or ''}")
        return ids
    if rc == 0:
        return set()
    ids = set()
    for line in text.splitlines():
        match = _LINT.match(line.strip())
        if match:
            bare = re.sub(r"\d+", "", line.strip())
            ids.add(f"lint:{match.group('file')}:{hashlib.sha1(bare.encode()).hexdigest()[:8]}")
    return ids or {f"cmd:{cmd}:exit{rc}"}


def run_checks(tree, commands: list[str], timeout_s) -> frozenset[str]:
    ids: set[str] = set()
    for cmd in commands:
        rc, out, err, timed = _run(tree, cmd, timeout_s)
        ids.add(f"cmd:{cmd}:timeout") if timed else ids.update(_parse(tree, cmd, rc, out, err))
    return frozenset(ids)


def _atomic_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _commands(cfg, repo_name) -> list[str]:
    found = (cfg.hygiene.get("commands") or {}).get(repo_name)
    return list(found) if found else []


def base_failures(tree, cfg, repo_name, reset_tree) -> frozenset[str]:
    path = os.path.join(cfg.paths.home, "state", f"hygiene-{repo_name}-{reset_tree}.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return frozenset(json.load(fh)["failures"])
    fails = run_checks(tree, _commands(cfg, repo_name), cfg.timeouts.hygiene_s)
    _atomic_json(path, {"failures": sorted(fails)})
    return fails


def hygiene(tree, cfg, base_set, repo_name, locked_ids: frozenset[str] = frozenset()) -> SignalH:
    base, commands = frozenset(base_set), _commands(cfg, repo_name)
    if not commands:
        return SignalH(True, frozenset(), (), len(base), True, f"no hygiene commands for {repo_name}")
    fails = run_checks(tree, commands, cfg.timeouts.hygiene_s)
    if locked_ids:
        fails = frozenset(fid for fid in fails if not any(p and f":{p}:" in f":{fid}:" for p in locked_ids))
    new = tuple(sorted(fails - base))
    return SignalH(not new, fails, new, len(base), False, "ok" if not new else "new: " + ", ".join(new))
