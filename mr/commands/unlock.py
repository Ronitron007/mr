"""`mr unlock --repo`: clear a crashed run's writer lock (mr-spec §10.3)."""
from __future__ import annotations

import json
import os
import sys

from mr import config, exitcodes as ec, isolation, state


def _patch_from_state(st: dict) -> str | None:
    patch = st.get("patch")
    if patch:
        return patch if isinstance(patch, str) else None
    rungs = st.get("rungs")
    if isinstance(rungs, list) and rungs and isinstance(rungs[-1], dict):
        p = rungs[-1].get("patch")
        return p if isinstance(p, str) else None
    return None


def run(args) -> int:
    cfg = config.load(args.config)
    repo = os.path.abspath(args.repo)
    path = isolation.lock_path(cfg, repo)

    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        print(f"no stale lock for {repo}")
        return ec.OK

    if isolation.is_held(cfg, repo):
        print("lock busy: a run is in progress", file=sys.stderr)
        return ec.UNSAFE_GIT

    with open(path, encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        print(f"no stale lock for {repo}")
        return ec.OK

    try:
        info = json.loads(raw)
    except ValueError:
        info = {}
    if not isinstance(info, dict):
        info = {}
    task = info.get("task")
    if task is not None:
        print(task)

    state_path = os.path.join(cfg.paths.home, "tasks", str(task), "state.json")
    if task and os.path.isfile(state_path):
        print(state_path)
        st = state.read_state(cfg.paths.home, str(task))
        if st:
            patch = _patch_from_state(st)
            if patch:
                print(patch)

    with open(path, "r+b") as f:
        f.truncate(0)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass

    return ec.OK
