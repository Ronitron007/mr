"""Escalation loop: pure next_step, counters, run_task (mr-spec §10.4, §10.6; p0 README §4)."""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from types import SimpleNamespace
from typing import NamedTuple

from mr import (acceptance, advisor, checkjudge, classify, droid, exitcodes, guard, hygiene, isolation, patches,
                pipeline, planner, review, router, runners, scope, state)
from mr.log import IST, now_ist, read as _read_runs

CAPS = {"trivial": 3, "easy": 3, "medium": 4, "hard": 4, "critical": 4}
CLAUDE = frozenset({"S", "S+", "O", "O+"})
OPUS = frozenset({"O", "O+"})
UP = {"G-l": "G-m", "G-m": "G-h", "G-h": "G-x", "K-h": "K-x", "S": "S+", "O": "O+"}
_BASE = {"G": "grok-4.7", "C25": "composer-2.5", "K": "kimi-k3",
         "S": "claude-sonnet-5-5", "S+": "claude-sonnet-5-5",
         "O": "claude-opus-5-5", "O+": "claude-opus-5-5"}
CLAUDE_CAP, OPUS_CAP = 2, 1
_RESET = frozenset({"off_track", "safety_violation", "scope_rejected"})  # the tree goes back before the next rung


def base(rung: str) -> str:
    return _BASE.get(rung) or _BASE[rung.split("-")[0]]


@dataclass(frozen=True)
class LoopState:
    ladder: tuple[str, ...]
    difficulty: str
    used_rungs: tuple[str, ...]
    current_rung: str
    cls: str
    attempts: int
    claude_rungs: int
    opus_rungs: int
    infra_retries_this_rung: int
    effort_up_used: tuple[str, ...]
    guard_allows: bool
    replans: int


@dataclass(frozen=True)
class Step:
    action: str  # "rung" | "replan" | "exit"
    rung: str | None = None
    exit: int | None = None
    reset: bool = False
    effort_up: bool = False
    reason: str = ""


def _exit(code: int, reason: str) -> Step:
    return Step("exit", exit=code, reason=reason)


def next_step(s: LoopState, *, caps=None, fallback=("G-x",)) -> Step:
    """README §4 steps 1-7; counters are taken after the failed current rung."""
    if any(r in CLAUDE for r in fallback):
        raise ValueError(f"Claude-denied fallback must be non-Anthropic: {fallback}")  # §0.3
    cap = (caps or CAPS)[s.difficulty]

    def ok(r: str) -> bool:
        return (r not in s.used_rungs
                and (r not in CLAUDE or s.claude_rungs < CLAUDE_CAP)
                and (r not in OPUS or s.opus_rungs < OPUS_CAP))

    def fb(why: str) -> Step:
        for r in fallback:
            if ok(r) and s.attempts < cap:
                return Step("rung", rung=r, reason=f"{why}: fallback {r}")
        return _exit(exitcodes.DEFERRED, f"{why}: fallback chain empty")

    def launch(r: str, why: str, **kw) -> Step:
        if r in CLAUDE and not s.guard_allows:
            return fb(f"{why}; guard denies {r}")
        return Step("rung", rung=r, reason=why, **kw)

    cur, cls = s.current_rung, s.cls
    if cls == "infra":
        if s.infra_retries_this_rung == 0:
            return launch(cur, "infra retry")
        return fb("second infra") if cur in CLAUDE else _exit(exitcodes.NOT_ACCEPTED, "second infra")
    if cls == "guard_deny":
        return fb("guard_deny")
    if cls == "spec_suspect":
        if s.replans == 0 and s.attempts < cap:
            return Step("replan", reset=True, reason="spec_suspect: re-plan")
        return _exit(exitcodes.HUMAN, "spec_suspect after re-plan")
    if s.attempts >= cap:
        return _exit(exitcodes.NOT_ACCEPTED, f"attempts cap {cap}")
    if cls == "scope_repair":  # same rung, kept tree; the used-rung rule does not apply
        return launch(cur, "scope repair: same rung")
    reset = cls in _RESET
    if cls == "skipped":
        u = UP.get(cur)
        if u and base(cur) not in s.effort_up_used and ok(u):
            st = launch(u, f"effort-up {cur} -> {u}")
            return Step(st.action, st.rung, st.exit, False, st.rung == u, st.reason)
    elif cls != "tried_wrong" and cls not in _RESET:
        raise ValueError(f"unknown cls: {cls!r}")
    for r in s.ladder:
        if ok(r):
            st = launch(r, f"{cls} -> {r}")
            return Step(st.action, st.rung, st.exit, reset and st.action == "rung", False, st.reason)
    return _exit(exitcodes.NOT_ACCEPTED, "ladder exhausted")


def count_after(counters: dict, rung: str, *, kind: str) -> dict:
    """§10.6: attempts = implementer rungs incl. effort-up/fallback; Reviewer calls (guarded, but uncounted) do not count toward claude_rungs."""
    out = {k: counters.get(k, 0) for k in ("attempts", "claude_rungs", "opus_rungs")}
    if kind in ("infra_retry", "planner", "reviewer"):
        return out
    if kind not in ("implementer", "effort_up", "fallback", "scope_repair"):
        raise ValueError(f"unknown kind: {kind!r}")
    out["attempts"] += 1
    if kind == "scope_repair":  # the same rung again: an attempt, not a new Claude rung
        return out
    out["claude_rungs"] += rung in CLAUDE
    out["opus_rungs"] += rung in OPUS
    return out


def reviewer_for_grok(cfg) -> str:
    """Signal C reviewer for a Grok-authored change: S by default (review.map.grok)."""
    return review.reviewer_rung("grok", "normal", cfg)


# --- run_task: the full `mr run` cascade (mr-spec §10.2, §10.3, §10.6) -------------------------

_PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "implementer.v2.txt")
_DIFFS = list(CAPS)  # trivial .. critical
_HANDOFF = ("Previous rung: {rung}, class {cls}. The tree {tree}.\n"
            "Failing acceptance tests: {a}\nNew hygiene failures: {h}\nScope problems: {d}\n{j}"
            "Reviewer findings: {c}\nPrevious patch (relative to the start of the task): {patch}\n")
_MAX_FAILING_A, _MAX_NEW_H = 3, 10


def _num(val) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _first_sentence(text: str) -> str:
    line = re.split(r"(?<=[.!?])\s+|\n", (text or "").strip(), maxsplit=1)[0]
    return line[:200] or "the change"


def _scope_paths(c) -> list[str]:
    """The decision files plus the checklist scope paths, de-duplicated (advisor sf_tags / high_risk_paths)."""
    paths = router.decision_files(c.d)
    if c.checklist:
        sc = c.checklist["scope"]
        paths += [p for p in sc["modify"] + sc["create"] if p not in paths]
    return paths


def _adv_counts(home: str) -> tuple[int, int]:
    """role:"advisor" records in the last 60 min and today (IST) (advisor-consult-spec §4 G4)."""
    now = time.time()
    today = datetime.fromtimestamp(now, IST).date()
    hour = day = 0
    for rec in _read_runs(home, role="advisor"):
        try:
            ts = datetime.fromisoformat(str(rec.get("ts"))).timestamp()
        except (TypeError, ValueError):
            continue
        if now - ts <= 3600:
            hour += 1
        if datetime.fromtimestamp(ts, IST).date() == today:
            day += 1
    return hour, day


