"""Rules router (mr-spec §5.3, §9.2). ``decide`` is rules only; the judge merge (mr.judge.merge) sits on top of it."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from mr import facts as facts_mod
from mr import prepass, shadow
from mr.config import Config, _match
from mr.prepass import Features
from mr.task import Task

_DIFF = ("trivial", "easy", "medium", "hard", "critical")
_RISK = ("low", "medium", "high")
_REVIEW_AT = frozenset({"code", "bugfix", "ios-native", "ui"})
_OPUS = frozenset({"O", "O+"})
_KIMI_DROP = frozenset({"K-h", "K-x"})
MAPPED = "design_mapped_to_cascade"
STRENGTH = ("S", "S+", "O", "O+")
_TRUSTED_SRC = frozenset({"named", "hint", "planner"})


class InvariantError(Exception):
    """A §9.2 invariant failed. v1 has no judge to drop, so this propagates."""


class RouteError(Exception):
    """Usage problem in routing. Callers map this to exit 2."""


@dataclass(frozen=True)
class Decision:
    route: str
    type: str
    difficulty: str
    risk: str
    risk_tags: frozenset[str]
    load_bearing: bool
    high_judgment: bool
    needs_review: bool
    isolate_hint: str | None
    needs_clarification: bool
    ladder: tuple[str, ...]
    reasons: tuple[str, ...]
    sources: dict[str, str]
    files: dict = field(default_factory=dict)
    risk_evidence: tuple = ()
    judge_status: str | None = None
    planner: str | None = None
    required_review: str | None = None


def files_from_facts(facts, hints=()) -> dict:
    """Decision files from the facts candidates that the text names or a hint gives (named wins over hint).

    Without a Facts (the build failed) the hints stand in as the files to change."""
    change: list[dict] = []
    new: list[dict] = []
    if isinstance(facts, facts_mod.Facts):
        for c in facts.candidates:
            src = "named" if "named" in c.why else "hint" if "hint" in c.why else None
            if src is not None:
                (change if c.exists else new).append({"path": c.path, "src": src})
    else:
        change = [{"path": p, "src": "hint"} for p in dict.fromkeys(hints)]
    return {"change": change, "new": new, "read": [], "partial": True}


def files_from_plan(checklist: dict) -> dict:
    """Decision files after a valid plan: the planner's scope is the final scope."""
    block = checklist.get("scope") or {}
    return {"change": [{"path": p, "src": "planner"} for p in block.get("modify") or ()],
            "new": [{"path": p, "src": "planner"} for p in block.get("create") or ()],
            "read": [], "partial": False}


def _entries(d) -> list[dict]:
    files = d if isinstance(d, dict) else getattr(d, "files", None)
    if not isinstance(files, dict):
        return []
    return [e for key in ("change", "new") for e in files.get(key) or ()]


def decision_files(d) -> list[str]:
    """Paths to change then paths to create, de-duplicated, order kept. ``d`` is a Decision, anything with
    ``.files``, or a files dict."""
    return list(dict.fromkeys(e["path"] for e in _entries(d)))


def scope_files(d) -> tuple[list[str], list[str]]:
    """(trusted, judge_only): paths from the text, a hint or the planner, and paths only the judge picked."""
    entries = _entries(d)
    trusted = list(dict.fromkeys(e["path"] for e in entries if e["src"] in _TRUSTED_SRC))
    judged = [e["path"] for e in entries if e["src"] == "judge" and e["path"] not in trusted]
    return trusted, list(dict.fromkeys(judged))


def path_tags(f: Features, facts=None) -> frozenset[str]:
    """Risk tags with path evidence: the facts' non-text risk items, else the path regexes over the prepass files."""
    if isinstance(facts, facts_mod.Facts):
        return frozenset(i.tag for i in facts.risk_evidence if i.source != "text")
    return frozenset(tag for tag, _text_rx, path_rx in prepass.RISK_PATTERNS
                     if any(path_rx.search(p) for p in f.files))


