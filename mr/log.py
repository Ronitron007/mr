"""Append-only JSONL run log (mr-spec §5.12, §7.4)."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import mr
from mr.paths import data_dir

IST = timezone(timedelta(hours=5, minutes=30))

_REDACT = frozenset(
    {"prompt", "raw_state", "state_raw", "env", "authorization", "api_key"}
)


def now_ist() -> str:
    return datetime.now(IST).replace(microsecond=0).isoformat()


class Log:
    def __init__(self, home: str) -> None:
        self.path = f"{home}/log/runs.jsonl"

    def append(self, record: dict) -> None:
        try:
            if "role" not in record:
                raise ValueError("record missing role")
            out = dict(record)
            out.setdefault("ts", now_ist())
            out.setdefault("v", 2)
            out.setdefault("mr_version", mr.__version__)
            out.setdefault("src", "mr")
            redacted: list[str] = []
            for key in list(out):
                if key.lower() in _REDACT:
                    redacted.append(key)
                    del out[key]
            if redacted:
                out["redacted_keys"] = redacted
            line = json.dumps(
                out,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line + "\n")
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            print(f"mr log: {exc}", file=sys.stderr)


def append(record: dict, home: str | None = None) -> None:
    Log(data_dir() if home is None else home).append(record)


def read(
    home: str,
    task: str | None = None,
    role: str | None = None,
) -> list[dict]:
    path = f"{home}/log/runs.jsonl"
    if not os.path.isfile(path):
        return []
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if task is not None and row.get("task") != task:
                continue
            if role is not None and row.get("role") != role:
                continue
            rows.append(row)
    return rows
