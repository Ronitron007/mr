"""Judge facade (mr-spec §5.5, §9.11): one shadow route call per ask, one judge record per call, cache hit or skip.

Shadow only: the answer is logged and handed back as a ShadowAnswer. Nothing here changes a routing decision.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from mr import facts as facts_mod
from mr import guard, runners, shadow
from mr.judge import breaker, budget, cache, client, policy
from mr.judge import prompt as prompt_mod
from mr.judge import questions as qmod
from mr.judge import state as state_mod

BACKEND = "haiku"
USE = "route"
QSET = "route.v1"
QSET_V2 = "route.v2"
_NO_CALL = frozenset({"guard_yield", "cli_unavailable"})  # the client reports these without running claude


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


class Judge:
    def __init__(self, cfg, mode: str, log, *, task_id: str, exec_fn=None):
        if mode not in ("shadow", "on"):
            raise ValueError(f"judge mode {mode!r}: expected shadow or on")
        self.cfg, self.log, self.task_id, self._exec = cfg, log, task_id, exec_fn
        self.mode = "shadow"  # "on" is not built: it runs as shadow
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
        allowed, reason, after = budget.charge(cfg, self.task_id, self.mode)
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
