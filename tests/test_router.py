"""Rules-only router (AC-RT-1, AC-RT-2, plan §3.1, §7.3)."""
import random
import unittest

from mr.config import from_dict
from mr.defaults import defaults
from mr.prepass import Features
from mr.router import InvariantError, RouteError, check_invariants, decide, route_record
from mr.task import Params, Task

_DIFF = ("trivial", "easy", "medium", "hard", "critical")
_RISK = ("low", "medium", "high")
_TYPES = (
    "code",
    "bugfix",
    "refactor",
    "test",
    "ios-native",
    "ui",
    "review",
    "plan",
    "explain",
    "docs",
)
_HIGH = frozenset({"auth", "payments", "migration", "data-deletion"})
_MED = frozenset({"native-config", "deps", "secrets"})
_TAGS = tuple(sorted(_HIGH | _MED))
_PREFERS = (
    None,
    "G-l",
    "G-m",
    "G-h",
    "G-x",
    "C25",
    "K-h",
    "K-x",
    "S",
    "S+",
    "O",
    "O+",
    "cursor:sonnet",
    "cursor:opus",
)

# plan §3.1; review is the single-reviewer row from spec §4.3, not the council.
_EXPECTED = {
    "code": {
        "trivial": ("G-l", "G-m"),
        "easy": ("G-m", "G-h", "S"),
        "medium": ("G-h", "G-x", "S"),
        "hard": ("G-x", "S+", "O"),
        "critical": "design_route",
    },
    "bugfix": {
        "trivial": ("G-m", "G-h"),
        "easy": ("G-h", "G-x", "S"),
        "medium": ("G-x", "S", "O"),
        "hard": ("G-x", "S+", "O"),
        "critical": ("S+", "O+"),
    },
    "refactor": {
        "trivial": ("G-m", "G-h"),
        "easy": ("G-h", "G-x"),
        "medium": ("G-x", "S"),
        "hard": ("G-x", "S+", "O"),
        "critical": "design_route",
    },
    "test": {
        "trivial": ("G-l", "G-m"),
        "easy": ("G-m", "G-h"),
        "medium": ("G-h", "G-x"),
        "hard": ("G-x", "S+"),
        "critical": ("S", "O"),
    },
    "ios-native": {
        "trivial": ("G-m", "G-h"),
        "easy": ("G-h", "G-x", "S"),
        "medium": ("G-x", "S", "O"),
        "hard": ("S+", "O"),
        "critical": "design_route",
    },
    "ui": {
        "trivial": ("C25", "G-m"),
        "easy": ("G-m", "G-h"),
        "medium": ("G-h", "G-x", "S"),
        "hard": ("G-x", "S+"),
        "critical": "design_route",
    },
    "review": {
        "trivial": ("G-m",),
        "easy": ("G-h",),
        "medium": ("S",),
        "hard": ("S",),
        "critical": ("S",),
    },
    "plan": {
        "trivial": ("G-m",),
        "easy": ("G-h",),
        "medium": ("G-x",),
        "hard": "design_route",
        "critical": "design_route",
    },
    "explain": {
        "trivial": ("G-l",),
        "easy": ("G-m",),
        "medium": ("G-h",),
        "hard": ("G-x", "S"),
        "critical": ("S",),
    },
    "docs": {
        "trivial": ("G-l",),
        "easy": ("G-l", "G-m"),
        "medium": ("G-m", "G-h"),
        "hard": ("G-h", "S"),
        "critical": ("S",),
    },
}


def _task(params: Params) -> Task:
    return Task(
        id="t-00000000",
        text="task",
        repo="/work/app",
        base="a" * 40,
        params=params,
        files_named=(),
        source="cli",
    )


def _features(**overrides) -> Features:
    base = dict(
        type_hits={},
        files_named=(),
        grep_hit_files=0,
        risk_tags=frozenset(),
        risk_floor="low",
        difficulty_floor="trivial",
        difficulty_est="trivial",
        cross_layer=0,
        public_interface=False,
        new_module=False,
        ambiguity=0,
        judgment_keywords=False,
        confident=True,
        type="code",
        n_files=0,
    )
    base.update(overrides)
    return Features(**base)


