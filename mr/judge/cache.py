"""Judge answer cache (mr-spec §9.9). Stores answers only, never the state or the prompt text."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import time

DAY_S = 86400
SCHEMA = """CREATE TABLE IF NOT EXISTS judge_cache (
    key TEXT PRIMARY KEY, use TEXT, backend TEXT, qset_sha TEXT, created_at REAL, expires_at REAL,
    answers_json TEXT, usage_json TEXT, model_served TEXT)"""


def make_key(use, backend, model, effort, prompt_sha, qset_sha, state_sha) -> str:
    parts = [use, backend, model, "" if effort is None else effort, prompt_sha, qset_sha, state_sha]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _connect(cfg) -> sqlite3.Connection:
    folder = os.path.join(cfg.paths.home, "state")
    os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(os.path.join(folder, "judge-cache.sqlite"), timeout=10)
    conn.execute(SCHEMA)
    return conn


def _ttl_s(cfg, use: str) -> float:
    block = ((cfg.judge or {}).get("cache")) or {}
    days = block.get(f"{use}_ttl_days", block.get("route_ttl_days", 7))
    return days * DAY_S


def put(cfg, key: str, *, use: str, backend: str, qset_sha: str, answers, usage, model_served,
        now: float | None = None) -> None:
    now = time.time() if now is None else now
    row = (key, use, backend, qset_sha, now, now + _ttl_s(cfg, use),
           json.dumps(answers), json.dumps(usage), model_served)
    with contextlib.closing(_connect(cfg)) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO judge_cache VALUES (?,?,?,?,?,?,?,?,?)", row)


def get(cfg, key: str, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    with contextlib.closing(_connect(cfg)) as conn:
        row = conn.execute("SELECT answers_json, usage_json, model_served FROM judge_cache "
                           "WHERE key = ? AND expires_at > ?", (key, now)).fetchone()
    if row is None:
        return None
    try:
        return {"answers": json.loads(row[0]), "usage": json.loads(row[1]), "model_served": row[2]}
    except ValueError:
        return None


def purge_expired(cfg, now: float | None = None) -> int:
    now = time.time() if now is None else now
    with contextlib.closing(_connect(cfg)) as conn, conn:
        return conn.execute("DELETE FROM judge_cache WHERE expires_at <= ?", (now,)).rowcount
