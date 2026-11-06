"""Classifier client: the cheap second opinion on routing (plan §4.4, judge-spec §5.4). Nothing calls it yet."""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass

from . import config, redact, runners
from .shadow import ShadowAnswer

PROMPT_VERSION = "classify.v1"
_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "classify.v1.txt")
TASK_CAP = 4000
REASON_CAP = 120
REPAIR_LINE = "\n\nYour last reply was not valid. Reply with ONE line of minified JSON matching the schema only."

TASK_TYPES = ("code", "bugfix", "refactor", "test", "ios-native", "ui", "review", "plan", "explain", "docs")
DIFFICULTIES = ("trivial", "easy", "medium", "hard", "critical")
RISKS = ("low", "medium", "high")
RISK_TAGS = ("auth", "payments", "migration", "data-deletion", "native-config", "deps", "secrets", "none")
LEVELS = ("low", "medium", "high")
NOVELTIES = ("boilerplate", "mixed", "novel")
CONTEXTS = ("small", "medium", "large")
EFFORTS = ("low", "medium", "high", "xhigh")
_BOOLS = ("load_bearing", "high_judgment", "needs_worktree", "needs_review", "needs_clarification")


def _alt(items) -> str:
    return "|".join(items)


SCHEMA = (
    '{"task_type":"' + _alt(TASK_TYPES) + '","difficulty":"' + _alt(DIFFICULTIES) + '","risk":"' + _alt(RISKS)
    + '","risk_tags":["' + _alt(RISK_TAGS) + '"],"load_bearing":false,"high_judgment":false,"scope_files_est":1,'
    + '"ambiguity":"' + _alt(LEVELS) + '","novelty":"' + _alt(NOVELTIES) + '","context":"' + _alt(CONTEXTS)
    + '","model":"<allowed rung code or null>","effort":"' + _alt(EFFORTS) + '|null",'
    + '"needs_worktree":false,"needs_review":false,"needs_clarification":false,'
    + '"confidence":0.0,"reason":"<= 15 words"}'
)


@dataclass(frozen=True)
class ClassifierOut:
    task_type: str
    difficulty: str
    risk: str
    risk_tags: tuple[str, ...]
    load_bearing: bool
    high_judgment: bool
    scope_files_est: int
    ambiguity: str
    novelty: str
    context: str
    model: str | None
    effort: str | None
    needs_worktree: bool
    needs_review: bool
    needs_clarification: bool
    confidence: float
    reason: str


@dataclass(frozen=True)
class ClassifyResult:
    out: ClassifierOut | None
    status: str  # ok | cache_hit | invalid | timeout | cli_error | cli_unavailable | redaction_abort | refused_model
    calls: int
    latency_ms: int | None
    prompt_sha: str
    ref: str
    redactions: dict
    model: str = ""  # the model asked, so record() needs no config


