"""CLI runners: cursor-agent and Claude subprocesses (mr-spec §5.7, §10.3, §10.4)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass

from mr import advisor, config, guard

STDERR_CAP = 64 * 1024
STDERR_TAIL_CHARS = 2000
TERM_GRACE_S = 5.0
MR_RESULT_WINDOW = 50

ALLOWED_TOOLS = (
    "Read Grep Glob Edit Write Bash(npm run *) Bash(npx tsc*) Bash(npx vitest*) "
    "Bash(git diff*) Bash(git status*)"
)
DISALLOWED_TOOLS = (
    "Bash(git worktree *) Bash(git checkout *) Bash(git switch *) Bash(git branch *) "
    "Bash(git reset *) Bash(git stash *) Bash(git commit *) Bash(git push *)"
)

_children: set[int] = set()  # pgids of live children (for the signal handler)


@dataclass(frozen=True)
class RunResult:
    rc: int
    is_error: bool
    result_text: str
    usage: dict
    model_used: str | None
    wall_s: float
    duration_ms: int | None
    mr_result: dict | None
    raw_path: str
    stderr_tail: str = ""
    timed_out: bool = False
    has_envelope: bool = False
    session_id: str | None = None
    usd_equiv: float | None = None
    turns: int | None = None
    model_drift: bool = False


# --- parsing ----------------------------------------------------------------

def parse_mr_result(text: str, tag: str = "MR_RESULT") -> dict | None:
    """§7.3: last matching line of the last 50 lines; its JSON must be an object."""
    prefix = f"{tag} "
    found = None
    for line in (text or "").splitlines()[-MR_RESULT_WINDOW:]:
        if line.startswith(prefix):
            found = line[len(prefix):]
    if found is None:
        return None
    try:
        data = json.loads(found)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _envelope(stdout: str) -> dict | None:
    """Whole stdout as one object, else the LAST line that is a {"type":"result"} object."""
    try:
        whole = json.loads(stdout)
        if isinstance(whole, dict) and whole.get("type") == "result":
            return whole
    except ValueError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return None


def _init_model(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if '"init"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "system" and obj.get("subtype") == "init":
            model = obj.get("model")
            if isinstance(model, str) and model:
                return model
    return None


def _num(val) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


# --- argv / env -------------------------------------------------------------

def child_env(claude: bool, advisor: bool = False) -> dict:
    """advisor=True is the S+A exception to AC-RUN-3 (advisor-consult-spec G6)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLOUDFLARE_") and k != "OPENROUTER_API_KEY"}
    env["GIT_CONFIG_COUNT"] = "3"
    for i, val in enumerate(("https://", "git@", "ssh://")):
        env[f"GIT_CONFIG_KEY_{i}"] = "url.file:///nonexistent/.pushInsteadOf"
        env[f"GIT_CONFIG_VALUE_{i}"] = val
    if claude and advisor:
        env.pop("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", None)
        env.pop("DISABLE_TELEMETRY", None)
    elif claude:
        env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] = "1"
    return env


def cursor_argv(cfg, model: str, prompt: str, tree: str, mode: str | None) -> list[str]:
    if config.is_anthropic(model):  # hard rule §0.3 (AC-RUN-5)
        raise config.ConfigError(config.ANTHROPIC_MSG)
    return [cfg.paths.cursor_agent, "-p", "--output-format", "json", "--model", model,
            "--trust", "--force", "--workspace", tree] + (["--mode", mode] if mode else []) + [prompt]


def claude_argv_tail(cfg, rung: str, prompt: str, permission_mode: str,
                     json_schema: str | None) -> list[str]:
    r = cfg.rungs[rung]
    adv = getattr(r, "advisor", None)
    fmt = ["stream-json", "--verbose"] if adv else ["json"]
    argv = ["-p", prompt, "--output-format", *fmt, "--model", r.model]
    eff = config.clamp_effort(r.model, r.effort)
    if eff is not None:
        argv += ["--effort", eff]
    if adv:  # S+A (advisor-consult-spec §5.1, §5.3)
        argv += ["--advisor", adv, "--append-system-prompt", advisor.system_prompt()]
    argv += ["--permission-mode", permission_mode, "--allowedTools", ALLOWED_TOOLS,
             "--disallowedTools", DISALLOWED_TOOLS, "--no-session-persistence"]
    if json_schema:
        argv += ["--json-schema", json_schema]
    return argv


# --- process execution ------------------------------------------------------

def _scale(env: dict | None = None) -> float:
    try:
        return float((env if env is not None else os.environ).get("MR_TEST_TIME_SCALE") or 1)
    except ValueError:
        return 1.0


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _text(data: bytes | str | None) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


