"""`mr run`: harness (signals, records, output) and the L0 single-rung pipeline (mr-spec §0.1, §0.2).

`--rung CODE --once`: one rung, then the isolation check, the patch, D and H, the records and
``state.json``. No escalation, no review, no planner, no reset. Without `--once` the harness
runs `loop.run_task` (the full ladder, L1).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import signal
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from mr import exitcodes as ec
from mr import (classifier, config, facts, hygiene, isolation, loop, patches, pipeline, prepass, redact, router,
                runners, scope, shadow, skillsync, state)
from mr.judge import Judge, judge as judge_mod
from mr.log import Log, now_ist
from mr.task import Params, TaskError, load_task, normalise

_L0 = "mr run --once needs --rung CODE"
DEPRECATED_FIELD = "files_named"
DEPRECATED_LINE = "files_named is deprecated; use hints.files"
_PROMPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "prompts", "implementer.v1.txt")
_OUTCOME = {
    ec.OK: "accepted", ec.NOT_ACCEPTED: "exhausted", ec.HUMAN: "needs_human",
    ec.UNSAFE_GIT: "unsafe", ec.DEFERRED: "deferred", ec.SIGINT: "interrupted",
    ec.SIGTERM: "interrupted", ec.INTERNAL: "internal",
}


class _Interrupted(BaseException):
    """Raised by the signal handler. BaseException so no ``except Exception`` swallows it."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


class _Ctx:
    """Everything the `finally:` block needs to write state.json and the task record."""

    def __init__(self, args, cfg, task, feats, decision, ladder: list[str], t0: float, started_at: str):
        self.args, self.cfg, self.task, self.feats, self.d = args, cfg, task, feats, decision
        self.rules = decision  # the rules decision; self.d is the merged one when the judge is on
        self.intake = None  # judge.Intake when the judge is on for this task
        self.route_parts: dict | None = None  # extra route-record keys (plain data)
        self.ladder = ladder
        self.rung = self.cur = ladder[0] if ladder else ""  # first rung; cur = the rung now running
        self.t0, self.started_at = t0, started_at
        self.log = Log(cfg.paths.home)
        self.lock = None
        self.exit, self.reason = ec.INTERNAL, "unfinished"
        self.iso = None
        self.high_risk = False  # path evidence of a high-risk change (pipeline.start)
        self.path_tags: frozenset = frozenset()
        self.branch = self.orig_branch = None
        self.reset_tree = None
        self.rungs: list[dict] = []
        self.patch_path = self.final_md = self.state_path = self.commit = None
        self.files: tuple[str, ...] = ()
        self.diff = {"files": 0, "add": 0, "del": 0}
        self.frees_hint = None
        # cursor_paid_rungs stays 0: the paid-Cursor allowance is retired, but router-logging-spec §cost keeps the key
        self.cost = {"claude_pts": 0, "usd_equiv": 0.0, "cursor_paid_rungs": 0,
                     "grok_rung_min": 0.0, "units": 0.0}
        self.counters = {"attempts": 0, "claude_rungs": 0, "opus_rungs": 0}  # loop.count_after
        self.checklist = None
        self.plan_rejected = False  # the plan was rejected twice and the task runs without one
        self.replans = self.n_plan = self.n_review = self.seq = 0
        self.shadow_judge = self.shadow_clf = None  # shadow.ShadowAnswer
        self.judge = None  # judge.Judge, set by _start_judge
        self.n_clf = 0  # classifier calls made
        self.clf_pool = self.clf_future = self.clf_skip = self.clf_ref = None  # see _start_classifier
        self.clf_consulted = False
        self.effort_up_used: list[str] = []
        self.locked: tuple[str, ...] = ()
        self.lockdoc = None
        self.base_set: frozenset = frozenset()
        self.prior: list[dict] = []  # complete attempts since the last (re-)plan, for classify
        self.infra = 0  # infra retries already made for the current rung
        self.in_rung = False  # a rung is running and its patch is not saved yet
        self.handling = False  # set once a signal is being handled or we are finalising

    @property
    def outcome(self) -> str:
        return _OUTCOME.get(self.exit, "unknown")

    @property
    def used(self) -> list[str]:
        """Rungs that actually ran (a guard-denied rung did not)."""
        return list(dict.fromkeys(r["rung"] for r in self.rungs if r["class"] != "guard_deny"))

    def stop(self, code: int, reason: str = "") -> int:
        self.exit, self.reason = code, reason
        return code

    def state_dict(self, next_: str) -> dict:
        return {
            "task": self.task.id, "repo": self.task.repo, "base": self.task.base,
            "route": self.d.route, "ladder": list(self.ladder), "isolation": self.iso,
            "reset_point": self.reset_tree, "checklist": self.checklist, "rungs": list(self.rungs),
            "used_rungs": self.used, "attempts": self.counters["attempts"],
            "claude_rungs": self.counters["claude_rungs"], "replans": self.replans, "next": next_,
            "outcome": self.outcome if next_ == "done" else None,
            "exit": self.exit if next_ == "done" else None,
            "reason": self.reason if next_ == "done" else None,
            "patch": self.patch_path, "final_md": self.final_md,
            "branch": self.branch, "orig_branch": self.orig_branch, "commit": self.commit,
        }

    def save_state(self, next_: str) -> str:
        self.state_path = state.write_state(self.cfg.paths.home, self.task.id, self.state_dict(next_))
        return self.state_path