def decide(task: Task, f: Features, cfg: Config, judge=None, clf=None, facts=None) -> Decision:
    """Merge params and prepass rules. ``judge`` and ``clf`` are shadow answers: they never change the
    decision and are logged by ``route_record``. ``facts`` (a facts.Facts, or None) gives the path evidence the
    design trigger reads; a design decision is mapped to cascade (``map_design``)."""
    reasons: list[str] = []
    kind, type_src = _resolve_type(task, f)
    difficulty, diff_src = _resolve_difficulty(task, f, reasons)
    cell_design = cfg.ladders[kind][difficulty] == "design_route"
    triggered = cell_design or _design_trigger(f, difficulty, cfg, facts)
    if f.judgment_keywords:
        reasons.append("judgment_keywords_logged_only")
    if task.params.route:
        route, route_src = task.params.route, "param"
        if triggered and task.params.route != "design":
            reasons.append("param_overrode_trigger")
    elif triggered:
        route, route_src = "design", "rule"
    else:
        route, route_src = "cascade", "default"
    ladder, ladder_src = build_ladder(route, kind, difficulty, cfg, task.params, reasons)
    needs_review = review_rule(kind, difficulty, cfg)
    decision = Decision(
        route=route,
        type=kind,
        difficulty=difficulty,
        risk=f.risk_floor,
        risk_tags=f.risk_tags,
        load_bearing=bool(f.public_interface or f.new_module),
        high_judgment=bool(f.judgment_keywords),
        needs_review=needs_review,
        isolate_hint=None,
        needs_clarification=f.ambiguity >= 3,
        ladder=ladder,
        reasons=tuple(reasons),
        files=files_from_facts(facts, task.hint_files),
        sources={
            "route": route_src,
            "type": type_src,
            "difficulty": diff_src,
            "risk": "rule",
            "ladder": ladder_src,
        },
    )
    check_invariants(decision, f, task, cfg)
    return map_design(decision, cfg, task.params)


def map_design(d: Decision, cfg: Config, params) -> Decision:
    """A design decision runs as cascade with an Opus plan and an S+ review until the design route is built."""
    if d.route != "design" or not (cfg.design_route or {}).get("map_to_cascade", True):
        return d
    reasons = list(d.reasons)
    ladder, _src = build_ladder("cascade", d.type, d.difficulty, cfg, params, reasons)
    reasons.append(MAPPED)
    return replace(d, route="cascade", ladder=ladder, planner="O+" if d.difficulty == "critical" else "O",
                   required_review="S+", needs_review=True, reasons=tuple(dict.fromkeys(reasons)))


def check_invariants(d: Decision, f: Features, task: Task, cfg: Config) -> None:
    """I1–I3. I4–I6 are judge-only and are not checked in v1."""
    if _RISK.index(d.risk) < _RISK.index(f.risk_floor) or not set(f.risk_tags) <= set(d.risk_tags):
        raise InvariantError("I1")
    floor = f.difficulty_floor
    if _DIFF.index(d.difficulty) < _DIFF.index(floor):
        raise InvariantError("I2")
    given = task.params.difficulty
    if given is not None and _DIFF.index(given) >= _DIFF.index(floor) and d.difficulty != given:
        raise InvariantError("I2")
    mode = _budget_mode(task.params.budget)
    for code in d.ladder:
        rung = cfg.rungs.get(code)
        if rung is None or not _allowed(cfg, rung) or not _keeps(cfg, code, mode):
            raise InvariantError("I3")


def _summary(d: Decision) -> dict:
    return {"route": d.route, "type": d.type, "difficulty": d.difficulty, "risk": d.risk,
            "risk_tags": sorted(d.risk_tags)}


def route_record(task: Task, f: Features, d: Decision, judge=None, clf=None, facts=None, rules_from=None,
                 parts=None) -> dict:
    """§7.4 route record; pipeline.start appends it. ``judge``/``clf`` are shadow answers or None.
    ``facts`` is a facts.Facts, or the exception its build raised; None leaves the facts keys out.
    With the judge on, ``d`` is the merged decision, ``rules_from`` the rules decision and ``parts`` the plain-data
    §9.1 keys (judge, hints, overrides, files, judge_absent, fallback, ...) to add."""
    rules = _summary(rules_from or d)
    applied = _summary(d)
    applied["ladder"] = list(d.ladder)
    rec = {
        "role": "route",
        "task": task.id,
        "fingerprint": task.fingerprint,
        "prepass_confident": f.confident,
        "rules": rules,
        "judge": shadow.route_summary(judge),
        "classifier": shadow.route_summary(clf),
        "applied": applied,
        "sources": dict(d.sources),
        "invariant_violation": False,
    }
    rec.update(parts or {})
    if facts is not None:
        rec["facts"], rec["risk_evidence"] = facts_mod.record_parts(facts)
    return rec


def at_least(rung: str | None, floor: str) -> str:
    """The stronger Claude rung of ``rung`` and ``floor`` (S < S+ < O < O+)."""
    return floor if rung is None or STRENGTH.index(rung) < STRENGTH.index(floor) else rung


def planner_rung(d: Decision, cfg: Config) -> str | None:
    """The planner rung ``mr run`` calls before the first rung (a set planner, or cascade medium and up), else None."""
    if d.planner or (d.route == "cascade" and _DIFF.index(d.difficulty) >= _DIFF.index("medium")):
        return d.planner or cfg.planner.rungs[d.difficulty]
    return None


