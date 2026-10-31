"""Plain-English sentences for one mr trace (headline and step story)."""

FAILURE_MEANING = {
    "skipped": "did not really attempt it; same model tries again with more effort",
    "tried_wrong": "attempted it but tests or review failed; a stronger model takes over",
    "spec_suspect": "the spec itself looks wrong; sent back to the planner",
    "off_track": "changed files outside scope or far too much; tree reset",
    "invalid_plan": "planner output was not a valid plan",
    "infra": "tool or process failure, not the model's fault",
}

_ROLE_VERB = {"planner": "Plan", "implementer": "Implement", "reviewer": "Review"}


def fmt_duration(seconds):
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return "–"
    s = max(0, int(round(seconds)))
    if s < 60:
        return str(s) + "s"
    if s < 3600:
        return "%dm %02ds" % (s // 60, s % 60)
    return "%dh %02dm" % (s // 3600, (s % 3600) // 60)


def _units(v):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        v = 0
    return ("%.2f" % v).rstrip("0").rstrip(".") or "0"


def _where(step):
    rung, model = step.get("rung"), step.get("model")
    if rung and model:
        return "%s (%s)" % (rung, model)
    return rung or model or "an unknown rung"


def shape(trace):
    """'mostly planning' / 'mostly retries' / 'mostly mr checks' when one kind of time is at least half the wall."""
    tm = (trace or {}).get("time") or {}
    wall = tm.get("wall_s")
    if not isinstance(wall, (int, float)) or wall < 30:  # too short for a shape to mean anything
        return None
    for key, tag in (("plan_s", "mostly planning"), ("retry_s", "mostly retries"), ("mr_s", "mostly mr checks"), ("unlogged_s", "mostly unlogged (job was killed mid-step)")):
        v = tm.get(key)
        if isinstance(v, (int, float)) and v / wall >= 0.5:
            return tag
    return None


def _tail(trace, units=True):
    parts = []
    dur = fmt_duration(trace.get("wall_s"))
    if dur != "–":
        parts.append(dur)
    if units:
        u = (trace.get("cost") or {}).get("units")
        if isinstance(u, (int, float)) and not isinstance(u, bool):
            parts.append("%s units" % _units(u))
    tag = shape(trace)
    if tag:
        parts.append(tag)
    return "".join(" · " + p for p in parts)


def headline(trace):
    trace = trace or {}
    status = trace.get("status") or "unknown"
    steps = [s for s in (trace.get("steps") or []) if isinstance(s, dict)]
    if status == "accepted":
        impl = [s for s in steps if s.get("role") == "implementer"]
        if not impl:  # e.g. a standalone `mr review`: say what we know, no template holes
            last = steps[-1] if steps else {}
            verb = _ROLE_VERB.get(last.get("role")) or "Job"
            return "%s: accepted%s" % (verb, _tail(trace, units=False))
        n = len(impl) - 1
        esc = "first try" if n == 0 else "%d escalation%s" % (n, "" if n == 1 else "s")
        return "Accepted on %s after %s%s" % (_where(impl[-1]), esc, _tail(trace))
    if status == "needs_human":
        failed = [s for s in steps if s.get("ok") is False or (not s.get("ok") and not s.get("live"))]
        fc = failed[-1].get("failure_class") if failed else None
        meaning = FAILURE_MEANING.get(fc) or ("unclassified failure (%s)" % fc if fc else "no failure recorded")
        return "Needs a human: %s%s" % (meaning, _tail(trace, units=False))
    if status == "review_failed":
        return "Review: changes requested%s" % _tail(trace, units=False)
    if status == "running":
        if not steps:
            return "Running: routing"
        last = steps[-1]
        return "Running: %s on %s" % (last.get("role") or "step", _where(last))
    return "%s%s" % (str(status).replace("_", " ").capitalize(), _tail(trace, units=False))


def step_line(step):
    step = step or {}
    role = step.get("role") or "step"
    verb = _ROLE_VERB.get(role) or str(role).capitalize()
    if step.get("ok"):
        outcome = "ok"
    else:
        fc = step.get("failure_class")
        outcome = "failed: %s" % fc if fc else "failed"
    line = "%s on %s · %s · %s" % (verb, _where(step), fmt_duration(step.get("wall_s")), outcome)
    if step.get("escalated_from"):
        line += " · escalated from %s" % step["escalated_from"]
    diff = step.get("diff")
    files = diff.get("files") if isinstance(diff, dict) else None
    if files:
        n = len(files) if isinstance(files, (list, tuple, set)) else files
        line += " · +%s -%s in %s file%s" % (diff.get("add") or 0, diff.get("del") or 0, n, "" if n == 1 else "s")
    return line