def _say(msg: str) -> None:
    print(msg, file=sys.stderr)


def _params(args) -> Params:
    return Params(
        type=args.type, difficulty=args.difficulty, route=args.route, prefer=args.prefer,
        budget=getattr(args, "budget", None) or "normal",
        latency=getattr(args, "latency", None) or "normal",
        experiment=bool(getattr(args, "experiment", False)),
        judge=getattr(args, "judge", None), classifier=getattr(args, "classifier", None),
        yes=bool(getattr(args, "yes", False)), wait_lock_s=getattr(args, "wait_lock_s", 600),
        pin=tuple(getattr(args, "pin", None) or ()),
    )


# --- signals ------------------------------------------------------------------

def _scale() -> float:
    try:
        return float(os.environ.get("MR_TEST_TIME_SCALE") or 1)
    except ValueError:
        return 1.0


def _kill_runner_children() -> None:
    """TERM the runner children and reap them first, else the 5 s TERM wait sees zombies as alive."""
    groups = list(runners._children)
    for pg in groups:
        try:
            os.killpg(pg, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    _reap(groups, 1.0 * _scale())
    runners.kill_all_children()


def _kill_strays() -> None:
    """TERM, wait, KILL any child that leads its own session but is not a runner child.

    Hygiene commands run in their own session and are not in ``runners._children``.
    """
    me = os.getpid()
    groups: list[int] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="utf-8", errors="replace") as fh:
                rest = fh.read().rsplit(")", 1)[1].split()
            if rest[0] == "Z" or int(rest[1]) != me:
                continue
            if os.getpgid(int(name)) == int(name):
                groups.append(int(name))
        except (OSError, IndexError, ValueError):
            continue

    def sig(num: int) -> None:
        for pg in groups:
            try:
                os.killpg(pg, num)
            except (ProcessLookupError, PermissionError):
                pass

    sig(signal.SIGTERM)
    scale = _scale()
    _reap(groups, 1.0 * scale)
    deadline = time.monotonic() + runners.TERM_GRACE_S * scale
    while time.monotonic() < deadline and any(_alive(pg) for pg in groups):
        time.sleep(0.05)
    sig(signal.SIGKILL)


def _reap(pids: list[int], wait_s: float) -> None:
    """Reap our own dead children so their (zombie) groups stop looking alive to killpg(pg, 0)."""
    pending = set(pids)
    end = time.monotonic() + wait_s
    while pending and time.monotonic() < end:
        for pid in list(pending):
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pending.discard(pid)
                continue
            if done:
                pending.discard(pid)
        time.sleep(0.02)


