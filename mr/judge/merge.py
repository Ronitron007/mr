"""Policy merge for the always-on judge (spec v0.2 §3.4-§3.6). Pure: no model, subprocess or git call.

Precedence: safety floors > judge (when its bar passes) > caller hint > rules > default. A stricter
answer needs ``raise_p``, a neutral one ``act_p``, a looser one ``act_p`` plus an evidence quote mr found
in the task text. ``mr run`` and ``mr route`` apply the result (judge.run_intake)."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from mr import prepass, router
from mr.judge import policy

_DIFF = router._DIFF
_RISK = router._RISK
_ROUTES = ("cascade", "judgment", "design")
_HIGH = prepass.HIGH_TAGS
_MEDIUM = prepass.MEDIUM_TAGS
_F3_FILES = 8


@dataclass(frozen=True)
class Override:
    field: str
    hint: object
    applied: object
    judge_conf: float | None
    evidence: str | None


@dataclass(frozen=True)
class Merged:
    decision: router.Decision
    overrides: tuple[Override, ...]
    risk_evidence: tuple[dict, ...]
    files: dict
    judge_absent: bool
    fallback: str | None
    gaps: tuple[str, ...]
    strong_review: bool


@dataclass(frozen=True)
class _Pick:
    value: object
    src: str
    ans: dict | None = None


class _Gate:
    """The three bars, the pins and the gap log for one merge."""

    def __init__(self, cfg, source: str, pins) -> None:
        self.act, self.raise_ = policy._thresholds(cfg)
        self.source = source
        self.pins = frozenset(pins)
        self.gaps: list[str] = []

    def accept(self, field: str, kind: str, ans: dict) -> bool:
        conf = ans["conf"]
        if kind == "stricter":
            return conf >= self.raise_ - policy._EPS
        if conf < self.act - policy._EPS:
            return False
        if kind == "neutral":
            return True
        if self.source != "judge":
            self.gaps.append(f"looser_ignored:{field}:{self.source}")
            return False
        if not ans.get("quote_found"):
            self.gaps.append(f"looser_ignored:{field}:no_quote")
            return False
        return True

    def step(self, field: str, ans: dict | None, base: int, value: int) -> int | None:
        """The judge's index when it moves ``base`` and passes its bar, else None."""
        if ans is None or field in self.pins or value == base:
            return None
        ok = self.accept(field, "stricter" if value > base else "looser", ans)
        return value if ok else None


def _answer(readings, key: str, valid) -> dict | None:
    ans = (readings or {}).get(key)
    if not isinstance(ans, dict) or not valid(ans.get("value")):
        return None
    conf = ans.get("conf")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        return None
    return ans


def _is_index(top: int):
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= top


def _is_bool(v) -> bool:
    return isinstance(v, bool)


def _difficulty(g, task, rules, readings, floor: int) -> _Pick:
    hint = task.params.difficulty
    if hint and "difficulty" not in g.pins and "param_raised_by_rule" in rules.reasons:
        hint = None
    base = _DIFF.index(hint or rules.difficulty)
    ans = _answer(readings, "difficulty", _is_index(len(_DIFF) - 1))
    judged = g.step("difficulty", ans, base, ans["value"] if ans else 0)
    idx, src = (base, "param" if hint else rules.sources.get("difficulty", "rule")) if judged is None else (judged, "judge")
    if ans and "difficulty" not in g.pins and abs(ans["value"] - _DIFF.index(rules.difficulty)) >= 2 \
            and ans["conf"] >= g.raise_ - policy._EPS:
        g.gaps.append(f"difficulty_gap:{rules.difficulty}->{_DIFF[ans['value']]}")
        if not hint and idx < _DIFF.index(rules.difficulty):
            idx, src = _DIFF.index(rules.difficulty), rules.sources.get("difficulty", "rule")
    if idx < floor:
        idx, src = floor, "floor"
    return _Pick(_DIFF[idx], src, ans if src == "judge" else None)


def _type_kind(old: str, new: str, difficulty: str, cfg) -> str:
    kept = not router.review_rule(old, difficulty, cfg) or router.review_rule(new, difficulty, cfg)
    ladder = router._select_ladder("cascade", new, difficulty, cfg, [])[0]
    claude = any(cfg.rungs[code].path == "max" for code in ladder if code in cfg.rungs)
    return "neutral" if kept and claude else "looser"


def _type(g, task, rules, readings, difficulty: str, cfg) -> _Pick:
    hint = task.params.type
    base = hint or rules.type
    kept = _Pick(base, "param" if hint else rules.sources.get("type", "rule"))
    ans = _answer(readings, "task_type", lambda v: isinstance(v, str) and v in cfg.ladders)
    if ans is None or "type" in g.pins or ans["value"] == base:
        return kept
    if g.accept("type", _type_kind(base, ans["value"], difficulty, cfg), ans):
        return _Pick(ans["value"], "judge", ans)
    return kept