def advisor_rung(c, rung: str) -> str:
    """T20 (advisor-consult-spec §3, §4): decide whether rung S+ launches as S+A this attempt.

    Stores the AdvisorDecision on ``c.adv_dec`` and returns the rung to run ("S+A" or "S+").
    A ``run_rung`` of None means even plain S+ does not fit the guard: return "S+" and let the
    normal guard-deny path handle it.
    """
    if rung != "S+":
        return rung
    a, cfg, task, d = c.args, c.cfg, c.task, c.d
    cfg_adv = cfg.advisor or {}
    mode = advisor.canon_mode(cfg_adv.get("mode", "on"))
    flag = getattr(a, "advisor", None) or "auto"
    budget = getattr(a, "budget", None) or "normal"
    paths = _scope_paths(c)
    sf = advisor.sf_tags(task.text, paths)
    # Cheap rows (N0-N5) never consult, so the guard pre-check is only needed past them.
    cheap_no = (mode == "off" or flag == "off" or budget in ("free", "low") or d.route == "design"
                or d.type in ("docs", "explain", "test", "review") or d.difficulty in ("trivial", "easy"))
    if cheap_no:
        guard_in = {"hour": 0, "reserved": 0, "five_hour_pct": 0}
    else:
        g = guard.check("S+A", cfg)  # pre-check only, no reservation; fail safe on guard errors
        lim = cfg.claude_limits
        guard_in = {"hour": g.hour if _num(g.hour) else lim.hour_max,  # None when no hourly cap
                    "reserved": g.reserved,
                    "five_hour_pct": g.five_hour if _num(g.five_hour) else lim.five_hour_max,
                    "hour_max": lim.hour_max,
                    "five_hour_max": lim.five_hour_max}
    scope_bytes = 0
    for p in paths[:20]:
        with contextlib.suppress(OSError):
            scope_bytes += os.path.getsize(os.path.join(task.repo, p))
    task_tok = max(1, len(task.text) // 4)
    handoff_tok = 375 if c.rungs else 0  # the §10.2 handoff is capped at 1.5k tokens
    est = {slot: advisor.est_consult(task_tok, handoff_tok, scope_bytes, 0, slot)[0]
           for slot in ("early", "final")}
    mon = advisor.Monitor(cfg.paths.home)
    rows_off = list(cfg_adv.get("rows_off") or [])
    with contextlib.suppress(OSError, ValueError):  # a row-scoped demotion acts like rows_off
        with open(mon.demoted_path, encoding="utf-8") as fh:
            rows_off += [r for r in (json.load(fh).get("rows") or []) if r not in rows_off]
    adv_hour, adv_day = _adv_counts(cfg.paths.home)
    env = runners.child_env(True, advisor=True)  # G6 is the S+A child env, not the mr process
    inp = {
        "mode": mode, "flag": flag,
        "consults_flag": int(getattr(a, "advisor_consults", None) or 1),
        "slot_flag": getattr(a, "advisor_slot", None),
        "budget": budget, "rung": "S+", "route": d.route, "item_kind": None, "type": d.type,
        "difficulty": d.difficulty, "risk": d.risk,
        "ambiguity": getattr(d, "ambiguity", "low"),
        "load_bearing": bool(getattr(d, "load_bearing", False)),
        "sf_tags": sorted(sf),
        "hist": [{"model": r.get("model"), "cls": r.get("class"),
                  "failing": list(r.get("failing") or [])} for r in c.rungs],
        "guard": guard_in, "adv_hour": adv_hour, "adv_day": adv_day,
        "est_consult_tokens": est, "rows_off": rows_off,
        "env_ok": ("DISABLE_TELEMETRY" not in env
                   and "CLAUDE_CODE_DISABLE_ADVISOR_TOOL" not in env),
        "demoted": mon.is_demoted(), "breaker": mon.breaker_open(),
        "sa_used_in_task": any(r.get("rung") == "S+A" and r.get("class") != "guard_deny"
                               for r in c.rungs),
    }
    dec = advisor.decide(inp, cfg_adv)
    c.adv_dec, c.adv_sf = dec, sf
    if not dec.consult and dec.row[:1] in ("F", "Y"):  # §7: the table said yes; a §4 gate or the mode refused
        c.log.append({"role": "advisor", "task": task.id, "attempt": c.seq + 1, "mode": mode,
                      "row": dec.row, "slots": list(dec.slots), "gate": dec.gate,
                      "consult_requested": True, "consult_made": 0,
                      "est_consult_tokens": dec.est_consult_tokens,
                      "est_consult_usd": dec.est_consult_usd,
                      "reasons": list(dec.reasons), "relay_status": None, "violations": []})
    return dec.run_rung or "S+"


def _adv_review_reqs(verdict: dict) -> list[dict]:
    """§6.3: relayed risks become explicit C review items."""
    return [{"id": f"ADV{i + 1}",
             "text": (f"advisor risk ({r.get('severity', '?')}): {r.get('what', '?')} "
                      f"at {r.get('where', '?')}")}
            for i, r in enumerate(verdict.get("risks") or [])]


def _advisor_audit(c, rr, attempt: int, repo: str):
    """§8 audit, the §9.1 record and §9.4 monitoring after an S+A rung. Returns (audit, §7 action)."""
    dec = getattr(c, "adv_dec", None)
    aud = advisor.audit(rr.raw_path + ".out", dec, rr.result_text, repo)
    verdict = aud.verdict or {}
    c.log.append({
        "role": "advisor", "task": c.task.id, "attempt": attempt, "rung_ref": f"r{attempt}",
        "mode": advisor.canon_mode((c.cfg.advisor or {}).get("mode", "on")),
        "row": dec.row if dec else None, "consult_requested": True,
        "slots": list(dec.slots) if dec else [], "gate": dec.gate if dec else None,
        "consult_made": aud.consults,
        "est_consult_tokens": dec.est_consult_tokens if dec else None,
        "advisor_in_tok": aud.advisor_in_tok, "advisor_out_tok": aud.advisor_out_tok,
        "advisor_usd": aud.advisor_usd, "sonnet_usd": aud.sonnet_usd,
        "relay_status": aud.relay_status, "violations": list(aud.violations),
        "verdict": verdict.get("verdict"),
        "risks": [{"where": r.get("where"), "severity": r.get("severity")}
                  for r in verdict.get("risks") or []],
        "events": list(aud.events)})
    mon = advisor.Monitor(c.cfg.paths.home)
    mon.record({"kind": "consult", "usd": aud.advisor_usd, "relay_status": aud.relay_status,
                "violations": list(aud.violations), "consult_failed": aud.consult_err})
    mon.demotion()  # §9.4: persist an immediate demotion trigger, if any
    if mon.relay_unreliable():
        print("advisor: relay_unreliable (relay ok < 50% over the last 10 consults); "
              "the advisor stays on", file=sys.stderr)
    launch = {"rc": rr.rc, "turns": rr.turns, "stderr": rr.stderr_tail,
              "reruns_used": getattr(c, "adv_reruns", 0)}
    if advisor.is_launch_error(launch):
        return aud, advisor.fallback_action(launch)
    return aud, advisor.fallback_action(aud, reruns_used=getattr(c, "adv_reruns", 0))


def _adv_verdict(c, rr, aud, green: bool, cls: str | None) -> dict:
    """§6.3: what mr does with the advisor outcome. A block never schedules an O rung by itself."""
    mrr = rr.mr_result if isinstance(rr.mr_result, dict) else {}
    pb = mrr.get("pushback")
    if mrr.get("status") == "pushback" and isinstance(pb, dict) and pb.get("type") == "advisor_block":
        risk = ""
        if aud is not None and aud.verdict:
            risks = aud.verdict.get("risks") or []
            risk = str((risks[0] or {}).get("what") or "") if risks else ""
        ev = (str(pb.get("evidence") or "") or risk or "no risk relayed")[:300]
        if c.checklist and c.replans == 0:
            return {"cls": "spec_suspect",
                    "why": f"advisor block pushback ({ev}); to the spec author for one re-plan"}
        return {"stop": exitcodes.HUMAN,
                "why": f"advisor block pushback ({ev}); the re-plan is used or there is no spec"}
    verdict = (aud.verdict or {}) if aud is not None and aud.relay_status == "ok" else {}
    if verdict.get("verdict") == "block" and green and cls is None and (
            c.d.risk == "high" or c.d.difficulty == "critical"):
        return {"stop": exitcodes.HUMAN,
                "why": "advisor verdict block with passing checks on a high-risk/critical task; "
                       "C reviewed the relayed risks, but a human must confirm; the patch is saved"}
    return {}


def _row_guarded(cfg, code: str, spend):
    """Last executed Claude call for a planner/reviewer row. Cursor rows stay null."""
    if not spend or cfg.rungs[code].path == "cursor":
        return None
    return spend[-1][1]


def _light(c, role: str, code: str, started: str, t0: float, signals: dict, cls: str | None,
           item: str | None = None, ok: bool = True, guarded=None) -> dict:
    """Attempt record for a planner or reviewer call. One row per invocation."""
    rung = c.cfg.rungs[code]
    claude_pts, pts_pre, pts_post = pipeline.call_points(None if rung.path == "cursor" else guarded)
    return {"role": role, "task": c.task.id, "route": c.d.route, "rung": code, "model": rung.model,
            "model_served": None, "path": rung.path, "attempt": c.seq, "item": item,
            "started_at": started, "wall_s": round(time.time() - t0, 2), "rc": 0, "is_error": not ok,
            "usd_equiv": None, "claude_pts": claude_pts, "pts_pre": pts_pre, "pts_post": pts_post,
            "diff": None, "mr_result": None, "signals": signals, "decision": "accept" if ok else "stop",
            "failure_class": cls, "isolation": c.iso, "patch": None, "raw": None}


_STRONGER = {"G-h": "S", "S": "O", "S+": "O", "O": "O+", "O+": "O+"}
PLAN_REJECTED = "plan rejected twice; running without a plan"
NO_TEST = "(no locked test covers this; check the patch against it directly) "


class Found(NamedTuple):
    """What the plan judge decided. kind: let_through | repaired (res is a valid plan), again (res is the
    mechanically repaired but still invalid plan), instruct (planner reruns with the instructions), replan,
    defer (the judge could not run; the task waits)."""
    kind: str
    res: object
    ref: str | None = None
    reason: str | None = None
    instructions: tuple = ()


def _hint(got) -> str:
    """When the budget frees, from a guard-denied judge call; else the usual wait."""
    guard_ = getattr(getattr(got, "guarded", None), "guard", None)
    return (got.status == "guard_deny" and getattr(guard_, "frees_hint", None)) or "≈ in 60 min"


def _decide(c, rung: str, prompt: str):
    """Up to two judge calls (the first invalid reply is retried). Returns (Verdict | None, status, calls made)."""
    verdict, status, made = None, "invalid", 0
    for _ in range(2):
        c.seq += 1
        got = checkjudge.call(c.cfg, rung, prompt, tree=c.task.repo, task_id=c.task.id, attempt=c.seq)
        if got.result is not None:
            made += 1
            pipeline.add_cost(c.cost, got.result, got.guarded)
        status, c.judge_hint = got.status, _hint(got)
        if status != "ok":
            break
        verdict = checkjudge.parse(got.text)
        status = "ok" if verdict else "invalid"
        if verdict:
            break
    c.judge_status = status
    return verdict, status, made


def _log_decision(c, ref: str, check: str, findings, rung: str, verdict, status: str, made: int,
                  deferred: bool = False) -> None:
    pts = c.cfg.guard.est_seed.get(rung, 0.0) * made if made else None
    c.log.append(checkjudge.record(
        task=c.task.id, ref=ref, check=check, findings=findings,
        verdict=verdict.verdict if verdict else None, reason=verdict.reason if verdict else None,
        instructions=verdict.instructions if verdict else [], rung=rung,
        judge_model=c.cfg.rungs[rung].model, pts_est=pts, deferred=deferred, status=status))


def _ask_plan_judge(c, res):
    """One decision: up to two calls (the first invalid reply is retried). Logs the check_judge row.

    Returns (Verdict | None, ref). A judge failure is a row with a null verdict (deferred when check_judge.defer
    is on)."""
    cfg, task = c.cfg, c.task
    rung = checkjudge.plan_rung(cfg, planner=res.rung, difficulty=c.d.difficulty,
                                risk="high" if getattr(c, "high_risk", False) else c.d.risk)
    state_ = checkjudge.build_state("plan", task.text, res.doc, res.errors,
                                    signals={"repairs": res.repairs, "repo": planner._excerpt(task.repo)})
    c.cj_calls = getattr(c, "cj_calls", 0) + 1
    ref = f"cj-{task.id}-{c.cj_calls}"
    verdict, status, made = _decide(c, rung, checkjudge.render(state_))
    _log_decision(c, ref, "plan", res.errors, rung, verdict, status, made,
                  deferred=verdict is None and _defer_on(c, "plan"))
    return verdict, ref


def _settled(c, res, doc, kind: str, *, allow=frozenset(), delete=()):
    """Run a judge-edited document like a planner attempt: a valid PlanResult (status ``kind``) or the invalid one."""
    task = c.task
    doc, repairs, errors = planner.check_doc(doc, cfg=c.cfg, tree=task.repo, task_id=task.id,
                                             allow_uncovered=allow, delete=delete)
    if errors:
        return res._replace(errors=errors, doc=doc, repairs=list(res.repairs) + repairs, attempts=0)
    return res._replace(ok=True, checklist=doc, reset_tree=patches.take_reset_tree(task.repo), errors=[], attempts=0,
                        status=kind, doc=doc, repairs=list(res.repairs) + repairs)


def _plan_findings(c, res) -> Found:
    """A plan still invalid after two attempts goes to the plan judge (at most check_judge.max_plan_calls per task)."""
    block = getattr(c.cfg, "check_judge", None)
    if block is None or res.doc is None or getattr(c, "cj_calls", 0) >= block["max_plan_calls"]:
        return Found("replan", res)
    verdict, ref = _ask_plan_judge(c, res)
    if verdict is None:
        return Found("defer" if _defer_on(c, "plan") else "replan", res, ref)
    c.plan_judge_reason = verdict.reason
    lost = Found("replan", res, ref, verdict.reason)
    if verdict.verdict == "reject":
        return lost
    if verdict.verdict == "accept":
        doc, allow, gone = planner.relax(res.doc, res.errors)
        if not any(item.get("tests") for item in doc.get("acceptance") or ()):
            return lost  # nothing left to lock: let the stronger planner write tests
        got = _settled(c, res, doc, "let_through", allow=allow, delete=gone)
        if not got.ok:
            return lost
        c.uncovered_reqs = tuple(sorted(allow))
        return Found("let_through", got, ref, verdict.reason)
    edits = [planner.parse_edit(text) for text in verdict.instructions]
    if not edits or None in edits:
        return Found("instruct", res, ref, verdict.reason, tuple(verdict.instructions or [verdict.reason]))
    got = _settled(c, res, planner.apply_edits(res.doc, edits), "repaired")
    return Found("repaired" if got.ok else "again", got, ref, verdict.reason)


def _plan_call(c, rung, prior_errors=None):
    """One planner.plan call (up to two attempts). Returns (result, planner row); the caller logs the row."""
    cfg, task = c.cfg, c.task
    c.seq += 1
    started, t0 = now_ist(), time.time()
    res = planner.plan(task, cfg=cfg, tree=task.repo, task_id=task.id, attempt=c.seq,
                       difficulty=c.d.difficulty, rung=rung, files=c.d.files, prior_errors=prior_errors)
    c.n_plan += res.attempts
    for rr, guarded in res.spend:
        pipeline.add_cost(c.cost, rr, guarded)
    old_way = (getattr(cfg, "plan", None) or {}).get("judge", "on") == "off"
    row = _light(c, "planner", res.rung, started, t0, {"fallback": list(res.notes)} if res.notes else {},
                 "invalid_plan" if old_way and not res.ok else None, ok=res.ok,
                 guarded=_row_guarded(cfg, res.rung, res.spend))
    row.update(status=res.status, repairs=res.repairs, retry_errors=res.retry_errors)
    return res, row


def _recover(c, res, row, stronger: bool = False):
    """Judge a plan that is still invalid; one stronger re-plan allowed. Logs every planner row after its decision.

    Returns the last PlanResult: valid, or invalid (plan-less run, or infra when the planner is unreachable).
    When the judge cannot run and check_judge.defer is on the task is parked (c.pending) and the row is kept in it."""
    while True:
        if res.ok or res.infra:
            c.log.append(row)
            if res.ok or stronger:
                return res
            found = Found("replan", res)  # nothing to judge: no document came back
        else:
            found = _plan_findings(c, res)
            if found.ref:
                row["plan_judge_ref"] = found.ref
        if found.kind in ("let_through", "repaired"):
            row.update(status=found.kind, is_error=False, decision="accept", repairs=found.res.repairs)
            c.log.append(row)
            return found.res
        if found.kind == "again":
            res = found.res
            continue
        if found.kind == "defer":
            return _defer_plan(c, res, row, stronger)
        if not (res.ok or res.infra):
            c.log.append(row)
        if found.kind == "instruct":
            res, row = _plan_call(c, res.rung, res.errors + list(found.instructions))
        elif stronger:
            return res
        else:
            stronger = True
            prior = None if res.infra else res.errors + ([f"plan judge: {found.reason}"] if found.reason else [])
            res, row = _plan_call(c, _STRONGER.get(res.rung, "S"), prior)


def _plan(c) -> int | None:
    """Planner (cfg.planner.rungs by difficulty): checklist, locked tests, red on base, new reset_tree.

    Not a ladder rung: a Claude planner call costs points but does not count to claude/opus rungs.
    With plan.judge off, exit 3 if invalid twice. With it on, a plan still invalid after two attempts goes to the
    plan judge (accept, repair or reject); a reject, a judge failure or an unfixed repair gets one re-plan on the
    stronger planner, then the task runs without a plan; an unreachable planner defers (75), and so does a plan
    judge that cannot run (check_judge.defer on), before any implementer rung.
    """
    c.uncovered_reqs = ()
    res, row = _plan_call(c, c.d.planner)
    return _plan_finish(c, res, row)


def _plan_finish(c, res, row, stronger: bool = False) -> int | None:
    """The plan judge, the stronger re-plan and the checklist state after the first planner call."""
    cfg, task = c.cfg, c.task
    if (getattr(cfg, "plan", None) or {}).get("judge", "on") == "on":
        res = _recover(c, res, row, stronger)
        if c.pending:
            return c.stop(exitcodes.DEFERRED, f"plan judge unavailable ({c.judge_status}); state is saved, "
                                              f"resume with: mr resume {task.id}")
        if res.infra:
            return c.stop(exitcodes.DEFERRED, f"planner unavailable ({'; '.join(res.errors[:2])}); "
                                              "state is saved, run it again later")
        if not res.ok:
            return _plan_less(c)
        if res.status == "let_through":
            c.d = replace(c.d, needs_review=True, required_review=router.at_least(c.d.required_review, "S+"))
    else:
        c.log.append(row)
    if not res.ok:
        raw = os.path.join(state.task_dir(cfg.paths.home, task.id), "planner-a*.txt")
        return c.stop(exitcodes.HUMAN, f"planner gave no valid checklist twice ({'; '.join(res.errors[:3])}); "
                                       f"raw outputs: {raw}")
    c.checklist, c.reset_tree = res.checklist, res.reset_tree
    c.d = replace(c.d, files=router.files_from_plan(res.checklist))
    c.locked = tuple(t["path"] for t in res.checklist["tests"])
    with open(os.path.join(state.task_dir(cfg.paths.home, task.id), "tests.lock"), encoding="utf-8") as fh:
        c.lockdoc = json.load(fh)
    c.prior = []  # the two-model rule counts only attempts after a (re-)plan
    return None


def _plan_less(c) -> None:
    """No usable plan: no checklist, the no-checklist D limits, and a required review of at least S+."""
    print(PLAN_REJECTED, file=sys.stderr)
    if getattr(c, "plan_judge_reason", None):
        print(f"plan judge: {c.plan_judge_reason}", file=sys.stderr)
    c.plan_rejected = True
    c.checklist, c.locked, c.lockdoc = None, (), None
    c.d = replace(c.d, needs_review=True, required_review=router.at_least(c.d.required_review, "S+"))
    c.prior = []


def _replan(c) -> int | None:
    """spec_suspect: reset to reset_tree, drop the old locked tests, plan again (one re-plan)."""
    task = c.task
    r = patches.reset(task.repo, c.reset_tree, c.patch_path, c.cfg.paths.home, task.id)
    if not r.ok:
        return c.stop(exitcodes.UNSAFE_GIT, f"re-plan reset refused: {r.reason}; saved {', '.join(r.saved)}")
    old = {}
    for rel in c.locked:
        with contextlib.suppress(OSError), open(os.path.join(task.repo, rel), encoding="utf-8") as fh:
            old[rel] = fh.read()
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(task.repo, rel))
    c.replans += 1
    code = _plan(c)
    if code is not None:  # put the old tests back so the tree still matches the old reset_tree
        for rel, text in old.items():
            with open(os.path.join(task.repo, rel), "w", encoding="utf-8") as fh:
                fh.write(text)
    return code


