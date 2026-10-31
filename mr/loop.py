"""Escalation loop: pure next_step, counters, run_task (mr-spec §10.4, §10.6; p0 README §4)."""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime

from mr import (acceptance, advisor, classify, exitcodes, guard, hygiene, isolation, patches,
                pipeline, planner, review, runners, scope, state)
from mr.log import IST, now_ist, read as _read_runs

CAPS = {"trivial": 3, "easy": 3, "medium": 4, "hard": 4, "critical": 4}
CLAUDE = frozenset({"S", "S+", "O", "O+"})
OPUS = frozenset({"O", "O+"})
UP = {"G-l": "G-m", "G-m": "G-h", "G-h": "G-x", "K-h": "K-x", "S": "S+", "O": "O+"}
_BASE = {"G": "grok-4.7", "C25": "composer-2.5", "K": "kimi-k3",
         "S": "claude-sonnet-5-5", "S+": "claude-sonnet-5-5",
         "O": "claude-opus-5-5", "O+": "claude-opus-5-5"}
CLAUDE_CAP, OPUS_CAP = 2, 1


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
    reset = cls == "off_track"
    if cls == "skipped":
        u = UP.get(cur)
        if u and base(cur) not in s.effort_up_used and ok(u):
            st = launch(u, f"effort-up {cur} -> {u}")
            return Step(st.action, st.rung, st.exit, False, st.rung == u, st.reason)
    elif cls not in ("tried_wrong", "off_track"):
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
    if kind not in ("implementer", "effort_up", "fallback"):
        raise ValueError(f"unknown kind: {kind!r}")
    out["attempts"] += 1
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
            "Failing acceptance tests: {a}\nNew hygiene failures: {h}\nScope problems: {d}\n"
            "Reviewer findings: {c}\nPrevious patch (relative to the start of the task): {patch}\n")
_MAX_FAILING_A, _MAX_NEW_H = 3, 10


def _num(val) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _first_sentence(text: str) -> str:
    line = re.split(r"(?<=[.!?])\s+|\n", (text or "").strip(), maxsplit=1)[0]
    return line[:200] or "the change"


