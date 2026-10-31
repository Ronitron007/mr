"""Tests for mr.classify (AC-SIG-3, §5.9, docs/p0/README.md §3, D-F1..D-F5)."""

import json
import os
import unittest

from mr.classify import Inputs, base_model, classify, is_complete

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "p0", "failure_table.json")


def _load_rows() -> list[dict]:
    with open(_FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


class TestFailureTable(unittest.TestCase):
    def test_failure_table(self) -> None:
        rows = _load_rows()
        self.assertEqual(len(rows), 20)
        for row in rows:
            with self.subTest(id=row["id"]):
                result = classify(Inputs.from_dict(row["inputs"]))
                expect = row["expect"]
                self.assertEqual(result.cls, expect["cls"])
                self.assertEqual(result.hard, expect["hard"])
                self.assertEqual(result.confident, expect["confident"])
                if expect["cls"] in ("guard_deny", "infra"):
                    # Pre-checks (§3 steps 1-2): detectors never run.
                    self.assertEqual(result.matched, ())
                else:
                    # confident is exactly "|M| == 1" (§3 step 4).
                    self.assertEqual(result.confident, len(result.matched) == 1)
                self.assertTrue(result.evidence)

    def test_matched_priority_order(self) -> None:
        rows = {row["id"]: row for row in _load_rows()}
        # F07: D fails and H fails -> {off_track, skipped}, off_track first (D-F5).
        f07 = classify(Inputs.from_dict(rows["F07"]["inputs"]))
        self.assertEqual(f07.matched, ("off_track", "skipped"))
        # F20: valid pushback plus H fail -> {spec_suspect, skipped}.
        f20 = classify(Inputs.from_dict(rows["F20"]["inputs"]))
        self.assertEqual(f20.matched, ("spec_suspect", "skipped"))
        # F19: no detector matches -> fallback tried_wrong, matched empty (D-F4).
        f19 = classify(Inputs.from_dict(rows["F19"]["inputs"]))
        self.assertEqual(f19.matched, ())
        self.assertEqual(f19.cls, "tried_wrong")


class TestBaseModel(unittest.TestCase):
    def test_strips_trailing_effort_suffix(self) -> None:
        cases = {
            "grok-4.7-low": "grok-4.7",
            "grok-4.7-medium": "grok-4.7",
            "grok-4.7-high": "grok-4.7",
            "grok-4.7-xhigh": "grok-4.7",
            "grok-4.7-low-fast": "grok-4.7",
            "grok-4.7-medium-fast": "grok-4.7",
            "grok-4.7-high-fast": "grok-4.7",
            "grok-4.7-xhigh-fast": "grok-4.7",
            "kimi-k3-high": "kimi-k3",
            "kimi-k3-max": "kimi-k3",
        }
        for given, want in cases.items():
            with self.subTest(model=given):
                self.assertEqual(base_model(given), want)

    def test_leaves_other_ids_alone(self) -> None:
        for given in (
            "grok-4.7",
            "claude-sonnet-5-5",
            "claude-opus-5-5",
            "composer-2.5",
            "grok-4.7-highlight",
            "grok-4.7-fast",
        ):
            with self.subTest(model=given):
                self.assertEqual(base_model(given), given)


class TestIsComplete(unittest.TestCase):
    def test_complete(self) -> None:
        rows = {row["id"]: row for row in _load_rows()}
        # F06: mapped files touched, H green, status done.
        self.assertTrue(is_complete(Inputs.from_dict(rows["F06"]["inputs"])))
        # F14: H fails.
        self.assertFalse(is_complete(Inputs.from_dict(rows["F14"]["inputs"])))
        # F13: mapped files untouched.
        self.assertFalse(is_complete(Inputs.from_dict(rows["F13"]["inputs"])))
        # F15: mr_result null -> status null.
        self.assertFalse(is_complete(Inputs.from_dict(rows["F15"]["inputs"])))


class TestMalformedMrResult(unittest.TestCase):
    def _inputs(self, mr_result: dict | None) -> Inputs:
        return Inputs(
            model="grok-4.7-high",
            rc=0,
            is_error=False,
            timed_out=False,
            wrapper_exit_75=False,
            hook_block=False,
            has_envelope=True,
            diff_files=(),
            diff_loc=0,
            mapped_files_touched=True,
            H=True,
            D=True,
            A_failing=(),
            C=None,
            mr_result=mr_result,
            prior_attempts=(),
        )

    def test_status_missing_behaves_as_null(self) -> None:
        # Same inputs, well-formed status "done" with no diff -> skipped.
        done = classify(
            self._inputs(
                {
                    "item": "I1",
                    "status": "done",
                    "requirements": [],
                    "deviations": [],
                    "pushback": None,
                    "confidence": 0.7,
                }
            )
        )
        self.assertEqual(done.cls, "skipped")
        self.assertTrue(done.confident)
        # Status key missing -> s is null, so the "done with no diff" detector
        # does not fire and no other detector matches -> fallback tried_wrong.
        malformed = classify(self._inputs({"item": "I1", "requirements": []}))
        self.assertEqual(malformed.cls, "tried_wrong")
        self.assertFalse(malformed.confident)
        self.assertEqual(malformed.matched, ())


class TestScopeNeededPushback(unittest.TestCase):
    def test_valid_scope_needed_pushback_no_diff(self) -> None:
        # D-F3, seen live: grok pushed back because the caller sat outside SCOPE.
        inputs = Inputs(
            model="grok-4.7-high",
            rc=0,
            is_error=False,
            timed_out=False,
            wrapper_exit_75=False,
            hook_block=False,
            has_envelope=True,
            diff_files=(),
            diff_loc=0,
            mapped_files_touched=False,
            H=True,
            D=True,
            A_failing=("A1",),
            C=None,
            mr_result={
                "item": "I1",
                "status": "pushback",
                "requirements": [{"id": "R1", "status": "not_met", "evidence": ""}],
                "deviations": [],
                "pushback": {
                    "type": "scope_needed",
                    "item": "A1",
                    "evidence": "caller src/features/chat/ChatScreen.tsx is outside SCOPE",
                    "proposal": "widen scope to include the caller",
                },
                "confidence": 0.7,
            },
            prior_attempts=(),
        )
        result = classify(inputs)
        self.assertEqual(result.cls, "spec_suspect")
        self.assertFalse(result.hard)  # D-F3: pushback is never hard
        self.assertTrue(result.confident)
        self.assertEqual(result.matched, ("spec_suspect",))
        self.assertTrue(any("scope_needed" in line for line in result.evidence))


if __name__ == "__main__":
    unittest.main()
