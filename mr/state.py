"""Task state files: ``<home>/tasks/<id>/state.json`` (mr-spec §10.5)."""
from __future__ import annotations

import glob
import json
import os
import tempfile


def _safe(task_id: str) -> str:
    s = str(task_id)
    if not s or "/" in s or "\\" in s or s in (".", "..") or "\0" in s:
        raise ValueError(f"unsafe task id: {s!r}")
    return s


def task_dir(home: str, task_id: str) -> str:
    return os.path.join(home, "tasks", _safe(task_id))


def _path(home: str, task_id: str) -> str:
    return os.path.join(task_dir(home, task_id), "state.json")


def write_state(home: str, task_id: str, state: dict) -> str:
    """Atomic write (temp file in the same dir + os.replace). Returns the path."""
    path = _path(home, task_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def _load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_state(home: str, task_id: str) -> dict | None:
    return _load(_path(home, task_id))


def _patch_nonempty(state: dict) -> bool:
    patch = state.get("patch")
    if not patch:
        rungs = state.get("rungs")
        patch = rungs[-1].get("patch") if isinstance(rungs, list) and rungs and isinstance(rungs[-1], dict) else None
    try:
        return bool(patch) and os.path.getsize(patch) > 0
    except OSError:
        return False


def find_w7_leftover(home: str, repo: str) -> dict | None:
    """Newest accepted W7 task on this repo (same realpath) that saved a non-empty patch.

    A W7 success leaves uncommitted changes in the user's tree, so the next run
    must not start on top of them (§10.3 "Task end").
    """
    want = os.path.realpath(repo)
    best: tuple[float, dict] | None = None
    for path in glob.glob(os.path.join(home, "tasks", "*", "state.json")):
        state = _load(path)
        if not state or state.get("isolation") != "W7" or state.get("outcome") != "accepted":
            continue
        if not isinstance(state.get("repo"), str) or os.path.realpath(state["repo"]) != want:
            continue
        if not _patch_nonempty(state):
            continue
        try:
            stamp = os.stat(path).st_mtime
        except OSError:
            continue
        if best is None or stamp >= best[0]:
            best = (stamp, state)
    return best[1] if best else None
