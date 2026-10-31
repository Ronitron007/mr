"""Judge call budget ledger (mr-spec §9.10). Every attempted call counts, timeouts too."""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from mr.log import IST

KEEP_S = 24 * 3600
SHADOW_FACTOR = 0.8


def _state(cfg, name: str) -> str:
    return os.path.join(cfg.paths.home, "state", name)


def _limits(cfg) -> tuple:
    block = ((cfg.judge or {}).get("budget")) or {}
    return block.get("calls_per_hour"), block.get("calls_per_day", 150), block.get("max_calls_per_task", 6)


def _parse(iso: str) -> float:
    text = iso.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    at = datetime.fromisoformat(text)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.timestamp()


def _valid(item) -> bool:
    try:
        _parse(item)
    except (AttributeError, ValueError):
        return False
    return True


@contextmanager
def _lock(cfg):
    os.makedirs(_state(cfg, ""), exist_ok=True)
    with open(_state(cfg, "judge-budget.lock"), "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read(cfg) -> dict:
    """The ledger, or an empty one when the file is missing or corrupt (the judge fails open)."""
    try:
        with open(_state(cfg, "judge-budget.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    recent = data.get("recent")
    by_task = data.get("calls_by_task")
    calls = data.get("calls_day")
    return {
        "ist_day": data.get("ist_day") if isinstance(data.get("ist_day"), str) else "",
        "calls_day": calls if isinstance(calls, int) and not isinstance(calls, bool) else 0,
        "recent": [r for r in recent if _valid(r)] if isinstance(recent, list) else [],
        "calls_by_task": ({k: v for k, v in by_task.items() if isinstance(v, int)}
                          if isinstance(by_task, dict) else {}),
    }


def _write(cfg, ledger: dict) -> None:
    path = _state(cfg, "judge-budget.json")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _stamps(ledger: dict) -> list[float]:
    return [_parse(item) for item in ledger["recent"]]


def charge(cfg, task_id: str, mode: str, now: float | None = None) -> tuple[bool, str | None, dict]:
    """Count one judge call, or deny it. Returns (allowed, reason, budget_after)."""
    now = time.time() if now is None else now
    per_hour, per_day, per_task = _limits(cfg)
    factor = SHADOW_FACTOR if mode == "shadow" else 1.0
    at = datetime.fromtimestamp(now, IST)
    today = at.date().isoformat()
    with _lock(cfg):
        ledger = _read(cfg)
        if ledger["ist_day"] != today:
            ledger.update(ist_day=today, calls_day=0, calls_by_task={})
        ledger["recent"] = [r for r, ts in zip(ledger["recent"], _stamps(ledger)) if ts > now - KEEP_S]
        calls_hour = sum(1 for ts in _stamps(ledger) if ts > now - 3600)
        calls_task = ledger["calls_by_task"].get(task_id, 0)
        denied = (ledger["calls_day"] >= per_day * factor
                  or calls_task >= per_task * factor
                  or (per_hour is not None and calls_hour >= per_hour * factor))
        if not denied:
            ledger["calls_day"] += 1
            ledger["calls_by_task"][task_id] = calls_task = calls_task + 1
            ledger["recent"].append(at.isoformat())
            calls_hour += 1
        _write(cfg, ledger)
        after = {"calls_hour": calls_hour, "calls_day": ledger["calls_day"], "calls_task": calls_task}
    return (False, "budget_denied", after) if denied else (True, None, after)


def calls_since(cfg, iso_ts: str) -> int:
    """Judge calls recorded later than iso_ts (a Z or offset suffix is accepted)."""
    since = _parse(iso_ts)
    ledger = _read(cfg)  # no lock: writers replace the file atomically, and the rung guard must not create state
    return sum(1 for ts in _stamps(ledger) if ts > since)
