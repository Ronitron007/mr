"""`mr log`: filter and print runs.jsonl (mr-spec §2.1, §7.4)."""
from __future__ import annotations

import json
import os
import sys

from mr import config, exitcodes as ec


def _read(path: str, task: str | None, role: str | None) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                print(f"mr log: skipping malformed line {n}", file=sys.stderr)
                continue
            if not isinstance(row, dict):
                print(f"mr log: skipping malformed line {n}", file=sys.stderr)
                continue
            if task is not None and row.get("task") != task:
                continue
            if role is not None and row.get("role") != role:
                continue
            rows.append(row)
    return rows


def _field(row: dict, key: str) -> str:
    val = row.get(key)
    if val is None or val == "":
        return "-"
    return str(val)


def _model_rung(row: dict) -> str:
    model, rung = row.get("model"), row.get("rung")
    if model and rung:
        return f"{model}/{rung}"
    if model:
        return str(model)
    if rung:
        return str(rung)
    return "-"


def _outcome(row: dict) -> str:
    if row.get("role") == "task":
        val = row.get("outcome")
    else:
        val = row.get("decision")
    return "-" if val is None or val == "" else str(val)


def _format(row: dict) -> str:
    exit_val = row.get("exit")
    exit_s = "-" if exit_val is None else str(exit_val)
    return " ".join(
        (
            _field(row, "ts"),
            _field(row, "role"),
            _field(row, "task"),
            _model_rung(row),
            _outcome(row),
            exit_s,
        )
    )


def run(args) -> int:
    cfg = config.load(args.config)
    path = f"{cfg.paths.home}/log/runs.jsonl"
    if not os.path.isfile(path):
        if getattr(args, "json", False):
            print("[]")
        return ec.OK

    task = getattr(args, "task", None)
    role = getattr(args, "role", None)
    rows = _read(path, task, role)
    tail = getattr(args, "tail", None)
    if tail is not None:
        if tail < 0:
            print("mr log: --tail must be >= 0", file=sys.stderr)
            return ec.USAGE
        rows = rows[-tail:] if tail else []

    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False))
    else:
        for row in rows:
            print(_format(row))
    return ec.OK