def exec_child(argv, cwd, env, timeout_s, raw_base) -> tuple[int, str, str, float, bool]:
    """Run argv in its own process group. Returns (rc, stdout, stderr, wall_s, timed_out)."""
    scale = _scale(env)
    start = time.monotonic()
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    pgid = proc.pid
    _children.add(pgid)
    timed_out = False
    try:
        try:
            out, err = proc.communicate(timeout=timeout_s * scale)
        except subprocess.TimeoutExpired:
            timed_out = True
            _killpg(pgid, signal.SIGTERM)
            try:
                out, err = proc.communicate(timeout=TERM_GRACE_S * scale)
            except subprocess.TimeoutExpired:
                _killpg(pgid, signal.SIGKILL)
                out, err = proc.communicate()
        _killpg(pgid, signal.SIGKILL)  # reap stragglers left in the group
    finally:
        _children.discard(pgid)
    wall_s = time.monotonic() - start
    err = (err or b"")[-STDERR_CAP:]
    os.makedirs(os.path.dirname(raw_base), exist_ok=True)
    with open(f"{raw_base}.out", "wb") as fh:
        fh.write(out or b"")
    with open(f"{raw_base}.err", "wb") as fh:
        fh.write(err)
    return proc.returncode, _text(out), _text(err), wall_s, timed_out


def kill_all_children() -> None:
    """TERM every registered group, wait up to 5 s (scaled), then KILL."""
    groups = list(_children)
    for pgid in groups:
        _killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + TERM_GRACE_S * _scale()
    while time.monotonic() < deadline and any(_alive(p) for p in groups):
        time.sleep(0.05)
    for pgid in groups:
        _killpg(pgid, signal.SIGKILL)


def _alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# --- runners ----------------------------------------------------------------

def _raw_base(cfg, task: str, attempt: int, model: str) -> str:
    return f"{cfg.paths.home}/raw/{task}/a{attempt}-{model}"


def run_cursor(model: str, prompt: str, tree: str, mode: str | None, timeout_s: int, *,
               cfg, task: str, attempt: int) -> RunResult:
    if config.is_anthropic(model):  # hard rule §0.3 (AC-RUN-5): refuse before spawning
        raise config.ConfigError(config.ANTHROPIC_MSG)
    base = _raw_base(cfg, task, attempt, model)
    rc, out, err, wall_s, timed_out = exec_child(
        cursor_argv(cfg, model, prompt, tree, mode), tree, child_env(False), timeout_s, base)
    env = _envelope(out)
    env_d = env or {}
    text = env_d.get("result") if isinstance(env_d.get("result"), str) else ""
    usage = env_d.get("usage") if isinstance(env_d.get("usage"), dict) else {}
    dur = env_d.get("duration_ms")
    sid = env_d.get("session_id")
    return RunResult(
        rc=rc,
        is_error=rc != 0 or env is None or bool(env_d.get("is_error")) or timed_out,
        result_text=text, usage=usage, model_used=_init_model(out) or model, wall_s=wall_s,
        duration_ms=int(dur) if _num(dur) else None, mr_result=parse_mr_result(text),
        raw_path=base, stderr_tail=err[-STDERR_TAIL_CHARS:], timed_out=timed_out,
        has_envelope=env is not None, session_id=sid if isinstance(sid, str) else None)


def _claude_result(rung_model: str, rc: int, out: str, err: str, wall_s: float,
                   timed_out: bool, base: str) -> RunResult:
    env = _envelope(out)
    env_d = env or {}
    text = env_d.get("result") if isinstance(env_d.get("result"), str) else ""
    usage = env_d.get("usage") if isinstance(env_d.get("usage"), dict) else {}
    mu = env_d.get("modelUsage") if isinstance(env_d.get("modelUsage"), dict) else {}
    keys = list(mu)
    dur, cost, turns, sid = (env_d.get(k) for k in ("duration_ms", "total_cost_usd",
                                                    "num_turns", "session_id"))
    return RunResult(
        rc=rc, is_error=rc != 0 or env is None or bool(env_d.get("is_error")) or timed_out,
        result_text=text, usage=usage, model_used=",".join(keys) or None, wall_s=wall_s,
        duration_ms=int(dur) if _num(dur) else None, mr_result=parse_mr_result(text),
        raw_path=base, stderr_tail=err[-STDERR_TAIL_CHARS:], timed_out=timed_out,
        has_envelope=env is not None, session_id=sid if isinstance(sid, str) else None,
        usd_equiv=float(cost) if _num(cost) else None,
        turns=int(turns) if _num(turns) else None,
        model_drift=bool(keys) and rung_model not in keys)


def run_claude(rung: str, prompt: str, tree: str, permission_mode: str, json_schema: str | None,
               timeout_s: int, *, cfg, task: str, attempt: int,
               wait_lock_s: int | None = None) -> tuple[guard.GuardedRun, RunResult | None]:
    """Only entry point for Claude rungs; always goes through the usage guard."""
    model = cfg.rungs[rung].model
    with_advisor = bool(getattr(cfg.rungs[rung], "advisor", None))
    base = _raw_base(cfg, task, attempt, model)
    tail = claude_argv_tail(cfg, rung, prompt, permission_mode, json_schema)

    def exec_fn(argv: list[str]) -> RunResult:
        rc, out, err, wall_s, timed_out = exec_child(
            argv, tree, child_env(True, advisor=with_advisor), timeout_s, base)
        return _claude_result(model, rc, out, err, wall_s, timed_out, base)

    guarded = guard.run_claude_guarded(rung, tail, cfg, task=task, exec_fn=exec_fn,
                                       wait_lock_s=wait_lock_s)
    return guarded, guarded.result
