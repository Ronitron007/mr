"""Signal C: one cross-family reviewer (mr-spec §0.2, §5.9, §7.5a; p0 README §6)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from mr import config, runners

_PATCH_CAP = 60_000
_NO_CHECKLIST = ("No checklist: judge whether the patch correctly and minimally "
                 "does what the task asks; skip style.")
_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "prompts", "reviewer.v1.txt")


def family(model: str) -> str:
    """Model name -> family: grok-*, kimi-*, composer-*, claude-*."""
    for fam in ("grok", "kimi", "composer", "claude"):
        if model.startswith(fam + "-"):
            return fam
    raise ValueError(f"unknown model family: {model!r}")


def _cross_family(author_family: str, rung: str, cfg) -> bool:
    reviewer = family(cfg.rungs[rung].model)
    return reviewer != author_family and {author_family, reviewer} != {"kimi", "composer"}


def reviewer_rung(author_family: str, risk: str, cfg) -> str:
    """AC-SIG-2: reviewer family != author family, never kimi<->composer."""
    if author_family == "grok" and risk == "high":
        rung = cfg.review.high_risk_grok
    else:
        rung = cfg.review.map[author_family]
    assert _cross_family(author_family, rung, cfg), f"reviewer {rung} breaks AC-SIG-2 for {author_family}"
    return rung


def parse_review(text: str) -> dict | None:
    """README §6 steps 1-4: last MR_REVIEW line of the last 50, schema-checked."""
    data = runners.parse_mr_result(text, "MR_REVIEW")
    if data is None:
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    for it in items:
        if (not isinstance(it, dict) or not isinstance(it.get("id"), str)
                or it.get("verdict") not in ("pass", "fail")
                or not isinstance(it.get("evidence"), str)):
            return None
    if data.get("overall") not in ("pass", "fail", "unclear"):
        return None
    return data


@dataclass(frozen=True)
class SignalC:
    verdict: str  # pass | fail | unclear
    ok: bool
    items: tuple
    reviewer: str
    attempts: int
    warning: str | None
    denied: bool
    spend: tuple = ()  # (RunResult, GuardedRun) per executed Claude reviewer call


def _prompt(patch: str, requirements: list[dict] | None, item: str | None,
            task_id: str) -> str:
    with open(_PROMPT_PATH, encoding="utf-8") as fh:
        tmpl = fh.read()
    if len(patch) > _PATCH_CAP:
        patch = patch[:_PATCH_CAP] + f"\n[mr: patch truncated to {_PATCH_CAP} chars]"
    reqs = json.dumps(requirements, ensure_ascii=False) if requirements else _NO_CHECKLIST
    return tmpl.format(item=item or "whole change", requirements=reqs,
                       patch=patch, task_id=task_id)


def review(patch: str, requirements: list[dict] | None, item: str | None,
           author_model: str, risk: str, *, cfg, tree, task_id, attempt) -> SignalC:
    author = family(author_model)
    rung = reviewer_rung(author, risk, cfg)
    prompt = _prompt(patch, requirements, item, task_id)
    timeout = cfg.timeouts.reviewer_s
    doc, attempts, denied, warning = None, 0, False, None
    spend: list = []
    while attempts < 2:
        r = cfg.rungs[rung]
        if r.path == "cursor":
            if config.is_anthropic(r.model):
                raise RuntimeError(f"cursor reviewer {rung} is Anthropic (hard rule)")
            res = runners.run_cursor(r.model, prompt, tree, "ask", timeout,
                                     cfg=cfg, task=task_id, attempt=attempt + attempts)
        else:
            guarded, res = runners.run_claude(rung, prompt, tree, "plan", None, timeout,
                                              cfg=cfg, task=task_id,
                                              attempt=attempt + attempts)
            if res is not None:  # executed, including a null delta or a wrapper deny
                spend.append((res, guarded))
            if res is None or guarded.wrapper_denied:
                denied = True
                fallback = cfg.review.claude_denied
                if not _cross_family(author, fallback, cfg):  # AC-SIG-2 beats a misconfigured fallback
                    warning = f"reviewer {rung} denied and review.claude_denied {fallback} is not cross-family"
                    break
                rung = fallback
                continue
        attempts += 1
        doc = parse_review(res.result_text)
        if doc is not None:
            break
    verdict = doc["overall"] if doc else "unclear"
    ok = verdict == "pass"
    if verdict == "unclear" and risk == "low":
        ok, warning = True, "reviewer unclear; accepted at low risk"
    return SignalC(verdict=verdict, ok=ok, items=tuple(doc["items"]) if doc else (),
                   reviewer=rung, attempts=attempts, warning=warning, denied=denied,
                   spend=tuple(spend))
