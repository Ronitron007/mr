"""Offline eval of the shadow judge and classifier (judge-spec §11). Reads the run log and a labels file; no calls."""
from __future__ import annotations

import json
import math
import os
import random
from collections import Counter
from datetime import date, datetime
from types import SimpleNamespace

from mr import log
from mr.judge import policy

FIELDS = ("type", "difficulty", "route", "risk", "risk_tags")
ARMS = ("rules", "judge", "classifier", "judge+classifier")
THETAS = (0.6, 0.7, 0.75, 0.8, 0.9)
DIFFICULTY = policy.DIFFICULTY
RISK = policy.RISK
PROMOTE, KEEP, DEMOTE = "PROMOTE", "KEEP SHADOW", "DEMOTE"

_SEED = 20261101
_BOOT = 1000
_OK = frozenset({"ok", "ok_partial", "cache_hit"})
_CALLED = frozenset({"ok", "ok_partial", "invalid", "invalid_response", "timeout", "cli_error", "http_error",
                     "model_drift"})
_FAILED = frozenset({"timeout", "cli_error", "http_error", "invalid_response", "invalid"})
_TAG_NAMES = {"tag.data_deletion": "data-deletion", "tag.native_config": "native-config"}
_JUDGE_QID = {"type": "task_type", "route": "route_class", "difficulty": "difficulty", "risk": "risk"}
_PROMOTE_BARS = {"consulted": 50, "medium": 10, "labelled": 30, "accuracy": 0.90, "coverage": 0.40,
                 "brier": 0.15, "failure": 0.05}


class _Task:
    def __init__(self, tid: str, route: dict, ts: datetime | None) -> None:
        self.tid, self.ts = tid, ts
        rules = route.get("rules")
        self.rules = rules if isinstance(rules, dict) else {}
        self.jsum = route.get("judge") if isinstance(route.get("judge"), dict) else None
        self.csum = route.get("classifier") if isinstance(route.get("classifier"), dict) else None
        self.judge_recs: list[dict] = []
        self.clf_recs: list[dict] = []
        self.labels: dict = {}
        self.parsed = None  # judge answers as the policy reader takes them
        self.clf = None  # classifier answers dict
        self.medium_plus = self.rules.get("difficulty") in DIFFICULTY[2:]


def _ist_ts(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return dt.replace(tzinfo=log.IST) if dt.tzinfo is None else dt.astimezone(log.IST)


def _read_labels(path: str | None) -> dict:
    out: dict = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("task"), str) and isinstance(row.get("labels"), dict):
                out.setdefault(row["task"], {}).update(_clean_labels(row["labels"]))
    return out


def _clean_labels(raw: dict) -> dict:
    out = {}
    for f in FIELDS:
        v = raw.get(f)
        if f == "risk_tags":
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                out[f] = v
        elif isinstance(v, str):
            out[f] = v
    return out


def _load_tasks(home: str, since: str | None, labels: dict) -> list[_Task]:
    rows = log.read(home)
    floor = date.fromisoformat(since) if since else None
    last: dict[str, dict] = {}
    for row in rows:
        if row.get("role") == "route" and isinstance(row.get("task"), str):
            last[row["task"]] = row
    tasks: dict[str, _Task] = {}
    for tid, route in last.items():
        ts = _ist_ts(route.get("ts"))
        if floor is not None and (ts is None or ts.date() < floor):
            continue
        tasks[tid] = _Task(tid, route, ts)
    for row in rows:
        t = tasks.get(row.get("task")) if isinstance(row.get("task"), str) else None
        if t is None:
            continue
        if row.get("role") == "judge":
            t.judge_recs.append(row)
        elif row.get("role") == "classifier":
            t.clf_recs.append(row)
    for t in tasks.values():
        t.labels = labels.get(t.tid, {})
        t.parsed = _judge_parsed(t)
        t.clf = _clf_answers(t)
    return list(tasks.values())


def _by_ref(recs: list[dict], summary: dict | None) -> dict | None:
    ref = (summary or {}).get("ref")
    return next((r for r in recs if ref is not None and r.get("ref") == ref), None)


def _judge_parsed(t: _Task):
    rec = _by_ref(t.judge_recs, t.jsum)
    if rec is None or rec.get("status") not in _OK or not isinstance(rec.get("answers"), dict) or not rec["answers"]:
        return None
    return SimpleNamespace(answers=rec["answers"], unsure=tuple(rec.get("unsure") or ()))