def _scope_on(c) -> bool:
    return (getattr(c.cfg, "check_judge", None) or {}).get("scope") == "on"


def _scope_plan(c, dres) -> dict:
    if c.checklist:
        sc = c.checklist["scope"]
        return {"requirements": c.checklist["requirements"],
                "scope": {"modify": list(sc["modify"]), "create": list(sc["create"])},
                "est_loc": c.checklist["est_loc"]}
    return {"named_files": list(router.scope_files(c.d)[0]), "est_loc": dres.est_loc}


def _scope_decision(c, code: str, dres, patch: bytes, sig: dict):
    """The scope judge on a flagged attempt whose H and A did not fail (one decision per attempt).

    Returns (scope_verdict, Verdict | None, ref): accept | repair | reject, or unavailable when the per-task cap is
    used up (no call, no row) or the judge cannot decide with check_judge.defer off; with it on a judge that cannot
    decide gives deferred. A second repair on the same rung counts as a reject."""
    cfg, task = c.cfg, c.task
    if getattr(c, "scope_calls", 0) >= cfg.check_judge["max_scope_calls"]:
        return "unavailable", None, None
    c.scope_calls = getattr(c, "scope_calls", 0) + 1
    ref = f"cjs-{task.id}-{c.scope_calls}"
    rung = checkjudge.scope_rung(cfg, implementer=code, difficulty=c.d.difficulty,
                                 risk="high" if getattr(c, "high_risk", False) else c.d.risk)
    state_ = checkjudge.build_state(
        "scope", task.text, _scope_plan(c, dres), dres.findings,
        signals={"H": "pass", "A": "pass" if sig["A"] else "not run"}, diff=patch.decode("utf-8", "replace"),
        attempt={"rung": code, "n": c.counters["attempts"]})
    verdict, status, made = _decide(c, rung, checkjudge.render(state_))
    defer = verdict is None and _defer_on(c, "scope")
    _log_decision(c, ref, "scope", dres.findings, rung, verdict, status, made, deferred=defer)
    if verdict is None:
        return "deferred" if defer else "unavailable", None, ref
    if verdict.verdict == "repair":
        repaired = getattr(c, "scope_repaired", ())
        if code in repaired:
            return "reject", verdict, ref
        c.scope_repaired = {*repaired, code}
    return verdict.verdict, verdict, ref