def prompt_sha() -> str:
    with open(_TEMPLATE, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


def _objects(text: str):
    """Yield each top-level JSON object found in text, in order."""
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        yield obj
        i = text.find("{", end)


def _enum(d: dict, key: str, allowed) -> str | None:
    v = d.get(key)
    return v if isinstance(v, str) and v in allowed else None


def parse(text: str) -> ClassifierOut | None:
    """Strict reader: the last complete {...} object in text, every enum and type checked."""
    last = None
    for obj in _objects(text):
        last = obj
    if not isinstance(last, dict):
        return None
    d = last
    picks = {k: _enum(d, k, a) for k, a in (
        ("task_type", TASK_TYPES), ("difficulty", DIFFICULTIES), ("risk", RISKS), ("ambiguity", LEVELS),
        ("novelty", NOVELTIES), ("context", CONTEXTS))}
    if None in picks.values():
        return None
    tags = d.get("risk_tags")
    if not isinstance(tags, list) or any(not isinstance(t, str) or t not in RISK_TAGS for t in tags):
        return None
    flags = {k: d.get(k) for k in _BOOLS}
    if any(not isinstance(v, bool) for v in flags.values()):
        return None
    n = d.get("scope_files_est")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        return None
    model, effort = d.get("model"), d.get("effort")
    if not (model is None or isinstance(model, str)) or not (effort is None or effort in EFFORTS):
        return None
    conf, reason = d.get("confidence"), d.get("reason")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or math.isnan(conf):
        return None
    if not isinstance(reason, str):
        return None
    return ClassifierOut(
        risk_tags=tuple(dict.fromkeys(t for t in tags if t != "none")), scope_files_est=n, model=model,
        effort=effort, confidence=min(1.0, max(0.0, float(conf))), reason=reason[:REASON_CAP], **picks, **flags)


def allowed_codes(cfg) -> list[str]:
    """Rung codes the classifier may name: the config's codes, sorted, without S+A and K-* (opt-in only)."""
    return sorted(c for c in cfg.rungs if c != "S+A" and not c.startswith("K-"))


def build_prompt(task, features, cfg, task_text: str) -> str:
    vals = {
        "REPO": os.path.basename(task.repo.rstrip("/")), "SCHEMA": SCHEMA, "CODES": ",".join(allowed_codes(cfg)),
        "TASK": task_text, "FILES": json.dumps(list(features.files)),
        "RISKS": json.dumps(sorted(features.risk_tags)),
    }
    with open(_TEMPLATE, encoding="utf-8") as fh:
        return re.sub(r"\{\{(\w+)\}\}", lambda m: vals[m.group(1)], fh.read())


def _cli_ok(path: str) -> bool:
    found = path if os.path.isabs(path) else shutil.which(path)
    return bool(found) and os.path.isfile(found) and os.access(found, os.X_OK)


def _connect(cfg) -> sqlite3.Connection:
    path = os.path.join(cfg.paths.home, "state", "classifier-cache.sqlite")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS classifier_cache("
               "key TEXT PRIMARY KEY, created_at REAL, expires_at REAL, answers_json TEXT)")
    return db


def _cache_get(cfg, key: str, now: float) -> ClassifierOut | None:
    try:
        with contextlib.closing(_connect(cfg)) as db:
            row = db.execute("SELECT expires_at, answers_json FROM classifier_cache WHERE key=?", (key,)).fetchone()
            if row is None:
                return None
            if row[0] <= now:
                db.execute("DELETE FROM classifier_cache WHERE key=?", (key,))
                db.commit()
                return None
            return parse(row[1])
    except (sqlite3.Error, OSError):
        return None


def _cache_put(cfg, key: str, out: ClassifierOut, now: float) -> None:
    ttl = float((cfg.classifier or {}).get("cache_days", 7)) * 86400.0
    try:
        with contextlib.closing(_connect(cfg)) as db:
            db.execute("INSERT OR REPLACE INTO classifier_cache VALUES(?,?,?,?)",
                       (key, now, now + ttl, json.dumps(dataclasses.asdict(out))))
            db.commit()
    except (sqlite3.Error, OSError):
        pass


def classify(task, features, cfg, *, ref: str, exec_fn=None, now: float | None = None) -> ClassifyResult:
    block = cfg.classifier or {}
    model = block.get("model", "grok-4.7-low")
    sha = prompt_sha()
    counts: dict = {}

    def result(status, out=None, calls=0, latency=None):
        return ClassifyResult(out, status, calls, latency, sha, ref, counts, model)

    if config.is_anthropic(model):
        return result("refused_model")
    if not _cli_ok(cfg.paths.cursor_agent):
        return result("cli_unavailable")
    text, counts = redact.redact(task.text)
    if redact.too_many(counts):
        return result("redaction_abort")
    prompt = build_prompt(task, features, cfg, text[:TASK_CAP])
    clock = time.time() if now is None else now
    key = hashlib.sha256("|".join((model, PROMPT_VERSION, sha, task.fingerprint)).encode()).hexdigest()
    hit = _cache_get(cfg, key, clock)
    if hit is not None:
        return result("cache_hit", hit, 0, 0)

    workdir = os.path.join(cfg.paths.home, "classifier-workdir")
    os.makedirs(workdir, exist_ok=True)
    env = runners.child_env(False)
    env.pop("CURSOR_AUTH_TOKEN", None)
    run = exec_fn or runners.exec_child
    head = [cfg.paths.cursor_agent, "-p", "--output-format", "json", "--mode", "ask", "--model", model,
            "--trust", "--workspace", workdir]
    timeout_s = block.get("timeout_s", 30)
    wall = 0.0
    ms = 0
    for n, body in enumerate((prompt, prompt + REPAIR_LINE), 1):
        raw_base = os.path.join(cfg.paths.home, "raw", task.id, f"classifier-{n}")
        rc, stdout, _err, wall_s, timed_out = run(head + [body], workdir, env, timeout_s, raw_base)
        wall += wall_s
        ms = int(wall * 1000)
        if timed_out:
            return result("timeout", None, n, ms)
        env_obj = runners._envelope(stdout) if rc == 0 else None
        if env_obj is None or env_obj.get("is_error"):
            return result("cli_error", None, n, ms)
        reply = env_obj.get("result")
        out = parse(reply) if isinstance(reply, str) else None
        if out is not None:
            _cache_put(cfg, key, out, clock)
            return result("ok", out, n, ms)
    return result("invalid", None, 2, ms)


def _default_cfg():
    from .defaults import defaults
    return config.from_dict(defaults())


def record(res: ClassifyResult, task, mode: str, consulted: bool) -> dict:
    """Log record for a classifier call (judge-spec §7.4). Never holds the prompt or the task text."""
    return {
        "role": "classifier", "task": task.id, "ref": res.ref, "mode": mode,
        "model": res.model or _default_cfg().classifier["model"],
        "status": res.status, "consulted": consulted, "calls": res.calls, "latency_ms": res.latency_ms,
        "prompt_sha": res.prompt_sha, "redactions": dict(res.redactions),
        "answers": dataclasses.asdict(res.out) if res.out else None,
        "reason": "classifier_invalid" if res.status == "invalid" else None,
    }


def to_shadow(res: ClassifyResult, consulted: bool, cfg=None) -> ShadowAnswer:
    cfg = cfg or _default_cfg()
    out = res.out
    base = dict(source="classifier", mode="shadow", status=res.status, ref=res.ref, consulted=consulted,
                latency_ms=res.latency_ms)
    if out is None:
        return ShadowAnswer(route=None, type=None, difficulty=None, risk=None, risk_tags=(), confidence=None,
                            first_rung=None, unsure_fields=(), **base)
    route = "design" if out.load_bearing else "judgment" if out.high_judgment else "cascade"
    unsure = (tuple(f.name for f in dataclasses.fields(ClassifierOut))
              if out.confidence < cfg.classifier.get("min_conf", 0.6) else ())
    return ShadowAnswer(route=route, type=out.task_type, difficulty=out.difficulty, risk=out.risk,
                        risk_tags=out.risk_tags, confidence=out.confidence,
                        first_rung=out.model if out.model in allowed_codes(cfg) else None,
                        unsure_fields=unsure, **base)