def _clf_answers(t: _Task) -> dict | None:
    rec = _by_ref(t.clf_recs, t.csum)
    if rec is None or rec.get("status") not in _OK or not isinstance(rec.get("answers"), dict):
        return None
    return rec["answers"]


def _norm(f: str, v):
    return tuple(sorted(v or ())) if f == "risk_tags" else v


def _rank(names: tuple, v) -> int:
    return names.index(v) if v in names else 0


# ---- deciders: what each one gives, per task ----

def _judge_gives(t: _Task, act_p: float, raise_p: float) -> dict:
    if t.parsed is None:
        return {}
    shim = SimpleNamespace(judge={"thresholds": {"act_p": act_p, "raise_p": raise_p}})
    try:
        dec = policy.read_route(t.parsed, t.rules, shim)
        ans = policy.to_shadow(t.parsed, dec, ref=None, status="ok", consulted=True, latency_ms=None)
    except (KeyError, TypeError, ValueError, IndexError):
        return {}
    out = {f: getattr(ans, f) for f in ("type", "difficulty", "route", "risk") if getattr(ans, f) is not None}
    if any(q.startswith("tag.") and d["decision"] != "unsure" for q, d in dec.items()):
        out["risk_tags"] = sorted(set(t.rules.get("risk_tags") or ()) | set(ans.risk_tags))
    return out


def _clf_gives(t: _Task, min_conf: float) -> dict:
    a = t.clf
    conf = a.get("confidence") if a else None
    if not isinstance(conf, (int, float)) or conf < min_conf - 1e-9:
        return {}
    out = {"route": "design" if a.get("load_bearing") else "judgment" if a.get("high_judgment") else "cascade"}
    for f, key in (("type", "task_type"), ("difficulty", "difficulty"), ("risk", "risk")):
        if isinstance(a.get(key), str):
            out[f] = a[key]
    if isinstance(a.get("risk_tags"), list):
        out["risk_tags"] = sorted(str(x) for x in a["risk_tags"])
    return out


def _combined(t: _Task, judge: dict, clf: dict) -> dict:
    out = dict(judge)
    if t.medium_plus:
        for f, v in clf.items():
            out.setdefault(f, v)
    return out


def _judge_pairs(t: _Task, f: str, label) -> list[tuple[float, int]]:
    """(confidence, 1 if right) from the raw logged answers, whatever the threshold."""
    if t.parsed is None:
        return []
    ans = t.parsed.answers
    try:
        if f == "risk_tags":
            want = set(label)
            return [(float(a["p"]), int(_TAG_NAMES.get(q, q[4:]) in want))
                    for q, a in ans.items() if q.startswith("tag.")]
        a = ans.get(_JUDGE_QID[f])
        if a is None:
            return []
        value = a["c"] if "c" in a else (DIFFICULTY if f == "difficulty" else RISK)[a["s"]]
        return [(float(a["conf"]), int(value == label))]
    except (KeyError, TypeError, ValueError, IndexError):
        return []


def _clf_pairs(t: _Task, f: str, label) -> list[tuple[float, int]]:
    a = t.clf
    conf = a.get("confidence") if a else None
    if not isinstance(conf, (int, float)):
        return []
    value = _clf_gives(t, 0.0).get(f)
    return [] if value is None else [(float(conf), int(_norm(f, value) == _norm(f, label)))]


# ---- metrics ----

def _ratio(a, b):
    return a / b if b else None


def _arm_metrics(tasks: list[_Task], gives: dict, f: str) -> dict:
    n = len(tasks)
    cover = agree = lab = right = 0
    for t in tasks:
        given = gives.get(t.tid, {})
        value = given.get(f, t.rules.get(f))
        cover += f in given
        agree += _norm(f, value) == _norm(f, t.rules.get(f))
        if f in t.labels:
            lab += 1
            right += _norm(f, value) == _norm(f, t.labels[f])
    return {"n": n, "coverage": cover / n if n else 0.0, "agreement": _ratio(agree, n), "accuracy": _ratio(right, lab)}


