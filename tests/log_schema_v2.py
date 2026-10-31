"""Stdlib checker for runs.jsonl v:2 records (AC-LOG-2)."""

from __future__ import annotations

import re

_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+05:30$")
_ATTEMPT = frozenset({"implementer", "planner", "reviewer"})
_OUTCOMES = frozenset(
    {
        "accepted",
        "exhausted",
        "deferred",
        "needs_human",
        "unsafe",
        "interrupted",
        "internal",
        "unknown",
    }
)
_W_CODES = frozenset(f"W{i}" for i in range(8))


def _num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _req_str(rec: dict, key: str, problems: list[str]) -> None:
    if key not in rec:
        problems.append(f"missing {key}")
    elif not isinstance(rec[key], str):
        problems.append(f"{key} must be str")


def _req_int(rec: dict, key: str, problems: list[str]) -> None:
    if key not in rec:
        problems.append(f"missing {key}")
    elif not isinstance(rec[key], int) or isinstance(rec[key], bool):
        problems.append(f"{key} must be int")


def _req_bool(rec: dict, key: str, problems: list[str]) -> None:
    if key not in rec:
        problems.append(f"missing {key}")
    elif not isinstance(rec[key], bool):
        problems.append(f"{key} must be bool")


def _req_dict(rec: dict, key: str, problems: list[str]) -> None:
    if key not in rec:
        problems.append(f"missing {key}")
    elif not isinstance(rec[key], dict):
        problems.append(f"{key} must be dict")


def _req_opt_int(rec: dict, key: str, problems: list[str]) -> None:
    if key not in rec:
        problems.append(f"missing {key}")
    elif rec[key] is not None and (
        not isinstance(rec[key], int) or isinstance(rec[key], bool)
    ):
        problems.append(f"{key} must be int or None")


def _req_opt_str(rec: dict, key: str, problems: list[str]) -> None:
    if key not in rec:
        problems.append(f"missing {key}")
    elif rec[key] is not None and not isinstance(rec[key], str):
        problems.append(f"{key} must be str or None")


def _req_opt_num(rec: dict, key: str, problems: list[str]) -> None:
    if key not in rec:
        problems.append(f"missing {key}")
    elif rec[key] is not None and not _num(rec[key]):
        problems.append(f"{key} must be number or None")


def _check_attempt(rec: dict, problems: list[str]) -> None:
    _req_str(rec, "rung", problems)
    _req_str(rec, "model", problems)
    if "path" not in rec:
        problems.append("missing path")
    elif rec["path"] not in ("cursor", "max"):
        problems.append("path must be cursor or max")
    _req_int(rec, "attempt", problems)
    _req_str(rec, "started_at", problems)
    _req_int(rec, "rc", problems)
    _req_bool(rec, "is_error", problems)
    if "wall_s" not in rec:
        problems.append("missing wall_s")
    elif not _num(rec["wall_s"]):
        problems.append("wall_s must be number")
    _req_dict(rec, "signals", problems)
    _req_opt_str(rec, "decision", problems)
    _req_opt_str(rec, "failure_class", problems)
    _req_opt_str(rec, "isolation", problems)
    _req_opt_str(rec, "patch", problems)
    if "diff" not in rec:
        problems.append("missing diff")
    elif rec["diff"] is not None and not isinstance(rec["diff"], dict):
        problems.append("diff must be dict or None")
    _req_opt_str(rec, "mr_result", problems)
    _req_opt_str(rec, "raw", problems)
    _req_opt_int(rec, "pts_pre", problems)
    _req_opt_int(rec, "pts_post", problems)
    _req_opt_num(rec, "claude_pts", problems)
    _req_opt_num(rec, "usd_equiv", problems)
    _req_opt_str(rec, "model_served", problems)
    _req_opt_str(rec, "item", problems)


def check(record: dict) -> list[str]:
    problems: list[str] = []
    if not isinstance(record, dict):
        return ["record must be dict"]
    ts = record.get("ts")
    if not isinstance(ts, str) or not _TS.match(ts):
        problems.append("ts must match IST ISO-8601 seconds")
    if record.get("v") != 2:
        problems.append("v must be 2")
    if not isinstance(record.get("mr_version"), str):
        problems.append("mr_version must be str")
    role = record.get("role")
    if not isinstance(role, str):
        problems.append("role must be str")
        return problems
    if role == "guard":
        if "task" not in record:
            problems.append("missing task")
        elif record["task"] is not None and not isinstance(record["task"], str):
            problems.append("task must be str or None for guard")
    elif not isinstance(record.get("task"), str):
        problems.append("task must be str")
    if role in _ATTEMPT:
        _check_attempt(record, problems)
    elif role == "route":
        _req_opt_str(record, "fingerprint", problems)
        _req_dict(record, "rules", problems)
        _req_dict(record, "applied", problems)
        applied = record.get("applied")
        if isinstance(applied, dict) and "ladder" not in applied:
            problems.append("applied must contain ladder")
        elif isinstance(applied, dict) and not isinstance(applied.get("ladder"), list):
            problems.append("applied.ladder must be list")
        _req_dict(record, "sources", problems)
        _req_bool(record, "invariant_violation", problems)
    elif role == "task":
        _req_str(record, "started_at", problems)
        _req_str(record, "ended_at", problems)
        _req_int(record, "wall_s", problems)
        _req_str(record, "repo", problems)
        _req_str(record, "base", problems)
        _req_str(record, "route", problems)
        if "ladder" not in record or not isinstance(record["ladder"], list):
            problems.append("ladder must be list")
        _req_dict(record, "attempts", problems)
        if "outcome" not in record:
            problems.append("missing outcome")
        elif record["outcome"] not in _OUTCOMES:
            problems.append("invalid outcome")
        _req_opt_int(record, "exit", problems)
        if "verified" not in record:
            problems.append("missing verified")
        elif record["verified"] is not None and not isinstance(record["verified"], bool):
            problems.append("verified must be bool or None")
        if "final_signals" not in record:
            problems.append("missing final_signals")
        elif record["final_signals"] is not None and not isinstance(
            record["final_signals"], dict
        ):
            problems.append("final_signals must be dict or None")
        _req_dict(record, "cost", problems)
    elif role == "guard":
        _req_str(record, "rung", problems)
        _req_bool(record, "allow", problems)
        _req_str(record, "reason", problems)
        _req_opt_num(record, "hour", problems)
        if "reserved" not in record:
            problems.append("missing reserved")
        elif not _num(record["reserved"]):
            problems.append("reserved must be number")
        if "est" not in record:
            problems.append("missing est")
        elif not _num(record["est"]):
            problems.append("est must be number")
        _req_opt_num(record, "five_hour", problems)
    elif role == "isolation":
        if "code" not in record:
            problems.append("missing code")
        elif record["code"] not in _W_CODES:
            problems.append("code must be W0..W7")
        _req_dict(record, "inputs", problems)
    elif role == "advisor":  # advisor-consult-spec §9.1 (T20)
        for key in ("task", "row", "gate", "consult_made", "relay_status"):
            if key not in record:
                problems.append(f"missing {key}")
        if not isinstance(record.get("violations", []), list):
            problems.append("violations must be list")
    else:
        problems.append("unknown role")
    return problems
