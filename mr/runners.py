"""CLI runners: cursor-agent, Claude, droid and Codex subprocesses (mr-spec §5.7, §10.3, §10.4)."""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Literal

from mr import advisor, config, droid, guard

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
# claude keeps using the Max plan and droid never bills Factory credits: these keys reach no claude/cursor child
_DROPPED_KEYS = frozenset({"OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "FACTORY_API_KEY"})
_CODEX_KEYS = frozenset({"OPENAI_API_KEY", "CODEX_API_KEY"})  # codex keeps the ChatGPT login, never the OpenAI API

CodexFailure = Literal["auth", "auth_api_key", "usage_limit", "timeout", "crash", "contract"]
CODEX_LOGIN_TIMEOUT_S = 30
# Guesses until the paid smoke run codex.smoke (docs/codex/PLAN.md) checks them: the -c keys codex must accept,
# and every text below except "Logged in using ChatGPT" (seen from codex-cli 0.149.1 login status, on stderr).
CODEX_LOCKDOWN = ("-c", 'approval_policy="never"', "-c", 'web_search="disabled"', "-c", "features.hooks=false")
CODEX_LOGIN_CHATGPT, CODEX_LOGIN_API_KEY = "chatgpt", "api key"
CODEX_FAILURE_TEXTS: tuple[tuple[CodexFailure, tuple[str, ...]], ...] = (
    ("auth", ("401", "unauthorized", "login")),
    ("usage_limit", ("usage limit", "rate limit", "quota", "429")),
)


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
    backend: str | None = None  # "cursor" | "claude" | "droid" | "codex"
    droid_failure: str | None = None  # droid only: None on success, else droid.classify_failure
    codex_failure: CodexFailure | None = None  # codex only: None on success, else codex_failure or the preflight


# --- parsing ----------------------------------------------------------------

def parse_mr_result(text: str, tag: str = "MR_RESULT") -> dict | None:
    """§7.3: last matching line of the last 50 lines; its JSON must be an object. A string status is stripped and
    lowercased here, so every reader sees `blocked` for "BLOCKED" or "Blocked " (self_report.status_case)."""
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
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("status"), str):
        data["status"] = data["status"].strip().lower()
    return data


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
           if not k.startswith("CLOUDFLARE_") and k not in _DROPPED_KEYS}
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


def droid_argv(cfg, twin: dict, prompt_file: str, tree: str, *, probe: bool = False) -> list[str]:
    """The only place the droid argv is built. probe=True leaves out --auto (read-only call)."""
    auto = [] if probe else ["--auto", cfg.droid["auto"]]
    return [cfg.paths.droid, "exec", "-o", "json", "--settings", cfg.droid["settings"],
            "-m", droid.model_id(twin), *auto, "--cwd", tree, "--disable-builtin-skills", "-f", prompt_file]


def droid_child_env(cfg) -> dict:
    """child_env(False) plus the provider key variable(s). FACTORY_API_KEY never reaches droid (BYOK only)."""
    env = child_env(False)
    for name in droid.key_envs(cfg):
        if os.environ.get(name):
            env[name] = os.environ[name]
    env.pop("FACTORY_API_KEY", None)
    return env


def codex_argv(cfg, prompt: str, tree: str, last_path: str) -> list[str]:
    """The only place the codex argv is built (docs/codex/SPEC.md codex.run)."""
    cx = cfg.review.codex
    return [cfg.paths.codex, "exec", "--json", "--color", "never", "-o", last_path,
            "-m", cx.model, "-c", f'model_reasoning_effort="{cx.effort}"', "-s", "read-only", *CODEX_LOCKDOWN,
            "--ignore-user-config", "--ignore-rules", "--ephemeral", "-C", tree, prompt]


def codex_child_env() -> dict:
    """child_env(False), push block kept, without the OpenAI API keys (codex.env)."""
    return {k: v for k, v in child_env(False).items() if k not in _CODEX_KEYS}


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
        has_envelope=env is not None, session_id=sid if isinstance(sid, str) else None, backend="cursor")


def _mask_raw_files(base: str, keys: list[str]) -> None:
    """Replace every key value in the raw .out/.err files exec_child wrote."""
    for ext in (".out", ".err"):
        path = base + ext
        with open(path, encoding="utf-8", errors="replace") as fh:
            old = fh.read()
        new = droid.scrub(old, keys)
        if new != old:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)


