"""Claude usage guard (mr-spec §5.6, §8). Fails closed; only the lock is waited on."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from mr.log import IST, Log

POLL_S = 30
CHECK_TIMEOUT_S = 60


@dataclass(frozen=True)
class GuardResult:
    allow: bool
    reason: str
    hour: float | None
    reserved: float
    est: float
    five_hour: float | None
    frees_hint: str | None
    probed: bool
    sample: dict | None


@dataclass
class GuardedRun:
    guard: GuardResult
    result: object | None
    wrapper_denied: bool
    delta: float | None


def _num(val) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _scale() -> float:
    try:
        return float(os.environ.get("MR_TEST_TIME_SCALE") or 1)
    except ValueError:
        return 1.0


def _state(cfg, name: str) -> str:
    return os.path.join(cfg.paths.home, "state", name)


def run_check(cfg, probe: bool) -> dict | None:
    """Run check.sh; parse stdout JSON whatever the exit code. None if unusable."""
    if not cfg.paths.check:
        return None
    try:
        proc = subprocess.run(
            [cfg.paths.check, "--probe" if probe else "--no-probe"],
            capture_output=True, text=True, timeout=CHECK_TIMEOUT_S, stdin=subprocess.DEVNULL,
        )
        data = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- reservations -----------------------------------------------------------

@contextmanager
def _res_lock(cfg, mode: int):
    os.makedirs(_state(cfg, ""), exist_ok=True)
    with open(_state(cfg, "reservations.lock"), "a") as fh:
        fcntl.flock(fh.fileno(), mode)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _load(cfg) -> list[dict]:
    """Raises ValueError on a corrupt file, so callers fail closed."""
    try:
        with open(_state(cfg, "reservations.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    items = data.get("reservations") if isinstance(data, dict) else None
    if not isinstance(items, list) or not all(isinstance(r, dict) for r in items):
        raise ValueError("reservations.json: malformed")
    return items


def _save(cfg, items: list[dict], now: float) -> None:
    keep = [r for r in items if _num(r.get("expires_at")) and r["expires_at"] > now]
    path = _state(cfg, "reservations.json")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"reservations": keep}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def reserve(cfg, task, rung: str, pts: float) -> str:
    now = time.time()
    rid = uuid.uuid4().hex
    with _res_lock(cfg, fcntl.LOCK_EX):
        items = _load(cfg)
        items.append({"id": rid, "task": task, "rung": rung, "pts": float(pts),
                      "expires_at": now + cfg.guard.reservation_ttl_s})
        _save(cfg, items, now)
    return rid


def release(cfg, rid: str) -> None:
    with _res_lock(cfg, fcntl.LOCK_EX):
        _save(cfg, [r for r in _load(cfg) if r.get("id") != rid], time.time())


def reserved_total(cfg, now: float | None = None) -> float:
    now = time.time() if now is None else now
    with _res_lock(cfg, fcntl.LOCK_SH):
        items = _load(cfg)
    return float(sum(r["pts"] for r in items
                     if _num(r.get("pts")) and _num(r.get("expires_at")) and r["expires_at"] > now))


# --- check (§8 steps 2-7) ---------------------------------------------------

def _frees_hint(sample: dict | None, now: float) -> str:
    raw = (sample or {}).get("five_hour_resets_at")
    try:
        at = datetime.fromtimestamp(raw, timezone.utc) if _num(raw) else datetime.fromisoformat(raw)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return "≈ in 60 min"
    if now < at.timestamp() < now + 3600:
        return f"five-hour window resets at {at.astimezone(IST):%H:%M} IST"
    return "≈ in 60 min"


def _check(rung: str, cfg, now: float | None) -> GuardResult:
    now = time.time() if now is None else now
    est = cfg.guard.est_seed.get(rung)
    probed = False
    sample = hour = fiveh = None
    reserved = 0.0

    def result(allow: bool, reason: str) -> GuardResult:
        hint = None if allow else _frees_hint(sample, now)
        return GuardResult(allow, reason, hour, reserved, est or 0.0, fiveh, hint, probed, sample)

    if not cfg.paths.check:
        return GuardResult(True, "no_check", None, 0.0, est or 0.0, None, None, False, None)
    if est is None:
        return result(False, "unknown rung")
    sample = run_check(cfg, probe=False)
    if sample is None:
        sample, probed = run_check(cfg, probe=True), True
        if sample is None:
            return result(False, "guard error")
    if sample.get("exceeded") is True:
        return result(False, "over_limit")
    age = sample.get("sample_age_s")
    if not probed and (not _num(age) or age > cfg.guard.probe_if_sample_older_s):
        sample, probed = run_check(cfg, probe=True), True
        if sample is None:
            return result(False, "stale sample, probe failed")
        if sample.get("exceeded") is True:
            return result(False, "over_limit")
    hour, fiveh = sample.get("hour_usage"), sample.get("five_hour_pct")
    if not _num(hour):
        hour = None
        return result(False, "guard error")
    if not _num(fiveh):
        fiveh = None
        return result(False, "no 5h baseline")
    try:
        reserved = reserved_total(cfg, now) + 0.0  # + judge_recent: always 0 in v1 (judge deferred)
    except (OSError, ValueError):
        return result(False, "guard error")
    lim = cfg.claude_limits
    if hour + reserved + est > lim.hour_max:
        return result(False, "budget")
    if fiveh + est > lim.five_hour_max:
        return result(False, "five_hour")
    return result(True, "ok")


def judge_should_yield(cfg, hour, five_hour_pct) -> bool:
    """The judge's yield, from the same ``claude_limits`` block the rung guard reads.

    Yields when the current reading has ``hour >= hour_max`` or
    ``five_hour_pct >= five_hour_max``. The S+A estimate stays inside ``est``
    on the rung side; this comparison does not add it.
    """
    lim = cfg.claude_limits
    return hour >= lim.hour_max or five_hour_pct >= lim.five_hour_max


def _log(cfg, res: GuardResult, rung: str, task, delta=None, wrapper_denied=False) -> None:
    rec = {"role": "guard", "task": task, "rung": rung}
    rec.update({k: v for k, v in asdict(res).items() if k != "sample"})
    rec.update(delta=delta, wrapper_denied=wrapper_denied,
               seven_day=(res.sample or {}).get("seven_day_pct"))
    Log(cfg.paths.home).append(rec)


def _safe_check(rung: str, cfg, now: float | None) -> GuardResult:
    try:
        return _check(rung, cfg, now)
    except Exception:  # fail closed on anything unexpected
        return GuardResult(False, "guard error", None, 0.0, cfg.guard.est_seed.get(rung, 0.0),
                           None, "≈ in 60 min", False, None)


def check(rung: str, cfg, now: float | None = None) -> GuardResult:
    res = _safe_check(rung, cfg, now)
    _log(cfg, res, rung, None)
    return res


# --- guarded run (§8 steps 1, 8, 9) -----------------------------------------

def _acquire(cfg, wait_lock_s: int):
    os.makedirs(_state(cfg, ""), exist_ok=True)
    fh = open(_state(cfg, "claude.lock"), "a")
    deadline = time.monotonic() + wait_lock_s * _scale()
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            left = deadline - time.monotonic()
            if left <= 0:
                fh.close()
                return None
            time.sleep(min(POLL_S * _scale(), left))


def _busy(rung: str, cfg) -> GuardResult:
    return GuardResult(False, "claude busy", None, 0.0, cfg.guard.est_seed.get(rung, 0.0),
                       None, None, False, None)


def _wrapper_denied(result) -> bool:
    rc = getattr(result, "rc", None)
    if rc == 75:
        return True
    text = (getattr(result, "stderr", "") or "") + (getattr(result, "result_text", "") or "")
    return rc == 2 and "hook" in text


def run_claude_guarded(rung: str, argv_tail: list[str], cfg, *, task: str, exec_fn,
                       wait_lock_s: int | None = None) -> GuardedRun:
    """The single entry point that runs Claude rungs (AC-GRD-5). A deny is never retried."""
    lock = _acquire(cfg, cfg.guard.wait_lock_s if wait_lock_s is None else wait_lock_s)
    if lock is None:
        res = _busy(rung, cfg)
        _log(cfg, res, rung, task)
        return GuardedRun(res, None, False, None)
    try:
        res = _safe_check(rung, cfg, None)
        if not res.allow:
            _log(cfg, res, rung, task)
            return GuardedRun(res, None, False, None)
        rid = reserve(cfg, task, rung, res.est)
        result = exec_fn([cfg.paths.claude, *argv_tail])
        denied = _wrapper_denied(result)
        if not cfg.paths.check:
            release(cfg, rid)
            _log(cfg, res, rung, task, None, denied)
            return GuardedRun(res, result, denied, None)
        post = run_check(cfg, probe=True)
        pre = res.sample or {}
        delta = None
        if post is not None and _num(post.get("five_hour_pct")):
            release(cfg, rid)
            if post.get("five_hour_resets_at") == pre.get("five_hour_resets_at"):
                delta = max(0.0, float(post["five_hour_pct"]) - float(pre["five_hour_pct"]))
        _log(cfg, res, rung, task, delta, denied)
        return GuardedRun(res, result, denied, delta)
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