def _prompt(c, handoff: str) -> str:
    task, name = c.task, os.path.basename(c.task.repo)
    goal, cmds = task.text, list((c.cfg.hygiene.get("commands") or {}).get(name) or [])
    if c.checklist:
        sc = c.checklist["scope"]
        own = [p for p in sc["modify"] + sc["create"] if p not in c.locked]
        goal += "\n\nREQUIREMENTS\n" + json.dumps(c.checklist["requirements"], ensure_ascii=False)
        if acceptance.runner_for(c.cfg, name) == "unittest":
            cmds.append("python3 -m unittest " + " ".join(p[:-3].replace("/", ".") for p in c.locked))
        else:
            cmds.append("npx vitest run " + " ".join(c.locked))
    else:
        own = router.decision_files(c.d)
    with open(_PROMPT, encoding="utf-8") as fh:
        template = fh.read()
    return template.format(
        goal=goal, scope="\n".join(f"- {p}" for p in own) or pipeline.NO_FILES, task_id=task.id,
        locked_files="\n".join(f"- {p}" for p in c.locked) or "(none — test files in SCOPE may be edited)",
        verify="\n".join(f"  {x}" for x in cmds) or "  (none configured: re-read your diff)",
        handoff=handoff) + pipeline.deps_line(c)


def _handoff(c, o: dict, cur: str, reset: bool) -> str:
    """Rendered by mr, never a transcript; capped well under 1.5k tokens."""
    def lst(items):
        items = list(items)
        return "; ".join(items[:8]) + (f" (+{len(items) - 8} more)" if len(items) > 8 else "") if items else "-"
    text = _HANDOFF.format(
        rung=cur, cls=o["cls"], a=lst(o["A"]), h=lst(o["H"]), d=lst(o["D"]), c=lst(o["C"]),
        j=f"Scope judge: {' '.join(o['J'])[:1500]}\n" if o.get("J") else "",
        patch=c.patch_path or "-",
        tree="was reset to the start of the task" if reset else "still holds the previous rung's changes")
    return text[:4000]


