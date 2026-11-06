"""Failure classification (mr-spec §5.9, §10.4; docs/p0/README.md §3, D-F1..D-F5).

Pure module: no I/O, no subprocess. The check order is normative:
guard_deny, then infra, then the four plan §7.3a detectors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_EFFORT = re.compile(r"-(low|medium|high|xhigh|max)(-fast)?$")

_PUSHBACK_TYPES = frozenset(
    {"spec_vs_code", "wrong_test", "scope_needed", "interface_impossible"}
)

# Priority order for the detector set M (D-F5).
_ORDER = ("safety_violation", "scope_rejected", "scope_repair", "off_track", "spec_suspect", "skipped",
          "tried_wrong")
_HARD = frozenset({"safety_violation", "scope_rejected", "scope_repair", "off_track"})


@dataclass(frozen=True)
class FailureRules:
    cls: str
    confident: bool
    hard: bool
    evidence: tuple[str, ...]
    matched: tuple[str, ...]


@dataclass(frozen=True)
class Inputs:
    model: str
    rc: int
    is_error: bool
    timed_out: bool
    wrapper_exit_75: bool
    hook_block: bool
    has_envelope: bool
    diff_files: tuple[str, ...]
    diff_loc: int
    mapped_files_touched: bool
    H: bool
    D: bool
    A_failing: tuple[str, ...]
    C: str | None
    mr_result: dict | None
    prior_attempts: tuple[dict, ...]
    D_status: str | None = None  # pass | flagged | violation; None when the scope judge is off
    scope_verdict: str | None = None  # accept | repair | reject | unavailable

    @classmethod
    def from_dict(cls, d: dict) -> "Inputs":
        return cls(
            model=d["model"],
            rc=d["rc"],
            is_error=d["is_error"],
            timed_out=d["timed_out"],
            wrapper_exit_75=d["wrapper_exit_75"],
            hook_block=d["hook_block"],
            has_envelope=d["has_envelope"],
            diff_files=tuple(d["diff_files"]),
            diff_loc=d["diff_loc"],
            mapped_files_touched=d["mapped_files_touched"],
            H=d["H"],
            D=d["D"],
            A_failing=tuple(d["A_failing"]),
            C=d["C"],
            mr_result=d["mr_result"],
            prior_attempts=tuple(d["prior_attempts"]),
            D_status=d.get("D_status"),
            scope_verdict=d.get("scope_verdict"),
        )


def base_model(m: str) -> str:
    """Strip a trailing effort suffix -(low|medium|high|xhigh|max)[-fast] (D-F1)."""
    return _EFFORT.sub("", m)


def _status(mr_result: dict | None) -> str | None:
    if not isinstance(mr_result, dict):
        return None
    status = mr_result.get("status")
    return status if isinstance(status, str) else None


def is_complete(i: Inputs) -> bool:
    return i.mapped_files_touched and i.H and _status(i.mr_result) == "done"


def _two(i: Inputs) -> dict[str, Any] | None:
    """The prior attempt witnessing the two-model rule, if any (D-F1)."""
    if not is_complete(i) or not i.A_failing:
        return None
    base = base_model(i.model)
    failing = set(i.A_failing)
    for prior in i.prior_attempts:
        if not isinstance(prior, dict) or not prior.get("complete"):
            continue
        if base_model(str(prior.get("model", ""))) == base:
            continue
        shared = failing & set(prior.get("A_failing") or ())
        if shared:
            return {"prior": prior, "shared": sorted(shared)}
    return None


def _valid_pushback(mr: dict, s: str | None) -> bool:
    if s != "pushback":
        return False
    pb = mr.get("pushback")
    return (
        isinstance(pb, dict)
        and pb.get("type") in _PUSHBACK_TYPES
        and isinstance(pb.get("item"), str)
        and bool(pb.get("item"))
        and isinstance(pb.get("evidence"), str)
        and bool(pb.get("evidence"))
    )


def _has_evidence(mr: dict) -> bool:
    reqs = mr.get("requirements")
    if isinstance(reqs, list) and any(
        isinstance(r, dict) and isinstance(r.get("evidence"), str) and r["evidence"]
        for r in reqs
    ):
        return True
    pb = mr.get("pushback")
    return (
        isinstance(pb, dict)
        and isinstance(pb.get("evidence"), str)
        and bool(pb.get("evidence"))
    )


def _skipped_reasons(i: Inputs, s: str | None, valid_pb: bool) -> list[str]:
    reasons = []
    if i.mr_result is None:
        reasons.append("no MR_RESULT")
    if s == "blocked" and not _has_evidence(i.mr_result or {}):
        reasons.append("blocked without evidence")
    if s == "pushback" and not valid_pb:
        reasons.append("pushback missing or invalid")
    if s == "done" and not i.diff_files:
        reasons.append("status done but no diff")
    if s != "pushback" and not i.mapped_files_touched:
        reasons.append("mapped files untouched")
    if not i.H:
        reasons.append("H failed")
    return reasons


def classify(i: Inputs) -> FailureRules:
    # 1. guard_deny comes before infra: a wrapper 75 also has rc != 0 and no envelope.
    if i.wrapper_exit_75 or i.hook_block:
        why = []
        if i.wrapper_exit_75:
            why.append("wrapper exit 75")
        if i.hook_block:
            why.append("hook block")
        return FailureRules(
            cls="guard_deny",
            confident=True,
            hard=False,
            evidence=("guard deny: " + " and ".join(why),),
            matched=(),
        )
    # 2. infra.
    if i.is_error or i.timed_out or (i.rc != 0 and not i.has_envelope):
        why = []
        if i.is_error:
            why.append("is_error")
        if i.timed_out:
            why.append("timed out")
        if i.rc != 0 and not i.has_envelope:
            why.append(f"rc {i.rc} with no envelope")
        return FailureRules(
            cls="infra",
            confident=True,
            hard=False,
            evidence=("infra: " + ", ".join(why),),
            matched=(),
        )
    # 3. The four detectors, evaluated independently into the set M.
    s = _status(i.mr_result)
    mr = i.mr_result if isinstance(i.mr_result, dict) else {}
    two = _two(i)
    valid_pb = _valid_pushback(mr, s)

    fired: dict[str, list[str]] = {}
    if i.D_status == "violation":
        fired["safety_violation"] = ["D: locked test edited or sensitive path outside the scope"]
    if i.scope_verdict in ("reject", "unavailable"):
        fired["scope_rejected"] = [f"scope judge {i.scope_verdict}"]
    if i.scope_verdict == "repair":
        fired["scope_repair"] = ["scope judge asked for a repair"]
    if not i.D and i.D_status is None:
        fired["off_track"] = ["D failed"]
    if two is not None:
        prior = two["prior"]
        fired.setdefault("spec_suspect", []).append(
            f"two models failed the same acceptance items {two['shared']}: "
            f"{base_model(i.model)} after {base_model(str(prior.get('model', '')))}"
        )
    if valid_pb:
        pb = mr["pushback"]
        fired.setdefault("spec_suspect", []).append(
            f"pushback {pb['type']}: {pb['evidence']}"
        )
    skip_reasons = _skipped_reasons(i, s, valid_pb)
    if skip_reasons:
        fired["skipped"] = ["; ".join(skip_reasons)]
    if (
        i.mapped_files_touched
        and i.H
        and s == "done"
        and (i.A_failing or i.C in ("fail", "unclear"))
        and two is None
    ):
        why = []
        if i.A_failing:
            why.append(f"A failing: {list(i.A_failing)}")
        if i.C in ("fail", "unclear"):
            why.append(f"C {i.C}")
        fired["tried_wrong"] = [
            "complete attempt, " + ", ".join(why)
        ]

    matched = tuple(name for name in _ORDER if name in fired)
    evidence = tuple(line for name in matched for line in fired[name])
    if matched:
        cls = matched[0]
    else:
        # D-F4: an empty M falls back to tried_wrong, which escalates.
        cls = "tried_wrong"
        evidence = ("no detector matched; fallback to tried_wrong",)
    confident = len(matched) == 1
    hard = cls in _HARD or (cls == "spec_suspect" and two is not None)
    return FailureRules(
        cls=cls, confident=confident, hard=hard, evidence=evidence, matched=matched
    )
