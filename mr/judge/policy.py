"""Reading judge answers (spec 9.5) for the route use. Shadow only: nothing here changes a routing Decision."""
from __future__ import annotations

from mr.shadow import ShadowAnswer

ACT_P = 0.75
RAISE_P = 0.60
_EPS = 1e-9  # keeps 1 - theta from missing a boundary value by float noise

SOFT = frozenset({"route_class", "task_type", "difficulty", "ambiguity", "load_bearing", "high_judgment",
                  "needs_clarification"})
RAISE_NOUL = frozenset({"needs_worktree", "needs_review", "tag.auth", "tag.payments", "tag.migration",
                        "tag.data_deletion", "tag.native_config", "tag.deps", "tag.secrets"})
RAISE_SCORE = frozenset({"risk", "scope"})
DIFFICULTY = ("trivial", "easy", "medium", "hard", "critical")
RISK = ("low", "medium", "high")
_TAG_NAMES = {"tag.data_deletion": "data-deletion", "tag.native_config": "native-config"}


def _thresholds(cfg) -> tuple[float, float]:
    t = (getattr(cfg, "judge", None) or {}).get("thresholds") or {}
    return t.get("act_p", ACT_P), t.get("raise_p", RAISE_P)


def _rules_level(qid: str, rules: dict) -> int:
    if qid == "risk":
        return RISK.index(rules["risk"]) if rules.get("risk") in RISK else 0
    return int(rules.get("scope", 0) or 0)


def _read_one(qid: str, compact: dict, rules: dict, act_p: float, raise_p: float) -> dict:
    if qid in SOFT:
        if "p" in compact:
            p = compact["p"]
            if p >= act_p - _EPS:
                return {"decision": "act", "value": True}
            if p <= 1 - act_p + _EPS:
                return {"decision": "act", "value": False}
        elif compact.get("conf", 0) >= act_p - _EPS:
            return {"decision": "act", "value": compact["c"] if "c" in compact else compact["s"]}
        return {"decision": "unsure", "value": None}
    if qid in RAISE_NOUL:
        p = compact["p"]
        if p >= raise_p - _EPS:
            return {"decision": "raise", "value": True}
        if p <= 1 - raise_p + _EPS:
            return {"decision": "ignored", "value": False}
        return {"decision": "unsure", "value": None}
    if qid in RAISE_SCORE:
        if compact["conf"] < raise_p - _EPS:
            return {"decision": "unsure", "value": None}
        if compact["s"] > _rules_level(qid, rules):
            return {"decision": "raise", "value": compact["s"]}
    return {"decision": "ignored", "value": None}


def read_route(parsed, rules: dict, cfg) -> dict:
    act_p, raise_p = _thresholds(cfg)
    known = SOFT | RAISE_NOUL | RAISE_SCORE
    out = {}
    for qid, compact in parsed.answers.items():
        out[qid] = _read_one(qid, compact, rules, act_p, raise_p) if qid in known else {
            "decision": "ignored", "value": None}
    for qid in parsed.unsure:
        out[qid] = {"decision": "unsure" if qid in known else "ignored", "value": None}
    return out


def to_shadow(parsed, decisions: dict, *, ref, status, consulted, latency_ms) -> ShadowAnswer:
    def value(qid, kinds):
        d = decisions.get(qid)
        return d["value"] if d and d["decision"] in kinds else None

    def level(qid, names, kinds):
        idx = value(qid, kinds)
        return names[idx] if idx is not None else None

    tags = sorted(
        _TAG_NAMES.get(qid, qid.split(".", 1)[1])
        for qid, d in decisions.items() if qid.startswith("tag.") and d["decision"] in ("raise", "act"))
    unsure = sorted(qid for qid, d in decisions.items() if d["decision"] == "unsure")
    return ShadowAnswer(
        source="judge", mode="shadow", status=status, ref=ref, consulted=consulted,
        route=value("route_class", ("act",)), type=value("task_type", ("act",)),
        difficulty=level("difficulty", DIFFICULTY, ("act",)),
        risk=level("risk", RISK, ("raise", "act")),
        risk_tags=tuple(tags), confidence=None, first_rung=None, unsure_fields=tuple(unsure),
        latency_ms=latency_ms)
