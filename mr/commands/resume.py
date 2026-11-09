"""`mr resume <task>`: continue a task that waited for a check judge (state.json next "check_judge").

The harness (context, records, output, signals) is mr.commands.run's; the loop picks the task up where it stopped.
"""
from __future__ import annotations

import argparse
import sys
import time

from mr import config, exitcodes as ec, hygiene, isolation, loop, patches, pipeline, state
from mr.commands import run as run_cmd
from mr.log import now_ist
from mr.task import Params, Task


def _say(msg: str) -> None:
    print(msg, file=sys.stderr)


def _deferred(home: str, task_id: str) -> tuple[dict, dict] | None:
    """(state.json, queue entry) of a task that waits for its judge, else None."""
    try:
        st = state.read_state(home, task_id)
    except ValueError:
        return None
    pending = st.get("pending") if st else None
    if not st or st.get("next") != "check_judge" or not isinstance(pending, dict) or "ctx" not in pending:
        return None
    entry = next((e for e in state.read_queue(home) if e["task"] == task_id), None)
    return (st, entry) if entry else None


def _refusal(st: dict) -> str | None:
    """Why the repo is not as the deferred run left it, or None. Nothing is changed."""
    repo, task_id = st["repo"], st["task"]
    head = pipeline.git(repo, "rev-parse", "HEAD").stdout.strip() or None
    if head != st.get("head"):
        return f"{task_id}: HEAD moved since it was deferred (saved {st.get('head')}, now {head}); nothing was changed"
    now = patches.take_reset_tree(repo)
    if now != st["pending"]["tree"]:
        return f"{task_id}: the work tree changed since it was deferred; nothing was changed"
    if st["pending"]["ctx"].get("base_set_format") != hygiene.ID_FORMAT and now != st.get("reset_point"):
        return (f"{task_id}: its hygiene base uses an old id format and the work tree is not at its reset tree "
                f"({st.get('reset_point')}), so mr cannot measure the base again; nothing was changed")
    return None


def _task(st: dict) -> Task:
    t = st["pending"]["ctx"]["task"]
    params = Params(**{**t["params"], "pin": tuple(t["params"].get("pin") or ())})
    return Task(st["task"], t["text"], st["repo"], st["base"], params, tuple(t["hint_files"]), t["source"],
                tuple(t["deprecated"]))


def _context(args, cfg, st: dict, queued_at: str):
    ctx = st["pending"]["ctx"]
    ns = argparse.Namespace(**ctx["args"], config=args.config, json=args.json, verbose=args.verbose,
                            command="resume", once=False)
    c = run_cmd._Ctx(ns, cfg, _task(st), None, loop.decision_from(ctx["decision"]), list(st["ladder"]),
                     time.time(), now_ist())
    c.facts, c.resumed_from = None, queued_at
    loop.restore(c, st)
    return c


def run(args) -> int:
    cfg = config.load(args.config)
    home, task_id = cfg.paths.home, args.task
    found = _deferred(home, task_id)
    if found is None:
        _say(f"mr resume: {task_id} is not deferred")
        return ec.USAGE
    st, entry = found
    lock = isolation.WriterLock(cfg, st["repo"], task_id)
    try:
        if not lock.acquire():
            _say(f"mr resume: {task_id}: lock busy, a run is in progress on {st['repo']}")
            return ec.UNSAFE_GIT
    except isolation.StaleLock as e:
        _say(f"mr resume: stale writer lock {e.path}: task {e.task} did not exit cleanly; check the tree, "
             "then empty the lock file")
        return ec.UNSAFE_GIT
    try:
        if (why := _refusal(st)) is not None:
            _say(f"mr resume: {why}")
            lock.release()
            return ec.TREE_MOVED
        c = _context(args, cfg, st, entry["queued_at"])
    except BaseException:
        lock.release()
        raise
    c.lock = lock
    code = run_cmd.run(c.args, (c, lambda: loop.resume_task(c, st["pending"])))
    if c.pending is None:
        state.queue_remove(home, task_id)
    return code
