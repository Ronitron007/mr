"""Task state files: ``<home>/tasks/<id>/state.json`` (mr-spec §10.5)."""
from __future__ import annotations

import fcntl
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


def queue_path(home: str) -> str:
    return os.path.join(home, "state", "queue.jsonl")


def read_queue(home: str) -> list[dict]:
    """The waiting tasks, oldest first. A line that is not a JSON object is skipped."""
    try:
        with open(queue_path(home), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("task") and not entry.get("done"):
            out.append(entry)
    return out


def _rewrite_queue(home: str, entries: list[dict]) -> None:
    path = queue_path(home)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _edit_queue(home: str, change) -> None:
    """Read, change and atomically rewrite the queue while holding a lock, so two runs cannot lose a line."""
    os.makedirs(os.path.dirname(queue_path(home)), exist_ok=True)
    with open(queue_path(home) + ".lock", "a", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        _rewrite_queue(home, change(read_queue(home)))


def queue_put(home: str, entry: dict) -> None:
    """One line per task: a task that defers again replaces its line."""
    _edit_queue(home, lambda entries: [e for e in entries if e["task"] != entry["task"]] + [entry])


def queue_remove(home: str, task_id: str) -> None:
    _edit_queue(home, lambda entries: [e for e in entries if e["task"] != task_id])


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
