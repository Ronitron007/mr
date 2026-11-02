"""Steps shared by the L0 single-rung path (commands/run.py) and the full ladder (loop.run_task)."""
from __future__ import annotations

import os
import subprocess

from mr import exitcodes as ec
from mr import isolation, router, state

DESIGN = "design route not built; re-run with --route cascade or plan by hand"
NO_FILES = "no files named: keep the change minimal and inside the repo"


def git(repo: str, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *argv], cwd=repo, env=env, capture_output=True, text=True, check=False)


def start(c) -> int | None:
    """Route record, design exit, writer lock, isolation and the W5 branch.

    Returns an exit code when the task stopped here (c.stop already called), else None.
    """
    a, cfg, task, d = c.args, c.cfg, c.task, c.d
    home, repo = cfg.paths.home, task.repo
    c.log.append(router.route_record(task, c.feats, d, judge=getattr(c, "shadow_judge", None),
                                     clf=getattr(c, "shadow_clf", None),
                                     facts=getattr(c, "facts", None)))
    if d.route == "design" and a.route is None:
        return c.stop(ec.HUMAN, DESIGN)

    c.lock = isolation.WriterLock(cfg, repo, task.id)
    try:
        got = c.lock.acquire()
    except isolation.StaleLock as e:
        return c.stop(ec.UNSAFE_GIT, (
            f"stale writer lock {e.path}: task {e.task} (pid {e.pid}) did not exit cleanly; "
            f"see its patches in {os.path.join(home, 'patches', str(e.task))} and "
            f"{os.path.join(home, 'tasks', str(e.task), 'state.json')}; "
            "check the tree, then empty the lock file (mr unlock is L1)"))
    inputs = isolation.compute_inputs(
        repo, ro=False, risk=d.risk, difficulty=d.difficulty, risk_tags=d.risk_tags,
        experiment=bool(a.experiment), other_writer=not got, cfg=cfg, est_files=len(task.files_named))
    if inputs.DIRTY_FOREIGN:
        left = state.find_w7_leftover(home, repo)
        if left:
            return c.stop(ec.UNSAFE_GIT, f"commit or discard {left.get('task')}'s changes first")
    c.iso = isolation.decide_isolation(inputs)
    why = isolation.unsupported_reason(c.iso)
    if why:
        return c.stop(ec.UNSAFE_GIT, why)
    if c.iso == "W5":
        try:
            c.orig_branch, c.branch = isolation.create_branch_w5(repo, task.id, task.text)
        except RuntimeError as e:
            return c.stop(ec.UNSAFE_GIT, str(e))
    return None


def commit_w5(c) -> str | None:
    """W5: commit on the mr/* branch with a Mr-Task trailer. Returns an error text or None."""
    repo = c.task.repo
    env = dict(os.environ)
    for key, name, mail in (("user.name", "GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"),
                            ("user.email", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")):
        if not git(repo, "config", key).stdout.strip():
            val = "mr" if key == "user.name" else "mr@localhost"
            env[name] = env[mail] = val
    title = (c.task.text.strip().splitlines() or ["mr task"])[0][:72]
    for argv in (("add", "-A"), ("commit", "-m", title, "-m", f"Mr-Task: {c.task.id}")):
        out = git(repo, *argv, env=env)
        if out.returncode != 0:
            return (f"W5 commit failed on {c.branch} (changes left uncommitted): "
                    f"git {argv[0]}: {(out.stderr or out.stdout).strip()}")
    c.commit = git(repo, "rev-parse", "HEAD").stdout.strip() or None
    return None


def tok(usage: dict, *keys: str):
    for k in keys:
        v = usage.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return None


def call_points(guarded) -> tuple:
    """(claude_pts, pts_pre, pts_post) for one call (§7.4).

    claude_pts is the measured delta, or None when the delta is unknown or the call
    is not Claude. pts_pre is the guard hour before the call, rounded, or None when
    there is no Claude guard reading. pts_post is pts_pre plus the rounded delta
    when both are known, and None otherwise.
    """
    if guarded is None:
        return None, None, None
    hour = guarded.guard.hour
    delta = guarded.delta
    pre = int(round(hour)) if isinstance(hour, (int, float)) else None
    post = pre + int(round(delta)) if pre is not None and delta is not None else None
    return delta, pre, post


def add_cost(cost: dict, rr, guarded) -> None:
    """Accumulate one executed call into the task cost block (§7.4).

    A Claude call that ran adds guarded.delta, or the guard estimate reserved for
    that call (guard.est) when the delta is unknown. A measured delta of 0 stays 0.
    A pre-check deny (no result) adds nothing. A cursor call adds minutes only.
    """
    if guarded is not None and guarded.result is None:
        return
    if guarded is None:
        pts = 0
        minutes = round(rr.wall_s / 60, 2)
    else:
        pts = guarded.delta if guarded.delta is not None else guarded.guard.est
        minutes = 0.0
    cost["claude_pts"] += pts
    cost["usd_equiv"] += rr.usd_equiv or 0.0
    cost["grok_rung_min"] = round(cost["grok_rung_min"] + minutes, 2)
    cost["units"] = round(cost["grok_rung_min"] + 10 * cost["claude_pts"], 2)


def attempt_record(c, rung, code: str, rr, guarded, attempt: int, started: str, sig: dict,
                   cls: str | None, *, decision: str | None = None) -> dict:
    """The §7.4 implementer attempt record for one rung run."""
    usage = rr.usage or {}
    delta, pre, post = call_points(guarded)
    mr_result = rr.mr_result.get("status") if isinstance(rr.mr_result, dict) else None
    return {
        "role": "implementer", "task": c.task.id, "route": c.d.route, "rung": code,
        "model": rung.model, "model_served": rr.model_used, "path": rung.path, "attempt": attempt,
        "item": None, "started_at": started, "wall_s": round(rr.wall_s, 2),
        "session_id": rr.session_id, "rc": rr.rc, "is_error": bool(rr.is_error), "stop": None,
        "turns": rr.turns, "tool_calls": None,
        "in_tok": tok(usage, "inputTokens", "input_tokens"),
        "out_tok": tok(usage, "outputTokens", "output_tokens"),
        "cache_tok": tok(usage, "cacheReadTokens", "cache_read_input_tokens"),
        "usd_equiv": rr.usd_equiv, "pts_pre": pre, "pts_post": post,
        "claude_pts": delta, "diff": dict(c.diff), "mr_result": mr_result, "signals": dict(sig),
        "decision": decision or ("accept" if cls is None else "stop"), "failure_class": cls,
        "failure_source": "rules" if cls else None, "isolation": c.iso, "spec_id": None,
        "judge_ref": None, "patch": c.patch_path,
        "raw": rr.raw_path + ".out" if rr.raw_path else None,
    }
