"""Judge circuit breaker (mr-spec §9.8). State lives in a file so every process shares it."""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager

FAILURES = frozenset({"timeout", "cli_error", "invalid_response", "model_drift"})
RESETS = frozenset({"ok", "ok_partial"})


def _state(cfg, name: str) -> str:
    return os.path.join(cfg.paths.home, "state", name)


def _limits(cfg) -> tuple[int, float]:
    block = ((cfg.judge or {}).get("breaker")) or {}
    return block.get("consecutive_failures", 3), block.get("open_minutes", 15) * 60.0


@contextmanager
def _lock(cfg):
    os.makedirs(_state(cfg, ""), exist_ok=True)
    with open(_state(cfg, "judge-breaker.lock"), "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read(cfg) -> dict:
    try:
        with open(_state(cfg, "judge-breaker.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(cfg, data: dict) -> None:
    path = _state(cfg, "judge-breaker.json")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _entry(data: dict, backend: str) -> dict:
    item = data.get(backend)
    item = item if isinstance(item, dict) else {}
    return {"fails": item.get("fails") if isinstance(item.get("fails"), int) else 0,
            "opened_at": item.get("opened_at"), "trial_at": item.get("trial_at")}


def allow(cfg, backend: str, now: float | None = None) -> bool:
    """False while the breaker is open; after the open time exactly one half-open trial gets True."""
    now = time.time() if now is None else now
    _, open_s = _limits(cfg)
    with _lock(cfg):
        data = _read(cfg)
        item = _entry(data, backend)
        opened = item["opened_at"]
        if not isinstance(opened, (int, float)):
            return True
        if now < opened + open_s:
            return False
        trial = item["trial_at"]
        if isinstance(trial, (int, float)) and now < trial + open_s:
            return False  # a trial is in flight; a lost one is retried after another open period
        item["trial_at"] = now
        data[backend] = item
        _write(cfg, data)
        return True


def record(cfg, backend: str, status: str, now: float | None = None) -> None:
    """Count a finished call. Only failures count; guard_yield and budget_denied are ignored."""
    if status not in FAILURES and status not in RESETS:
        return
    now = time.time() if now is None else now
    limit, _ = _limits(cfg)
    with _lock(cfg):
        data = _read(cfg)
        if status in RESETS:
            item = {"fails": 0, "opened_at": None, "trial_at": None}
        else:
            item = _entry(data, backend)
            item["fails"] += 1
            if item["fails"] >= limit:
                item["opened_at"], item["trial_at"] = now, None
        data[backend] = item
        _write(cfg, data)
