"""Logging seam for the shadow deciders (judge, classifier). No model calls: it only carries their answers."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

_OK = frozenset({"ok", "ok_partial", "cache_hit"})


@dataclass(frozen=True)
class ShadowAnswer:
    source: str  # "judge" | "classifier"
    mode: str  # "shadow"
    status: str  # ok, ok_partial, cache_hit, timeout, skipped_confident, ...
    ref: str | None
    consulted: bool
    route: str | None
    type: str | None
    difficulty: str | None
    risk: str | None
    risk_tags: tuple[str, ...]
    confidence: float | None
    first_rung: str | None
    unsure_fields: tuple[str, ...]
    latency_ms: int | None


def route_summary(ans: ShadowAnswer | None) -> dict | None:
    """Compact form for the route record. Shadow never acts, so acted_fields is always empty."""
    if ans is None:
        return None
    out = {
        "mode": ans.mode, "ref": ans.ref, "status": ans.status, "consulted": ans.consulted,
        "route": ans.route, "type": ans.type, "difficulty": ans.difficulty, "risk": ans.risk,
        "risk_tags": list(ans.risk_tags), "acted_fields": [], "unsure_fields": list(ans.unsure_fields),
    }
    if ans.source == "classifier":
        out["confidence"] = ans.confidence
    return out


def task_summary(ans: ShadowAnswer | None) -> dict | None:
    """Form for the task record's ``shadow`` block; None unless the answer is usable."""
    if ans is None or ans.status not in _OK:
        return None
    if ans.source == "judge":
        return {"route": ans.route, "first_rung": ans.first_rung}
    return {"route": ans.route}


def should_run(params, prepass_confident: bool, fingerprint: str, sample_n: int = 3) -> tuple[bool, bool]:
    """(run, consulted) per judge-spec §9.6 (1): unsure rules always run, confident ones are sampled 1 in n."""
    given = params.type and params.difficulty and params.route
    consulted = not given and not prepass_confident
    if consulted:
        return True, True
    if sample_n <= 0:
        return False, False
    return int(hashlib.sha256(fingerprint.encode()).hexdigest()[:8], 16) % sample_n == 0, False