_DROID_RUNGS = frozenset({"S", "S+"})
_DROID_FALLBACK = {"credits": "credits_exhausted", "auth": "auth_failed", "transient": None}


def droid_twin(cfg, code: str) -> dict | None:
    """The droid twin for rung S or S+ when droid can run it now, else None."""
    twins = (getattr(cfg, "droid", None) or {}).get("twins") or {}
    if code not in _DROID_RUNGS or code not in twins or not droid.available(cfg, time.time())[0]:
        return None
    return twins[code]


def _restore_tree(c, snap: str, model: str) -> None:
    """Undo whatever a failed droid run left in the tree (saved as a patch first)."""
    repo, home = c.task.repo, c.cfg.paths.home
    diff = patches.cumulative_diff(repo, snap)
    if not diff:
        return
    path = patches.save_patch(home, c.task.id, len(c.rungs) + 1, f"droid-{model}", diff)
    res = patches.reset(repo, snap, path, home, c.task.id)
    if not res.ok:
        raise RuntimeError(f"droid fallback: could not restore the tree: {res.reason}; saved {', '.join(res.saved)}")


def _droid_attempt(c, code: str, twin: dict, prompt: str, attempt: int):
    """Run the rung on droid. Returns (rr, None) when that result is the attempt, else (rr, droid_try)
    after the tree is restored; the caller then runs the same rung on the claude CLI."""
    cfg = c.cfg
    snap = patches.take_reset_tree(c.task.repo)
    rr = runners.run_droid(code, twin, prompt, c.task.repo, cfg.timeouts.rung_s["droid"],
                           cfg=cfg, task=c.task.id, attempt=attempt)
    why, now = rr.droid_failure, time.time()
    if why not in _DROID_FALLBACK:
        if not rr.is_error and droid.read_state(cfg)["status"] != "ok":
            droid.write_state(cfg, "ok", now)
        return rr, None
    status = _DROID_FALLBACK[why]
    if status:
        st = droid.write_state(cfg, status, now, rr.result_text or rr.stderr_tail)
        until = datetime.fromtimestamp(st["recheck_after"], IST).strftime("%Y-%m-%d %H:%M IST")
        print(f"droid: {status}, falling back to the claude CLI until {until}", file=sys.stderr)
    else:
        print("droid: transient API error, falling back to the claude CLI for this attempt", file=sys.stderr)
    _restore_tree(c, snap, twin["model"])
    return rr, {"backend": "droid", "fallback": why, "wall_s": round(rr.wall_s, 2), "rc": rr.rc,
                "raw": rr.raw_path + ".out"}


def run_rung(c, code: str, prompt: str, attempt: int):
    """Run one implementer rung on its backend. Returns (rr, guarded, droid_try).

    Cursor rungs run on cursor-agent. Rung S or S+ with a droid twin runs on droid first (no claude guard
    check, no reservation); a credits, auth or transient failure falls back to the claude CLI in the same attempt.
    """
    cfg, repo, rung = c.cfg, c.task.repo, c.cfg.rungs[code]
    if rung.path == "cursor":
        return runners.run_cursor(rung.model, prompt, repo, None, cfg.timeouts.rung_s["cursor"],
                                  cfg=cfg, task=c.task.id, attempt=attempt), None, None
    sub = None
    if (twin := droid_twin(cfg, code)) is not None:
        rr, sub = _droid_attempt(c, code, twin, prompt, attempt)
        if sub is None:
            return rr, None, None
    guarded, rr = runners.run_claude(code, prompt, repo, "acceptEdits", None, cfg.timeouts.rung_s["claude"],
                                     cfg=cfg, task=c.task.id, attempt=attempt, wait_lock_s=c.args.wait_lock_s)
    return rr, guarded, sub


def denied(guarded, rr) -> bool:
    """A claude rung the guard (or the wrapper) refused. A cursor or droid run has no guard result."""
    return guarded is not None and (rr is None or guarded.wrapper_denied or rr.rc == 75)


def tag_record(rec: dict, rr, droid_try: dict | None) -> dict:
    rec["backend"], rec["droid_try"] = rr.backend, droid_try
    return rec


