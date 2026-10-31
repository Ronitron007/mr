"""Make mr traces safe to publish (public demo data). Never mutates its input."""
from __future__ import annotations

import copy
import os
import re

PUBLIC_REPOS = ("scratch-repo",)
STEP_DROP = ("summary", "pushback", "requirements", "patch", "raw", "session_id", "task_hint")
TRACE_DROP = ("dirs", "commit", "caller")
SECRET = re.compile(
    r"(gh[opsu]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}|xox[bpas]-[A-Za-z0-9-]{8,}"
    r"|AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._~+/=-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S{6,})",
    re.I,
)
PATH = re.compile(r"/(home|workspace|tmp|root|Users)/")


def public_repos():
    """PUBLIC_REPOS plus MR_TRACE_PUBLIC_REPOS, read on this call."""
    raw = os.environ.get("MR_TRACE_PUBLIC_REPOS", "")
    extra = tuple(name.strip() for name in raw.split(",") if name.strip())
    return PUBLIC_REPOS + extra


_public_repos = public_repos


def scrub_traces(traces, public_repos=None):
    """Public demo data. Titles are always synthetic: real task text never leaves the box."""
    if public_repos is None:
        public_repos = _public_repos()
    aliases: dict[str, str] = {}
    ids: dict[str, str] = {}
    out = []
    for t in traces or []:
        t = copy.deepcopy(t)
        repo = t.get("repo")
        tid = str(t.get("task") or "")
        if repo and repo not in public_repos:
            aliases.setdefault(repo, f"private-repo-{len(aliases) + 1}")
            t["repo"] = aliases[repo]
        if not tid.startswith("t-"):  # labels like 'review-<feature>' name the work; replace them
            ids.setdefault(tid, f"review-{len(ids) + 1}")
            t["task"] = ids[tid]
        t["title"] = synthetic_title(t)
        t["why"] = None
        for k in TRACE_DROP:
            t[k] = None
        for s in t.get("steps") or []:
            for k in STEP_DROP:
                s[k] = None
            s["tools"] = [{"name": x.get("name"), "kind": x.get("kind")} for x in (s.get("tools") or []) if x.get("kind") != "say"]
        t["events"] = [{k: e.get(k) for k in ("kind", "t", "rung", "allow", "reason", "label")} for e in (t.get("events") or [])]
        out.append(_strings(t))
    return out


def synthetic_title(t: dict) -> str:
    """e.g. 'Code task, medium, 2 attempts (demo)': built only from routing facts, never from task text."""
    steps = t.get("steps") or []
    n = sum(1 for s in steps if (s or {}).get("role") == "implementer")
    if not str(t.get("task") or "").startswith("t-"):
        return "Standalone review (demo)"
    kind = (t.get("type") or "task").capitalize()
    diff = t.get("difficulty") or "unknown difficulty"
    att = f", {n} attempt{'s' if n != 1 else ''}" if n else ""
    return f"{kind} task, {diff}{att} (demo)"


def _strings(o):
    if isinstance(o, dict):
        return {k: _strings(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_strings(v) for v in o]
    if isinstance(o, str):
        if SECRET.search(o):
            return "[redacted]"
        if PATH.search(o):
            return "[path]"
    return o


def find_secrets(obj) -> list[str]:
    hits: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            hits += find_secrets(v)
    elif isinstance(obj, list):
        for v in obj:
            hits += find_secrets(v)
    elif isinstance(obj, str) and SECRET.search(obj):
        hits.append(obj)
    return hits
