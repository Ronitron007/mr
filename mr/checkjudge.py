"""Check judge (spec 8A): state, prompt, strict reply and the guarded Sonnet call for the plan and scope checks."""
from __future__ import annotations

import json
import os
from typing import NamedTuple

from mr import redact, runners

_PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "check_judge.v1.txt")
KIND = "mr.check.v1"
VERDICTS = ("accept", "repair", "reject")
_KEYS = frozenset({"verdict", "reason", "instructions", "findings_ok"})
REASON_MAX = 600
DIFF_CAP_BYTES = 8000 * 3  # 8000 tokens at 3 bytes per token
_TRUNCATED = "\n[mr: diff truncated]"
_STRONG_PLANNERS = frozenset({"O", "O+"})
_HARD = frozenset({"hard", "critical"})


class Verdict(NamedTuple):
    verdict: str
    reason: str
    instructions: list
    findings_ok: list


class Call(NamedTuple):
    status: str  # ok | cli_error | guard_deny | timeout
    text: str
    result: object = None  # RunResult of a call that ran
    guarded: object = None


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def parse(text) -> Verdict | None:
    """The reply as a Verdict, or None unless it is exactly one JSON object with the four keys."""
    if not isinstance(text, str):
        return None
    try:
        doc = json.loads(text.strip())
    except ValueError:
        return None
    if not isinstance(doc, dict) or set(doc) != _KEYS:
        return None
    verdict, reason = doc["verdict"], doc["reason"]
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        return None
    if not isinstance(reason, str) or not reason.strip() or len(reason) > REASON_MAX:
        return None
    if not _strings(doc["instructions"]) or not _strings(doc["findings_ok"]):
        return None
    return Verdict(verdict, reason, doc["instructions"], doc["findings_ok"])


def _scrub(obj):
    """Redact every string value and every dict key."""
    if isinstance(obj, str):
        return redact.redact(obj)[0]
    if isinstance(obj, dict):
        return {_scrub(k): _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


def _scope_paths(plan) -> set[str]:
    scope = plan.get("scope") if isinstance(plan, dict) else None
    if not isinstance(scope, dict):
        return set()
    return {p for key in ("modify", "create") for p in scope.get(key) or () if isinstance(p, str)}


def _capped_diff(diff: str, plan) -> str:
    """The diff with the file sections that touch the plan's scope first, redacted, within the byte cap."""
    sections: list[list[str]] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            sections.append([])
        if sections:
            sections[-1].append(line)
    mine = _scope_paths(plan)
    touches = [any(s[0].rstrip("\n").endswith(" b/" + p) for p in mine) for s in sections]
    order = [s for s, hit in zip(sections, touches) if hit] + [s for s, hit in zip(sections, touches) if not hit]
    text = redact.redact("".join("".join(s) for s in order) or diff)[0]
    raw = text.encode("utf-8")
    if len(raw) <= DIFF_CAP_BYTES:
        return text
    return raw[:DIFF_CAP_BYTES].decode("utf-8", "ignore") + _TRUNCATED


def build_state(check, task, plan, findings, *, signals=None, diff=None, attempt=None) -> dict:
    """The mr.check.v1 state the judge reads. Redacted; the diff is capped with in-scope files first."""
    state = _scrub({"kind": KIND, "check": check, "task": task, "plan": plan, "findings": list(findings),
                    "signals": signals if signals is not None else {}, "attempt": attempt})
    state["diff"] = _capped_diff(diff, plan) if isinstance(diff, str) else None
    return state


def render(state: dict) -> str:
    with open(_PROMPT, encoding="utf-8") as fh:
        template = fh.read()
    return template.replace("<<STATE>>", json.dumps(state, ensure_ascii=False, indent=1))


def plan_rung(cfg, *, planner: str, difficulty: str, risk: str) -> str:
    """S by default; S+ when an Opus planner wrote the plan or the work is hard, critical or high risk."""
    block = cfg.check_judge
    if planner in _STRONG_PLANNERS or difficulty in _HARD or risk == "high":
        return block["plan_rung_strong"]
    return block["plan_rung"]


def call(cfg, rung: str, prompt: str, *, tree: str, task_id: str, attempt: int) -> Call:
    """One judge call through the usage guard and the wrapper, in plan mode (no edits)."""
    guarded, res = runners.run_claude(rung, prompt, tree, "plan", None, cfg.check_judge["timeout_s"],
                                      cfg=cfg, task=task_id, attempt=attempt)
    if res is None or guarded.wrapper_denied:
        return Call("guard_deny", "", res, guarded)
    if res.timed_out:
        return Call("timeout", "", res, guarded)
    if res.rc != 0 or res.is_error or not res.result_text:
        return Call("cli_error", "", res, guarded)
    return Call("ok", res.result_text, res, guarded)


def record(*, task, ref, check, findings, verdict, reason, instructions, rung, judge_model, pts_est, deferred,
           status) -> dict:
    """The runs.jsonl row of one judge decision (role check_judge)."""
    return {"role": "check_judge", "task": task, "ref": ref, "check": check, "findings": list(findings),
            "verdict": verdict, "reason": reason, "instructions": list(instructions), "rung": rung,
            "judge_model": judge_model, "pts_est": pts_est, "deferred": deferred, "status": status}
