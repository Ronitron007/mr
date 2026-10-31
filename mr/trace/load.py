"""Join mr's own records into one trace per task.

Sources (all already written by mr; nothing new is required to run this):
  log/runs.jsonl          distilled records: route, guard, planner, implementer, reviewer, advisor, task
  tasks/<id>/state.json   checklist goal (title) for planned tasks
  raw/<id>/a<n>-<model>.out   CLI output per attempt (cursor json / claude json / claude stream-json)
  ~/.cursor/projects/*/agent-transcripts/<session>/<session>.jsonl   tool calls of a cursor attempt
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta

from mr.paths import data_dir

CURSOR_PROJECTS = os.path.expanduser(os.environ.get("MRTRACE_CURSOR_PROJECTS", "~/.cursor/projects"))

# Cost/strength order of rungs: the y axis of the ladder view (cheap at the bottom).
RUNG_ORDER = ["G-l", "G-m", "C25", "G-h", "K-h", "G-x", "K-x", "S", "S+", "S+A", "O", "O+"]
STEP_ROLES = ("planner", "implementer", "reviewer", "architect", "classifier", "judge")

# Fields the viewer reads from each log role. A bool is not an int.
READ_FIELDS = {
    "route": {
        "task": str,
        "ts": str,
        "applied": dict,
        "caller": (str, type(None)),
        "sources": dict,
        "prepass_confident": bool,
    },
    "planner": {
        "task": str,
        "rung": str,
        "model": str,
        "attempt": int,
        "ts": str,
    },
    "implementer": {
        "task": str,
        "rung": str,
        "model": str,
        "attempt": int,
        "ts": str,
    },
    "reviewer": {
        "task": str,
        "rung": str,
        "model": str,
        "attempt": int,
        "ts": str,
    },
    "advisor": {
        "task": str,
        "ts": str,
        "row": (str, type(None)),
        "gate": (str, type(None)),
        "consult_made": int,
    },
    "guard": {
        "task": (str, type(None)),
        "ts": str,
        "rung": str,
        "allow": bool,
        "reason": str,
    },
    "task": {
        "task": str,
        "started_at": str,
        "ended_at": str,
        "title": (str, type(None)),
        "caller": (str, type(None)),
        "outcome": str,
        "ladder": list,
    },
}

TOOL_KIND = {
    "Read": "read", "Glob": "search", "Grep": "search", "LS": "search", "SemanticSearch": "search",
    "Edit": "edit", "Write": "edit", "MultiEdit": "edit", "StrReplace": "edit", "Delete": "edit", "ApplyPatch": "edit",
    "NotebookEdit": "edit", "Shell": "shell", "Bash": "shell", "TodoWrite": "plan", "WebSearch": "web", "WebFetch": "web",
}


def rung_rank(rung: str | None) -> int:
    if rung in RUNG_ORDER:
        return RUNG_ORDER.index(rung)
    return len(RUNG_ORDER)


def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def read_rows(home: str | None = None) -> list[dict]:
    if home is None:
        home = data_dir()
    rows: list[dict] = []
    for name in ("runs.jsonl", "ingested.jsonl"):
        p = os.path.join(home, "log", name)
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    return rows


_MR_RESULT = re.compile(r"MR_RESULT\s*(\{.*\})\s*$", re.S)


def _mr_result(text: str) -> dict | None:
    if not text or "MR_RESULT" not in text:
        return None
    tail = text[text.rfind("MR_RESULT"):]
    m = _MR_RESULT.search(tail)
    if not m:
        return None
    blob = m.group(1)
    # trim to the matching closing brace
    depth = 0
    for i, ch in enumerate(blob):
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0:
            blob = blob[: i + 1]
            break
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def parse_raw(path: str | None) -> dict:
    """Summarise one attempt's CLI output. Never returns prompt text."""
    out: dict = {"raw_exists": False}
    if not path or not os.path.isfile(path):
        return out
    out["raw_exists"] = True
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return out
    result = None
    tools: list[dict] = []
    events = []
    stripped = text.strip()
    if stripped.startswith("{") and "\n{" not in stripped:
        try:
            events = [json.loads(stripped)]
        except json.JSONDecodeError:
            events = []
    else:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    for ev in events:
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "result" or "duration_api_ms" in ev:
            result = ev
        elif t == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tools.append(_tool(c.get("name"), c.get("input")))
        elif t == "tool_call" and ev.get("subtype") == "started":
            tc = ev.get("tool_call") or {}
            name = next(iter(tc), "tool")
            tools.append(_tool(name.replace("ToolCall", "").replace("_", " ").title().replace(" ", ""), tc.get(name, {}).get("args")))
    if result:
        res_text = result.get("result") or ""
        out["session_id"] = result.get("session_id")
        out["duration_ms"] = result.get("duration_ms")
        out["is_error"] = result.get("is_error")
        out["stop_reason"] = result.get("stop_reason") or result.get("terminal_reason") or result.get("subtype")
        out["turns"] = result.get("num_turns")
        out["usd"] = result.get("total_cost_usd")
        u = result.get("usage") or {}
        out["tokens"] = {
            "in": u.get("inputTokens", u.get("input_tokens")),
            "out": u.get("outputTokens", u.get("output_tokens")),
            "cache": u.get("cacheReadTokens", u.get("cache_read_input_tokens")),
        }
        mrr = _mr_result(res_text)
        if mrr:
            out["mr_result"] = mrr
        body = res_text.split("MR_RESULT")[0].strip()
        out["summary"] = body[-600:] if len(body) > 600 else body
    if tools:
        out["tools"] = tools
    return out


