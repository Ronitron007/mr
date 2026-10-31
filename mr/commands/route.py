"""`mr route`: rules-only dry run. Writes nothing (mr-spec §2.1, §2.3)."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

from mr import classifier, shadow
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
    models = not getattr(args, "no_models", False)
    if models and getattr(args, "judge", None) in ("shadow", "on"):
        print("judge not built", file=sys.stderr)
    try:
        cfg = load(getattr(args, "config", None))
        repo = os.path.abspath(getattr(args, "repo", None) or os.getcwd())
        task = make_task(text, repo, read_head(repo), _params(args))
        features = extract(task, None)
        decision = decide(task, features, cfg)
        mode = shadow.effective_mode(getattr(args, "classifier", None), cfg.classifier or {}) if models else "off"
        clf = _shadow_classifier(args, cfg, task, features, mode)
    except (TaskError, RouteError) as exc:
        print(str(exc), file=sys.stderr)
        return ec.USAGE
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return ec.USAGE
    code = _exit_code(decision)
    if getattr(args, "json", False):
        payload = _payload(task, features, decision, code)
        if mode == "shadow":
            payload["classifier_shadow"] = shadow.route_summary(clf)
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(_explain(task, decision, features))
        if clf is not None:
            print(_clf_line(clf))
    return code


def _shadow_classifier(args, cfg, task, features, mode):
    """Run the classifier in shadow when the mode says so; a ShadowAnswer or None. Never changes the decision."""
    cli = getattr(args, "classifier", None)
    block = cfg.classifier or {}
    if mode != "shadow":
        return None
    run_it, consulted = shadow.should_run(task.params, features.confident, task.fingerprint,
                                          block.get("sample_n", 3))
    if not (run_it or cli == "shadow"):
        return None
    ref = f"c-{task.id}-1"
    try:
        res = classifier.classify(task, features, cfg, ref=ref)
    except Exception:
        res = classifier.ClassifyResult(None, "cli_error", 1, None, classifier.prompt_sha(), ref, {},
                                        block.get("model", ""))
    return classifier.to_shadow(res, consulted, cfg)


def _clf_line(ans) -> str:
    conf = "-" if ans.confidence is None else ans.confidence
    return (f"classifier (shadow): type={ans.type or '-'} difficulty={ans.difficulty or '-'} "
            f"risk={ans.risk or '-'} route={ans.route or '-'} conf={conf} status={ans.status}")


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