def _bootstrap(hits: list[int]):
    if not hits:
        return None
    rng, n = random.Random(_SEED), len(hits)
    means = sorted(sum(rng.choices(hits, k=n)) / n for _ in range(_BOOT))
    return [means[int(0.025 * _BOOT)], means[int(0.975 * _BOOT) - 1]]


def _macro_f1(pairs: list[tuple]):
    if not pairs:
        return None
    scores = []
    for c in {x for p in pairs for x in p}:
        tp = sum(1 for g, p in pairs if g == c and p == c)
        fp = sum(1 for g, p in pairs if g != c and p == c)
        fn = sum(1 for g, p in pairs if g == c and p != c)
        scores.append(2 * tp / (2 * tp + fp + fn) if tp else 0.0)
    return sum(scores) / len(scores)


def _calibration(pairs: list[tuple[float, int]]) -> dict:
    bins = [[] for _ in range(5)]
    for conf, hit in pairs:
        bins[min(int(conf * 5), 4)].append((conf, hit))
    n = len(pairs)
    rel, ece = [], 0.0
    for i, b in enumerate(bins):
        mean_c = sum(c for c, _ in b) / len(b) if b else None
        acc = sum(h for _, h in b) / len(b) if b else None
        if b:
            ece += len(b) / n * abs(acc - mean_c)
        rel.append({"lo": i / 5, "hi": (i + 1) / 5, "n": len(b), "confidence": mean_c, "accuracy": acc})
    return {"brier": sum((c - h) ** 2 for c, h in pairs) / n if n else None, "ece": ece if n else None,
            "reliability": rel}


def _field_at(tasks, gives_at, f):
    """Coverage, accuracy and the pieces of it for one field at one operating point."""
    cover, hits, pairs = 0, [], []
    for t in tasks:
        g = gives_at(t)
        if f not in g:
            continue
        cover += 1
        if f in t.labels:
            hits.append(int(_norm(f, g[f]) == _norm(f, t.labels[f])))
            pairs.append((t.labels[f], g[f]))
    return {"coverage": cover / len(tasks) if tasks else 0.0, "accuracy": _ratio(sum(hits), len(hits)),
            "hits": hits, "pairs": pairs}


def _decider_fields(tasks, gives_at, sweep_at, pairs_of) -> dict:
    out = {}
    for f in FIELDS:
        base = _field_at(tasks, gives_at, f)
        cal_pairs = [p for t in tasks if f in t.labels for p in pairs_of(t, f, t.labels[f])]
        m = {"coverage": base["coverage"], "accuracy": base["accuracy"], "accuracy_ci": _bootstrap(base["hits"]),
             "n_labelled_covered": len(base["hits"])}
        m.update(_calibration(cal_pairs))
        m["selective"] = []
        for theta in THETAS:
            s = _field_at(tasks, lambda t, th=theta: sweep_at(t, th), f)
            m["selective"].append({"theta": theta, "coverage": s["coverage"], "accuracy": s["accuracy"]})
        if f in ("type", "route"):
            m["macro_f1"] = _macro_f1(base["pairs"])
        elif f in ("difficulty", "risk"):
            names = DIFFICULTY if f == "difficulty" else RISK
            diffs = [abs(_rank(names, g) - _rank(names, lab)) for lab, g in base["pairs"]]
            m["mae"] = _ratio(sum(diffs), len(diffs))
        out[f] = m
    return out


def _percentile(vals: list, q: float):
    if not vals:
        return None
    vals = sorted(vals)
    return vals[max(math.ceil(q * len(vals)) - 1, 0)]


def _ops(tasks: list[_Task], recs_of, summary_of) -> dict:
    recs = [r for t in tasks for r in recs_of(t)]
    called = [r for r in recs if r.get("status") in _CALLED]
    sums = [summary_of(t) for t in tasks if summary_of(t) is not None]
    lat = [r["latency_ms"] for r in called if isinstance(r.get("latency_ms"), (int, float))]
    non_skip = [r for r in recs if not str(r.get("status", "")).startswith("skipped")]
    task_n = len({r.get("task") for r in recs})
    return {
        "status_mix": dict(Counter(str(r.get("status")) for r in recs)),
        "failure_rate": _ratio(sum(r.get("status") in _FAILED for r in called), len(called)),
        "latency_p50": _percentile(lat, 0.5), "latency_p95": _percentile(lat, 0.95),
        "calls_per_task": _ratio(sum(r.get("calls", 1) if isinstance(r.get("calls"), int) else 1 for r in called),
                                 task_n),
        "pts_est_sum": sum(r["pts_est"] for r in recs if isinstance(r.get("pts_est"), (int, float))),
        "usd_equiv_sum": sum(r["usd_equiv"] for r in recs if isinstance(r.get("usd_equiv"), (int, float))),
        "cache_hit_share": _ratio(sum(r.get("status") == "cache_hit" for r in recs), len(non_skip)),
        "consulted": sum(1 for s in sums if s.get("consulted") is True),
        "sampled": sum(1 for s in sums if s.get("consulted") is False),
    }