def _arg_hint(name: str, args) -> str:
    if not isinstance(args, dict):
        return ""
    for k in ("path", "file_path", "target_file", "glob_pattern", "pattern", "description", "command", "query", "url"):
        v = args.get(k)
        if isinstance(v, str) and v:
            v = v.strip().splitlines()[0] if v.strip() else v
            return v[:140]
    return ""


def _tool(name, args) -> dict:
    name = name or "tool"
    return {"name": name, "kind": TOOL_KIND.get(name, "other"), "hint": _arg_hint(name, args)}


_transcript_index: dict[str, str] | None = None


def transcript_path(session_id: str | None) -> str | None:
    global _transcript_index
    if not session_id:
        return None
    if _transcript_index is None:
        _transcript_index = {}
        for p in glob.glob(os.path.join(CURSOR_PROJECTS, "*", "agent-transcripts", "*", "*.jsonl")):
            sid = os.path.basename(os.path.dirname(p))
            if os.path.basename(p) == sid + ".jsonl":
                _transcript_index[sid] = p
    return _transcript_index.get(session_id)


def parse_transcript(path: str | None) -> dict:
    """Tool calls (name + short hint) and whether the turn ended, from a cursor-agent transcript."""
    if not path:
        return {}
    tools: list[dict] = []
    narration: list[str] = []
    ended = False
    first_user = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "turn_ended":
                    ended = True
                    continue
                msg = d.get("message") or {}
                for c in msg.get("content") or []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use":
                        tools.append(_tool(c.get("name"), c.get("input")))
                    elif c.get("type") == "text" and d.get("role") == "assistant":
                        t = (c.get("text") or "").strip()
                        if t:
                            narration.append(t.splitlines()[0][:200])
                            tools.append({"name": "say", "kind": "say", "hint": t.splitlines()[0][:200]})
                    elif c.get("type") == "text" and d.get("role") == "user" and first_user is None:
                        first_user = c.get("text") or ""
    except OSError:
        return {}
    return {"tools": tools, "turn_ended": ended, "task_hint": _task_hint(first_user)}


def _task_hint(prompt: str | None) -> str | None:
    """One line describing the task, taken from the 'TASK' block of mr's prompt."""
    if not prompt:
        return None
    m = re.search(r"\bTASK\b[:\s]*(.+)", prompt)
    if not m:
        return None
    line = m.group(1).strip()
    line = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
    return line[:160]