def _rung(c, code: str, kind: str, handoff: str) -> dict:
    """Run one rung, then violation, patch, D, H, A, C, class. Returns the outcome dict."""
    cfg, task, d = c.cfg, c.task, c.d
    repo, name, rung = task.repo, os.path.basename(task.repo), cfg.rungs[code]
    c.seq += 1
    attempt, c.cur, c.in_rung = c.seq, code, True
    prompt, before, started = _prompt(c, handoff), isolation.snapshot(repo), now_ist()
    if code == "S+A":  # §5.2: the injected instruction goes at the end of the user prompt
        dec = getattr(c, "adv_dec", None)
        if dec is not None and dec.consult:
            prompt += "\n\n" + advisor.build_injection(
                dec, decision_text=_first_sentence(task.text),
                focus=", ".join(sorted(getattr(c, "adv_sf", None) or ())) or "correctness of the change",
                high_risk_paths=_scope_paths(c))
    out = {"cls": None, "why": "", "keep": True, "A": [], "H": [], "D": [], "C": [], "J": [], "stop": None,
           "D_warn": []}
    rr, guarded, droid_try = run_rung(c, code, prompt, attempt)
    if denied(guarded, rr):
        c.in_rung = False
        c.frees_hint = guarded.guard.frees_hint or "≈ in 60 min"
        c.rungs.append({"rung": code, "model": rung.model, "class": "guard_deny", "signals": {},
                        "patch": None})
        return dict(out, cls="guard_deny", why=f"{code} denied: "
                    + (guarded.guard.reason if rr is None else "wrapper refused (exit 75)"))
    c.counters = count_after(c.counters, "S+" if code == "S+A" else code, kind=kind)  # S+A counts as S+
    pipeline.add_cost(c.cost, rr, guarded)
    viol = isolation.violation(before, isolation.snapshot(repo), allow_new_branch=c.branch)
    patch = patches.cumulative_diff(repo, c.reset_tree)
    c.patch_path = patches.save_patch(cfg.paths.home, task.id, len(c.rungs) + 1, rung.model, patch)
    c.diff, c.in_rung = patches.numstat(patch), False
    sig = {"H": None, "D": None, "A": None, "C": None}
    infra, empty, hres, sa = rr.is_error or rr.timed_out, not patch.strip(), None, None
    aud, sv, jv, ref, dres, green = None, None, None, None, None, False
    if code == "S+A" and not viol:
        aud, out["adv_action"] = _advisor_audit(c, rr, attempt, repo)
    if viol:
        out.update(cls="isolation_violation", stop=exitcodes.UNSAFE_GIT, why=(
            f"isolation violation: {viol}; in {c.iso} mr cannot restore refs, so the rung's change to "
            f"the repo was left as is; the tree edits are saved in {c.patch_path}"))
    else:
        trusted, judge_only = router.scope_files(d)
        dres = scope.scope(patch, checklist=c.checklist, difficulty=d.difficulty, files_named=tuple(trusted),
                           locked_files=c.locked, judge_files=tuple(judge_only))
        judged = _scope_on(c)
        # with the scope judge on only a violation stops H and A; findings wait for the judge
        checkable = dres.status != "violation" if judged else dres.ok
        c.files, sig["D"] = dres.files, dres.status if judged else dres.ok
        if not infra and not empty:
            if checkable:
                hres = hygiene.hygiene(repo, cfg, c.base_set, name, frozenset(c.locked))
                sig["H"] = hres.ok
            if checkable and c.checklist:
                sa = acceptance.acceptance(repo, c.checklist, c.lockdoc, cfg=cfg, repo_name=name)
                sig["A"] = sa.ok
        out["D"], out["D_warn"] = list(dres.reasons), list(dres.warnings)
        out["H"] = list(hres.new_failures) if hres else []
        if sa:
            out["A"] = ([f"{f}::{t}" for f, t in sa.failed + sa.missing]
                        + [f"locked: {p}" for p in sa.lock_violations])
        green = not (infra or empty) and checkable and bool(sig["H"]) and sig["A"] is not False
    at = SimpleNamespace(code=code, kind=kind, handoff=handoff, attempt=attempt, started=started, rr=rr,
                         guarded=guarded, droid_try=droid_try, sig=sig, out=out, viol=viol, infra=infra,
                         dres=dres, hres=hres, sa=sa, aud=aud, green=green, patch=patch, row=None)
    if _scope_flagged(c, dres, green):
        sv, jv, ref = _scope_decision(c, code, dres, patch, sig)
        if sv == "deferred":
            return _defer_scope(c, at, ref)
    return _finish(c, at, sv, jv, ref)


def _scope_flagged(c, dres, green: bool) -> bool:
    """The scope judge decides this attempt: it is on, H and A did not fail, and D only has findings."""
    return dres is not None and _scope_on(c) and green and dres.status == "flagged"


def _finish(c, at, sv, jv, ref) -> dict:
    """The rest of an attempt after the scope decision: review C, class, the rung entry and the attempt record."""
    cfg, d, out, sig, code = c.cfg, c.d, at.out, at.sig, at.code
    rung, d_kept = cfg.rungs[code], False
    if not at.viol:
        judged, dres, green = _scope_on(c), at.dres, at.green
        if sv is not None:
            green = sv == "accept"
            out["J"] = [jv.reason, *jv.instructions] if jv else ["scope judge unavailable"]
        d_kept = (dres.status == "pass" or sv == "accept") if judged else bool(dres.ok)
        aud = at.aud
        verdict = (aud.verdict or {}) if aud is not None and aud.relay_status == "ok" else {}
        adv_c = verdict.get("verdict") == "block" or (  # §6.3: C required on the relayed risks
            verdict.get("verdict") == "concern" and aud is not None and "unaddressed" in aud.violations)
        rv = None
        if green and (d.needs_review or adv_c or sv == "accept"):  # C only for a candidate that passes D, H and A
            rv, sig["C"] = _review(c, rung, at.patch,
                                   extra_reqs=_adv_review_reqs(verdict) if adv_c else None)
            out["C"] = [f"{it.get('id')}: {it.get('evidence')}" for it in rv.items if it.get("verdict") == "fail"]
        if rv is not None and rv.deferred:
            c.frees_hint = guard.check(rv.reviewer, cfg).frees_hint or "≈ in 60 min"
            out.update(stop=exitcodes.DEFERRED, why=f"required review {rv.reviewer} denied by the guard on a "
                       f"high-risk task; the patch is saved in {c.patch_path}")
        elif not green or (rv is not None and not rv.ok):
            out.update(_classify(c, rung, at.rr, dres, at.hres, at.sa, rv, at.infra, judged, sv))
            if out["J"]:
                out["why"] += "; scope judge: " + out["J"][0]
        if code == "S+A":
            out.update(_adv_verdict(c, at.rr, aud, green, out["cls"]))
    out["keep"] = (not at.viol and not at.infra and (d_kept or out["cls"] == "scope_repair")
                   and len(out["A"]) <= _MAX_FAILING_A and len(out["H"]) <= _MAX_NEW_H)
    c.rungs.append({"rung": code, "model": rung.model, "class": out["cls"], "signals": dict(sig),
                    "patch": c.patch_path, "failing": list(out["A"])})
    c.log.append(_attempt_row(c, rung, at, out["cls"], out["stop"]) | {"d_judge_ref": ref})
    return out


def _attempt_row(c, rung, at, cls: str | None, stop) -> dict:
    """The implementer record. A resumed attempt carries the row made when it was deferred; only the verdict-
    dependent fields are set now."""
    if at.row is None:
        return tag_record(pipeline.attempt_record(c, rung, at.code, at.rr, at.guarded, at.attempt, at.started,
                                                  at.sig, cls, decision="stop" if stop is not None else None,
                                                  d_warnings=at.out["D_warn"]), at.rr, at.droid_try)
    return dict(at.row, signals=dict(at.sig), failure_class=cls, failure_source="rules" if cls else None,
                decision="stop" if stop is not None or cls else "accept")


def _review(c, rung, patch: bytes, extra_reqs=None):
    """Signal C for a candidate; reviewer calls do not count toward the rung caps."""
    c.seq += 1
    started, t0 = now_ist(), time.time()
    reqs = c.checklist["requirements"] if c.checklist else None
    untested = getattr(c, "uncovered_reqs", ())
    if reqs and untested:  # the plan judge let these through without a locked test
        reqs = [dict(r, text=NO_TEST + r["text"]) if r["id"] in untested else r for r in reqs]
    if extra_reqs:
        reqs = list(reqs or []) + list(extra_reqs)
    rv = review.review(patch.decode("utf-8", "replace"), reqs,
                       None, rung.model, c.d.risk, cfg=c.cfg, tree=c.task.repo, task_id=c.task.id,
                       attempt=c.seq, required=c.d.required_review, defer=getattr(c, "high_risk", False))
    c.seq += 1
    c.n_review += rv.attempts
    for rr, guarded in rv.spend:
        pipeline.add_cost(c.cost, rr, guarded)
    c.log.append(_light(c, "reviewer", rv.reviewer, started, t0, {"C": rv.verdict}, None, ok=rv.ok,
                        guarded=_row_guarded(c.cfg, rv.reviewer, rv.spend)))
    return rv, rv.ok