def _floors(tags: frozenset[str]) -> tuple[str, str]:
    if tags & _HIGH:
        return "high", "hard"
    if tags & _MED:
        return "medium", "medium"
    return "low", "trivial"


class TestRouter(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = from_dict(defaults())

    def test_table_cells(self) -> None:
        self.assertEqual(len(_TYPES) * len(_DIFF), 50)
        blank = _features()
        for kind in _TYPES:
            for diff in _DIFF:
                with self.subTest(type=kind, difficulty=diff):
                    cell = _EXPECTED[kind][diff]
                    self.assertEqual(self.cfg.ladders[kind][diff], cell)
                    decision = decide(
                        _task(Params(type=kind, difficulty=diff)), blank, self.cfg
                    )
                    if cell == "design_route":
                        self.assertEqual(decision.route, "design")
                        self.assertEqual(decision.ladder, ())
                    else:
                        self.assertEqual(decision.route, "cascade")
                        self.assertEqual(decision.ladder, cell)

    def test_precedence_properties(self) -> None:
        rng = random.Random(1)
        raised = cursor = 0
        for i in range(5000):
            tags = frozenset(tag for tag in _TAGS if rng.random() < 0.3)
            risk, floor = _floors(tags)
            kind = rng.choice(_TYPES)
            features = _features(
                type=kind,
                type_hits={kind: 1} if rng.random() < 0.5 else {},
                risk_tags=tags,
                risk_floor=risk,
                difficulty_floor=floor,
                difficulty_est=rng.choice(_DIFF),
                cross_layer=rng.randrange(4),
                ambiguity=rng.randrange(4),
                public_interface=bool(rng.randrange(2)),
                new_module=bool(rng.randrange(2)),
                judgment_keywords=bool(rng.randrange(2)),
                confident=bool(rng.randrange(2)),
            )
            prefer = rng.choice(_PREFERS)
            budget = rng.choice((None, "free", "low", "normal", "high"))
            params = Params(
                type=rng.choice((None, kind)),
                difficulty=rng.choice((None,) + _DIFF),
                route=rng.choice((None, "design", "judgment", "cascade")),
                prefer=prefer,
                budget="normal" if budget is None else budget,
            )
            if budget is None:
                params = Params(
                    type=params.type,
                    difficulty=params.difficulty,
                    route=params.route,
                    prefer=prefer,
                    budget=None,
                )
            task = _task(params)
            try:
                decision = decide(task, features, self.cfg)
            except RouteError as exc:
                self.assertIn(prefer, ("cursor:sonnet", "cursor:opus"), i)
                self.assertEqual(
                    str(exc), "Anthropic models run only via the claude CLI (hard rule)"
                )
                cursor += 1
                continue
            except InvariantError as exc:
                self.fail(f"{i}: {exc}")
            check_invariants(decision, features, task, self.cfg)
            self.assertGreaterEqual(_RISK.index(decision.risk), _RISK.index(risk))
            self.assertTrue(tags <= decision.risk_tags)
            self.assertGreaterEqual(
                _DIFF.index(decision.difficulty), _DIFF.index(floor)
            )
            if params.difficulty is not None and _DIFF.index(params.difficulty) >= _DIFF.index(floor):
                self.assertEqual(decision.difficulty, params.difficulty)
            if (
                params.difficulty is not None
                and _DIFF.index(params.difficulty) < _DIFF.index(floor)
            ):
                self.assertEqual(decision.difficulty, floor)
                self.assertIn("param_raised_by_rule", decision.reasons)
                raised += 1
            if params.budget == "free":
                for code in decision.ladder:
                    rung = self.cfg.rungs[code]
                    self.assertNotEqual(rung.path, "max", (i, code))
                    self.assertNotIn(code, ("K-h", "K-x"), (i, code))
            if features.judgment_keywords:
                self.assertIn("judgment_keywords_logged_only", decision.reasons)
        self.assertGreater(cursor, 0)
        self.assertGreater(raised, 0)

    def test_prefer_cursor_rejected(self) -> None:  # hard rule §0.3, AC-CFG-5
        blank = _features()
        for prefer in ("cursor:sonnet", "cursor:opus", "cursor:grok-4.7-high"):
            with self.subTest(prefer=prefer):
                with self.assertRaises(RouteError) as caught:
                    decide(
                        _task(Params(type="code", difficulty="easy", prefer=prefer)),
                        blank,
                        self.cfg,
                    )
                self.assertEqual(
                    str(caught.exception),
                    "Anthropic models run only via the claude CLI (hard rule)",
                )

    def test_prefer_moves_rung_first(self) -> None:
        blank = _features()
        inserted = decide(
            _task(Params(type="code", difficulty="easy", prefer="G-l")), blank, self.cfg
        )
        self.assertEqual(inserted.ladder, ("G-l", "G-m", "G-h", "S"))
        moved = decide(
            _task(Params(type="code", difficulty="easy", prefer="G-h")), blank, self.cfg
        )
        self.assertEqual(moved.ladder, ("G-h", "G-m", "S"))

    def test_budget_low_drops_opus(self) -> None:
        blank = _features()
        medium = decide(
            _task(Params(type="bugfix", difficulty="medium", budget="low")),
            blank,
            self.cfg,
        )
        self.assertEqual(medium.ladder, ("G-x", "S"))
        critical = decide(
            _task(Params(type="bugfix", difficulty="critical", budget="low")),
            blank,
            self.cfg,
        )
        self.assertEqual(critical.ladder, ("S+",))
        self.assertNotIn("O", critical.ladder)
        self.assertNotIn("O+", critical.ladder)

    def test_budget_free_and_empty_ladder(self) -> None:
        blank = _features()
        kept = decide(
            _task(Params(type="bugfix", difficulty="medium", budget="free")),
            blank,
            self.cfg,
        )
        self.assertEqual(kept.ladder, ("G-x",))
        empty = decide(
            _task(Params(type="ios-native", difficulty="hard", budget="free")),
            blank,
            self.cfg,
        )
        self.assertEqual(empty.ladder, ())
        self.assertFalse(empty.needs_clarification)
        self.assertIn("empty_ladder_after_budget", empty.reasons)

    def test_design_trigger_and_cascade_override(self) -> None:
        features = _features(
            risk_tags=frozenset({"auth"}),
            risk_floor="high",
            difficulty_floor="hard",
            difficulty_est="hard",
        )
        design = decide(_task(Params(type="code")), features, self.cfg)
        self.assertEqual(design.route, "design")
        self.assertEqual(design.ladder, ())
        forced = decide(_task(Params(type="code", route="cascade")), features, self.cfg)
        self.assertEqual(forced.route, "cascade")
        self.assertIn("param_overrode_trigger", forced.reasons)
        self.assertEqual(forced.ladder, ("G-x", "S+", "O"))

    def test_forced_cascade_uses_lower_cell(self) -> None:
        blank = _features()
        critical = decide(
            _task(Params(type="plan", difficulty="critical", route="cascade")),
            blank,
            self.cfg,
        )
        self.assertEqual(critical.route, "cascade")
        self.assertEqual(critical.ladder, ("G-x",))
        self.assertIn("design_cell_forced_cascade:critical", critical.reasons)
        self.assertIn("param_overrode_trigger", critical.reasons)
        hard = decide(
            _task(Params(type="plan", difficulty="hard", route="cascade")),
            blank,
            self.cfg,
        )
        self.assertEqual(hard.ladder, ("G-x",))
        self.assertIn("design_cell_forced_cascade:hard", hard.reasons)

    def test_route_record(self) -> None:
        features = _features(type="bugfix", type_hits={"bugfix": 1}, confident=False)
        task = _task(Params())
        decision = decide(task, features, self.cfg)
        record = route_record(task, features, decision)
        self.assertEqual(record["role"], "route")
        self.assertEqual(record["fingerprint"], task.fingerprint)
        self.assertFalse(record["prepass_confident"])
        self.assertIsNone(record["judge"])
        self.assertIsNone(record["classifier"])
        self.assertFalse(record["invariant_violation"])
        self.assertEqual(record["applied"]["ladder"], list(decision.ladder))
        self.assertIsInstance(record["rules"]["risk_tags"], list)
        self.assertEqual(record["rules"]["route"], decision.route)


if __name__ == "__main__":
    unittest.main()