def _last50_failure(tasks, recs_of):
    called = [r for t in tasks for r in recs_of(t) if r.get("status") in _CALLED][-50:]
    return _ratio(sum(r.get("status") in _FAILED for r in called), len(called))


def _verdict(f, m, ctx) -> tuple[str, list[str]]:
    """PROMOTE / KEEP SHADOW / DEMOTE for one field (§11.5), plus the bars that lacked data."""
    bars = _PROMOTE_BARS
    acc, rules_acc, lo = m["accuracy"], ctx["rules_acc"][f], (m["accuracy_ci"] or [None])[0]
    missing = []
    if ctx["consulted"] < bars["consulted"]:
        missing.append("consulted tasks %d/%d" % (ctx["consulted"], bars["consulted"]))
    if ctx["medium"] < bars["medium"]:
        missing.append("medium+ consulted tasks %d/%d" % (ctx["medium"], bars["medium"]))
    if ctx["labelled"][f] < bars["labelled"]:
        missing.append("labelled tasks %d/%d" % (ctx["labelled"][f], bars["labelled"]))
    if acc is None:
        missing.append("no labelled answers to score")
    if m["brier"] is None:
        missing.append("no labelled answers for Brier")
    if ctx["failure_rate"] is None:
        missing.append("no call records")
    if (ctx["last50"] or 0) > 0.20 or ctx["drift"] or ctx["under"] or (
            ctx["labelled"][f] >= bars["labelled"] and acc is not None and rules_acc is not None
            and rules_acc - acc > 0.10 + 1e-9):
        return DEMOTE, missing
    ok = (not missing and acc >= bars["accuracy"] and m["coverage"] >= bars["coverage"] and rules_acc is not None
          and lo is not None and lo > rules_acc and m["brier"] <= bars["brier"]
          and ctx["failure_rate"] <= bars["failure"])
    return (PROMOTE if ok else KEEP), missing


def _under_routes(tasks, answered, risk_of) -> int:
    """Tasks labelled risk high where a decider that answered left the arm's risk lower."""
    return sum(1 for t in tasks
               if answered(t) and t.labels.get("risk") == "high" and _rank(RISK, risk_of(t)) < 2)