def _classify(c, rung, rr, dres, hres, sa, rv, infra: bool, judged: bool = False, sv: str | None = None) -> dict:
    """Failure class from the rules (classify.py) plus the prior-attempt book-keeping."""
    named = router.decision_files(c.d)
    groups = (list(c.checklist["files_map"].values()) if c.checklist
              else [named] if named else [])
    touched = all(any(f in dres.files for f in g) for g in groups) if groups else bool(dres.files)
    failing = tuple(f"{f}::{t}" for f, t in sa.failed + sa.missing) if sa else ()
    h_ok = hres.ok if hres else True
    inputs = classify.Inputs(
        model=rung.model, rc=rr.rc, is_error=bool(rr.is_error), timed_out=rr.timed_out,
        wrapper_exit_75=False, hook_block=False, has_envelope=rr.has_envelope, diff_files=dres.files,
        diff_loc=dres.loc, mapped_files_touched=touched, H=h_ok, D=dres.ok, A_failing=failing,
        C=rv.verdict if rv else None, mr_result=rr.mr_result, prior_attempts=tuple(c.prior),
        D_status=dres.status if judged else None, scope_verdict=sv)
    rules = classify.classify(inputs)
    if not infra:
        c.prior.append({"model": rung.model, "A_failing": list(failing), "complete": classify.is_complete(inputs)})
    return {"cls": rules.cls, "why": "; ".join(rules.evidence + tuple(dres.reasons))}


def _step(c, cur: str, cls: str) -> Step:
    """next_step with guard_allows from guard.check (a pre-check, no reservation) for the next Claude rung."""
    s = LoopState(
        ladder=tuple(c.ladder), difficulty=c.d.difficulty,
        used_rungs=tuple("S+" if r == "S+A" else r for r in c.used), current_rung=cur,
        cls=cls, attempts=c.counters["attempts"], claude_rungs=c.counters["claude_rungs"],
        opus_rungs=c.counters["opus_rungs"], infra_retries_this_rung=c.infra,
        effort_up_used=tuple(c.effort_up_used), guard_allows=True,
        replans=c.replans)
    kw = {"caps": c.cfg.caps.attempts, "fallback": tuple(c.cfg.fallback_claude_denied)}
    step = next_step(s, **kw)
    if step.action == "rung" and step.rung in CLAUDE and droid_twin(c.cfg, step.rung) is None:
        g = guard.check(step.rung, c.cfg)
        if not g.allow:
            c.frees_hint = g.frees_hint or "≈ in 60 min"
            step = next_step(replace(s, guard_allows=False), **kw)
    return step


_ARGS = ("budget", "advisor", "advisor_consults", "advisor_slot", "wait_lock_s", "experiment", "yes", "judge",
         "classifier", "route", "rung")


def _defer_on(c, check: str) -> bool:
    """A judge that cannot run parks the task. A plan judge only does so before the first rung (no re-plan)."""
    on = (getattr(c.cfg, "check_judge", None) or {}).get("defer") == "on"
    return on and (check == "scope" or c.replans == 0)


def decision_from(doc: dict) -> router.Decision:
    return router.Decision(**{**doc, "risk_tags": frozenset(doc["risk_tags"]), "ladder": tuple(doc["ladder"]),
                              "reasons": tuple(doc["reasons"]), "risk_evidence": tuple(doc.get("risk_evidence") or ())})


def _context(c) -> dict:
    """Everything a resumed run needs besides state.json's own fields and the judge step's pending block."""
    task = c.task
    return {
        "task": {"text": task.text, "params": asdict(task.params), "hint_files": list(task.hint_files),
                 "source": task.source, "deprecated": list(task.deprecated)},
        "decision": {**asdict(c.d), "risk_tags": sorted(c.d.risk_tags)},
        "args": {k: getattr(c.args, k, None) for k in _ARGS}, "started_at": c.started_at,
        "cost": dict(c.cost), "seq": c.seq, "n_review": c.n_review, "files": list(c.files),
        "effort_up_used": list(c.effort_up_used), "prior": list(c.prior), "infra": c.infra,
        "adv_reruns": getattr(c, "adv_reruns", 0), "uncovered_reqs": list(getattr(c, "uncovered_reqs", ())),
        "plan_rejected": bool(getattr(c, "plan_rejected", False)),
        "plan_judge_reason": getattr(c, "plan_judge_reason", None), "high_risk": bool(c.high_risk),
        "path_tags": sorted(c.path_tags), "base_set": sorted(c.base_set), "base_set_format": hygiene.ID_FORMAT,
    }


def _defer(c, check: str, pending: dict) -> int:
    """Park the task: pending block and HEAD for state.json, one queue line, the resume hint on stderr. Returns 75."""
    task, home = c.task, c.cfg.paths.home
    used = "scope_calls" if check == "scope" else "cj_calls"
    pending.update(check=check, ctx=_context(c), n_plan=c.n_plan, counters=dict(c.counters),
                   scope_calls=getattr(c, "scope_calls", 0), cj_calls=getattr(c, "cj_calls", 0),
                   scope_repaired=sorted(getattr(c, "scope_repaired", ())), tree=patches.take_reset_tree(task.repo))
    pending[used] -= 1  # the call that got no verdict does not use up a slot of the cap
    c.pending, c.deferred = pending, {"check": check, "status": c.judge_status}
    c.head = pipeline.git(task.repo, "rev-parse", "HEAD").stdout.strip() or None
    c.frees_hint = c.judge_hint
    state.queue_put(home, {"task": task.id, "repo": task.repo, "check": check, "queued_at": now_ist(),
                           "frees_hint": c.frees_hint})
    print(f"deferred: {check} judge unavailable ({c.judge_status}); resume with: mr resume {task.id}",
          file=sys.stderr)
    return exitcodes.DEFERRED


def _defer_plan(c, res, row: dict, stronger: bool):
    _defer(c, "plan", {"findings": list(res.errors), "doc": res.doc, "repairs": list(res.repairs),
                       "retry_errors": list(res.retry_errors), "status": res.status, "rung": res.rung,
                       "stronger": stronger, "row": row})
    return res


def _defer_scope(c, at, ref) -> dict:
    """The scope judge could not run: keep the patch, put the tree back and park the attempt."""
    task, rung = c.task, c.cfg.rungs[at.code]
    c.rungs.append({"rung": at.code, "model": rung.model, "class": "deferred", "signals": dict(at.sig),
                    "patch": c.patch_path, "failing": []})
    r = patches.reset(task.repo, c.reset_tree, c.patch_path, c.cfg.paths.home, task.id)
    if not r.ok:
        return dict(at.out, stop=exitcodes.UNSAFE_GIT,
                    why=f"reset refused: {r.reason}; saved {', '.join(r.saved)}")
    aud = at.aud
    code = _defer(c, "scope", {
        "findings": list(at.dres.findings), "rung": at.code, "attempt": at.attempt, "kind": at.kind,
        "handoff": at.handoff, "dres": asdict(at.dres), "sig": dict(at.sig), "out": at.out,
        "rr": {"rc": at.rr.rc, "is_error": at.rr.is_error, "timed_out": at.rr.timed_out,
               "has_envelope": at.rr.has_envelope, "mr_result": at.rr.mr_result},
        "aud": {"verdict": aud.verdict, "relay_status": aud.relay_status,
                "violations": list(aud.violations)} if aud is not None else None,
        "row": _attempt_row(c, rung, at, None, None)})
    return dict(at.out, stop=code, why=f"scope judge unavailable ({c.judge_status}); the patch is saved in "
                                       f"{c.patch_path}; resume with: mr resume {task.id}")