def _alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _install_handlers(c: _Ctx) -> dict:
    def handler(signum, frame):
        if c.handling:
            return
        c.handling = True
        _kill_runner_children()
        _kill_strays()
        raise _Interrupted(ec.SIGINT if signum == signal.SIGINT else ec.SIGTERM)

    old: dict = {}
    try:
        for s in (signal.SIGINT, signal.SIGTERM):
            old[s] = signal.signal(s, handler)
    except ValueError:  # not the main thread
        pass
    return old


def _restore_handlers(old: dict) -> None:
    for s, h in old.items():
        try:
            signal.signal(s, h)
        except (ValueError, TypeError):
            pass


# --- the pipeline ---------------------------------------------------------------

def run(args) -> int:
    t0, started_at = time.time(), now_ist()
    if args.once and not args.rung:
        _say(_L0)
        return ec.USAGE
    if getattr(args, "dry_run", False):
        _say("mr run --dry-run is not built; use `mr route --explain`")
        return ec.USAGE
    cfg = config.load(args.config)
    if args.rung and args.rung not in cfg.rungs:
        _say(f"unknown rung {args.rung!r}; known: {', '.join(cfg.rungs)}")
        return ec.USAGE
    try:
        task = load_task(args.task, args.task_file, _params(args), args.repo or os.getcwd(),
                         home=cfg.paths.home)
        found = facts.try_build(task.text, task.repo, task.hint_files)
        feats = prepass.extract(task, task.repo,
                                router.decision_files(router.files_from_facts(found, task.hint_files)))
        rules = decision = router.decide(task, feats, cfg, facts=found)
    except (TaskError, router.RouteError) as exc:
        _say(str(exc))
        return ec.USAGE
    if DEPRECATED_FIELD in task.deprecated:
        _say(DEPRECATED_LINE)
    intake, parts = _intake(args, cfg, task, feats, rules, found)
    if intake is not None:
        decision = router.map_design(intake.decision, cfg, task.params)
    if router.MAPPED in decision.reasons:
        _say(f"{router.MAPPED}: planned by an Opus planner and reviewed at S+ at least")
    if args.once:
        ladder = [args.rung]
    else:  # --rung X without --once puts X first, like --prefer
        ladder = ([args.rung] if args.rung else []) + [r for r in decision.ladder if r != args.rung]
    c = _Ctx(args, cfg, task, feats, decision, ladder, t0, started_at)
    c.facts, c.rules, c.intake, c.route_parts = found, rules, intake, parts
    if intake is not None and intake.clf is not None:
        c.n_clf = intake.clf.calls
    _start_classifier(c)
    _start_judge(c)
    old = _install_handlers(c)
    try:
        try:
            try:
                _body(c) if args.once else loop.run_task(c)
            except _Interrupted as e:
                c.stop(e.code, "interrupted by " + ("SIGINT" if e.code == ec.SIGINT else "SIGTERM"))
            except Exception as e:
                _say(traceback.format_exc())
                c.stop(ec.INTERNAL, f"internal error: {type(e).__name__}: {e}")
        except _Interrupted as e:  # signal landed while handling the exception above
            c.stop(e.code, "interrupted by " + ("SIGINT" if e.code == ec.SIGINT else "SIGTERM"))
        finally:
            c.handling = True  # no more interruptions while we write the records
            try:
                _finalize(c)
            finally:
                if c.lock is not None:
                    try:
                        c.lock.release()
                    except OSError as e:
                        _say(f"could not release writer lock: {e}")
    finally:
        _restore_handlers(old)
        # after _finalize and the lock release, never at run start: a record that started while
        # agents edit could commit half-done work. This one call also covers edits made before the run.
        skillsync.notify("run-end")
    _output(c)
    return c.exit