def evaluate(cfg, home: str, since: str | None, labels_path: str | None) -> dict:
    labels = _read_labels(labels_path)
    tasks = _load_tasks(home, since, labels)
    jcfg, ccfg = cfg.judge or {}, cfg.classifier or {}
    th = jcfg.get("thresholds") or {}
    act_p, raise_p = th.get("act_p", policy.ACT_P), th.get("raise_p", policy.RAISE_P)
    min_conf = ccfg.get("min_conf", 0.6)
    jg = {t.tid: _judge_gives(t, act_p, raise_p) for t in tasks}
    cg = {t.tid: _clf_gives(t, min_conf) for t in tasks}
    gives = {"rules": {}, "judge": jg, "classifier": cg,
             "judge+classifier": {t.tid: _combined(t, jg[t.tid], cg[t.tid]) for t in tasks}}
    arms = {a: {f: _arm_metrics(tasks, gives[a], f) for f in FIELDS} for a in ARMS}
    rules_acc = {f: arms["rules"][f]["accuracy"] for f in FIELDS}
    has_data = any(t.judge_recs or t.clf_recs for t in tasks)
    specs = {
        "judge": (lambda t: t.judge_recs, lambda t: t.jsum, lambda t: jg[t.tid],
                  lambda t, theta: _judge_gives(t, theta, raise_p), _judge_pairs),
        "classifier": (lambda t: t.clf_recs, lambda t: t.csum, lambda t: cg[t.tid],
                       lambda t, theta: _clf_gives(t, theta), _clf_pairs),
    }
    deciders = {}
    for name, (recs_of, summary_of, gives_at, sweep_at, pairs_of) in specs.items():
        fields = _decider_fields(tasks, gives_at, sweep_at, pairs_of)
        ops = _ops(tasks, recs_of, summary_of)
        consulted = [t for t in tasks if (summary_of(t) or {}).get("consulted") is True]
        under = _under_routes(tasks, lambda t: (t.parsed if name == "judge" else t.clf) is not None,
                              lambda t: gives_at(t).get("risk", t.rules.get("risk")))
        drift = any(r.get("status") == "model_drift" for t in tasks for r in recs_of(t))
        ctx = {"consulted": len(consulted), "medium": sum(t.medium_plus for t in consulted), "under": under,
               "labelled": {f: sum(f in t.labels for t in tasks) for f in FIELDS}, "rules_acc": rules_acc,
               "failure_rate": ops["failure_rate"], "last50": _last50_failure(tasks, recs_of), "drift": drift}
        verdicts, missing = {}, {}
        for f in FIELDS:
            verdicts[f], missing[f] = _verdict(f, fields[f], ctx)
            if not has_data:
                verdicts[f], missing[f] = KEEP, missing[f] or ["no shadow data"]
        deciders[name] = {"verdicts": verdicts, "missing": missing, "fields": fields, "ops": ops,
                          "under_routes": under}
    return {"no_data": not has_data, "since": since, "tasks": len(tasks),
            "labelled": sum(t.tid in labels for t in tasks), "arms": arms, "deciders": deciders}


# ---- text output ----

def _n(v) -> str:
    return "-" if v is None else ("%.3f" % v if isinstance(v, float) else str(v))


def _table(head: list[str], rows: list[list]) -> list[str]:
    return (["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
            + ["| " + " | ".join(_n(c) for c in r) + " |" for r in rows] + [""])


def render(payload: dict, today: str) -> str:
    lines = ["# mr eval report", "",
             "Generated %s (IST). Window since %s. Tasks: %d, labelled: %d." % (
                 today, payload["since"] or "the start", payload["tasks"], payload["labelled"]), ""]
    if payload["no_data"]:
        lines += ["no shadow data: the window has no judge or classifier records; every verdict stays KEEP SHADOW.", ""]
    lines += ["## Arms", ""]
    lines += _table(["arm", "field", "n", "coverage", "agreement", "accuracy"],
                    [[a, f, m["n"], m["coverage"], m["agreement"], m["accuracy"]]
                     for a in ARMS for f, m in payload["arms"][a].items()])
    for name, dec in payload["deciders"].items():
        lines += ["## %s" % name.capitalize(), "", "Verdicts (promotion stays a human decision):", ""]
        lines += _table(["field", "verdict", "missing"],
                        [[f, dec["verdicts"][f], "; ".join(dec["missing"][f]) or "-"] for f in FIELDS])
        lines += _table(["field", "coverage", "accuracy", "accuracy 95% CI", "brier", "ece"],
                        [[f, m["coverage"], m["accuracy"], "-" if m["accuracy_ci"] is None else "[%.3f, %.3f]" % tuple(
                            m["accuracy_ci"]), m["brier"], m["ece"]] for f, m in dec["fields"].items()])
        lines += ["High-risk under-routes: %d" % dec["under_routes"], "", "Ops:", ""]
        lines += ["- %s: %s" % (k, json.dumps(v) if isinstance(v, dict) else _n(v)) for k, v in dec["ops"].items()]
        lines += [""]
    return "\n".join(lines)


def sweep_text(payload: dict) -> str:
    lines = []
    for name, dec in payload["deciders"].items():
        lines += ["%s threshold sweep (coverage/accuracy)" % name]
        rows = []
        for i, theta in enumerate(THETAS):
            rows.append(["%g" % theta] + ["%s/%s" % (_n(dec["fields"][f]["selective"][i]["coverage"]),
                                                    _n(dec["fields"][f]["selective"][i]["accuracy"]))
                                          for f in FIELDS])
        lines += _table(["theta"] + list(FIELDS), rows)
    return "\n".join(lines)
