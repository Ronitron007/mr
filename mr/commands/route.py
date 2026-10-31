"""`mr route`: rules-only dry run. Writes nothing (mr-spec §2.1, §2.3)."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

from mr import exitcodes as ec
from mr.config import ConfigError, load
from mr.gitmeta import read_head
from mr.prepass import extract
from mr.router import Decision, RouteError, decide
from mr.task import Params, TaskError, make_task

_DESIGN = "design route not built; re-run with --route cascade or plan by hand"


def run(args) -> int:
    text = getattr(args, "task", None)
    if not isinstance(text, str) or text.strip() == "":
        print("task text missing", file=sys.stderr)
        return ec.USAGE
    if not getattr(args, "no_models", False):
        print("judge/classifier not built", file=sys.stderr)
    try:
        cfg = load(getattr(args, "config", None))
        repo = os.path.abspath(getattr(args, "repo", None) or os.getcwd())
        task = make_task(text, repo, read_head(repo), _params(args))
        features = extract(task, None)
        decision = decide(task, features, cfg)
    except (TaskError, RouteError) as exc:
        print(str(exc), file=sys.stderr)
        return ec.USAGE
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return ec.USAGE
    code = _exit_code(decision)
    if getattr(args, "json", False):
        print(json.dumps(_payload(task, features, decision, code), ensure_ascii=False))
    else:
        print(_explain(task, decision, features))
    return code


def _params(args) -> Params:
    return Params(
        type=args.type,
        difficulty=args.difficulty,
        route=args.route,
        prefer=args.prefer,
        budget=getattr(args, "budget", None) or "normal",
        latency=getattr(args, "latency", None) or "normal",
        experiment=bool(getattr(args, "experiment", False)),
        judge=getattr(args, "judge", None),
        classifier=getattr(args, "classifier", None),
        yes=bool(getattr(args, "yes", False)),
        wait_lock_s=getattr(args, "wait_lock_s", 600),
    )


def _exit_code(decision: Decision) -> int:
    code = ec.OK
    if decision.route == "design":
        print(_DESIGN, file=sys.stderr)
        code = ec.HUMAN
    if "empty_ladder_after_budget" in decision.reasons:
        print("empty ladder after budget", file=sys.stderr)
        code = ec.HUMAN
    if decision.needs_clarification:
        print("needs clarification", file=sys.stderr)
        code = ec.HUMAN
    return code


def _payload(task, features, decision: Decision, code: int) -> dict:
    return {
        "task_id": task.id,
        "fingerprint": task.fingerprint,
        "features": _jsonable(asdict(features)),
        "decision": _jsonable(asdict(decision)),
        "reasons": list(decision.reasons),
        "exit": code,
    }


def _jsonable(obj):
    if isinstance(obj, frozenset):
        return sorted(obj)
    if isinstance(obj, dict):
        return {key: _jsonable(val) for key, val in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(val) for val in obj]
    return obj


def _explain(task, decision: Decision, features) -> str:
    src = decision.sources
    tags = ", ".join(sorted(decision.risk_tags)) or "-"
    ladder = " ".join(decision.ladder) or "-"
    reasons = ", ".join(decision.reasons) or "-"
    return "\n".join(
        (
            f"task_id: {task.id}",
            f"fingerprint: {task.fingerprint}",
            f"route: {decision.route} ({src['route']})",
            f"type: {decision.type} ({src['type']})",
            f"difficulty: {decision.difficulty} ({src['difficulty']})",
            f"risk: {decision.risk} ({src['risk']})",
            f"risk_tags: {tags}",
            f"ladder: {ladder}",
            f"needs_review: {decision.needs_review}",
            f"needs_clarification: {decision.needs_clarification}",
            f"load_bearing: {decision.load_bearing}",
            f"high_judgment: {decision.high_judgment}",
            f"reasons: {reasons}",
            f"prepass_confident: {features.confident}",
        )
    )