def _find_raw(home: str, task: str, attempt, model) -> str | None:
    if attempt is None:
        return None
    d = os.path.join(home, "raw", task)
    if model:
        p = os.path.join(d, f"a{attempt}-{model}.out")
        if os.path.isfile(p):
            return p
    hits = sorted(glob.glob(os.path.join(d, f"a{attempt}-*.out")))
    return hits[0] if hits else None


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def running_info() -> dict:
    """Live mr work from the process table: {task: {role, model, since}} plus '*' when an mr run has no child yet."""
    try:
        out = subprocess.run(["ps", "-eo", "etimes=,args="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {}
    info: dict = {}
    now = time.time()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        et, _, args = line.partition(" ")
        if "mr task t-" in args or ("--model" in args and "You are the" in args):
            m = re.search(r"\b(t-[0-9a-f]{8})\b", args)
            if not m:
                continue
            role = re.search(r"You are the (planner|implementer|reviewer|architect)", args)
            model = re.search(r"--model\s+(\S+)", args)
            info[m.group(1)] = {"role": role.group(1) if role else "implementer",
                                "model": model.group(1) if model else None,
                                "since": now - float(et or 0)}
        elif re.search(r"/mr(\s|$).*\brun\b", args):
            info.setdefault("*", {"since": now - float(et or 0)})
    return info


def running_tasks() -> set[str]:
    return {k for k in running_info() if k != "*"}


def _v_newer_than_2(v) -> bool:
    """True only for an integer log version above 2. Bool is not an int here."""
    return type(v) is int and v > 2


def build_traces(home: str | None = None, with_transcripts: bool = True) -> list[dict]:
    if home is None:
        home = data_dir()
    rows = read_rows(home)
    by_task: dict[str, list[dict]] = {}
    loose_guards: list[dict] = []
    for r in rows:
        t = r.get("task")
        if t:
            by_task.setdefault(t, []).append(r)
        elif r.get("role") == "guard":
            loose_guards.append(r)
    live = running_info()
    if "*" in live:
        # an mr run with no model child yet: the newest task without an end record is the live one
        open_ids = [t for t, rs in by_task.items() if not any(r.get("role") == "task" for r in rs)]
        if open_ids:
            newest = max(open_ids, key=lambda t: max(parse_ts(r.get("ts")) or 0 for r in by_task[t]))
            live.setdefault(newest, {"role": None, "model": None, "since": time.time()})
    traces = []
    for tid, recs in by_task.items():
        tr = _build_one(home, tid, recs, live, with_transcripts)
        if any(_v_newer_than_2(r.get("v")) for r in recs):
            tr["newer_log_format"] = True
        traces.append(tr)
    # attach task-less guard checks to the task running at that moment
    for g in loose_guards:
        ts = parse_ts(g.get("ts"))
        for tr in traces:
            s, e = tr["t0"], tr["t1"] or time.time()
            if s is not None and ts is not None and s - 15 <= ts <= e + 1:
                tr["events"].append(_guard_event(g))
                break
    for tr in traces:
        if not tr["task"].startswith("t-") and tr["t0"] is not None and tr["t1"] is not None:
            # a reused standalone review label: keep only events from the run we show
            tr["events"] = [e for e in tr["events"] if e["t"] is None or tr["t0"] - 60 <= e["t"] <= tr["t1"] + 60]
        tr["events"].sort(key=lambda e: e["t"] or 0)
    traces.sort(key=lambda t: t["t0"] or 0, reverse=True)
    return traces


def _guard_event(g: dict) -> dict:
    return {
        "kind": "guard", "t": parse_ts(g.get("ts")), "rung": g.get("rung"), "allow": g.get("allow"),
        "reason": g.get("reason"), "label": f"Claude gate {g.get('rung')}: {'allowed' if g.get('allow') else 'denied ('+str(g.get('reason'))+')'}",
        "detail": {k: g.get(k) for k in ("hour", "five_hour", "seven_day", "est", "frees_hint")},
    }


def _union(iv):
    total, end = 0.0, None
    for a, b in sorted(iv):
        if end is None or a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total


def _time_split(steps, t0, t1, wall, killed=False):
    """Wall time split: model sessions (by role, failed attempts = retries) vs mr's own checks, tests and git.

    A killed job (interrupted / no end record) loses the step that was running: mr logs a step only when it
    ends. The time after the last logged step is reported as unlogged_s, not as mr overhead."""
    if t0 is None:
        return {}
    end = t1 or time.time()
    wall = wall or (end - t0)
    iv = lambda pred: [(max(s["t0"], t0), min(s["t1"] or end, end)) for s in steps
                       if s.get("t0") is not None and pred(s) and (s["t1"] or end) > s["t0"]]
    model = _union(iv(lambda s: True))
    # retries = attempts that were thrown away: any implementer/reviewer step followed by a later implementer step,
    # plus a final failed implementer step of a job that ended without acceptance
    order = [s for s in steps if s.get("t0") is not None]
    thrown = set()
    for i, s in enumerate(order):
        if s["role"] in ("implementer", "reviewer") and any(x["role"] == "implementer" for x in order[i + 1:]):
            thrown.add(id(s))
        elif s["role"] == "implementer" and s.get("ok") is False:
            thrown.add(id(s))
    unlogged = 0.0
    if killed:
        last = max([s["t1"] for s in steps if s.get("t1")] or [t0])
        unlogged = max(t0 + wall - last, 0)
    return {
        "wall_s": round(wall, 1),
        "model_s": round(min(model, wall), 1),
        "mr_s": round(max(wall - model - unlogged, 0), 1),
        "unlogged_s": round(unlogged, 1),
        "plan_s": round(_union(iv(lambda s: s["role"] == "planner")), 1),
        "retry_s": round(_union(iv(lambda s: id(s) in thrown)), 1),
    }


def _build_one(home: str, tid: str, recs: list[dict], live: dict, with_transcripts: bool) -> dict:
    route = next((r for r in recs if r.get("role") == "route"), None)
    task = next((r for r in reversed(recs) if r.get("role") == "task"), None)
    steps = []
    events = []
    for r in recs:
        role = r.get("role")
        if role in STEP_ROLES:
            steps.append(_step(home, tid, r, with_transcripts))
        elif role == "guard":
            events.append(_guard_event(r))
        elif role == "advisor":
            events.append({
                "kind": "advisor", "t": parse_ts(r.get("ts")), "label": f"Advisor {r.get('row') or ''}: {'consulted' if r.get('consult_made') else 'no consult'} ({r.get('gate')})",
                "detail": {k: r.get(k) for k in ("mode", "gate", "consult_requested", "consult_made", "verdict", "reasons", "est_consult_usd")},
            })
    if not tid.startswith("t-") and not steps:
        steps = _standalone_review_steps(home, tid)
    steps.sort(key=lambda s: (s["t0"] or 0, s["attempt"] or 0))
    t0 = parse_ts(task.get("started_at")) if task else None
    if t0 is None:
        t0 = parse_ts(route.get("ts")) if route else (steps[0]["t0"] if steps else None)
    t1 = parse_ts(task.get("ended_at")) if task else None
    status = (task or {}).get("outcome")
    if not task:
        status = "running" if tid in live else "no_end_record"
        if not tid.startswith("t-") and steps and tid not in live:
            status = "accepted" if all(x["ok"] for x in steps) else "review_failed"
            t0 = min(x["t0"] for x in steps)
            t1 = max(x["t1"] for x in steps)
        if status == "no_end_record" and steps:
            t1 = max((s["t1"] or 0) for s in steps) or None
        li = live.get(tid)
        if li and li.get("role"):
            st_t0 = max([li["since"]] + [s["t1"] or 0 for s in steps])
            steps.append({"role": li["role"], "attempt": None, "rung": _rung_for(li.get("model"), steps, route) or "?",
                          "rank": 0, "model": li.get("model"), "t0": st_t0, "t1": None, "wall_s": None, "decision": None,
                          "ok": None, "live": True, "failure_class": None, "signals": {}, "tokens": {}, "tools": [],
                          "diff": None, "escalated_from": None})
    applied = (route or {}).get("applied") or {}
    title = (task or {}).get("title")
    state_p = os.path.join(home, "tasks", tid, "state.json")
    repo_path = None
    if os.path.isfile(state_p):
        try:
            st = json.load(open(state_p))
            title = title or ((st.get("checklist") or {}).get("goal"))
            repo_path = st.get("repo")
        except Exception:
            pass
    if not title:
        title = next((s.get("task_hint") for s in steps if s.get("task_hint")), None)
    standalone = not tid.startswith("t-")
    if standalone and not title:
        title = f"Standalone mr review ({tid})"
    if task and task.get("repo"):
        repo = task.get("repo")
    elif repo_path:
        repo = os.path.basename(repo_path)
    else:
        repo = None
    why = None
    fin = os.path.join(home, "patches", tid, "FINAL.md")
    if os.path.isfile(fin):
        try:
            m = re.search(r"\*\*Why it stopped:\*\*\s*(.+)", open(fin, encoding="utf-8", errors="replace").read())
            why = m.group(1).strip()[:400] if m else None
        except OSError:
            pass
    ladder = (task or {}).get("ladder") or applied.get("ladder") or []
    for s in steps:
        s.setdefault("escalated_from", None)
        s["rank"] = rung_rank(s.get("rung"))
    impl = [s for s in steps if s["role"] == "implementer" and not s.get("live")]
    for a, b in zip(impl, impl[1:]):
        b["escalated_from"] = a["rung"]
    time_split = _time_split(steps, t0, t1, (task or {}).get("wall_s") if task else None,
                             killed=status in ("interrupted", "no_end_record"))
    cost = (task or {}).get("cost") or {}
    tok_in = sum((s["tokens"].get("in") or 0) for s in steps)
    tok_out = sum((s["tokens"].get("out") or 0) for s in steps)
    usd = sum((s.get("usd") or 0) for s in steps)
    return {
        "task": tid,
        "title": title,
        "why": why,
        "repo": repo,
        "caller": (task or {}).get("caller") or (route or {}).get("caller"),
        "status": status,
        "exit": (task or {}).get("exit"),
        "route": (task or {}).get("route") or applied.get("route") or ("review" if standalone else None),
        "type": (task or {}).get("type") or applied.get("type") or ("review" if standalone else None),
        "difficulty": (task or {}).get("difficulty") or applied.get("difficulty"),
        "risk": (task or {}).get("risk") or applied.get("risk"),
        "route_sources": (route or {}).get("sources"),
        "prepass_confident": (route or {}).get("prepass_confident"),
        "ladder": ladder,
        "first_rung": (task or {}).get("first_rung"),
        "final_rung": (task or {}).get("final_rung"),
        "esc": (task or {}).get("esc"),
        "final_signals": (task or {}).get("final_signals"),
        "cost": {"units": cost.get("units"), "claude_pts": cost.get("claude_pts"), "usd_equiv": round(usd, 4) or cost.get("usd_equiv"),
                  "tok_in": tok_in, "tok_out": tok_out},
        "dirs": (task or {}).get("dirs"),
        "commit": (task or {}).get("commit"),
        "t0": t0, "t1": t1, "start": _iso(t0), "end": _iso(t1),
        "wall_s": (task or {}).get("wall_s") or ((t1 - t0) if (t0 and t1) else None),
        "time": time_split,
        "cost_measured": bool(task and task.get("cost")),
        "steps": steps,
        "events": events,
    }


def _standalone_review_steps(home: str, tid: str) -> list[dict]:
    """`mr review` logs only a guard record; its reviewer session lives in raw/<label>/a<n>-<model>.out."""
    out = []
    for p in sorted(glob.glob(os.path.join(home, "raw", tid, "a*.out"))):
        raw = parse_raw(p)
        if not raw.get("raw_exists"):
            continue
        model = re.sub(r"^a\d+-", "", os.path.basename(p))[:-4]
        t1 = os.path.getmtime(p)
        wall = (raw.get("duration_ms") or 0) / 1000 or None
        text = raw.get("summary") or ""
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                body = f.read()
        except OSError:
            body = ""
        m = re.search(r'\\?"verdict\\?"\s*:\s*\\?"(\w+)', body)
        verdict = m.group(1).lower() if m else None
        ok = verdict in ("pass", "approve", "approved", "ok") if verdict else None
        out.append({"role": "reviewer", "attempt": None, "rung": _rung_for(model, [], None) or "?", "rank": 0, "model": model,
                    "path": "max" if "claude" in model else "cursor", "t0": t1 - wall if wall else t1 - 1, "t1": t1,
                    "wall_s": wall, "decision": verdict, "ok": ok if ok is not None else True, "failure_class": None if ok is not False else "review_fail",
                    "failure_source": None, "signals": {"C": verdict} if verdict else {}, "diff": None, "mr_result": None,
                    "pushback": None, "requirements": None, "confidence": None, "summary": text, "is_error": raw.get("is_error"),
                    "rc": None, "tokens": raw.get("tokens") or {}, "usd": raw.get("usd"), "claude_pts": None,
                    "session_id": raw.get("session_id"), "tools": raw.get("tools") or [], "turn_ended": None, "task_hint": None,
                    "patch": None, "raw": p, "isolation": None, "escalated_from": None})
    if out:  # the same label can be reused across days; keep the latest run only
        last = max(x["t1"] for x in out)
        out = [x for x in out if last - x["t1"] < 3600]
    return out


def _rung_for(model, steps, route):
    for s in steps:
        if s.get("model") == model:
            return s.get("rung")
    if model:
        m = model.replace("-fast", "")
        for tag, rung in (("-low", "G-l"), ("-medium", "G-m"), ("-xhigh", "G-x"), ("-high", "G-h"), ("composer", "C25"),
                          ("kimi", "K-h"), ("sonnet", "S"), ("opus", "O")):
            if tag in m:
                return rung
    return None


def _step(home: str, tid: str, r: dict, with_transcripts: bool) -> dict:
    t0 = parse_ts(r.get("started_at"))
    wall = r.get("wall_s")
    t1 = (t0 + wall) if (t0 is not None and wall is not None) else parse_ts(r.get("ts"))
    raw_path = r.get("raw") or _find_raw(home, tid, r.get("attempt"), r.get("model"))
    raw = parse_raw(raw_path)
    sid = r.get("session_id") or raw.get("session_id")
    tr = parse_transcript(transcript_path(sid)) if (with_transcripts and r.get("path") == "cursor") else {}
    tools = tr.get("tools") or raw.get("tools") or []
    mrr = raw.get("mr_result") or {}
    tokens = {
        "in": r.get("in_tok") if r.get("in_tok") is not None else (raw.get("tokens") or {}).get("in"),
        "out": r.get("out_tok") if r.get("out_tok") is not None else (raw.get("tokens") or {}).get("out"),
        "cache": r.get("cache_tok") if r.get("cache_tok") is not None else (raw.get("tokens") or {}).get("cache"),
    }
    ok = r.get("decision") == "accept"
    return {
        "role": r.get("role"),
        "attempt": r.get("attempt"),
        "rung": r.get("rung"),
        "rank": rung_rank(r.get("rung")),
        "model": r.get("model_served") or r.get("model"),
        "path": r.get("path"),
        "t0": t0, "t1": t1, "wall_s": wall,
        "decision": r.get("decision"),
        "ok": ok,
        "failure_class": r.get("failure_class"),
        "failure_source": r.get("failure_source"),
        "signals": r.get("signals") or {},
        "diff": r.get("diff"),
        "mr_result": r.get("mr_result") or mrr.get("status"),
        "pushback": mrr.get("pushback"),
        "requirements": mrr.get("requirements"),
        "confidence": mrr.get("confidence"),
        "summary": raw.get("summary"),
        "is_error": r.get("is_error") if r.get("is_error") is not None else raw.get("is_error"),
        "rc": r.get("rc"),
        "tokens": tokens,
        "usd": r.get("usd_equiv") if r.get("usd_equiv") is not None else raw.get("usd"),
        "claude_pts": r.get("claude_pts"),
        "session_id": sid,
        "tools": tools,
        "turn_ended": tr.get("turn_ended"),
        "task_hint": tr.get("task_hint"),
        "patch": r.get("patch"),
        "raw": raw_path,
        "isolation": r.get("isolation"),
    }
