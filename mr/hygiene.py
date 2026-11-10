"""Hygiene signal H, base-relative (spec §5.9, §10.3)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass

from mr import acceptance

# A failure id is ``kind:where:case``. ``where`` is the file the failure belongs to (the dotted test id for
# unittest) and holds no colon; a repeat of the same id gets ``#2``, ``#3``. So a base failure masks only the
# same case, once. A command that exits nonzero with no parsed case is ``cmd:<command>:exit<rc>``.
_TSC = re.compile(r"^(?P<file>[^\s(]+)\(\d+,\d+\): error (?P<code>TS\d+)(?P<msg>.*)")
_LINT = re.compile(r"^(?P<file>[\w./-]+):(\d+):(\d+)")
_UNITTEST = re.compile(r"^(?P<outcome>FAIL|ERROR): (?P<name>\S+) \((?P<qual>[^)\s]+)\)(?P<sub>.*)$")
_PYTEST = re.compile(r"^(?P<outcome>FAILED|ERROR) (?P<file>[^\s:(][^\s:]*)(?P<node>::\S+)?")


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


def _kill(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _run(tree: str, cmd: str, timeout_s) -> tuple[int, str, str, bool]:
    proc = subprocess.Popen(
        cmd, cwd=tree, shell=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out_b, err_b = proc.communicate(timeout=float(timeout_s) * _scale())
        timed = False
    except subprocess.TimeoutExpired:
        timed = True
        _kill(proc.pid)
        out_b, err_b = proc.communicate()
    except BaseException:  # a signal: the command runs in its own session, so stop it before mr moves on
        _kill(proc.pid)
        proc.wait()
        raise
    out = (out_b or b"").decode("utf-8", "replace")
    err = (err_b or b"").decode("utf-8", "replace")
    return proc.returncode or 0, out, err, timed


def _digest(text: str) -> str:
    """Digits out, so a diagnostic that only moved keeps its id."""
    return hashlib.sha1(re.sub(r"\d+", "", text).encode()).hexdigest()[:8]


def _vitest(tree: str, out: str) -> list[str] | None:
    start, end = out.find("{"), out.rfind("}")
    try:
        if start < 0 or end < start:
            raise ValueError("no json")
        data = json.loads(out[start:end + 1])
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (ValueError, json.JSONDecodeError):
        return None
    ids: list[str] = []
    for result in data.get("testResults") or []:
        if not isinstance(result, dict):
            continue
        name = str(result.get("name") or "").replace("\\", "/")
        rel = os.path.relpath(name, tree) if os.path.isabs(name) else name
        rel = rel[2:] if rel.startswith("./") else rel
        failed = [f"vitest:{rel}::{item.get('fullName') or ''}" for item in result.get("assertionResults") or []
                  if isinstance(item, dict) and item.get("status") == "failed"]
        ids += failed
        if result.get("message") or (result.get("status") == "failed" and not failed):
            ids.append(f"vitest:{rel}::(suite)")
    return ids


def _cases(text: str) -> list[str]:
    ids = []
    for raw in text.splitlines():
        line = raw.strip()
        if m := _UNITTEST.match(line):
            where = m["name"] if "_FailedTest" in m["qual"] else m["qual"]
            ids.append(f"unittest:{where}:{m['outcome']} {m['name']}{m['sub']}")
        elif m := _PYTEST.match(line):
            ids.append(f"pytest:{m['file']}:{m['outcome']} {m['node'] or ''}")
        elif m := _LINT.match(line):
            ids.append(f"lint:{m['file']}:{_digest(line)}")
    return ids


def _numbered(ids: list[str]) -> set[str]:
    seen: dict[str, int] = {}
    out: set[str] = set()
    for fid in ids:
        seen[fid] = seen.get(fid, 0) + 1
        out.add(fid if seen[fid] == 1 else f"{fid}#{seen[fid]}")
    return out


def _parse(tree: str, cmd: str, rc: int, out: str, err: str) -> set[str]:
    text = f"{out}\n{err}"
    if "tsc" in cmd:
        ids = [f"tsc:{m['file']}:{m['code']}:{_digest(m['msg'])}"
               for line in text.splitlines() if (m := _TSC.match(line.strip()))]
    elif "vitest" in cmd and "--reporter=json" in cmd:
        ids = _vitest(tree, out)
        if ids is None:
            return {f"vitest:{cmd}:unparsable"} if rc != 0 else set()
    elif rc == 0:
        return set()
    else:
        ids = _cases(text)
    return _numbered(ids) or ({f"cmd:{cmd}:exit{rc}"} if rc != 0 else set())


def _locked(fid: str, paths) -> bool:
    """True when the failure belongs to a locked test file (signal A owns those)."""
    kind, where = (fid.split(":", 2) + [""])[:2]
    if kind == "unittest":
        parts = where.split(".")
        return any(acceptance.module_matches(".".join(parts[:i]), paths) for i in range(1, len(parts) + 1))
    return kind != "cmd" and where in paths


def run_checks(tree, commands: list[str], timeout_s) -> frozenset[str]:
    ids: set[str] = set()
    for cmd in commands:
        rc, out, err, timed = _run(tree, cmd, timeout_s)
        ids.add(f"cmd:{cmd}:timeout") if timed else ids.update(_parse(tree, cmd, rc, out, err))
    return frozenset(ids)


def _commands(cfg, repo_name) -> list[str]:
    found = (cfg.hygiene.get("commands") or {}).get(repo_name)
    return list(found) if found else []


def unchecked(cfg, repo_name) -> str | None:
    """The refusal text when the repo has no hygiene commands, so mr could not check any change; else None."""
    if _commands(cfg, repo_name):
        return None
    return (f"no hygiene commands for {repo_name}, so mr cannot check a change; add hygiene.commands.{repo_name} "
            f"to the config, for example hygiene: {{commands: {{{repo_name}: [\"npm test\"]}}}}, "
            "or pass --no-checks to run anyway")


def base_failures(tree, cfg, repo_name) -> frozenset[str]:
    """Measured on every run, never cached: the tree sha cannot see untracked deps, toolchains or the timeout."""
    return run_checks(tree, _commands(cfg, repo_name), cfg.timeouts.hygiene_s)


def unparsed(cfg, repo_name, base_set) -> list[str]:
    """The hygiene commands that fail on the base tree with no case id, so a base failure cannot mask one case."""
    return [cmd for cmd in _commands(cfg, repo_name)
            if any(f.startswith(f"cmd:{cmd}:") or f == f"vitest:{cmd}:unparsable" for f in base_set)]


def refusal(commands: list[str], base_set) -> str:
    return "; ".join(f"hygiene command {cmd!r} timed out on the base tree; fix it or raise timeouts.hygiene_s"
                     if f"cmd:{cmd}:timeout" in base_set else
                     f"hygiene command {cmd!r} already fails on the base tree and mr cannot tell its failures "
                     "apart; fix it or use a reporter mr can parse" for cmd in commands)


def hygiene(tree, cfg, base_set, repo_name, locked_ids: frozenset[str] = frozenset()) -> SignalH:
    base, commands = frozenset(f for f in base_set if not _locked(f, locked_ids)), _commands(cfg, repo_name)
    if not commands:
        return SignalH(True, frozenset(), (), len(base), True, f"no hygiene commands for {repo_name}")
    fails = frozenset(f for f in run_checks(tree, commands, cfg.timeouts.hygiene_s) if not _locked(f, locked_ids))
    new = tuple(sorted(fails - base))
    return SignalH(not new, fails, new, len(base), False, "ok" if not new else "new: " + ", ".join(new))