def _intake(args, cfg, task, feats, rules, found):
    """(judge.Intake or None, extra route-record keys). With the judge on this asks it once and merges."""
    flag = getattr(args, "judge", None)
    if flag == "off":
        _say("judge off by flag (debug)")
        return None, {"judge_off_by_flag": True}
    if shadow.judge_mode(flag, cfg.judge or {}) != "on":
        return None, None
    usable = found if isinstance(found, facts.Facts) else facts.Facts((), {}, (), (), (), {}, 0)
    intake = judge_mod.run_intake(cfg, task, feats, usable, rules, Log(cfg.paths.home),
                                  classifier_off=getattr(args, "classifier", None) == "off")
    for o in intake.overrides:
        _say(judge_mod.override_line(o))
    hints = {k: v for k, v in (("type", task.params.type), ("difficulty", task.params.difficulty),
                               ("route", task.params.route)) if v is not None}
    return intake, intake.route_parts(hints)


def _clf_result(c: _Ctx, status: str, calls: int):
    return classifier.ClassifyResult(None, status, calls, None, classifier.prompt_sha(), c.clf_ref, {},
                                     (c.cfg.classifier or {}).get("model", ""))


def _start_classifier(c: _Ctx) -> None:
    """Shadow classifier: decide whether it runs and start it in a background thread. It never acts."""
    if getattr(c, "intake", None) is not None:  # the judge is on: only its fallback may call the classifier
        return
    block = c.cfg.classifier or {}
    if shadow.effective_mode(getattr(c.args, "classifier", None), block) != "shadow":
        return
    task = c.task
    c.clf_ref = f"c-{task.id}-1"
    run_it, c.clf_consulted = shadow.should_run(task.params, c.feats.confident, task.fingerprint,
                                                block.get("sample_n", 3))
    if not run_it:
        c.clf_skip = _clf_result(c, "skipped_confident", 0)
        c.shadow_clf = classifier.to_shadow(c.clf_skip, False, c.cfg)
        return
    c.clf_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mr-classifier")
    c.clf_future = c.clf_pool.submit(lambda: classifier.classify(task, c.feats, c.cfg, ref=c.clf_ref))
    c.shadow_clf = classifier.to_shadow(_clf_result(c, "pending", 0), c.clf_consulted, c.cfg)


def _join_classifier(c: _Ctx) -> None:
    """Log the shadow classifier's record before the task record. Every failure is logged and ignored."""
    try:
        if c.clf_skip is not None:
            res = c.clf_skip
        elif c.clf_future is None:
            return
        else:
            try:
                res = c.clf_future.result(timeout=(c.cfg.classifier or {}).get("join_timeout_s", 75))
            except FutureTimeout:
                res = _clf_result(c, "timeout", 1)
                runners.kill_all_children()  # only the classifier child is left at task end
            except Exception:
                res = _clf_result(c, "cli_error", 1)
        c.n_clf = res.calls
        c.shadow_clf = classifier.to_shadow(res, c.clf_consulted, c.cfg)
        c.log.append(classifier.record(res, c.task, "shadow", c.clf_consulted))
    except Exception as e:
        _say(f"could not log the classifier: {e}")
    finally:
        if c.clf_pool is not None:
            c.clf_pool.shutdown(wait=False, cancel_futures=True)


def _start_judge(c: _Ctx) -> None:
    """Shadow judge: decide whether it runs and start the route call in the background. It never acts."""
    block = c.cfg.judge or {}
    task = c.task
    try:
        if shadow.judge_mode(getattr(c.args, "judge", None), block) != "shadow":
            return
        c.judge = Judge(c.cfg, "shadow", c.log, task_id=task.id)
        run_it, consulted = shadow.should_run(task.params, c.feats.confident, task.fingerprint,
                                              block.get("sample_n", 3))
        if run_it:
            given = c.facts if isinstance(c.facts, facts.Facts) else None
            ref, status = c.judge.ask_route(task, c.feats, consulted, facts=given), "pending"
        else:
            ref, status = c.judge.skip_route("skipped_confident", False), "skipped_confident"
        c.shadow_judge = shadow.ShadowAnswer("judge", "shadow", status, ref, consulted, None, None, None, None,
                                             (), None, None, (), None)
    except Exception as e:
        _say(f"judge shadow failed: {type(e).__name__}: {e}")
        if c.judge is not None:
            c.judge.skip_route("cli_error", False)
        c.judge = c.shadow_judge = None