def _route(g, task, rules, readings) -> _Pick:
    hint = task.params.route
    base = hint or rules.route
    kept = _Pick(base, "param" if hint else rules.sources.get("route", "rule"))
    ans = _answer(readings, "route_class", lambda v: v in _ROUTES)
    if ans is None:
        return kept
    idx = g.step("route", ans, _ROUTES.index(base), _ROUTES.index(ans["value"]))
    return kept if idx is None else _Pick(_ROUTES[idx], "judge", ans)


def _flag(g, field: str, base: bool, readings, *, loosen: bool) -> bool:
    """A bool field: True is the stricter answer. ``loosen`` False means a False answer is never taken."""
    ans = _answer(readings, field, _is_bool)
    if ans is None or field in g.pins or ans["value"] == base:
        return base
    if not ans["value"] and not loosen:
        return base
    idx = g.step(field, ans, int(base), int(ans["value"]))
    return base if idx is None else bool(idx)


def _tier(tag: str) -> int:
    return 2 if tag in _HIGH else 1


def _text_tags(g, facts, readings, path_tags: set[str]):
    """Resolve each text-only tag: (applied, dropped, unresolved, evidence log)."""
    items = {i.tag: i for i in facts.risk_evidence if i.source == "text" and i.tag not in path_tags}
    answers = (readings or {}).get("tags") or {}
    applied: set[str] = set()
    dropped: set[str] = set()
    unresolved: set[str] = set()
    evidence: list[dict] = []
    for tag in sorted(items):
        ans = answers.get(tag)
        usable = isinstance(ans, dict) and "risk_tags" not in g.pins and isinstance(ans.get("conf"), (int, float))
        says = ans.get("changes_behavior") if usable else None
        if says is True and g.accept("tag:" + tag, "stricter", ans):
            applied.add(tag)
            evidence.append(_tag_entry(tag, ans, "changes_behavior"))
        elif says is False and g.accept("tag:" + tag, "looser", ans):
            dropped.add(tag)
            evidence.append(_tag_entry(tag, ans, "mention_only"))
        else:
            unresolved.add(tag)
    return applied, dropped, unresolved, evidence


def _tag_entry(tag: str, ans: dict, judge: str) -> dict:
    return {"tag": tag, "source": "text", "quote": ans.get("evidence"), "judge": judge, "conf": ans["conf"]}


def _risk(g, rules, readings, text_only: set[str], floor: int, tags_floor: int) -> _Pick:
    base = _RISK.index(rules.risk)
    if text_only:  # a rules risk that rests on words alone is not kept: the text tags are settled below
        remaining = [_tier(t) for t in set(rules.risk_tags) - text_only]
        base = min(base, max(remaining, default=0))
    ans = _answer(readings, "risk", _is_index(len(_RISK) - 1))
    judged = g.step("risk", ans, base, ans["value"] if ans else 0)
    idx, src = (base, rules.sources.get("risk", "rule")) if judged is None else (judged, "judge")
    if idx < max(floor, tags_floor):
        idx, src = max(floor, tags_floor), "floor"
    return _Pick(_RISK[idx], src, ans if src == "judge" else None)


def _candidates_files(facts) -> tuple[list[dict], list[dict]]:
    change: list[dict] = []
    new: list[dict] = []
    for c in facts.candidates:
        src = "named" if "named" in c.why else "hint" if "hint" in c.why else None
        if src is None:
            continue
        if c.exists:
            change.append({"path": c.path, "src": src})
        elif src == "named":
            new.append({"path": c.path, "src": src})
    return change, new


def _outside(path: str) -> bool:
    return not path or path.startswith(("/", "~")) or ".." in path.split("/")


def _files(g, facts, readings) -> dict:
    change, new = _candidates_files(facts)
    reading = (readings or {}).get("files")
    if not isinstance(reading, dict):
        return {"change": change, "new": new, "partial": True}
    if "files" not in g.pins:
        conf = reading.get("conf") or {}
        existing = {c.path for c in facts.candidates if c.exists}
        listed = {e["path"] for e in change}
        for path in _passing(g, reading, conf, "change"):
            if path in existing and path not in listed:
                change.append({"path": path, "src": "judge"})
                listed.add(path)
        listed = {e["path"] for e in new}
        for path in _passing(g, reading, conf, "new"):
            if path not in existing and path not in listed and not _outside(path):
                new.append({"path": path, "src": "judge"})
                listed.add(path)
    return {"change": change, "new": new, "partial": reading.get("complete") is not True}


