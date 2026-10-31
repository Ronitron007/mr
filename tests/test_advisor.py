"""S+A advisor contract (advisor-consult-spec AC-ADV-1..5, -7, -8, -10; AC-RUN-3 exception)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mr import advisor, config, runners
from mr.defaults import defaults

FIX = Path(__file__).resolve().parent / "fixtures" / "advisor"
TABLE = json.loads((FIX / "decide_table.json").read_text(encoding="utf-8"))
CFG = config.ADVISOR_DEFAULTS
TRIAL_TEXT = ("Fix applyAgentStep: a late `running` event regresses a step that already reached "
              "`complete` or `error`. Add agent-steps.test.ts.")
TRIAL_PATHS = ["src/lib/chat/agent-steps.ts", "src/lib/chat/agent-steps.test.ts"]
INJECTION_SHA = "437a522ad33be5022e669928e634a1dbe8c59f50cd6d1f5de58626b7c985eb50"  # adv.v2 render, trial task


def _base() -> dict:
    return copy.deepcopy(TABLE[1]["inputs"]) | {"flag": "auto"}  # V02 minus --advisor off: a Y2 task


def _dec(slots=("early",), row="Y2") -> advisor.AdvisorDecision:
    return advisor.AdvisorDecision(True, tuple(slots), row, (), 45000, 0.218, "ok", "S+A")


class Stream:
    """Synthetic stream-json writer."""

    def __init__(self, tmp: str) -> None:
        self.tmp, self.lines, self.n = tmp, [{"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"}], 0

    def msg(self, ts: str, *blocks) -> Stream:
        self.n += 1
        self.lines.append({"type": "assistant", "timestamp": ts,
                           "message": {"id": f"m{self.n}", "content": list(blocks)}})
        return self

    def consult(self, ts: str, uid: str = "srv1") -> Stream:
        return self.msg(ts, {"type": "server_tool_use", "id": uid, "name": "advisor", "input": {}})

    def result_block(self, ts: str, uid: str = "srv1", error: str | None = None) -> Stream:
        content = ({"type": "advisor_tool_result_error", "error_code": error} if error
                   else {"type": "advisor_redacted_result", "encrypted_content": "x"})
        return self.msg(ts, {"type": "advisor_tool_result", "tool_use_id": uid, "content": content})

    def result(self, text: str = "done", opus: dict | None = None, iterations=None) -> Stream:
        mu = {"claude-sonnet-5-5": {"inputTokens": 10, "outputTokens": 100, "costUSD": 0.13}}
        if opus is not None:
            mu.update(opus)
        self.lines.append({"type": "result", "subtype": "success", "is_error": False, "result": text,
                           "num_turns": 5, "total_cost_usd": 0.3, "modelUsage": mu,
                           "usage": {"iterations": iterations or [{"type": "message"}]}})
        return self

    def write(self, name: str = "s.jsonl") -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(x) for x in self.lines) + "\n")
        return path


OPUS = {"claude-opus-5-5": {"inputTokens": 40000, "outputTokens": 1500, "costUSD": 0.19}}


class TreeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.tree = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(self.tree, "src/lib/chat"))
        Path(self.tree, "src/lib/chat/agent-steps.ts").write_text("x\n" * 100)

    def tearDown(self):
        self._tmp.cleanup()

    def relay(self, **over) -> str:
        data = {"slot": "early", "verdict": "concern",
                "risks": [{"what": "late running clears error", "where": "src/lib/chat/agent-steps.ts:86",
                           "severity": "med", "test": "error then running"}],
                "missing_test": None, "confidence": 0.7, "approach": "keep", "relayed": "verbatim"} | over
        return "MR_ADVISOR " + json.dumps(data)


class TestDecide(unittest.TestCase):
    def test_decision_table(self):  # AC-ADV-1
        self.assertEqual(len(TABLE), 50)
        for row in TABLE:
            with self.subTest(row=row["id"]):
                dec = advisor.decide(row["inputs"], CFG)
                got = {"row": dec.row, "consult": dec.consult, "slots": list(dec.slots), "gate": dec.gate,
                       "run_rung": dec.run_rung}
                self.assertEqual(got, row["expect"], row.get("note"))
                self.assertEqual(dec.run_rung == "S+A", dec.consult)
                self.assertTrue(dec.reasons and dec.reasons[0].startswith(dec.row))
                if dec.consult:
                    est = row["inputs"]["est_consult_tokens"]
                    self.assertEqual(dec.est_consult_tokens, est[dec.slots[0]])
                    self.assertAlmostEqual(dec.est_consult_usd,
                                           sum(e * 4e-6 + 0.038 for e in (est[s] for s in dec.slots)), 6)

    def test_never_rows(self):  # AC-ADV-2, seeded property test
        rnd = random.Random(20261031)  # 20261030 yields 44 consults at hour_max 30 / five_hour_max 85 (67 at the old 35/92 gate); 20261031 yields 59
        choice = rnd.choice
        rungs = ["S+", "S", "O", "O+", "G-h", "G-x", "K-h", "C25"]
        diffs = ["trivial", "easy", "medium", "hard", "critical"]
        tags = ["state-machine", "ordering", "event-stream", "idempotency", "money", "agent-flow", "auth"]
        models = ["grok-4.7-high", "kimi-k3-high", "claude-sonnet-5-5", "composer-2.5"]
        seen = {"consult": 0, "never": 0}
        for _ in range(4000):
            inp = {
                "mode": choice(["on"] * 6 + ["off", "shadow", "trial", True, False]),
                "flag": choice(["auto"] * 4 + ["off", "force"]), "consults_flag": choice([1, 2]),
                "slot_flag": choice([None, "early", "final"]),
                "budget": choice(["normal"] * 4 + ["free", "low", "high"]),
                "rung": choice(["S+"] * 8 + rungs), "route": choice(["cascade"] * 3 + ["design", "judgment"]),
                "item_kind": choice([None, "logic", "mechanical", "ui", "native"]),
                "type": choice(["code", "bugfix", "refactor"] * 2 + ["docs", "explain", "test", "review", "ui",
                                                                     "ios-native"]),
                "difficulty": choice(["hard", "critical"] * 2 + diffs), "risk": choice(["low", "medium", "high"]),
                "ambiguity": choice(["low", "medium", "high"]), "load_bearing": choice([True, False]),
                "sf_tags": rnd.sample(tags, rnd.randint(0, 3)),
                "hist": [{"model": choice(models), "cls": choice(["tried_wrong", "skipped", "infra"]),
                          "failing": [choice(["A1", "A2"])]} for _ in range(rnd.randint(0, 2))],
                "guard": {"hour": rnd.randint(0, 30), "reserved": rnd.choice([0, 0, 6, 12]),
                          "five_hour_pct": rnd.randint(0, 95)},
                "adv_hour": rnd.randint(0, 3), "adv_day": rnd.randint(0, 10),
                "est_consult_tokens": {"early": rnd.randint(30000, 160000), "final": rnd.randint(30000, 190000)},
                "rows_off": rnd.sample(["Y1", "Y2", "Y3", "Y4", "Y5", "F1"], rnd.randint(0, 2)),
                "env_ok": rnd.random() > 0.05, "demoted": rnd.random() < 0.05, "breaker": rnd.random() < 0.05,
                "sa_used_in_task": rnd.random() < 0.05,
            }
            dec = advisor.decide(inp, CFG)
            never = (inp["rung"] != "S+" or inp["difficulty"] in ("trivial", "easy") or inp["route"] == "design"
                     or inp["mode"] in ("off", False) or inp["flag"] == "off" or inp["budget"] in ("free", "low"))
            seen["never"] += never
            seen["consult"] += dec.consult
            if never:
                self.assertFalse(dec.consult, inp)
                self.assertEqual(dec.gate, "ok", inp)
            self.assertEqual(dec.consult, dec.run_rung == "S+A", inp)
            self.assertEqual(dec.consult, dec.gate == "ok" and bool(dec.slots), inp)
            if dec.consult:
                self.assertIn(dec.row, ("F1", "Y1", "Y2", "Y3", "Y4", "Y5"))
                self.assertTrue(set(dec.slots) <= {"early", "final"})
                if len(dec.slots) == 2:
                    self.assertEqual((dec.row, inp["consults_flag"]), ("Y1", 2))
                for slot in dec.slots:
                    self.assertLessEqual(inp["est_consult_tokens"][slot], 150000)
                self.assertNotIn(dec.row, inp["rows_off"])
            else:
                self.assertEqual(dec.slots, ())
        self.assertGreater(seen["consult"], 50)
        self.assertGreater(seen["never"], 1000)

    def test_gates(self):  # AC-ADV-3
        def run(**over):
            inp = _base()
            inp["guard"]["hour_max"] = 30  # explicit hourly opt-in; the default has no hourly cap
            for key, val in over.items():
                if key in ("hour", "five_hour_pct", "reserved"):
                    inp["guard"][key] = val
                else:
                    inp[key] = val
            return advisor.decide(inp, CFG)

        self.assertTrue(run(hour=18, reserved=0).consult)  # 18 + 0 + 12 = 30
        self.assertEqual((run(hour=19).gate, run(hour=19).run_rung), ("budget", "S+"))  # 19 + 12 = 31
        self.assertTrue(run(five_hour_pct=73).consult)  # 73 + 12 = 85
        self.assertEqual((run(five_hour_pct=74).gate, run(five_hour_pct=74).run_rung), ("budget", "S+"))
        self.assertEqual(run(hour=30).run_rung, None)
        self.assertTrue(run(adv_hour=2).consult)
        self.assertEqual(run(adv_hour=3).gate, "hourly_cap")
        self.assertEqual(run(adv_day=10).gate, "hourly_cap")
        cfg = CFG | {"per_hour_box": 5}
        self.assertTrue(advisor.decide(_base() | {"adv_hour": 3}, cfg).consult)
        self.assertEqual(run(mode=True).gate, "ok")  # YAML `on`
        self.assertEqual(run(mode=False).row, "N0")

    def test_est_consult(self):
        self.assertEqual(advisor.est_consult(0, 0, 0, 0, "early"), (45000, 0.218))
        tok, usd = advisor.est_consult(1000, 1500, 35000, 2000, "final")
        self.assertEqual(tok, 45000 + 1000 + 1500 + 10000 + 4000 + 6000)
        self.assertAlmostEqual(usd, tok * 4e-6 + 1900 * 20e-6, 9)
        self.assertEqual(advisor.est_consult(0, 0, 0, 999, "early", base=32000)[0], 32000)
        self.assertAlmostEqual(advisor.est_consult(105000, 0, 0, 0, "early")[1], 0.638, 6)  # ~$0.64 at cap


class TestSfTags(unittest.TestCase):
    def test_trial_task(self):
        tags = advisor.sf_tags(TRIAL_TEXT + " Late event ordering.", TRIAL_PATHS)
        self.assertTrue({"state-machine", "ordering", "event-stream", "agent-flow"} <= tags)

    def test_rules(self):
        cases = [
            ("debounce the stale retry", [], {"ordering"}),
            ("avoid a double charge on refund", [], {"idempotency", "money"}),
            ("handle the SSE websocket", [], {"event-stream"}),
            ("rename a button", ["app/checkout/Pay.tsx"], {"money", "payments"}),
            ("rename a button", ["src/lib/cartStore.ts"], {"state-machine"}),
            ("rename a button", ["lib/realtime/x.ts"], {"event-stream"}),
            ("rename a button", ["pkg/agents/run.ts"], {"agent-flow"}),
            ("rename a button", ["src/lib/chat/x.ts"], {"agent-flow"}),
            ("trace the docs", ["README.md"], set()),  # "race" needs a word start
            ("Exactly Once delivery", [], {"idempotency"}),
            ("fix login", [], {"auth"}),
        ]
        for text, paths, want in cases:
            with self.subTest(text=text, paths=paths):
                tags = advisor.sf_tags(text, paths)
                self.assertTrue(want <= tags if want else not tags, tags)
        self.assertEqual(advisor.sf_tags("", []), frozenset())


class TestPrompts(unittest.TestCase):
    def render(self):
        inp = TABLE[15]["inputs"] | {"flag": "force", "difficulty": "hard"}  # trial task under --advisor force
        dec = advisor.decide(inp, CFG)
        self.assertEqual((dec.row, dec.slots), ("F1", ("early",)))
        return advisor.build_injection(
            dec, decision_text="the terminal-state rule for late `running` events",
            focus="state transitions that must never regress; out-of-order events",
            high_risk_paths=["src/lib/chat/agent-steps.ts"])

    def test_injection_golden(self):  # AC-ADV-5
        text = self.render()
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), INJECTION_SHA)
        self.assertNotIn("max_tokens", text)
        self.assertNotIn("{N}", text)
        self.assertNotIn("{SLOT_LINES}", text)
        self.assertNotIn("{FOCUS}", text)
        self.assertIn("exactly 1 time(s)", text)
        self.assertIn("about to commit to an approach on: the terminal-state rule", text)
        self.assertTrue(text.startswith("--- mr advisor protocol adv.v2 (consults allowed: 1) ---"))
        self.assertTrue(advisor.injection_sha().startswith("adv.v2:"))

    def test_injection_two_slots(self):
        dec = _dec(("early", "final"), "Y1")
        paths = [f"p{i}.ts" for i in range(20)]
        text = advisor.build_injection(dec, decision_text="d", focus="f", high_risk_paths=paths)
        self.assertIn("exactly 2 time(s)", text)
        self.assertIn("p11.ts.", text)
        self.assertNotIn("p12.ts", text)  # at most 12 paths
        with self.assertRaises(ValueError):
            advisor.build_injection(advisor.decide(TABLE[0]["inputs"], CFG), decision_text="d", focus="f",
                                    high_risk_paths=[])

    def test_system_prompt(self):
        text = advisor.system_prompt()
        self.assertIn("last two lines of your final message MUST be", text)
        adv = text.index("\nMR_ADVISOR {")
        res = text.index("\nMR_RESULT {")
        self.assertLess(adv, res)
        tail = text[res:]
        for key in ('"advisor":{', '"consults"', '"verdict"', '"summary"'):
            self.assertIn(key, tail)


class TestRelay(TreeCase):
    def test_parse_relay(self):
        ok_line = self.relay()
        status, verdict = advisor.parse_relay(f"all done\n{ok_line}\nMR_RESULT {{}}", self.tree)
        self.assertEqual(status, "ok")
        self.assertEqual(verdict["verdict"], "concern")
        self.assertEqual(advisor.parse_relay("no relay here", self.tree), ("missing", None))
        risk = {"what": "w", "where": "src/lib/chat/agent-steps.ts:1", "severity": "low"}
        bad = [
            self.relay(verdict="maybe"),
            self.relay(risks=[risk] * 4),
            self.relay(risks=[risk | {"where": "src/missing.ts:3"}]),
            self.relay(risks=[risk | {"where": "src/lib/chat/agent-steps.ts"}]),
            self.relay(risks=[risk | {"where": "../etc/passwd:1"}]),
            self.relay(risks=[risk | {"severity": "critical"}]),
            self.relay(confidence=1.5),
            self.relay(confidence="high"),
            self.relay(missing_test={"name": "x" * 2000}),
            "MR_ADVISOR {not json",
            "MR_ADVISOR []",
        ]
        for line in bad:
            with self.subTest(line=line[:80]):
                self.assertEqual(advisor.parse_relay(line, self.tree), ("invalid", None))
        self.assertEqual(advisor.parse_relay(self.relay(risks=[], confidence=0), self.tree)[0], "ok")

    def test_relay_enforced(self):  # AC-ADV-10
        path = (Stream(self.tmp).consult("2026-10-30T10:00:00Z").result_block("2026-10-30T10:00:20Z")
                .result("Fixed it.\nMR_RESULT {\"status\":\"done\"}", opus=OPUS).write())
        aud = advisor.audit(path, _dec(), None, self.tree)
        self.assertEqual((aud.consults, aud.consult_ok, aud.relay_status), (1, 1, "missing"))
        self.assertEqual((aud.advisor_in_tok, aud.advisor_out_tok, aud.advisor_usd), (40000, 1500, 0.19))
        self.assertEqual(aud.consult_latency_s, 20.0)
        self.assertEqual(aud.violations, ())
        self.assertEqual(advisor.fallback_action(aud), "none")
        # the rung result itself is unchanged by the audit
        res = runners._claude_result("claude-sonnet-5-5", 0, Path(path).read_text(), "", 1.0, False, "b")
        self.assertEqual(res.mr_result, {"status": "done"})
        self.assertFalse(res.is_error)
        mon = advisor.Monitor(self.tmp)
        for i in range(10):
            self.assertIsNone(mon.demotion()) if i < 9 else None
            mon.record({"kind": "consult", "usd": aud.advisor_usd, "relay_status": aud.relay_status,
                        "violations": list(aud.violations)})
        self.assertEqual(mon.demotion(), "relay_unreliable")
        self.assertFalse(mon.is_demoted())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "state", "advisor-demoted.json")))

    def test_audit_relay_states(self):
        ok_final = self.relay() + '\nMR_RESULT {"advisor":{"consults":1,"verdict":"concern","summary":"s",' \
                                  '"dispositions":[{"risk":0,"action":"fixed","evidence":"x"}]}}'
        one = Stream(self.tmp).consult("2026-10-30T10:00:00Z").result_block("2026-10-30T10:00:05Z")
        path = one.result(ok_final, opus=OPUS).write("ok.jsonl")
        aud = advisor.audit(path, _dec(), None, self.tree)
        self.assertEqual((aud.relay_status, aud.violations), ("ok", ()))
        self.assertEqual(aud.verdict["risks"][0]["severity"], "med")
        unaddressed = self.relay() + '\nMR_RESULT {"advisor":{"consults":1,"verdict":"concern","summary":"s"}}'
        self.assertEqual(advisor.audit(path, _dec(), unaddressed, self.tree).violations, ("unaddressed",))
        self.assertEqual(advisor.audit(path, _dec(), "x\nMR_ADVISOR {bad", self.tree).relay_status, "invalid")
        none = Stream(self.tmp).msg("2026-10-30T10:00:00Z", {"type": "text", "text": "hi"})
        fab = advisor.audit(none.result(self.relay()).write("fab.jsonl"), _dec(), None, self.tree)
        self.assertEqual((fab.consults, fab.relay_status, fab.violations), (0, "fabricated", ("fabricated",)))
        self.assertIn("consult_not_made", fab.events)
        two = (Stream(self.tmp).consult("2026-10-30T10:00:00Z", "a").result_block("2026-10-30T10:00:05Z", "a")
               .consult("2026-10-30T10:01:00Z", "b").result_block("2026-10-30T10:01:05Z", "b")
               .result("x", opus={"claude-opus-5-5": {"inputTokens": 180001, "outputTokens": 1, "costUSD": 1}}))
        over = advisor.audit(two.write("two.jsonl"), _dec(), None, self.tree)
        self.assertEqual(over.violations, ("over_cap", "over_transcript"))
        self.assertEqual(advisor.audit(two.write("two.jsonl"), _dec(("early", "final"), "Y1"), None,
                                       self.tree).violations, ("over_transcript",))

    def test_usage_fallback_and_drift(self):
        its = [{"type": "message"}, {"type": "advisor_message", "input_tokens": 30000, "output_tokens": 1000,
                                     "model": "claude-opus-5-5"}]
        s = Stream(self.tmp).consult("2026-10-30T10:00:00Z").result_block("2026-10-30T10:00:05Z")
        aud = advisor.audit(s.result("x", iterations=its).write("it.jsonl"), _dec(), None, self.tree)
        self.assertEqual((aud.advisor_in_tok, aud.advisor_out_tok), (30000, 1000))
        self.assertAlmostEqual(aud.advisor_usd, 0.14, 6)
        self.assertFalse(aud.model_drift)
        s2 = Stream(self.tmp).consult("2026-10-30T10:00:00Z").result_block("2026-10-30T10:00:05Z")
        drift = advisor.audit(s2.result("x", opus={"claude-opus-5-6": {"inputTokens": 1, "outputTokens": 2,
                                                                       "costUSD": 0.5}}).write("d.jsonl"),
                              _dec(), None, self.tree)
        self.assertEqual((drift.model_drift, drift.advisor_usd, drift.sonnet_usd), (True, 0.5, 0.13))

    def test_audit_trial_streams(self):  # AC-ADV-8
        a = advisor.audit(str(FIX / "A.jsonl"), _dec(), None, None)
        self.assertEqual(a.consults, 0)
        self.assertIsNone(a.advisor_in_tok)
        self.assertIn("consult_not_made", a.events)
        c = advisor.audit(str(FIX / "C.jsonl"), _dec(), None, None)
        self.assertEqual((c.consults, c.consult_ok, c.consult_err), (1, 1, 0))
        self.assertEqual(c.advisor_in_tok, 33660)
        self.assertEqual(c.advisor_out_tok, 1870)
        self.assertAlmostEqual(c.advisor_usd, 0.17204, delta=1e-6)
        self.assertAlmostEqual(c.sonnet_usd, 0.1317453, delta=1e-6)
        self.assertFalse(c.model_drift)
        self.assertEqual(c.relay_status, "missing")  # trial lesson: 0/10 relays
        self.assertAlmostEqual(c.consult_latency_s, 22.118, delta=1e-3)
        self.assertEqual(c.violations, ())
        self.assertEqual(c.events, ())


class TestFallbacks(TreeCase):  # AC-ADV-7
    def test_fallbacks(self):
        err = (Stream(self.tmp).consult("2026-10-30T10:00:00Z")
               .result_block("2026-10-30T10:00:03Z", error="overloaded").result("x").write("e.jsonl"))
        aud = advisor.audit(err, _dec(), None, None)
        self.assertEqual((aud.consult_ok, aud.consult_err, aud.events), (0, 1, ("consult_failed:overloaded",)))
        self.assertEqual(advisor.fallback_action(aud), "log_only")  # no retry, no re-run
        slow = (Stream(self.tmp).consult("2026-10-30T10:00:00Z").result_block("2026-10-30T10:02:30Z")
                .result("x").write("slow.jsonl"))
        aud = advisor.audit(slow, _dec(), None, None)
        self.assertEqual(aud.events, ("advisor_slow",))
        self.assertEqual(advisor.fallback_action(aud), "log_only")
        fast = (Stream(self.tmp).consult("2026-10-30T10:00:00Z").result_block("2026-10-30T10:02:29Z")
                .result("x").write("fast.jsonl"))
        self.assertEqual(advisor.audit(fast, _dec(), None, None).events, ())
        stall = (Stream(self.tmp).consult("2026-10-30T10:00:00Z")
                 .msg("2026-10-30T10:05:00Z", {"type": "text", "text": "..."}).write("stall.jsonl"))
        aud = advisor.audit(stall, _dec(), None, None)
        self.assertEqual(aud.events, ("advisor_stall",))
        self.assertEqual(advisor.fallback_action(aud), "rerun_without_advisor")
        self.assertEqual(advisor.fallback_action(aud, reruns_used=1), "log_only")
        self.assertEqual(advisor.fallback_action({"events": list(aud.events), "reruns_used": 1}), "log_only")
        launch = {"rc": 1, "turns": 0, "stderr": "Error: advisor model not allowed for this pairing"}
        self.assertEqual(advisor.fallback_action(launch), "open_breaker_and_rerun")
        self.assertEqual(advisor.fallback_action(launch | {"reruns_used": 1}), "log_only")
        self.assertEqual(advisor.fallback_action({"rc": 1, "turns": 0, "stderr": "boom"}), "none")
        self.assertEqual(advisor.fallback_action({"rc": 1, "turns": 3, "stderr": "advisor"}), "none")
        self.assertEqual(advisor.fallback_action(advisor.audit(str(FIX / "A.jsonl"), _dec(), None, None)),
                         "log_only")  # consult_not_made
        # at most one re-run per task, whatever the mix of failures
        used = 0
        for case in (aud, launch, aud, launch):
            if advisor.fallback_action(case, reruns_used=used) != "log_only":
                used += 1
        self.assertEqual(used, 1)


class TestMonitor(TreeCase):
    def mon(self, now=1_000_000.0):
        self.clock = [now]
        return advisor.Monitor(self.tmp, now=lambda: self.clock[0])

    def demoted_file(self):
        return os.path.join(self.tmp, "state", "advisor-demoted.json")

    def test_harm_rate(self):
        mon = self.mon()
        for i in range(9):
            mon.record({"kind": "pair", "harm": i == 8, "row": "Y3"})
        self.assertIsNone(mon.demotion())  # < 10 pairs
        self.assertFalse(os.path.exists(self.demoted_file()))
        for _ in range(11):
            mon.record({"kind": "pair", "harm": False, "row": "Y3"})
        self.assertIsNone(mon.demotion())  # 1/20 = 5% is not > 5%
        mon.record({"kind": "pair", "harm": True, "row": "Y2"})
        self.assertEqual(mon.demotion(), "harm_rate")  # last 20 overall: 2/20 = 10%
        self.assertTrue(mon.is_demoted())

    def test_harm_high_risk_row_only(self):
        mon = self.mon()
        mon.record({"kind": "pair", "harm": True, "row": "Y1", "high_risk": True})
        self.assertEqual(mon.demotion(), "harm_high_risk:Y1")
        self.assertTrue(mon.is_demoted("Y1"))
        self.assertFalse(mon.is_demoted("Y2"))
        self.assertFalse(mon.is_demoted())

    def test_immediate(self):
        for viol in ("over_cap", "fabricated"):
            with self.subTest(viol=viol):
                self.tearDown()
                self.setUp()
                mon = self.mon()
                mon.record({"kind": "consult", "usd": 0.2, "relay_status": "ok", "violations": [viol]})
                self.assertEqual(mon.demotion(), viol)
                self.assertTrue(mon.is_demoted())
                self.assertEqual(mon.breaker_open(), viol == "over_cap")
        self.tearDown()
        self.setUp()
        mon = self.mon()
        for _ in range(2):
            mon.record({"kind": "consult", "consult_failed": 1, "relay_status": "ok"})
        self.clock[0] += 25 * 3600
        mon.record({"kind": "consult", "consult_failed": 1, "relay_status": "ok"})
        self.assertIsNone(mon.demotion())  # only 1 within 24 h
        mon.record({"kind": "consult", "consult_failed": 2, "relay_status": "ok"})
        self.assertEqual(mon.demotion(), "consult_failed_3x_24h")

    def test_usd_p90(self):
        mon = self.mon()
        for usd in [0.3] * 8 + [0.85]:
            mon.record({"kind": "consult", "usd": usd, "relay_status": "ok"})
        self.assertIsNone(mon.demotion())  # < 10 runs
        mon.record({"kind": "consult", "usd": 0.79, "relay_status": "ok"})
        self.assertIsNone(mon.demotion())  # p90 of 10 = 9th value = 0.79
        mon.record({"kind": "consult", "usd": 0.9, "relay_status": "ok"})
        self.assertEqual(mon.demotion(), "usd_p90")
        data = json.loads(Path(self.demoted_file()).read_text())
        self.assertEqual(data["reason"], "usd_p90")
        self.assertTrue(data["at"].endswith("+05:30"))

    def test_breaker(self):
        mon = self.mon()
        self.assertFalse(mon.breaker_open())
        mon.open_breaker("launch_error")
        self.assertTrue(mon.breaker_open())
        self.clock[0] += 86400
        self.assertFalse(mon.breaker_open())

    def test_routes_never_written(self):
        mon = self.mon()
        mon.record({"kind": "consult", "violations": ["fabricated"]})
        mon.demotion()
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, "state"))),
                         ["advisor-demoted.json", "advisor-monitor.jsonl"])


class TestRunnersAdvisor(unittest.TestCase):
    def test_advisor_argv_golden(self):
        cfg = config.load()
        tail = runners.claude_argv_tail(cfg, "S+A", "do it", "acceptEdits", None)
        golden = [
            "-p", "do it", "--output-format", "stream-json", "--verbose", "--model", "claude-sonnet-5-5",
            "--effort", "xhigh", "--advisor", "claude-opus-5-5", "--append-system-prompt", advisor.system_prompt(),
            "--permission-mode", "acceptEdits", "--allowedTools", runners.ALLOWED_TOOLS,
            "--disallowedTools", runners.DISALLOWED_TOOLS, "--no-session-persistence",
        ]
        self.assertEqual(tail, golden)
        plain = runners.claude_argv_tail(cfg, "S+", "do it", "acceptEdits", None)
        self.assertEqual(plain[2:4], ["--output-format", "json"])
        for flag in ("--advisor", "--verbose", "--append-system-prompt"):
            self.assertNotIn(flag, plain)

    def test_advisor_env(self):  # AC-ADV-4
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1", "DISABLE_TELEMETRY": "1"}):
            adv = runners.child_env(True, advisor=True)
            plain = runners.child_env(True)
            cursor = runners.child_env(False)
        self.assertNotIn("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", adv)
        self.assertNotIn("DISABLE_TELEMETRY", adv)
        self.assertEqual(plain["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"], "1")
        self.assertEqual(cursor["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"], "1")  # inherited, not added
        self.assertEqual(adv["GIT_CONFIG_COUNT"], "3")

    def test_run_claude_uses_advisor_env(self):
        cfg = config.load()
        seen = {}

        def fake_exec(argv, cwd, env, timeout_s, raw_base):
            seen["env"] = env
            return 0, (FIX / "C.jsonl").read_text(), "", 1.0, False

        def fake_guarded(rung, tail, cfg, *, task, exec_fn, wait_lock_s=None):
            res = exec_fn(tail)
            return mock.Mock(result=res)

        with mock.patch.object(runners, "exec_child", fake_exec), \
                mock.patch.object(runners.guard, "run_claude_guarded", fake_guarded):
            _, res = runners.run_claude("S+A", "p", "/tmp", "acceptEdits", None, 900, cfg=cfg, task="t", attempt=1)
            self.assertNotIn("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", seen["env"])
            runners.run_claude("S+", "p", "/tmp", "acceptEdits", None, 900, cfg=cfg, task="t", attempt=1)
            self.assertEqual(seen["env"]["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"], "1")
        # stream-json result event -> same RunResult fields as json output
        self.assertTrue(res.has_envelope)
        self.assertFalse(res.is_error)
        self.assertAlmostEqual(res.usd_equiv, 0.3037853, 6)
        self.assertEqual(res.model_used, "claude-sonnet-5-5,claude-opus-5-5")
        self.assertFalse(res.model_drift)
        self.assertTrue(res.result_text.startswith(json.loads(
            (FIX / "C.jsonl").read_text().splitlines()[-1])["result"][:20]))
        self.assertIsInstance(res.turns, int)


class TestConfigAdvisor(unittest.TestCase):
    def test_defaults_block(self):
        cfg = config.load()
        self.assertEqual(cfg.rungs["S+A"].advisor, "claude-opus-5-5")
        self.assertIsNone(cfg.rungs["S+"].advisor)
        self.assertEqual(cfg.guard.est_seed["S+A"], 12)
        self.assertEqual(cfg.advisor["mode"], "on")  # YAML `on` canonicalised
        self.assertEqual(cfg.advisor["transcript_cap_tokens"], {"early": 150000, "final": 150000,
                                                                "audit_max": 180000})
        self.assertEqual(cfg.advisor["monitor"]["usd_p90_max"], 0.80)
        raw = copy.deepcopy(defaults())
        raw.pop("advisor")
        absent = config.from_dict(raw).advisor
        self.assertEqual(absent, config.ADVISOR_DEFAULTS)
        self.assertEqual((absent["reserve_pts"], absent["per_hour_box"], absent["per_day_box"]), (12, 3, 10))
        part = config.load(overrides={"advisor": {"mode": "shadow", "transcript_cap_tokens": {"early": 100000}}})
        self.assertEqual(part.advisor["mode"], "shadow")
        self.assertEqual(part.advisor["transcript_cap_tokens"]["final"], 150000)

    def test_advisor_validation(self):
        bad = [
            {"advisor": {"mode": "maybe"}},
            {"advisor": {"rows_off": ["N1"]}},
            {"advisor": {"injection": "adv.v1"}},
            {"advisor": {"max_consults": 3}},
            {"advisor": {"per_hour_box": -1}},
            {"advisor": {"transcript_cap_tokens": {"audit_max": 140000}}},
            {"advisor": {"timeouts_s": {"slow": 400}}},
            {"advisor": {"monitor": {"usd_p90_max": "x"}}},
            {"advisor": {"bogus": 1}},
            {"advisor": [1]},
            {"rungs": {"S+A": {"advisor": "claude-sonnet-5-5"}}},
            {"rungs": {"O": {"advisor": "claude-opus-5-5"}}},
            {"rungs": {"G-h": {"advisor": "claude-opus-5-5"}}},
        ]
        for over in bad:
            with self.subTest(over=over):
                with self.assertRaises(config.ConfigError):
                    config.load(overrides=over)
        with self.assertRaisesRegex(config.ConfigError, r"rungs\.O\.advisor"):
            config.load(overrides={"rungs": {"O": {"advisor": "claude-opus-5-5"}}})


if __name__ == "__main__":
    unittest.main()