def _join_judge(c: _Ctx) -> None:
    """Wait for the shadow judge before the task record. Every failure is logged and ignored."""
    if c.judge is None:
        return
    try:
        c.judge.join_shadow(((c.cfg.judge or {}).get("timeouts_ms") or {}).get("decision_point", 15000) / 1000)
        c.shadow_judge = c.judge.route_answer() or c.shadow_judge
    except Exception as e:
        _say(f"could not join the judge: {e}")


def _body(c: _Ctx) -> int:
    cfg, task = c.cfg, c.task
    home, repo = cfg.paths.home, task.repo
    if (code := pipeline.start(c)) is not None:
        return code
    d = c.d

    c.reset_tree = patches.take_reset_tree(repo)
    c.save_state("rung")
    base_set = hygiene.base_failures(repo, cfg, os.path.basename(repo), c.reset_tree)

    rung = cfg.rungs[c.rung]
    with open(_PROMPT, encoding="utf-8") as fh:
        template = fh.read()
    scope_text = "\n".join(f"- {p}" for p in router.decision_files(d)) or pipeline.NO_FILES
    prompt = template.format(goal=task.text, scope=scope_text, locked_files="(none)",
                             handoff="(first attempt)", task_id=task.id) + pipeline.deps_line(c)
    before = isolation.snapshot(repo)
    attempt_started = now_ist()
    rr, guarded, droid_try = loop.run_rung(c, c.rung, prompt, 1)
    if loop.denied(guarded, rr):
        c.frees_hint = guarded.guard.frees_hint or "≈ in 60 min"
        reason = guarded.guard.reason if rr is None else "wrapper refused (exit 75)"
        return c.stop(ec.DEFERRED, f"claude rung {c.rung} deferred: {reason}; no fallback in --once")

    after = isolation.snapshot(repo)
    viol = isolation.violation(before, after, allow_new_branch=c.branch)
    patch = patches.cumulative_diff(repo, c.reset_tree)
    c.patch_path = patches.save_patch(home, task.id, 1, rung.model, patch)
    c.diff = patches.numstat(patch)
    infra = rr.is_error or rr.timed_out
    empty = not patch.strip()
    sig = {"H": None, "D": None, "A": None, "C": None}
    cls, why, warnings = None, "", []
    if viol:
        cls = "isolation_violation"
        why = (f"isolation violation: {viol}; in {c.iso} mr cannot restore refs, so the rung's "
               f"change to the repo was left as is (HEAD {after.head}, branch {after.branch}); "
               f"the tree edits are saved in {c.patch_path}")
    else:
        trusted, judge_only = router.scope_files(d)
        dres = scope.scope(patch, checklist=None, difficulty=d.difficulty, files_named=tuple(trusted),
                           judge_files=tuple(judge_only))
        c.files, warnings = dres.files, list(dres.warnings)
        sig["D"] = dres.ok
        if not infra and dres.ok and not empty:
            hres = hygiene.hygiene(repo, cfg, base_set, os.path.basename(repo))
            sig["H"] = hres.ok
        else:
            hres = None
        if infra:
            cls = "infra"
            why = "infra: " + ("timeout" if rr.timed_out else (rr.stderr_tail.strip()[-200:] or f"rc={rr.rc}"))
        elif not dres.ok:
            cls, why = "off_track", "D failed: " + "; ".join(dres.reasons)
        elif empty:
            cls, why = "skipped", "no diff: the rung changed nothing"
        elif not hres.ok:
            cls, why = "skipped", "H failed: " + "; ".join(hres.new_failures)

    c.counters = loop.count_after(c.counters, c.rung, kind="implementer")
    pipeline.add_cost(c.cost, rr, guarded)
    c.rungs.append({"rung": c.rung, "model": rung.model, "class": cls, "signals": dict(sig),
                    "patch": c.patch_path})
    c.log.append(loop.tag_record(pipeline.attempt_record(c, rung, c.rung, rr, guarded, 1, attempt_started, sig, cls,
                                                    d_warnings=warnings), rr, droid_try))
    c.save_state("finish")

    if viol:
        return c.stop(ec.UNSAFE_GIT, why)
    if cls is not None:
        return c.stop(ec.NOT_ACCEPTED, why)
    if c.iso == "W5":
        err = pipeline.commit_w5(c)
        if err:
            return c.stop(ec.UNSAFE_GIT, err)
    return c.stop(ec.OK, "accepted")