def run_droid(code: str, twin: dict, prompt: str, tree: str, timeout_s: int, *,
              cfg, task: str, attempt: int, probe: bool = False) -> RunResult:
    """One `droid exec` run on a BYOK twin. The key is masked everywhere before anything is parsed."""
    droid.ensure_settings(cfg)
    base = _raw_base(cfg, task, attempt, f"droid-{twin['model']}")
    os.makedirs(os.path.dirname(base), exist_ok=True)
    with open(f"{base}.prompt", "w", encoding="utf-8") as fh:
        fh.write(prompt)
    rc, out, err, wall_s, timed_out = exec_child(
        droid_argv(cfg, twin, f"{base}.prompt", tree, probe=probe), tree, droid_child_env(cfg), timeout_s, base)
    keys = droid.key_values(cfg)
    _mask_raw_files(base, keys)
    out, err = droid.scrub(out, keys), droid.scrub(err, keys)
    env = _envelope(out)
    env_d = env or {}
    text = env_d.get("result") if isinstance(env_d.get("result"), str) else ""
    usage = env_d.get("usage") if isinstance(env_d.get("usage"), dict) else {}
    dur, turns, sid = env_d.get("duration_ms"), env_d.get("num_turns"), env_d.get("session_id")
    tail = err[-STDERR_TAIL_CHARS:]
    is_error = rc != 0 or env is None or bool(env_d.get("is_error")) or timed_out
    return RunResult(
        rc=rc, is_error=is_error, result_text=text, usage=usage, model_used=twin["model"], wall_s=wall_s,
        duration_ms=int(dur) if _num(dur) else None, mr_result=parse_mr_result(text), raw_path=base,
        stderr_tail=tail, timed_out=timed_out, has_envelope=env is not None,
        session_id=sid if isinstance(sid, str) else None, turns=int(turns) if _num(turns) else None,
        backend="droid", droid_failure=droid.classify_failure(text, tail) if is_error else None)


def codex_failure(text: str, stderr_tail: str, timed_out: bool) -> CodexFailure:
    """The class of a failed codex run; text is the turn.failed or error event message (codex.failure)."""
    if timed_out:
        return "timeout"
    hay = f"{text}\n{stderr_tail}".lower()
    for cls, needles in CODEX_FAILURE_TEXTS:
        if any(n in hay for n in needles):
            return cls
    return "crash"


def _login_failure(rc: int, status: str, timed_out: bool) -> CodexFailure | None:
    status = status.lower()
    if rc != 0 or timed_out:
        return "auth"
    if CODEX_LOGIN_API_KEY in status:  # an API key login bills the OpenAI API: refused
        return "auth_api_key"
    return None if CODEX_LOGIN_CHATGPT in status else "auth"


def _codex_events(out: str) -> tuple[str, dict, str | None, bool]:
    """(last agent_message text, last turn.completed usage, failure message or None, saw turn.completed)."""
    agent, usage, failed, completed = "", {}, None, False
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        typ, item = ev.get("type"), ev.get("item")
        if typ == "turn.completed":
            completed = True
            usage = ev["usage"] if isinstance(ev.get("usage"), dict) else {}
        elif typ in ("turn.failed", "error"):
            src = ev.get("error") if typ == "turn.failed" else ev
            msg = src.get("message") if isinstance(src, dict) else None
            failed = msg if isinstance(msg, str) else ""
        elif typ == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            agent = item.get("text") if isinstance(item.get("text"), str) else ""
    return agent, usage, failed, completed


def run_codex(cfg, prompt: str, tree: str, timeout_s: int, *, task: str, attempt: int) -> RunResult:
    """The only entry point for the Codex reviewer: a free `codex login status` preflight, then one
    read-only `codex exec`. No exec starts after an auth failure (codex.auth)."""
    model = cfg.review.codex.model
    base = _raw_base(cfg, task, attempt, model)
    last = f"{base}.last"
    with contextlib.suppress(FileNotFoundError):  # a re-ask or a later attempt reuses the name
        os.remove(last)
    env = codex_child_env()
    rc, out, err, wall_s, timed_out = exec_child(
        [cfg.paths.codex, "login", "status"], tree, env, CODEX_LOGIN_TIMEOUT_S, f"{base}-login")
    refused = _login_failure(rc, out + err, timed_out)
    if refused:
        return RunResult(rc=rc, is_error=True, result_text="", usage={}, model_used=model, wall_s=wall_s,
                         duration_ms=None, mr_result=None, raw_path=f"{base}-login",
                         stderr_tail=err[-STDERR_TAIL_CHARS:], backend="codex", codex_failure=refused)
    rc, out, err, wall_s, timed_out = exec_child(codex_argv(cfg, prompt, tree, last), tree, env, timeout_s, base)
    try:
        with open(last, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        text = ""
    agent, usage, failed, completed = _codex_events(out)
    if not text.strip():
        text = agent
    tail = err[-STDERR_TAIL_CHARS:]
    is_error = rc != 0 or timed_out or failed is not None or not text.strip()
    return RunResult(
        rc=rc, is_error=is_error, result_text=text, usage=usage, model_used=model, wall_s=wall_s,
        duration_ms=None, mr_result=parse_mr_result(text), raw_path=base, stderr_tail=tail, timed_out=timed_out,
        has_envelope=completed, backend="codex",
        codex_failure=codex_failure(failed or "", tail, timed_out) if is_error else None)


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
        model_drift=bool(keys) and rung_model not in keys, backend="claude")


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