def _passing(g, reading: dict, conf: dict, key: str) -> list[str]:
    value = conf.get(key)
    paths = reading.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < g.act - policy._EPS:
        return []
    return [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []


def _overrides(task, picks: dict[str, _Pick]) -> tuple[Override, ...]:
    out = []
    for name, pick in picks.items():
        hint = getattr(task.params, name)
        if hint is None or pick.value == hint:
            continue
        judged = pick.ans if pick.src == "judge" else None
        out.append(Override(name, hint, pick.value, judged["conf"] if judged else None,
                            judged.get("evidence") if judged else None))
    return tuple(out)


def _check(d: router.Decision, task, cfg, *, path_tags: set[str], risk_floor: int, diff_floor: int,
           strong: bool, pins: frozenset[str]) -> None:
    """I1'-I4'. Any failure raises router.InvariantError."""
    if _RISK.index(d.risk) < risk_floor or not path_tags <= set(d.risk_tags):
        raise router.InvariantError("I1'")
    if risk_floor == 2 and not (strong and d.needs_review):
        raise router.InvariantError("I1'")
    if _DIFF.index(d.difficulty) < diff_floor:
        raise router.InvariantError("I2'")
    hint = task.params.difficulty
    if "difficulty" in pins and hint and _DIFF.index(d.difficulty) != max(_DIFF.index(hint), diff_floor):
        raise router.InvariantError("I2'")
    mode = router._budget_mode(task.params.budget)
    for code in d.ladder:
        rung = cfg.rungs.get(code)
        if rung is None or not router._allowed(cfg, rung) or not router._keeps(cfg, code, mode):
            raise router.InvariantError("I3'")
    if d.route == "design" and d.ladder:
        raise router.InvariantError("I4'")
    for name in ("type", "route"):
        if name in pins and getattr(task.params, name) and getattr(d, name) != getattr(task.params, name):
            raise router.InvariantError("I4'")


def merge(task, facts, rules: router.Decision, readings: dict | None, cfg, *, source: str = "judge") -> Merged:
    g = _Gate(cfg, source, task.params.pin)
    path_tags = {i.tag for i in facts.risk_evidence if i.source != "text"}
    risk_floor = 2 if path_tags & _HIGH else 1 if path_tags & _MEDIUM else 0
    named = [c for c in facts.candidates if c.exists and "named" in c.why]
    diff_floor = _DIFF.index("hard") if len(named) >= _F3_FILES else 0

    text_only = {i.tag for i in facts.risk_evidence if i.source == "text"} - path_tags
    applied, dropped, unresolved, evidence = _text_tags(g, facts, readings, path_tags)
    tags_floor = max([_tier(t) for t in applied] + [1 if unresolved else 0])
    strong = bool(path_tags & _HIGH) or bool(unresolved)

    difficulty = _difficulty(g, task, rules, readings, diff_floor)
    kind = _type(g, task, rules, readings, difficulty.value, cfg)
    route = _route(g, task, rules, readings)
    risk = _risk(g, rules, readings, text_only, risk_floor, tags_floor)
    load_bearing = _flag(g, "load_bearing", rules.load_bearing, readings, loosen=True)
    review = _flag(g, "needs_review", False, readings, loosen=False)
    files = _files(g, facts, readings)

    reasons = list(rules.reasons)
    params = task.params
    if route.value == "design" and params.prefer:
        # A design route has no ladder to put a rung into (I4'), so --prefer is dropped here.
        params = dataclasses.replace(params, prefer=None)
        reasons.append("prefer_ignored_design")
    ladder, ladder_src = router.build_ladder(route.value, kind.value, difficulty.value, cfg, params, reasons)
    sources = dict(rules.sources, type=kind.src, difficulty=difficulty.src, route=route.src, risk=risk.src,
                   ladder=ladder_src)
    tags = (set(rules.risk_tags) - dropped) | path_tags | applied | unresolved
    decision = dataclasses.replace(
        rules, route=route.value, type=kind.value, difficulty=difficulty.value, risk=risk.value,
        risk_tags=frozenset(tags), load_bearing=load_bearing, ladder=ladder,
        needs_review=(router.review_rule(kind.value, difficulty.value, cfg) or strong or review
                      or rules.required_review is not None),
        planner=("O+" if rules.planner and difficulty.value == "critical" else rules.planner),
        reasons=tuple(dict.fromkeys(reasons)), sources=sources,
        files={"change": list(files["change"]), "new": list(files["new"]), "partial": files["partial"]},
        risk_evidence=tuple(evidence))
    _check(decision, task, cfg, path_tags=path_tags, risk_floor=risk_floor, diff_floor=diff_floor,
           strong=strong, pins=g.pins)
    picks = {"type": kind, "difficulty": difficulty, "route": route}
    return Merged(
        decision=decision, overrides=_overrides(task, picks), risk_evidence=tuple(evidence), files=files,
        judge_absent=readings is None, fallback="rules" if readings is None else
        "classifier" if source == "classifier" else None,
        gaps=tuple(dict.fromkeys(g.gaps)), strong_review=strong)