def restore(c, st: dict) -> None:
    """Put a context built from state.json back to where the deferred run stopped."""
    p = st["pending"]
    ctx = p["ctx"]
    c.reset_tree, c.checklist, c.rungs = st["reset_point"], st["checklist"], list(st["rungs"])
    c.iso, c.branch, c.orig_branch, c.patch_path, c.replans = (
        st["isolation"], st["branch"], st["orig_branch"], st["patch"], st["replans"])
    c.counters, c.cost, c.n_plan = dict(p["counters"]), dict(ctx["cost"]), p["n_plan"]
    c.seq, c.n_review, c.infra = ctx["seq"], ctx["n_review"], ctx["infra"]
    c.scope_calls, c.cj_calls, c.scope_repaired = p["scope_calls"], p["cj_calls"], set(p["scope_repaired"])
    c.files, c.effort_up_used, c.prior = tuple(ctx["files"]), list(ctx["effort_up_used"]), list(ctx["prior"])
    c.adv_reruns, c.uncovered_reqs = ctx["adv_reruns"], tuple(ctx["uncovered_reqs"])
    c.plan_rejected, c.plan_judge_reason = ctx["plan_rejected"], ctx["plan_judge_reason"]
    c.high_risk, c.path_tags = ctx["high_risk"], frozenset(ctx["path_tags"])
    if ctx.get("base_set_format") == hygiene.ID_FORMAT:
        c.base_set = frozenset(ctx["base_set"])
    else:  # saved with older ids: measure again on the reset tree, which mr resume checked is the work tree
        c.base_set = hygiene.base_failures(c.task.repo, c.cfg, os.path.basename(c.task.repo), c.reset_tree)
    if c.checklist:
        c.locked = tuple(t["path"] for t in c.checklist["tests"])
        with open(os.path.join(state.task_dir(c.cfg.paths.home, c.task.id), "tests.lock"), encoding="utf-8") as fh:
            c.lockdoc = json.load(fh)


def _plan_result(p: dict) -> planner.PlanResult:
    return planner.PlanResult(False, None, None, list(p["findings"]), 0, [], p["rung"], (), (), list(p["repairs"]),
                              list(p["retry_errors"]), p["status"], False, p["doc"])


def _apply_saved_patch(c) -> str | None:
    """Put the deferred attempt's change back on the tree (which is at the reset point). Returns an error text."""
    repo = c.task.repo
    top = pipeline.git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    got = pipeline.git(top, "apply", "--binary", "--whitespace=nowarn", os.path.abspath(c.patch_path))
    if got.returncode != 0:
        return f"could not restore the saved patch {c.patch_path}: {(got.stderr or got.stdout).strip()}"
    return None


def _resume_rung(c, p: dict) -> dict:
    """The deferred attempt: run the scope judge again with the saved findings, then finish it as _rung does."""
    code, sig, out, rr = p["rung"], p["sig"], p["out"], p["rr"]
    with open(c.patch_path, "rb") as fh:
        patch = fh.read()
    dres = scope.SignalD(**{k: tuple(v) if isinstance(v, list) else v for k, v in p["dres"].items()})
    c.cur, c.files, c.diff = code, dres.files, patches.numstat(patch)
    if c.rungs and c.rungs[-1]["class"] == "deferred":
        c.rungs.pop()
    saved = p.get("aud")
    at = SimpleNamespace(
        code=code, kind=p["kind"], handoff=p["handoff"], attempt=p["attempt"], started=p["row"]["started_at"],
        rr=SimpleNamespace(**rr), guarded=None, droid_try=None, sig=sig, out=out, viol=None, infra=False, dres=dres,
        hres=SimpleNamespace(ok=True, new_failures=tuple(out["H"])) if sig["H"] is not None else None,
        sa=SimpleNamespace(ok=True, failed=(), missing=(), lock_violations=()) if sig["A"] is not None else None,
        aud=SimpleNamespace(**saved) if saved else None, green=True, patch=patch, row=p["row"])
    sv, jv, ref = _scope_decision(c, code, dres, patch, sig)
    if sv == "deferred":
        return _defer_scope(c, at, ref)
    return _finish(c, at, sv, jv, ref)


def resume_task(c, p: dict) -> int:
    """Continue a deferred task where it stopped: the plan judge, or the scope judge on the saved attempt."""
    if (code := refuse_unparsed(c, c.base_set)) is not None:
        return code
    if p["check"] == "plan":
        c.uncovered_reqs = ()
        if (code := _plan_finish(c, _plan_result(p), p["row"], p["stronger"])) is not None:
            return code
        return _ladder(c, c.ladder[0], "implementer", "(first attempt)")
    if err := _apply_saved_patch(c):
        return c.stop(exitcodes.UNSAFE_GIT, err)
    return _ladder(c, p["rung"], p["kind"], p["handoff"], p)


def refuse_unparsed(c, base_set) -> int | None:
    """Stop before any model call when a hygiene command is red on the base and mr parses no case from it."""
    if bad := hygiene.unparsed(c.cfg, os.path.basename(c.task.repo), base_set):
        c.refused = {"check": "hygiene_base", "commands": bad, "reason": hygiene.refusal(bad)}
        return c.stop(exitcodes.UNSAFE_GIT, c.refused["reason"])
    return None


def run_task(c) -> int:
    """The full ladder: planner (medium+), rungs, signals, escalation, accept or exit (§10.2)."""
    cfg, task = c.cfg, c.task
    if (code := pipeline.start(c)) is not None:
        return code
    d = c.d
    if not c.ladder:
        return c.stop(exitcodes.NOT_ACCEPTED, "empty ladder after the budget filter")
    c.reset_tree = patches.take_reset_tree(task.repo)
    # the baseline is the tree before the planner writes its red tests, so they cannot hide a new failure
    c.base_set = hygiene.base_failures(task.repo, cfg, os.path.basename(task.repo), c.reset_tree)
    if (code := refuse_unparsed(c, c.base_set)) is not None:
        return code
    c.save_state("plan")
    if d.planner or (d.route == "cascade" and _DIFFS.index(d.difficulty) >= _DIFFS.index("medium")):
        if (code := _plan(c)) is not None:
            return code
    return _ladder(c, c.ladder[0], "implementer", "(first attempt)")


def _ladder(c, cur: str, kind: str, handoff: str, pending: dict | None = None) -> int:
    """The rung loop. ``pending`` is a deferred attempt: its scope judge runs first instead of a new rung."""
    cfg, task = c.cfg, c.task
    while True:
        if pending is not None:
            o, pending = _resume_rung(c, pending), None
        else:
            cur = advisor_rung(c, cur) if cur == "S+" else cur
            c.save_state("rung")
            o = _rung(c, cur, kind, handoff)
        c.save_state("next")
        if cur == "S+A":  # §7: at most one re-run of the rung without the advisor, not an attempt
            action = o.pop("adv_action", "none")
            if (action in ("rerun_without_advisor", "open_breaker_and_rerun")
                    and getattr(c, "adv_reruns", 0) < 1 and o["stop"] is None):
                if action == "open_breaker_and_rerun":
                    advisor.Monitor(cfg.paths.home).open_breaker("launch_error")
                c.adv_reruns = getattr(c, "adv_reruns", 0) + 1
                o = _rung(c, "S+", "infra_retry", handoff)
                cur = "S+"
                c.save_state("next")
        if o["stop"] is not None:
            return c.stop(o["stop"], o["why"])
        if o["cls"] is None:
            if c.iso == "W5" and (err := pipeline.commit_w5(c)):
                return c.stop(exitcodes.UNSAFE_GIT, err)
            return c.stop(exitcodes.OK, "accepted")
        step = _step(c, cur, o["cls"])
        fresh = False
        if step.action == "replan":
            if (code := _replan(c)) is not None:
                return code
            step, fresh = _step(c, cur, "tried_wrong"), True
        if step.action == "exit":
            return c.stop(step.exit, f"{step.reason}; last: {o['why']}")
        reset = fresh
        if not fresh and (step.reset or not o["keep"]) and o["cls"] != "guard_deny":
            r = patches.reset(task.repo, c.reset_tree, c.patch_path, cfg.paths.home, task.id)
            if not r.ok:
                return c.stop(exitcodes.UNSAFE_GIT, f"reset refused: {r.reason}; saved {', '.join(r.saved)}")
            reset = True
        retry = o["cls"] == "infra" and step.rung == cur
        c.infra = c.infra + 1 if retry else 0
        kind = "infra_retry" if retry else "scope_repair" if o["cls"] == "scope_repair" else "implementer"
        if step.effort_up:
            c.effort_up_used.append(base(cur))
        if o["cls"] != "guard_deny":
            handoff = _handoff(c, o, cur, reset)
        cur = step.rung