def _scope_paths(c) -> list[str]:
    """files_named plus the checklist scope paths, de-duplicated (advisor sf_tags / high_risk_paths)."""
    paths = list(c.task.files_named)
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
        guard_in = {"hour": g.hour if _num(g.hour) else lim.hour_max,
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


def _plan(c) -> int | None:
    """Planner (cfg.planner.rungs by difficulty): checklist, locked tests, red on base, new reset_tree.

    Not a ladder rung: a Claude planner call costs points but does not count to claude/opus rungs.
    Exit 3 if invalid twice.
    """
    cfg, task = c.cfg, c.task
    c.seq += 1
    started, t0 = now_ist(), time.time()
    res = planner.plan(task, cfg=cfg, tree=task.repo, task_id=task.id, attempt=c.seq,
                       difficulty=c.d.difficulty)
    c.n_plan += res.attempts
    for rr, guarded in res.spend:
        pipeline.add_cost(c.cost, rr, guarded)
    c.log.append(_light(c, "planner", res.rung, started, t0, {"fallback": list(res.notes)} if res.notes else {},
                        None if res.ok else "invalid_plan", ok=res.ok,
                        guarded=_row_guarded(cfg, res.rung, res.spend)))
    if not res.ok:
        raw = os.path.join(state.task_dir(cfg.paths.home, task.id), "planner-a*.txt")
        return c.stop(exitcodes.HUMAN, f"planner gave no valid checklist twice ({'; '.join(res.errors[:3])}); "
                                       f"raw outputs: {raw}")
    c.checklist, c.reset_tree = res.checklist, res.reset_tree
    c.locked = tuple(t["path"] for t in res.checklist["tests"])
    with open(os.path.join(state.task_dir(cfg.paths.home, task.id), "tests.lock"), encoding="utf-8") as fh:
        c.lockdoc = json.load(fh)
    c.base_set = hygiene.base_failures(task.repo, cfg, os.path.basename(task.repo), c.reset_tree)
    c.prior = []  # the two-model rule counts only attempts after a (re-)plan
    return None


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
        own = list(task.files_named)
    with open(_PROMPT, encoding="utf-8") as fh:
        template = fh.read()
    return template.format(
        goal=goal, scope="\n".join(f"- {p}" for p in own) or pipeline.NO_FILES, task_id=task.id,
        locked_files="\n".join(f"- {p}" for p in c.locked) or "(none — test files in SCOPE may be edited)",
        verify="\n".join(f"  {x}" for x in cmds) or "  (none configured: re-read your diff)",
        handoff=handoff)


def _handoff(c, o: dict, cur: str, reset: bool) -> str:
    """Rendered by mr, never a transcript; capped well under 1.5k tokens."""
    def lst(items):
        items = list(items)
        return "; ".join(items[:8]) + (f" (+{len(items) - 8} more)" if len(items) > 8 else "") if items else "-"
    text = _HANDOFF.format(
        rung=cur, cls=o["cls"], a=lst(o["A"]), h=lst(o["H"]), d=lst(o["D"]), c=lst(o["C"]),
        patch=c.patch_path or "-",
        tree="was reset to the start of the task" if reset else "still holds the previous rung's changes")
    return text[:4000]


def _rung(c, code: str, kind: str, handoff: str) -> dict:
    """Run one rung, then violation, patch, D, H, A, C, class. Returns the outcome dict."""
    a, cfg, task, d = c.args, c.cfg, c.task, c.d
    repo, name, rung = task.repo, os.path.basename(task.repo), cfg.rungs[code]
    c.seq += 1
    attempt, c.cur, c.in_rung = c.seq, code, True
    prompt, before, started, guarded = _prompt(c, handoff), isolation.snapshot(repo), now_ist(), None
    if code == "S+A":  # §5.2: the injected instruction goes at the end of the user prompt
        dec = getattr(c, "adv_dec", None)
        if dec is not None and dec.consult:
            prompt += "\n\n" + advisor.build_injection(
                dec, decision_text=_first_sentence(task.text),
                focus=", ".join(sorted(getattr(c, "adv_sf", None) or ())) or "correctness of the change",
                high_risk_paths=_scope_paths(c))
    out = {"cls": None, "why": "", "keep": True, "A": [], "H": [], "D": [], "C": [], "stop": None}
    if rung.path == "cursor":
        rr = runners.run_cursor(rung.model, prompt, repo, None, cfg.timeouts.rung_s["cursor"],
                                cfg=cfg, task=task.id, attempt=attempt)
    else:
        guarded, rr = runners.run_claude(code, prompt, repo, "acceptEdits", None,
                                         cfg.timeouts.rung_s["claude"], cfg=cfg, task=task.id,
                                         attempt=attempt, wait_lock_s=a.wait_lock_s)
        if rr is None or guarded.wrapper_denied or rr.rc == 75:
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
    infra, empty, hres, sa, rv = rr.is_error or rr.timed_out, not patch.strip(), None, None, None
    aud = None
    if code == "S+A" and not viol:
        aud, out["adv_action"] = _advisor_audit(c, rr, attempt, repo)
    if viol:
        out.update(cls="isolation_violation", stop=exitcodes.UNSAFE_GIT, why=(
            f"isolation violation: {viol}; in {c.iso} mr cannot restore refs, so the rung's change to "
            f"the repo was left as is; the tree edits are saved in {c.patch_path}"))
    else:
        dres = scope.scope(patch, checklist=c.checklist, difficulty=d.difficulty,
                           files_named=task.files_named, locked_files=c.locked)
        c.files, sig["D"] = dres.files, dres.ok
        if not infra and not empty:
            if dres.ok:
                hres = hygiene.hygiene(repo, cfg, c.base_set, name, frozenset(c.locked))
                sig["H"] = hres.ok
            if dres.ok and c.checklist:
                sa = acceptance.acceptance(repo, c.checklist, c.lockdoc, cfg=cfg, repo_name=name)
                sig["A"] = sa.ok
        out["D"] = list(dres.reasons)
        out["H"] = list(hres.new_failures) if hres else []
        if sa:
            out["A"] = ([f"{f}::{t}" for f, t in sa.failed + sa.missing]
                        + [f"locked: {p}" for p in sa.lock_violations])
        green = not (infra or empty) and bool(sig["D"] and sig["H"]) and sig["A"] is not False
        verdict = (aud.verdict or {}) if aud is not None and aud.relay_status == "ok" else {}
        adv_c = verdict.get("verdict") == "block" or (  # §6.3: C required on the relayed risks
            verdict.get("verdict") == "concern" and aud is not None and "unaddressed" in aud.violations)
        if green and (d.needs_review or adv_c):  # C only for a candidate that already passes D, H and A
            rv, sig["C"] = _review(c, rung, patch,
                                   extra_reqs=_adv_review_reqs(verdict) if adv_c else None)
            out["C"] = [f"{it.get('id')}: {it.get('evidence')}" for it in rv.items if it.get("verdict") == "fail"]
        if not green or (rv is not None and not rv.ok):
            out.update(_classify(c, rung, rr, dres, hres, sa, rv, infra))
        if code == "S+A":
            out.update(_adv_verdict(c, rr, aud, green, out["cls"]))
    out["keep"] = (not viol and not infra and bool(sig["D"]) and len(out["A"]) <= _MAX_FAILING_A
                   and len(out["H"]) <= _MAX_NEW_H)
    c.rungs.append({"rung": code, "model": rung.model, "class": out["cls"], "signals": dict(sig),
                    "patch": c.patch_path, "failing": list(out["A"])})
    c.log.append(pipeline.attempt_record(c, rung, code, rr, guarded, attempt, started, sig, out["cls"]))
    return out


def _review(c, rung, patch: bytes, extra_reqs=None):
    """Signal C for a candidate; reviewer calls do not count toward the rung caps."""
    c.seq += 1
    started, t0 = now_ist(), time.time()
    reqs = c.checklist["requirements"] if c.checklist else None
    if extra_reqs:
        reqs = list(reqs or []) + list(extra_reqs)
    rv = review.review(patch.decode("utf-8", "replace"), reqs,
                       None, rung.model, c.d.risk, cfg=c.cfg, tree=c.task.repo, task_id=c.task.id,
                       attempt=c.seq)
    c.seq += 1
    c.n_review += rv.attempts
    for rr, guarded in rv.spend:
        pipeline.add_cost(c.cost, rr, guarded)
    c.log.append(_light(c, "reviewer", rv.reviewer, started, t0, {"C": rv.verdict}, None, ok=rv.ok,
                        guarded=_row_guarded(c.cfg, rv.reviewer, rv.spend)))
    return rv, rv.ok


def _classify(c, rung, rr, dres, hres, sa, rv, infra: bool) -> dict:
    """Failure class from the rules (classify.py) plus the prior-attempt book-keeping."""
    task = c.task
    groups = (list(c.checklist["files_map"].values()) if c.checklist
              else [list(task.files_named)] if task.files_named else [])
    touched = all(any(f in dres.files for f in g) for g in groups) if groups else bool(dres.files)
    failing = tuple(f"{f}::{t}" for f, t in sa.failed + sa.missing) if sa else ()
    h_ok = hres.ok if hres else True
    inputs = classify.Inputs(
        model=rung.model, rc=rr.rc, is_error=bool(rr.is_error), timed_out=rr.timed_out,
        wrapper_exit_75=False, hook_block=False, has_envelope=rr.has_envelope, diff_files=dres.files,
        diff_loc=dres.loc, mapped_files_touched=touched, H=h_ok, D=dres.ok, A_failing=failing,
        C=rv.verdict if rv else None, mr_result=rr.mr_result, prior_attempts=tuple(c.prior))
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
    if step.action == "rung" and step.rung in CLAUDE:
        g = guard.check(step.rung, c.cfg)
        if not g.allow:
            c.frees_hint = g.frees_hint or "≈ in 60 min"
            step = next_step(replace(s, guard_allows=False), **kw)
    return step


def run_task(c) -> int:
    """The full ladder: planner (medium+), rungs, signals, escalation, accept or exit (§10.2)."""
    cfg, task, d = c.cfg, c.task, c.d
    if (code := pipeline.start(c)) is not None:
        return code
    if not c.ladder:
        return c.stop(exitcodes.NOT_ACCEPTED, "empty ladder after the budget filter")
    c.reset_tree = patches.take_reset_tree(task.repo)
    c.save_state("plan")
    if d.route == "cascade" and _DIFFS.index(d.difficulty) >= _DIFFS.index("medium"):
        if (code := _plan(c)) is not None:
            return code
    else:
        c.base_set = hygiene.base_failures(task.repo, cfg, os.path.basename(task.repo), c.reset_tree)
    cur, kind, handoff = c.ladder[0], "implementer", "(first attempt)"
    while True:
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
        kind = "infra_retry" if retry else "implementer"
        if step.effort_up:
            c.effort_up_used.append(base(cur))
        if o["cls"] != "guard_deny":
            handoff = _handoff(c, o, cur, reset)
        cur = step.rung
