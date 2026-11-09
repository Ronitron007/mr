"""Signal C: one cross-family reviewer (mr-spec §0.2, §5.9, §7.5a; p0 README §6)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from mr import config, runners
from mr.router import STRENGTH

_PATCH_CAP = 60_000
TASK_ID = "TASK"  # the one item id of a plan-less review
UNAVAILABLE = "unavailable"  # the reviewer crashed, timed out or broke the reply contract twice: no verdict
_NO_CHECKLIST = (f'No checklist. Judge the patch against the TASK as one item with id "{TASK_ID}": does it '
                 "correctly and minimally do what the task asks? Skip style.")
_NO_TASK = "(no task text: judge whether the change is correct and breaks nothing)"
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


def reviewer_rung(author_family: str, risk: str, cfg, required: str | None = None) -> str:
    """AC-SIG-2: reviewer family != author family, never kimi<->composer.

    ``required`` is a Claude rung the review must reach at least (S < S+ < O < O+). A mapped Claude reviewer
    at least that strong stays; otherwise ``required`` reviews, even in the author's own family.
    """
    if author_family == "grok" and risk == "high":
        rung = cfg.review.high_risk_grok
    else:
        rung = cfg.review.map[author_family]
    assert _cross_family(author_family, rung, cfg), f"reviewer {rung} breaks AC-SIG-2 for {author_family}"
    if required and not (rung in STRENGTH and STRENGTH.index(rung) >= STRENGTH.index(required)):
        return required
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


def verdict(doc: dict | None, ids: list[str] | None) -> str | None:
    """mr's verdict from a parsed MR_REVIEW; None when the reply breaks the contract (re-ask, like bad JSON).

    With requirement ``ids`` the items cover each id exactly once, in any order; with none (plan-less) there
    is at least one item. Any failed item fails; the reviewer's overall can only turn all-pass into unclear.
    """
    if doc is None:
        return None
    items = doc["items"]
    if not items or (ids is not None and sorted(it["id"].strip() for it in items) != sorted(ids)):
        return None
    if any(it["verdict"] == "fail" for it in items):
        return "fail"
    return "unclear" if doc["overall"] == "unclear" else "pass"


@dataclass(frozen=True)
class SignalC:
    verdict: str  # pass | fail | unclear | unavailable
    ok: bool
    items: tuple
    reviewer: str
    attempts: int
    warning: str | None
    denied: bool
    spend: tuple = ()  # (RunResult, GuardedRun) per executed Claude reviewer call
    deferred: bool = False  # the guard denied the required strong review and there is no fallback


def _prompt(patch: str, requirements: list[dict] | None, item: str | None,
            task_id: str, task_text: str | None = None) -> str:
    with open(_PROMPT_PATH, encoding="utf-8") as fh:
        tmpl = fh.read()
    if len(patch) > _PATCH_CAP:
        patch = patch[:_PATCH_CAP] + f"\n[mr: patch truncated to {_PATCH_CAP} chars]"
    reqs = json.dumps(requirements, ensure_ascii=False) if requirements else _NO_CHECKLIST
    return tmpl.format(item=item or "whole change", requirements=reqs, task=task_text or _NO_TASK,
                       patch=patch, task_id=task_id)


def review(patch: str, requirements: list[dict] | None, item: str | None,
           author_model: str, risk: str, *, cfg, tree, task_id, attempt,
           required: str | None = None, defer: bool = False, task_text: str | None = None) -> SignalC:
    """``required`` is the weakest Claude reviewer allowed. With ``defer`` a guard deny of that reviewer does
    not fall back to review.claude_denied: the result is ``deferred``. No valid reply after the re-ask is
    ``unavailable`` and never ok, at any risk."""
    author = family(author_model)
    rung = reviewer_rung(author, risk, cfg, required)
    prompt = _prompt(patch, requirements, item, task_id, task_text)
    ids = [r["id"] for r in requirements] if requirements else None
    timeout = cfg.timeouts.reviewer_s
    doc, v, attempts, denied, deferred, warning = None, None, 0, False, False, None
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
                if defer:
                    deferred = True
                    break
                fallback = cfg.review.claude_denied
                if not _cross_family(author, fallback, cfg):  # AC-SIG-2 beats a misconfigured fallback
                    warning = f"reviewer {rung} denied and review.claude_denied {fallback} is not cross-family"
                    break
                rung = fallback
                continue
        attempts += 1
        doc = parse_review(res.result_text)
        v = verdict(doc, ids)
        if v is not None:
            break
    ok = v == "pass"
    if v == "unclear" and risk == "low":
        ok, warning = True, "reviewer unclear; accepted at low risk"
    if v is None and not deferred:
        warning = warning or f"reviewer {rung} gave no valid MR_REVIEW in {attempts} attempts"
    return SignalC(verdict=v or UNAVAILABLE, ok=ok, items=tuple(doc["items"]) if v else (),
                   reviewer=rung, attempts=attempts, warning=warning, denied=denied,
                   spend=tuple(spend), deferred=deferred)
