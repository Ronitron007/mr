"""Judge facade (mr-spec §5.5, §9.11): one route call per ask, one judge record per call, cache hit or skip.

Shadow mode logs the answer and hands it back as a ShadowAnswer; nothing there changes a routing decision.
Mode ``on`` (decide_route) is the synchronous intake call whose readings mr merges into the decision.
"""
from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

from mr import guard, router, runners, shadow
from mr import facts as facts_mod
from mr.judge import breaker, budget, cache, client
from mr.judge import merge as merge_mod
from mr.judge import policy
from mr.judge import prompt as prompt_mod
from mr.judge import questions as qmod
from mr.judge import state as state_mod

BACKEND = "haiku"
USE = "route"
QSET = "route.v1"
QSET_V2 = "route.v2"
_NO_CALL = frozenset({"guard_yield", "cli_unavailable"})  # the client reports these without running claude
_USABLE = frozenset({"ok", "ok_partial", "cache_hit"})
_FIELDS = ("type", "difficulty", "route", "risk")
_TASK_CALLS_ON = 3  # spec §3.7


def _scale() -> float:
    try:
        return float(os.environ.get("MR_TEST_TIME_SCALE") or 1)
    except ValueError:
        return 1.0


def _killpg(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _text(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", "replace")


def _scope_level(features) -> int:
    n = len(features.files_named)
    return 0 if n <= 1 else 1 if n <= 3 else 2 if n <= 6 else 3 if n <= 12 else 4


@dataclass(frozen=True)
class JudgeOutcome:
    status: str
    readings: dict | None
    ref: str
    latency_ms: int | None
    source: str  # "judge" | "cache"


class Judge:
    def __init__(self, cfg, mode: str, log, *, task_id: str, exec_fn=None):
        if mode not in ("shadow", "on"):
            raise ValueError(f"judge mode {mode!r}: expected shadow or on")
        self.cfg, self.log, self.task_id, self._exec = cfg, log, task_id, exec_fn
        self.mode = mode
        self._block = cfg.judge or {}
        self._qset = self._block.get("qset", QSET_V2)
        self._v2 = self._qset == QSET_V2
        self._questions = qmod.load(self._qset)
        self._qset_sha = qmod.qset_sha(self._questions)
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mr-judge")
        self._lock = threading.Lock()
        self._n = self._calls = 0
        self._futures: dict[str, object] = {}
        self._recs: dict[str, dict] = {}
        self._pgids: dict[str, int] = {}
        self._done: set[str] = set()
        self._abandoned: set[str] = set()
        self._answer: shadow.ShadowAnswer | None = None

    # --- public API ---------------------------------------------------------

    def ask_route(self, task, features, consulted: bool, facts=None) -> str:
        """Start one route call in the background and return its ref ("j-<task>-<n>"). Never raises."""
        ref = self._next_ref()
        self._recs[ref] = self._blank(ref, consulted)
        try:
            self._futures[ref] = self._pool.submit(self._run, ref, task, features, consulted, facts)
        except Exception:
            self._finish(ref, "cli_error", None)
        return ref

    def skip_route(self, status: str, consulted: bool) -> str:
        """Log one record for a task the judge does not look at (for example skipped_confident)."""
        ref = self._next_ref()
        self._recs[ref] = self._blank(ref, consulted)
        self._finish(ref, status, None, bare=False)
        return ref

    def decide_route(self, task, features, facts, rules_decision) -> JudgeOutcome:
        """Mode on: one synchronous route call (or a cache hit). Never raises, sleeps or waits for headroom.

        The record is written later by note_applied, once the merge knows what it took from the judge.
        ``rules_decision`` is part of the call contract; the judge's state is built from ``features``.
        """
        ref = self._next_ref()
        rec = self._recs[ref] = self._blank(ref, True)
        start = time.monotonic()
        status = "cli_error"
        try:
            _answer, status = self._steps(ref, rec, task, features, True, facts)
        except Exception:
            pass
        rec["status"] = status
        usable = status in _USABLE
        latency = rec["latency_ms"]
        if not isinstance(latency, int):
            latency = int((time.monotonic() - start) * 1000)
        return JudgeOutcome(status, rec["decisions"] if usable else None, ref, latency,
                            "cache" if status == "cache_hit" else "judge")

    def note_applied(self, ref: str, applied, overrides, status: str | None = None) -> None:
        """Write the one record of a decide_route call: the fields the merge took from the judge and its overrides."""
        rec = dict(self._recs[ref])
        rec["applied"] = list(applied)
        rec["overrides"] = [dataclasses.asdict(o) if dataclasses.is_dataclass(o) else dict(o) for o in overrides]
        if status is not None:
            rec["status"] = status
        self.log.append(rec)

    def join_shadow(self, timeout_s: float) -> None:
        """Wait for the calls. Each one still running is logged as timeout and its child process group is killed."""
        wait(list(self._futures.values()), timeout=timeout_s)
        with self._lock:
            late = [ref for ref in self._futures if ref not in self._done]
            self._abandoned.update(late)
            for ref in late:
                self._futures[ref].cancel()
            pgids = [self._pgids[ref] for ref in late if ref in self._pgids]
            for ref in late:
                self._done.add(ref)
                rec = dict(self._recs[ref], status="timeout")
                self.log.append(rec)
                self._answer = self._bare(ref, rec)
        for pgid in pgids:
            _killpg(pgid)
        if late:
            try:
                breaker.record(self.cfg, BACKEND, "timeout")
            except Exception:
                pass
        self._pool.shutdown(wait=False, cancel_futures=True)

    def route_answer(self) -> shadow.ShadowAnswer | None:
        with self._lock:
            return self._answer

    def calls(self) -> int:
        with self._lock:
            return self._calls

    # --- one call -------------------------------------------------------------

    def _run(self, ref: str, task, features, consulted: bool, facts=None) -> None:
        rec = self._recs[ref]
        answer, status = None, "cli_error"
        try:
            answer, status = self._steps(ref, rec, task, features, consulted, facts)
        except Exception:
            pass
        self._finish(ref, status, answer)

    def _steps(self, ref: str, rec: dict, task, features, consulted: bool, facts=None):
        """The route call of §9.11. Returns (ShadowAnswer | None, status); a failing step ends the call."""
        cfg, block = self.cfg, self._block
        name = os.path.basename(task.repo)
        repos = block.get("repos", "all")
        if repos != "all" and not (isinstance(repos, (list, tuple)) and name in repos):
            return None, "skipped_explicit"
        if not guard.judge_cli_available(cfg):
            return None, "cli_unavailable"
        if not breaker.allow(cfg, BACKEND):
            return None, "breaker_open"
        questions, qsha, built, rules, facts = self._prepare(task, features, name, facts)
        rec.update(qset_sha=qsha, state_sha=built.state_sha, state_bytes=built.state_bytes,
                   state_tokens_est=built.est_tokens, redactions=built.redactions, truncated=built.truncated,
                   egress=built.egress)
        if built.status:
            return None, built.status
        if built.egress == "off":
            return None, "egress_off"
        key = cache.make_key(USE, BACKEND, rec["model_req"], rec["effort"], rec["prompt_sha"], qsha,
                             built.state_sha)
        hit = self._from_cache(cache.get(cfg, key))
        if hit is not None:
            parsed, rec["model_served"] = hit
            return self._read(ref, rec, parsed, rules, "cache_hit", consulted), "cache_hit"
        caps = {"per_task": (block.get("budget") or {}).get("max_calls_per_task_on", _TASK_CALLS_ON)} \
            if self.mode == "on" else {}
        allowed, reason, after = budget.charge(cfg, self.task_id, self.mode, **caps)
        rec["budget_after"] = after
        if not allowed:
            return None, reason
        render = prompt_mod.render_prompt_v2 if self._v2 else prompt_mod.render_prompt
        text, qmap = render(built.state, questions)
        with self._lock:
            self._calls += 1
        rec["pts_est"] = (self._block.get("budget") or {}).get("est_pts_per_call", 0.2)
        raw = client.call(text, cfg, mode=self.mode, exec_fn=self._exec or self._spawn(ref))
        rec.update(exit_code=raw.exit_code, latency_ms=raw.latency_ms, duration_api_ms=raw.duration_api_ms,
                   input_tokens=raw.input_tokens, output_tokens=raw.output_tokens, usd_equiv=raw.usd_equiv,
                   model_served=raw.model_served)
        if raw.status in _NO_CALL:
            rec["pts_est"] = 0
        if ref in self._abandoned:  # join_shadow already logged the timeout: no late result, no breaker count
            return None, "timeout"
        if raw.status != "ok":
            breaker.record(cfg, BACKEND, raw.status)
            return None, raw.status
        if self._v2:
            parsed = prompt_mod.parse_answers_v2(raw.result_text or "", qmap, questions, facts, built.state["task"])
        else:
            parsed = prompt_mod.parse_answers(raw.result_text or "", qmap, questions)
        breaker.record(cfg, BACKEND, parsed.status)
        if parsed.status == "invalid_response":
            return None, parsed.status
        usage = {"input_tokens": raw.input_tokens, "output_tokens": raw.output_tokens, "usd_equiv": raw.usd_equiv,
                 "duration_api_ms": raw.duration_api_ms}
        stored = {"status": parsed.status, "answers": parsed.answers, "unsure": list(parsed.unsure)}
        cache.put(cfg, key, use=USE, backend=BACKEND, qset_sha=qsha, answers=stored, usage=usage,
                  model_served=raw.model_served)
        return self._read(ref, rec, parsed, rules, parsed.status, consulted), parsed.status

    def _prepare(self, task, features, name: str, facts):
        """(questions, qset_sha, BuiltState, rules, facts) for this call; route.v2 expands the questions from the facts."""
        if not self._v2:
            built = state_mod.build_route_state(task, features, self.cfg, name, self._questions)
            return (self._questions, self._qset_sha, built,
                    {"risk": features.risk_floor, "scope": _scope_level(features)}, None)
        if facts is None:
            facts = facts_mod.build_facts(task.text, task.repo, hints=task.files_named)
        questions = qmod.expand_v2(self._questions, facts)
        rules = {"type": features.type, "difficulty": features.difficulty_est, "risk": features.risk_floor}
        built = state_mod.build_route_state_v2(task, facts, self.cfg, name, questions, rules=rules)
        return questions, qmod.qset_sha(questions), built, rules, facts

    def _read(self, ref: str, rec: dict, parsed, rules: dict, status: str, consulted: bool):
        read = policy.read_route_v2 if self._v2 else policy.read_route
        decisions = read(parsed, rules, self.cfg)
        rec.update(answers=parsed.answers, decisions=decisions)
        if self.mode == "on":
            return None
        return policy.to_shadow(parsed, decisions, ref=ref, status=status, consulted=consulted,
                                latency_ms=rec["latency_ms"])

    @staticmethod
    def _from_cache(hit):
        """(ParsedAnswers, model_served) from a cache row, or None for a miss or a row that does not parse."""
        try:
            data = hit["answers"]
            parsed = prompt_mod.ParsedAnswers(data["status"], dict(data["answers"]), tuple(data["unsure"]))
            return parsed, hit["model_served"]
        except (TypeError, KeyError, ValueError):
            return None

    # --- records ---------------------------------------------------------------

    def _next_ref(self) -> str:
        with self._lock:
            self._n += 1
            return f"j-{self.task_id}-{self._n}"

    def _blank(self, ref: str, consulted: bool) -> dict:
        haiku = (self._block.get("backends") or {}).get(BACKEND) or {}
        return {
            "role": "judge", "task": self.task_id, "ref": ref, "use": USE, "qset": self._qset, "qset_sha": self._qset_sha,
            "mode": self.mode, "backend": BACKEND, "effort": haiku.get("effort"),
            "model_req": haiku.get("model", guard.JUDGE_MODEL), "model_served": None, "status": None,
            "exit_code": None, "latency_ms": None, "duration_api_ms": None, "prompt_sha": prompt_mod.prompt_sha(2 if self._v2 else 1),
            "state_sha": None, "state_bytes": 0, "state_tokens_est": 0, "input_tokens": None,
            "output_tokens": None, "usd_equiv": None, "pts_est": 0, "redactions": {}, "truncated": False,
            "egress": self._block.get("egress", "excerpts"), "answers": {}, "decisions": {}, "applied": [],
            "overridden_by": {}, "budget_after": {}, "consulted": consulted,
        }

    def _bare(self, ref: str, rec: dict) -> shadow.ShadowAnswer:
        """An answer that carries only the status, for a call that gave no usable answer."""
        return shadow.ShadowAnswer("judge", self.mode, rec["status"], ref, rec["consulted"], None, None, None, None,
                                   (), None, None, (), rec["latency_ms"])

    def _finish(self, ref: str, status: str, answer, *, bare: bool = True) -> None:
        """Write the call's one record, unless join_shadow gave up on it first."""
        with self._lock:
            if ref in self._abandoned:
                return
            rec = self._recs[ref]
            rec["status"] = status
            if answer is None and bare:
                answer = self._bare(ref, rec)
            self._done.add(ref)
            if answer is not None:
                self._answer = answer
            self.log.append(rec)

    # --- the child process -------------------------------------------------------

    def _spawn(self, ref: str):
        """An exec_fn like runners.exec_child that remembers the call's process group so join_shadow can kill it."""
        def run(argv, cwd, env, timeout_s, raw_base):
            start = time.monotonic()
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True)
            pgid = proc.pid
            runners._children.add(pgid)
            with self._lock:
                self._pgids[ref] = pgid
                gone = ref in self._abandoned
            timed_out = False
            try:
                if gone:
                    _killpg(pgid)
                try:
                    out, err = proc.communicate(timeout=timeout_s * _scale())
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _killpg(pgid)
                    out, err = proc.communicate()
                _killpg(pgid)  # stragglers left in the group
            finally:
                runners._children.discard(pgid)
            return proc.returncode, _text(out), _text(err), time.monotonic() - start, timed_out
        return run


# --- the intake: judge, fallback chain, merge (spec §3.6) ---------------------------------------------

@dataclass
class Intake:
    """What `mr run` and `mr route` take from one intake: the merged decision and the parts of the route record."""
    decision: router.Decision
    outcome: JudgeOutcome
    overrides: tuple
    files: dict  # counts by src
    judge_absent: bool
    fallback: str | None
    invariant: bool
    judge: dict
    clf: object = None  # the fallback's classifier.ClassifyResult, when it made a call

    def route_parts(self, hints: dict) -> dict:
        return {
            "judge": dict(self.judge), "hints": dict(hints),
            "overrides": [dataclasses.asdict(o) for o in self.overrides], "files": dict(self.files),
            "judge_absent": self.judge_absent, "fallback": self.fallback, "invariant_violation": self.invariant,
        }


def override_line(o) -> str:
    conf = "-" if o.judge_conf is None else o.judge_conf
    quote = " ".join((o.evidence or "").split())
    return f'judge override: {o.field} {o.hint} -> {o.applied} (conf {conf}, "{quote}")'


def _clf_readings(out) -> dict | None:
    """Judge-shaped readings from a classifier answer; the merge treats them as stricter-only."""
    def reading(value) -> dict:
        return {"value": value, "conf": out.confidence, "evidence": None, "quote_found": False}

    try:
        return {"task_type": reading(out.task_type), "difficulty": reading(router._DIFF.index(out.difficulty)),
                "risk": reading(router._RISK.index(out.risk)), "load_bearing": reading(out.load_bearing),
                "needs_review": reading(out.needs_review)}
    except ValueError:
        return None


def _applied(decision: router.Decision) -> list[str]:
    fields = [f for f in _FIELDS if decision.sources.get(f) == "judge"]
    if any(e.get("src") == "judge" for k in ("change", "new") for e in decision.files.get(k, ())):
        fields.append("files")
    if decision.risk_evidence:
        fields.append("risk_tags")
    return fields


def files_by_src(files: dict) -> dict:
    counts: dict[str, int] = {}
    for key in ("change", "new"):
        for entry in files.get(key, ()):
            counts[entry["src"]] = counts.get(entry["src"], 0) + 1
    return counts


def run_intake(cfg, task, features, facts, rules: router.Decision, log, *, classifier_off: bool = False,
               exec_fn=None) -> Intake:
    """Ask the judge once, fall back (cache, one classifier call, rules) when it cannot answer, then merge.

    Never raises for a judge failure; the one judge record is written here, after the merge."""
    judge = Judge(cfg, "on", log, task_id=task.id, exec_fn=exec_fn)
    outcome = judge.decide_route(task, features, facts, rules)
    clf = None
    if outcome.status in _USABLE:
        readings, source = outcome.readings, "judge"
    else:
        clf = shadow.classify_fallback(cfg, task, features, log, classifier_off)
        usable = clf is not None and clf.status in ("ok", "cache_hit") and clf.out is not None
        readings, source = (_clf_readings(clf.out) if usable else None), "classifier"
    try:
        merged = merge_mod.merge(task, facts, rules, readings, cfg, source=source)
    except router.InvariantError:
        merged = None
    summary = {"status": outcome.status, "backend": BACKEND, "latency_ms": outcome.latency_ms,
               "qset": judge._qset, "ref": outcome.ref}
    if merged is None:
        judge.note_applied(outcome.ref, [], (), status="invariant_violation")
        return Intake(rules, outcome, (), {}, True, "rules", True, summary, clf)
    judge.note_applied(outcome.ref, _applied(merged.decision) if source == "judge" else [], merged.overrides)
    fallback = "cache" if source == "judge" and outcome.status == "cache_hit" else merged.fallback
    return Intake(merged.decision, outcome, merged.overrides, files_by_src(merged.decision.files),
                  merged.judge_absent, fallback, False, summary, clf)