def require_strong(d: Decision) -> Decision:
    """High risk (path evidence): a planner of at least S+ (O when hard or critical) and an S+ review at least."""
    planner = "O" if d.difficulty in ("hard", "critical") else "S+"
    return replace(d, planner=at_least(d.planner, planner), required_review=at_least(d.required_review, "S+"),
                   needs_review=True)


def build_ladder(route, kind, difficulty, cfg, params, reasons) -> tuple[tuple[str, ...], str]:
    """Select the ladder for a route and cell, then apply ``params.prefer`` and ``params.budget``."""
    ladder, ladder_src = _select_ladder(route, kind, difficulty, cfg, reasons)
    ladder = _apply_prefer(ladder, params.prefer)
    before = ladder
    ladder = _apply_budget(ladder, cfg, params.budget, reasons)
    if route != "design" and before and not ladder:
        reasons.append("empty_ladder_after_budget")
    return ladder, ladder_src


def review_rule(kind: str, difficulty: str, cfg: Config) -> bool:
    return bool(
        (kind in _REVIEW_AT and _DIFF.index(difficulty) >= _DIFF.index("medium"))
        or kind == "refactor"
        or cfg.review_required.get(f"{kind}_{difficulty}", False)
    )


def _resolve_type(task: Task, f: Features) -> tuple[str, str]:
    if task.params.type:
        return task.params.type, "param"
    if f.type == "code" and not any(f.type_hits.values()):
        return "code", "default"
    return f.type, "rule"


def _resolve_difficulty(task: Task, f: Features, reasons: list[str]) -> tuple[str, str]:
    floor = f.difficulty_floor
    given = task.params.difficulty
    if given is not None:
        if _DIFF.index(given) < _DIFF.index(floor):
            reasons.append("param_raised_by_rule")
            return floor, "rule"
        return given, "param"
    est = f.difficulty_est
    chosen = est if _DIFF.index(est) >= _DIFF.index(floor) else floor
    return chosen, "rule"


def _design_trigger(f: Features, difficulty: str, cfg: Config, facts=None) -> bool:
    """Only path evidence triggers design: a text-only risk word never does."""
    spec = (cfg.design_route or {}).get("triggers") or {}
    tags = spec.get("risk_tags") or ()
    if path_tags(f, facts) & set(tags):
        return True
    minimum = spec.get("cross_layer_min")
    layers = f.cross_layer
    if isinstance(facts, facts_mod.Facts):
        layers = prepass._cross_layer([c.path for c in facts.candidates if c.exists])
    if isinstance(minimum, int) and not isinstance(minimum, bool) and layers >= minimum:
        return True
    return bool(spec.get("hard_and_ambiguous") and difficulty == "hard" and f.ambiguity >= 2)


def _select_ladder(route, kind, difficulty, cfg, reasons):
    if route == "design":
        return (), "rule"
    if route == "judgment":
        key = "critical" if difficulty == "critical" else "implement"
        return tuple(cfg.judgment_route[key]), "rule"
    cell = cfg.ladders[kind][difficulty]
    if cell != "design_route":
        return tuple(cell), "default"
    for lower_name in reversed(_DIFF[: _DIFF.index(difficulty)]):
        lower = cfg.ladders[kind][lower_name]
        if lower != "design_route":
            reasons.append(f"design_cell_forced_cascade:{difficulty}")
            return tuple(lower), "rule"
    reasons.append(f"design_cell_forced_cascade:{difficulty}")
    return (), "rule"


def _apply_prefer(ladder: tuple[str, ...], prefer: str | None) -> tuple[str, ...]:
    if not prefer:
        return ladder
    if prefer.startswith("cursor:"):  # hard rule §0.3: no cursor-path models via --prefer
        raise RouteError("Anthropic models run only via the claude CLI (hard rule)")
    return (prefer,) + tuple(code for code in ladder if code != prefer)


def _apply_budget(ladder, cfg, budget, reasons) -> tuple[str, ...]:
    if budget == "high":
        reasons.append("budget_high_not_built")
        return ladder
    mode = _budget_mode(budget)
    if mode == "normal":
        return ladder
    return tuple(code for code in ladder if _keeps(cfg, code, mode))


def _budget_mode(budget: str | None) -> str:
    if budget in (None, "normal", "high"):
        return "normal"
    return budget


def _keeps(cfg: Config, code: str, budget: str) -> bool:
    rung = cfg.rungs.get(code)
    if rung is None or budget == "normal":
        return True
    if budget == "low":
        return code not in _OPUS
    if budget == "free":
        return rung.path != "max" and code not in _KIMI_DROP
    return True


def _allowed(cfg: Config, rung) -> bool:
    patterns: list[str] = []
    for entry in cfg.allowed:
        if not isinstance(entry, str) or entry.startswith("judge:"):
            continue
        patterns.append(entry if entry.startswith(("cursor:", "max:")) else "cursor:" + entry)
    return _match(f"{rung.path}:{rung.model}", patterns)