# --- records --------------------------------------------------------------------

def _title(text: str) -> str | None:
    """First non-empty line of the task text, trimmed and cut to 120 characters."""
    if not text:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return None


def _caller() -> str | None:
    """Who started this run: MR_CALLER, else the parent process name. Never raises."""
    try:
        raw = os.environ.get("MR_CALLER")
        if isinstance(raw, str) and raw:
            return raw[:60]
        with open("/proc/%s/comm" % os.getppid(), encoding="utf-8") as fh:
            name = fh.read().strip()
        return name[:60] or None
    except Exception:
        return None


def _feature_hash(feats) -> str | None:
    """First 16 hex of sha256 over the prepass Features as canonical JSON, or None without features."""
    if feats is None:
        return None
    doc = {k: sorted(v) if isinstance(v, (set, frozenset)) else v for k, v in dataclasses.asdict(feats).items()}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]


def _intake_counts(c: _Ctx) -> dict:
    if getattr(c, "intake", None) is None:
        return {}
    return {"judge_overrides": len(c.intake.overrides)}


def _deprecated(c: _Ctx) -> dict:
    return {"deprecated_field": DEPRECATED_FIELD} if DEPRECATED_FIELD in getattr(c.task, "deprecated", ()) else {}


def _task_record(c: _Ctx, ended: str) -> dict:
    d, task, last = c.d, c.task, (c.rungs[-1] if c.rungs else None)
    esc = {k: sum(r["class"] == k for r in c.rungs) for k in (
        "skipped", "tried_wrong", "spec_suspect", "off_track", "safety_violation", "scope_rejected", "scope_repair")}
    ran = c.used
    dirs: list[str] = []
    for f in c.files:
        top = "/".join(f.split("/")[:-1][:2])
        if top and top not in dirs:
            dirs.append(top)
    return {
        "role": "task", "task": task.id, "started_at": c.started_at, "ended_at": ended,
        "wall_s": int(time.time() - c.t0), "repo": os.path.basename(task.repo),
        "base": task.base[:12], "text_sha": hashlib.sha256(normalise(task.text).encode()).hexdigest()[:16],
        "title": _title(task.text), "caller": _caller(), "type": d.type, "difficulty": d.difficulty, "risk": d.risk,
        "risk_tags": sorted(d.risk_tags), "route": d.route, "route_src": d.sources.get("route"),
        "shadow": {"judge": shadow.task_summary(getattr(c, "shadow_judge", None)),
                   "classifier": shadow.task_summary(getattr(c, "shadow_clf", None))},
        "text_redacted": redact.redact(task.text)[0][:4000],
        "feature_hash": _feature_hash(getattr(c, "feats", None)),
        "ladder": list(c.ladder),
        "first_rung": c.rung, "final_rung": ran[-1] if ran else c.rung,
        "attempts": {"impl": c.counters["attempts"], "review": c.n_review, "plan": c.n_plan,
                     "classifier": getattr(c, "n_clf", 0)},
        "esc": esc,
        "outcome": c.outcome, "exit": c.exit, "verified": False,
        "final_signals": dict(last["signals"]) if last else None, "cost": dict(c.cost),
        "ttg_s": int(time.time() - c.t0) if c.exit == ec.OK else None,
        "human": {"questions": 0, "exit3": int(c.exit == ec.HUMAN), "marks": 0, "edits_before_merge": None},
        "spec": None, "advisor": None, "files_n": c.diff["files"], "dirs": dirs[:5],
        "commit": c.commit[:12] if c.commit else None,
        "raw_dir": os.path.join(c.cfg.paths.home, "raw", task.id) + "/",
        "label": {"state": "provisional"}, "unknown": [],
        "files_src": judge_mod.files_by_src(getattr(d, "files", None) or {}),
        **({"plan_rejected": True} if getattr(c, "plan_rejected", False) else {}),
        **_deprecated(c),
        **_intake_counts(c),
    }


