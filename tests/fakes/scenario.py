"""Shared scenario engine for the mr test fakes (mr-spec 14.2).

A scenario file (env MR_FAKE_SCENARIO) is JSON:
{"cursor": {"<model>": [step, ...]}, "claude": {"<model>": [step, ...]}}.

Each invocation of a fake consumes the next step for its (cli, model) pair;
the counter lives in "<scenario>.state.json" under fcntl.flock. When the
steps run out, the last step is reused. Every invocation appends one JSON
line to "<scenario>.calls.jsonl".
"""

import fcntl
import json
import os
import subprocess
import time
import uuid

ENV_KEYS_FIXED = [
    "CLAUDE_CODE_DISABLE_ADVISOR_TOOL",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_KEY_1",
    "GIT_CONFIG_KEY_2",
    "GIT_CONFIG_VALUE_0",
    "GIT_CONFIG_VALUE_1",
    "GIT_CONFIG_VALUE_2",
]

DEFAULT_RESULT = (
    "done\n"
    'MR_RESULT {"item":null,"status":"done","requirements":[],'
    '"deviations":[],"pushback":null,"confidence":0.8}'
)


def scenario_path():
    path = os.environ.get("MR_FAKE_SCENARIO")
    if not path:
        raise SystemExit("fake: MR_FAKE_SCENARIO is not set")
    return path


def result_text(step):
    return step.get("result", DEFAULT_RESULT)


def next_step(cli, model, consume=True):
    """Return the step for this invocation (last step repeats when exhausted)."""
    path = scenario_path()
    with open(path, encoding="utf-8") as f:
        scenario = json.load(f)
    steps = scenario.get(cli, {}).get(model) or []
    state_path = path + ".state.json"
    with open(state_path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            state = json.load(f)
        except ValueError:
            state = {}
        counters = state.setdefault("counters", {})
        key = cli + ":" + model
        idx = counters.get(key, 0)
        step = steps[min(idx, len(steps) - 1)] if steps else {}
        if consume:
            counters[key] = idx + 1
            f.seek(0)
            f.truncate()
            json.dump(state, f)
            f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)
    return step


def record_call(cli, model, argv, cwd, prompt, stdin_prompt_len):
    env = {k: os.environ[k] for k in ENV_KEYS_FIXED if k in os.environ}
    env["env_keys"] = sorted(os.environ.keys())
    rec = {
        "cli": cli,
        "model": model,
        "argv": argv,
        "cwd": cwd,
        "env": env,
        "stdin_prompt_len": stdin_prompt_len,
        "prompt": prompt,
    }
    with open(scenario_path() + ".calls.jsonl", "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(rec) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)


def read_prompt(positional):
    """Prompt from a positional/-p argument, else piped stdin."""
    if positional:
        return positional, 0
    if not sys_stdin_isatty():
        data = sys_stdin_read()
        if data:
            return data, len(data)
    return "", 0


def sys_stdin_isatty():
    import sys

    return sys.stdin.isatty()


def sys_stdin_read():
    import sys

    try:
        return sys.stdin.read()
    except OSError:
        return ""


def apply_step(cwd, step, write_files=True):
    """Apply file writes/deletes, the git violation and the sleep, in cwd."""
    if write_files:
        for rel, content in (step.get("files") or {}).items():
            dest = os.path.join(cwd, rel)
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
        for rel in step.get("delete") or []:
            try:
                os.remove(os.path.join(cwd, rel))
            except FileNotFoundError:
                pass
    _violation(cwd, step.get("violation"))
    sleep_s = float(step.get("sleep_s") or 0)
    if sleep_s > 0:
        scale = float(os.environ.get("MR_TEST_TIME_SCALE") or "1")
        time.sleep(sleep_s * scale)


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _violation(cwd, kind):
    if not kind:
        return
    if kind == "branch":
        _git(cwd, "branch", "mr-evil")
    elif kind == "switch":
        _git(cwd, "checkout", "-b", "evil-switch")
    elif kind == "tag":
        _git(cwd, "tag", "evil-tag")
    elif kind == "retarget":
        _git(cwd, "branch", "-f", "keep-me", "HEAD")
    elif kind == "remote_move":
        _git(cwd, "update-ref", "refs/remotes/origin/main", "HEAD")
    elif kind == "stash":
        with open(os.path.join(cwd, "evil-stash.txt"), "w", encoding="utf-8") as f:
            f.write("stash me\n")
        _git(cwd, "stash", "-u")
    elif kind == "worktree":
        _git(cwd, "worktree", "add", os.path.join("..", "mr-evil-wt-" + uuid.uuid4().hex[:8]))
    else:
        raise SystemExit(f"fake: unknown violation {kind!r}")
