"""S+A advisor contract: decide, injection, relay, audit, fallbacks, demotion (advisor-consult-spec)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from mr import config, prepass

_PROMPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
INJECTION_VERSION = "adv.v2"
INJECT_FILE = os.path.join(_PROMPTS, "advisor_inject.v2.txt")
SYSTEM_FILE = os.path.join(_PROMPTS, "advisor_system.v2.txt")

IST = timezone(timedelta(hours=5, minutes=30))
ADVISOR_MODEL = "claude-opus-5-5"
SONNET_MODEL = "claude-sonnet-5-5"
USD_IN, USD_OUT, OUT_TOK_EST = 4e-6, 20e-6, 1900  # §4.3, rates derived from the trial
S_PLUS_PTS = 6
_LIM = config.defaults()["claude_limits"]
GUARD = {"hour_max": _LIM.get("hour_max"), "five_hour_max": _LIM["five_hour_max"]}  # claude_limits
DEFAULTS: dict = config.ADVISOR_DEFAULTS  # §11
ERROR_CODES = frozenset({"max_uses_exceeded", "too_many_requests", "overloaded", "prompt_too_long",
                         "execution_time_exceeded", "model_not_found", "unavailable"})
VERDICTS = ("approve", "concern", "block")
_NO_TYPES = frozenset({"docs", "explain", "test", "review"})
_WHERE = re.compile(r"^[\w./-]+:\d+$")
RELAY_MAX_CHARS = 2000
TAG_WINDOW = 50
HIGH_RISK_PATHS_MAX = 12


def _cfg(cfg_advisor: dict | None, key: str):
    val = (cfg_advisor or {}).get(key)
    if isinstance(DEFAULTS[key], dict):
        return {**DEFAULTS[key], **(val if isinstance(val, dict) else {})}
    return DEFAULTS[key] if val is None else val


def canon_mode(value) -> str:
    """YAML 1.1 reads bare on/off as booleans."""
    return "on" if value is True else "off" if value is False else value


# --- §3.1 sf_tags -------------------------------------------------------------

def _glob_rx(pat: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


_SF_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("state-machine", ("status", "state machine", "transition", "reducer", r"terminal (state|status)",
                       "regress", "lifecycle"),
     ("**/*reducer*", "**/*state*", "**/*store*", "src/lib/chat/agent-steps.ts")),
    ("ordering", ("out of order", r"late (event|update)", "race", "concurren", "stale", "debounce", "retry",
                  "reorder"), ()),
    ("event-stream", ("event", "stream", "SSE", "websocket", "realtime", "subscription", "webhook", "queue"),
     ("**/realtime/**", "**/*stream*", "**/*events*")),
    ("idempotency", ("idempoten", "dedup", "exactly once", r"double (charge|submit|send)", "duplicate"), ()),
    ("money", ("checkout", "payment", "price", "amount", "currency", "refund", "invoice", "IAP", "purchase",
               "stripe"),
     ("**/checkout/**", "**/payments/**", "**/billing/**")),
    ("agent-flow", ("agent step", "tool_use", "agent loop", "checkout agent", "multi-step", "tool call"),
     ("src/lib/chat/**", "**/agent*/**")),
)
_SF_RES = tuple(
    (tag, re.compile(r"\b(?:" + "|".join(texts) + ")", re.IGNORECASE), tuple(_glob_rx(p) for p in paths))
    for tag, texts, paths in _SF_RULES
)


def sf_tags(text: str, paths) -> frozenset[str]:
    """§3.1: silent-failure tags (case-insensitive; text or path hit) plus the prepass risk tags."""
    text = text or ""
    paths = tuple(p[2:] if p.startswith("./") else p for p in (paths or ()))
    tags = {tag for tag, text_rx, path_rxs in _SF_RES
            if text_rx.search(text) or any(rx.match(p) for rx in path_rxs for p in paths)}
    return frozenset(tags | prepass.risk_tags(text, paths))


# --- §3.2 / §4 decide ---------------------------------------------------------

@dataclass(frozen=True)
class AdvisorDecision:
    consult: bool
    slots: tuple[str, ...]
    row: str
    reasons: tuple[str, ...]
    est_consult_tokens: int
    est_consult_usd: float
    gate: str  # ok | budget | hourly_cap | transcript | mode | unsupported | task_cap
    run_rung: str | None


def est_consult(task_prompt_tokens: int, handoff_tokens: int, scope_bytes: int, diff_tokens: int, slot: str,
                *, base: int = 45000) -> tuple[int, float]:
    """§4.3 consult transcript estimate (base 45k until the EWMA has 10 samples)."""
    tokens = base + task_prompt_tokens + handoff_tokens + round(scope_bytes / 3.5)
    if slot == "final":
        tokens += 2 * diff_tokens + 6000
    return int(tokens), _usd(int(tokens))


def _usd(tokens: int) -> float:
    return round(tokens * USD_IN + OUT_TOK_EST * USD_OUT, 6)


def _tried_wrong(hist) -> list[dict]:
    return [h for h in hist or () if isinstance(h, dict) and h.get("cls") == "tried_wrong"]


def _table(inp: dict) -> tuple[str, tuple[str, ...], str]:
    """First match wins: N0..N6, F1, Y1..Y5, N7. Returns (row, slots, reason)."""
    mode = canon_mode(inp.get("mode", "on"))
    flag, rung, route = inp.get("flag", "auto"), inp.get("rung"), inp.get("route")
    kind, typ, diff = inp.get("item_kind"), inp.get("type"), inp.get("difficulty")
    risk, amb, lb = inp.get("risk"), inp.get("ambiguity"), bool(inp.get("load_bearing"))
    sf = bool(inp.get("sf_tags"))
    wrong = _tried_wrong(inp.get("hist"))
    if mode == "off" or flag == "off" or inp.get("budget") in ("free", "low"):
        return "N0", (), "advisor off (mode, --advisor off or --budget free|low)"
    if rung != "S+":
        return "N1", (), f"rung {rung} is not Max-path S+"
    if route == "design":
        return "N2", (), "design-route spec item: Opus already planned it"
    if kind == "mechanical" or typ in _NO_TYPES:
        return "N3", (), "mechanical item or docs/explain/test/review type"
    if route == "judgment" and not sf and not lb:
        return "N4", (), "judgment route taste work (no sf_tag, not load-bearing)"
    if diff in ("trivial", "easy"):
        return "N5", (), f"difficulty {diff}"
    if diff == "medium" and not wrong and risk != "high":
        return "N6", (), "routine medium work"
    if flag == "force":
        return "F1", (inp.get("slot_flag") or "early",), "--advisor force"
    if diff == "critical" and (sf or risk == "high"):
        two = inp.get("consults_flag") == 2
        return "Y1", ("early", "final") if two else ("early",), "critical with sf_tags or high risk"
    if diff == "hard" and lb:
        return "Y2", ("early",), "hard and load-bearing"
    if diff == "hard" and sf:
        return "Y3", ("final",), "hard with sf_tags"
    if diff in ("hard", "critical") and amb == "high":
        return "Y4", ("early",), "hard/critical with high ambiguity off the design route"
    if (diff in ("medium", "hard", "critical") and len(wrong) == 1
            and wrong[0].get("model") != SONNET_MODEL and (sf or risk in ("medium", "high"))):
        return "Y5", ("early",), f"one tried_wrong by {wrong[0].get('model')} on {wrong[0].get('failing')}"
    return "N7", (), "eligible_no_trigger"


def _fits(guard: dict, pts: float) -> bool:
    """G1: same rule as the rung guard, with ``pts`` the advisor reservation (S+A est)."""
    g = {**GUARD, **{k: v for k, v in guard.items() if k in GUARD}}
    hour_ok = (g["hour_max"] is None
               or (guard.get("hour") or 0) + guard.get("reserved", 0) + pts <= g["hour_max"])
    return hour_ok and guard.get("five_hour_pct", 0) + pts <= g["five_hour_max"]


def decide(inp: dict, cfg_advisor: dict | None) -> AdvisorDecision:
    """§3.2 table, then gates G1, G3, G4, G5, G6, G7 in order (docs/p0/ADVISOR.md conventions)."""
    row, slots, why = _table(inp)
    est = inp.get("est_consult_tokens") or {}
    rung = inp.get("rung")

    def out(consult, final_slots, gate, run_rung, extra=()):
        priced = final_slots or slots
        tokens = int(est.get(priced[0], 0)) if priced else 0
        usd = round(sum(_usd(int(est.get(s, 0))) for s in priced), 6) if priced else 0.0
        return AdvisorDecision(consult, tuple(final_slots), row, (f"{row}: {why}",) + tuple(extra),
                               tokens, usd, gate, run_rung)

    if not slots:
        return out(False, (), "ok", rung)
    guard = inp.get("guard") or {}
    reserve = _cfg(cfg_advisor, "reserve_pts")
    if not _fits(guard, reserve):
        return out(False, (), "budget", "S+" if _fits(guard, S_PLUS_PTS) else None,
                   (f"G1: S+A reservation {reserve} pts does not fit",))
    if inp.get("sa_used_in_task"):
        return out(False, (), "task_cap", "S+", ("G3: S+A already used in this task",))
    if (inp.get("adv_hour", 0) >= _cfg(cfg_advisor, "per_hour_box")
            or inp.get("adv_day", 0) >= _cfg(cfg_advisor, "per_day_box")):
        return out(False, (), "hourly_cap", "S+", ("G4: box-wide advisor cap reached",))
    caps = _cfg(cfg_advisor, "transcript_cap_tokens")

    def fits(slot):
        return slot in est and est[slot] <= caps.get(slot, 0)

    kept: list[str] = []
    for slot in slots:
        if fits(slot):
            kept.append(slot)
        elif slot == "final" and "early" not in slots and "early" not in kept and fits("early"):
            kept.append("early")
    notes = () if kept == list(slots) else (f"G5: slots {list(slots)} -> {kept}",)
    if not kept:
        return out(False, (), "transcript", "S+", ("G5: consult transcript over cap",))
    if not inp.get("env_ok", True):
        return out(False, (), "unsupported", "S+", ("G6: advisor env vars set",))
    mode = canon_mode(inp.get("mode", _cfg(cfg_advisor, "mode")))
    rows_off = inp.get("rows_off", _cfg(cfg_advisor, "rows_off")) or ()
    if mode == "shadow" or inp.get("demoted") or inp.get("breaker") or row in rows_off:
        return out(False, (), "mode", "S+", ("G7: shadow, demoted, breaker or row off",))
    return out(True, tuple(kept), "ok", "S+A", notes)


# --- §5.2 / §5.3 prompts ------------------------------------------------------

def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def injection_sha() -> str:
    return f"{INJECTION_VERSION}:{hashlib.sha256(_read(INJECT_FILE).encode()).hexdigest()}"


def system_prompt() -> str:
    """The --append-system-prompt relay instruction (adv.v2)."""
    return _read(SYSTEM_FILE).rstrip("\n")


def build_injection(dec: AdvisorDecision, *, decision_text: str, focus: str, high_risk_paths) -> str:
    """Render the user-prompt block appended after the spec excerpt or handoff."""
    if not dec.consult or not dec.slots:
        raise ValueError("build_injection: decision has no consult")
    lines = []
    for slot in dec.slots:
        if slot == "early":
            lines.append("- Before your first Edit/Write, once you've read the relevant files and are about to "
                         f"commit to an approach on: {decision_text}.")
        elif slot == "final":
            paths = ", ".join(list(high_risk_paths or ())[:HIGH_RISK_PATHS_MAX]) or "(none listed)"
            lines.append(f"- After your tests pass, if your diff touches any of: {paths}.")
        else:
            raise ValueError(f"build_injection: unknown slot {slot!r}")
    text = _read(INJECT_FILE)
    for key, val in (("{SLOT_LINES}", "\n  ".join(lines)), ("{N}", str(len(dec.slots))), ("{FOCUS}", focus)):
        text = text.replace(key, val)
    return text


# --- §6.1 relay ---------------------------------------------------------------

def _tagged(text: str, tag: str) -> str | None:
    """mr-spec §7.3 rule: last line starting with '<tag> ' within the last 50 lines."""
    prefix, found = f"{tag} ", None
    for line in (text or "").splitlines()[-TAG_WINDOW:]:
        if line.startswith(prefix):
            found = line
    return found


def _tagged_json(text: str, tag: str) -> dict | None:
    line = _tagged(text, tag)
    if line is None:
        return None
    try:
        data = json.loads(line[len(tag) + 1:])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _num(val) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _where_ok(where, tree: str | None) -> bool:
    if not isinstance(where, str) or not _WHERE.match(where):
        return False
    path = where.rsplit(":", 1)[0]
    if tree is None:
        return True
    if os.path.isabs(path) or ".." in path.split("/"):
        return False
    return os.path.isfile(os.path.join(tree, path))


def parse_relay(final_text: str, tree: str | None) -> tuple[str, dict | None]:
    """§6.1: ('ok', verdict) | ('missing', None) | ('invalid', None)."""
    line = _tagged(final_text, "MR_ADVISOR")
    if line is None:
        return "missing", None
    if len(line) > RELAY_MAX_CHARS:
        return "invalid", None
    try:
        data = json.loads(line[len("MR_ADVISOR "):])
    except ValueError:
        return "invalid", None
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
        return "invalid", None
    risks = data.get("risks", [])
    if not isinstance(risks, list) or len(risks) > 3:
        return "invalid", None
    for risk in risks:
        if not isinstance(risk, dict) or not _where_ok(risk.get("where"), tree):
            return "invalid", None
        if "severity" in risk and risk["severity"] not in ("low", "med", "high"):
            return "invalid", None
    conf = data.get("confidence")
    if not _num(conf) or not 0 <= conf <= 1:
        return "invalid", None
    return "ok", data


# --- §8 audit -----------------------------------------------------------------

@dataclass(frozen=True)
class AdvisorAudit:
    consults: int
    consult_ok: int
    consult_err: int
    advisor_in_tok: int | None
    advisor_out_tok: int | None
    advisor_usd: float | None
    sonnet_usd: float | None
    model_drift: bool
    relay_status: str  # ok | missing | invalid | fabricated
    verdict: dict | None
    violations: tuple[str, ...]
    consult_at_turn: int | None
    consult_latency_s: float | None
    events: tuple[str, ...]  # §7: consult_failed:<code>, advisor_slow, advisor_stall, consult_not_made
    error_codes: tuple[str, ...] = field(default=())


def _ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _events(stream_path: str) -> list[dict]:
    out = []
    with open(stream_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _opus_usage(result: dict) -> tuple[int | None, int | None, float | None, bool]:
    mu = result.get("modelUsage") if isinstance(result.get("modelUsage"), dict) else {}
    key = ADVISOR_MODEL if ADVISOR_MODEL in mu else next((k for k in mu if "opus" in k), None)
    if key is not None and isinstance(mu[key], dict):
        u = mu[key]
        tin, tout, usd = u.get("inputTokens"), u.get("outputTokens"), u.get("costUSD")
        return (int(tin) if _num(tin) else None, int(tout) if _num(tout) else None,
                float(usd) if _num(usd) else None, key != ADVISOR_MODEL)
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    its = [i for i in usage.get("iterations") or () if isinstance(i, dict) and i.get("type") == "advisor_message"]
    if not its:
        return None, None, None, False
    tin = sum(int(i.get("input_tokens") or 0) for i in its)
    tout = sum(int(i.get("output_tokens") or 0) for i in its)
    drift = any(i.get("model") not in (None, ADVISOR_MODEL) for i in its)
    return tin, tout, round(tin * USD_IN + tout * USD_OUT, 6), drift


def _unaddressed(verdict: dict | None, final_text: str) -> bool:
    if not verdict:
        return False
    serious = [i for i, r in enumerate(verdict.get("risks") or ()) if r.get("severity") in ("med", "high")]
    if not serious:
        return False
    adv = (_tagged_json(final_text, "MR_RESULT") or {}).get("advisor")
    disp = adv.get("dispositions") if isinstance(adv, dict) else None
    covered = {d.get("risk") for d in disp or () if isinstance(d, dict)}
    return any(i not in covered for i in serious)


def audit(stream_path: str, dec: AdvisorDecision, final_text: str | None, tree: str | None, *,
          audit_max: int = 180000, slow_s: float = 150, stall_s: float = 300) -> AdvisorAudit:
    """§8 post-run audit of an S+A stream-json run."""
    evs = _events(stream_path)
    consults = ok = err = 0
    codes: list[str] = []
    pending: dict[str, float | None] = {}
    latencies: list[float] = []
    at_turn = None
    msg_ids: list[str] = []
    last_ts = None
    result: dict = {}
    for ev in evs:
        ts = _ts(ev.get("timestamp"))
        last_ts = ts if ts is not None else last_ts
        if ev.get("type") == "result":
            result = ev
            continue
        msg = ev.get("message") if ev.get("type") == "assistant" else None
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        mid = msg.get("id")
        if mid and mid not in msg_ids:
            msg_ids.append(mid)
        for block in msg["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "server_tool_use" and block.get("name") == "advisor":
                consults += 1
                at_turn = at_turn or len(msg_ids)
                pending[block.get("id") or f"#{consults}"] = ts
            elif block.get("type") == "advisor_tool_result":
                content = block.get("content") if isinstance(block.get("content"), dict) else {}
                if content.get("type") == "advisor_tool_result_error":
                    err += 1
                    codes.append(str(content.get("error_code") or "unknown"))
                else:
                    ok += 1
                key = block.get("tool_use_id")
                key = key if key in pending else next(iter(pending), None)
                start = pending.pop(key) if key is not None else None
                if start is not None and ts is not None:
                    latencies.append(round(ts - start, 3))
    events = [f"consult_failed:{c}" for c in codes]
    waits = list(latencies) + [last_ts - s for s in pending.values() if s is not None and last_ts is not None]
    worst = max(waits, default=0.0)
    if worst >= stall_s:
        events.append("advisor_stall")
    elif worst >= slow_s:
        events.append("advisor_slow")
    if consults == 0:
        events.append("consult_not_made")
    if final_text is None:
        final_text = result.get("result") if isinstance(result.get("result"), str) else ""
    tin, tout, usd, drift = _opus_usage(result)
    mu = result.get("modelUsage") if isinstance(result.get("modelUsage"), dict) else {}
    son = (mu.get(SONNET_MODEL) or {}).get("costUSD") if isinstance(mu.get(SONNET_MODEL), dict) else None
    status, verdict = parse_relay(final_text, tree)
    if status != "missing" and consults == 0:
        status, verdict = "fabricated", None
    allowed = len(dec.slots) if dec.consult else 0
    violations = []
    if consults > allowed:
        violations.append("over_cap")
    if tin is not None and tin > audit_max:
        violations.append("over_transcript")
    if status == "ok" and _unaddressed(verdict, final_text):
        violations.append("unaddressed")
    if status == "fabricated":
        violations.append("fabricated")
    return AdvisorAudit(
        consults=consults, consult_ok=ok, consult_err=err, advisor_in_tok=tin, advisor_out_tok=tout,
        advisor_usd=usd, sonnet_usd=float(son) if _num(son) else None, model_drift=drift,
        relay_status=status, verdict=verdict, violations=tuple(violations), consult_at_turn=at_turn,
        consult_latency_s=latencies[0] if latencies else None, events=tuple(events), error_codes=tuple(codes))


# --- §7 fallbacks -------------------------------------------------------------

def is_launch_error(launch: dict) -> bool:
    """Non-zero exit, 0 turns and an advisor message on stderr ([unverified wording])."""
    return (launch.get("rc") not in (0, None) and not launch.get("turns")
            and "advisor" in str(launch.get("stderr") or "").lower())


def fallback_action(audit_or_launch, *, reruns_used: int = 0) -> str:
    """§7 action: none | rerun_without_advisor | open_breaker_and_rerun | log_only. One re-run per task."""
    obj = audit_or_launch
    if isinstance(obj, dict):
        reruns_used = int(obj.get("reruns_used", reruns_used) or 0)
        events = tuple(obj.get("events") or ())
        launch = is_launch_error(obj)
    else:
        events, launch = tuple(obj.events), False
    can_rerun = reruns_used < 1
    if launch:
        return "open_breaker_and_rerun" if can_rerun else "log_only"
    if "advisor_stall" in events:
        return "rerun_without_advisor" if can_rerun else "log_only"
    if any(e.startswith("consult_failed:") or e in ("advisor_slow", "consult_not_made") for e in events):
        return "log_only"
    return "none"


# --- §9.4 monitoring and demotion ---------------------------------------------

DAY_S = 86400


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True)
    os.replace(tmp, path)


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


class Monitor:
    """Rolling advisor counters under <home>/state. Demotion writes advisor-demoted.json, never routes.yaml.

    record() takes {"kind": "pair", "harm": bool, "row": str, "high_risk": bool} or
    {"kind": "consult", "usd": float|None, "relay_status": str, "violations": [...], "consult_failed": int}.
    """

    def __init__(self, home: str, monitor: dict | None = None, *, now=time.time) -> None:
        self.state = os.path.join(home, "state")
        self.cfg = {**DEFAULTS["monitor"], **(monitor or {})}
        self.now = now
        self.log_path = os.path.join(self.state, "advisor-monitor.jsonl")
        self.demoted_path = os.path.join(self.state, "advisor-demoted.json")
        self.breaker_path = os.path.join(self.state, "advisor-breaker.json")

    def record(self, rec: dict) -> None:
        rec = {"ts": self.now(), **rec}
        os.makedirs(self.state, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        if "over_cap" in (rec.get("violations") or ()):
            self.open_breaker("over_cap")

    def _records(self) -> list[dict]:
        try:
            with open(self.log_path, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except (OSError, ValueError):
            return []

    def _reason(self, recs: list[dict]) -> tuple[str, list[str] | None] | None:
        consults = [r for r in recs if r.get("kind") == "consult"]
        pairs = [r for r in recs if r.get("kind") == "pair"]
        for r in consults:
            for v in ("over_cap", "fabricated"):
                if v in (r.get("violations") or ()):
                    return v, None
        cutoff = self.now() - DAY_S
        if sum(int(r.get("consult_failed") or 0) for r in consults if r.get("ts", 0) >= cutoff) >= 3:
            return "consult_failed_3x_24h", None
        win, need, cap = self.cfg["window_pairs"], self.cfg["min_pairs"], self.cfg["harm_rate_max"]
        groups = {None: pairs[-win:]}
        for row in sorted({p.get("row") for p in pairs if p.get("row")}):
            groups[row] = [p for p in pairs if p.get("row") == row][-win:]
        for row, window in groups.items():
            if len(window) >= need and sum(bool(p.get("harm")) for p in window) / len(window) > cap:
                return ("harm_rate", None) if row is None else (f"harm_rate:{row}", [row])
        for p in pairs:
            if p.get("harm") and p.get("high_risk"):
                return f"harm_high_risk:{p.get('row')}", [p.get("row")]
        usd = [float(r["usd"]) for r in consults if _num(r.get("usd"))][-10:]
        if len(usd) >= 10 and _p90(usd) > self.cfg["usd_p90_max"]:
            return "usd_p90", None
        return None

    def relay_unreliable(self) -> bool:
        last = [r for r in self._records() if r.get("kind") == "consult"][-10:]
        return len(last) >= 10 and sum(r.get("relay_status") == "ok" for r in last) / len(last) \
            < self.cfg["relay_alarm_below"]

    def demotion(self) -> str | None:
        """Demotion reason (writes the demoted file), else 'relay_unreliable' (flag only), else None."""
        hit = self._reason(self._records())
        if hit is not None:
            reason, rows = hit
            _write_json(self.demoted_path, {"reason": reason, "rows": rows, "ts": self.now(),
                                            "at": datetime.fromtimestamp(self.now(), IST).isoformat()})
            return reason
        return "relay_unreliable" if self.relay_unreliable() else None

    def is_demoted(self, row: str | None = None) -> bool:
        try:
            with open(self.demoted_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return True  # unreadable demotion file: stay safe
        rows = data.get("rows") if isinstance(data, dict) else None
        return not rows or (row is not None and row in rows)

    def open_breaker(self, reason: str) -> None:
        _write_json(self.breaker_path, {"reason": reason, "opened": self.now(), "until": self.now() + DAY_S})

    def breaker_open(self) -> bool:
        try:
            with open(self.breaker_path, encoding="utf-8") as fh:
                until = json.load(fh).get("until")
        except FileNotFoundError:
            return False
        except (OSError, ValueError, AttributeError):
            return True
        return not _num(until) or self.now() < until