def _finalize(c: _Ctx) -> None:
    """Runs in a `finally:`: salvage the patch, FINAL.md, state.json, task record."""
    home, task = c.cfg.paths.home, c.task
    if (c.exit in (ec.SIGINT, ec.SIGTERM, ec.INTERNAL) and c.reset_tree
            and (not c.patch_path or c.in_rung)):
        try:
            patch = patches.cumulative_diff(task.repo, c.reset_tree)
            c.patch_path = patches.save_patch(home, task.id, len(c.rungs) + 1,
                                              c.cfg.rungs[c.cur].model, patch)
            c.diff = patches.numstat(patch)
        except Exception as e:
            _say(f"could not save the patch: {e}")
    if c.exit != ec.OK and c.reset_tree:
        try:
            c.final_md = patches.write_final_md(
                home, task.id, outcome=c.outcome, exit_code=c.exit, reason=c.reason,
                rungs=list(c.rungs), patch=c.patch_path, reset_tree=c.reset_tree, base=task.base)
        except Exception as e:
            _say(f"could not write FINAL.md: {e}")
    try:
        c.save_state("done")
    except Exception as e:
        _say(f"could not write state.json: {e}")
    _join_classifier(c)
    _join_judge(c)
    c.log.append(_task_record(c, now_ist()))


# --- output ---------------------------------------------------------------------

def _doc(c: _Ctx) -> dict:
    doc = {
        "task": c.task.id, "exit": c.exit, "outcome": c.outcome, "route": c.d.route,
        "ladder": list(c.ladder),
        "rungs": [{"rung": r["rung"], "class": r["class"], "signals": r["signals"]} for r in c.rungs],
        "patch": c.patch_path, "final_md": c.final_md, "state": c.state_path, "reason": c.reason,
    }
    if c.exit == ec.DEFERRED:
        doc["frees_hint"] = c.frees_hint
    if c.branch:
        doc.update(branch=c.branch, orig_branch=c.orig_branch, commit=c.commit)
    return doc


def _output(c: _Ctx) -> None:
    if c.exit != ec.OK:
        _say(f"{c.task.id}: {c.outcome} (exit {c.exit}): {c.reason}")
        if c.frees_hint:
            _say(f"claude budget frees: {c.frees_hint}")
    if c.final_md:
        _say(f"FINAL.md: {c.final_md}")
    if c.orig_branch:
        _say(f"original branch: {c.orig_branch} (work is on {c.branch})")
    if c.exit == ec.OK and c.high_risk and c.branch:
        _say(f"review and merge by hand: branch {c.branch} is not merged")
    if getattr(c.args, "json", False):
        print(json.dumps(_doc(c), ensure_ascii=False))
        return
    last = c.rungs[-1] if c.rungs else None
    lines = [f"{c.task.id}: {c.outcome} (exit {c.exit})", f"route: {c.d.route}  isolation: {c.iso or '-'}"]
    if last:
        sig = " ".join(f"{k}={v}" for k, v in last["signals"].items() if v is not None) or "-"
        lines.append(f"rung: {last['rung']}  class: {last['class'] or '-'}  signals: {sig}")
    lines.append(f"patch: {c.patch_path or '-'}")
    lines.append(f"state: {c.state_path or '-'}")
    print("\n".join(lines))
