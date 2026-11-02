"""Facts stage (logged only): file candidates, risk evidence, sensitive paths and type features.

Everything here is deterministic. Git is read through one helper (`_git`), once for the tracked-file
listing and once per symbol. Nothing in this module changes a decision."""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from mr import prepass, redact, scope

MAX_CANDIDATES = 40
MAX_PER_GLOB = 20
MAX_SYMBOLS = 5
MAX_PER_SYMBOL = 10
MAX_QUOTE = 80
_VERBS = ("implement", "add", "fix", "refactor", "rename", "write", "test", "document", "explain",
          "review", "plan", "update", "remove", "migrate", "create", "build")
_SYMBOL = re.compile(
    r"`([A-Za-z_]\w{2,})`"
    r"|\b([A-Za-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+)\b"
    r"|\b([A-Za-z0-9]+(?:_[A-Za-z0-9]+)+)\b"
)
_SIBLING_EXTS = ("test.ts", "test.tsx", "spec.ts", "spec.tsx")
_RANK_NONE = 99


@dataclass(frozen=True)
class Candidate:
    path: str
    exists: bool
    why: tuple[str, ...]


@dataclass(frozen=True)
class RiskItem:
    tag: str
    source: str
    quote: str | None
    path: str | None


@dataclass(frozen=True)
class Facts:
    path_tokens: tuple[str, ...]
    paths_exist: dict[str, bool]
    candidates: tuple[Candidate, ...]
    risk_evidence: tuple[RiskItem, ...]
    sensitive_paths: tuple[str, ...]
    type_features: dict
    repo_files: int


def _git(repo: str, *argv: str) -> list[str]:
    """Run `git -C repo ...` (callers pass -z); any failure gives an empty list."""
    try:
        done = subprocess.run(["git", "-C", repo, *argv], capture_output=True, text=True,
                              errors="replace", check=False, timeout=30)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [p for p in (done.stdout or "").split("\0") if p]


def _norm(token: str) -> str:
    while token.startswith("./"):
        token = token[2:]
    return token


def _outside(token: str) -> bool:
    return token.startswith(("/", "~")) or ".." in token.split("/")


def _is_glob(token: str) -> bool:
    return any(ch in token for ch in "*?[")


def _glob_match(token: str, path: str) -> bool:
    if "[" in token:
        return fnmatch.fnmatchcase(path, token)
    return bool(scope._glob(token if "/" in token else "**/" + token).match(path))


class _Pool:
    """Candidates in first-seen order; a repeated path merges its why values."""

    def __init__(self) -> None:
        self.items: dict[str, tuple[bool, list[str]]] = {}

    def add(self, path: str, exists: bool, *why: str) -> None:
        _exists, got = self.items.setdefault(path, (exists, []))
        got.extend(w for w in why if w not in got)

    def existing(self) -> list[str]:
        return [p for p, (exists, _why) in self.items.items() if exists]


def _rank(why: Sequence[str]) -> int:
    best = _RANK_NONE
    for w in why:
        if w in ("named", "hint"):
            best = min(best, 0)
        elif w == "glob":
            best = min(best, 1)
        elif w.startswith("symbol:"):
            best = min(best, 2)
        elif w == "sibling_test":
            best = min(best, 3)
    return best


def _tokens(text: str, hints: Sequence[str]) -> tuple[tuple[str, ...], set[str]]:
    base = prepass.path_tokens(text)
    extra = tuple(h for h in dict.fromkeys(hints) if h not in base)
    return base + extra, {_norm(t) for t in base}


def _add_tokens(pool: _Pool, tokens: Sequence[str], named: set[str], listing: list[str],
                tracked: set[str]) -> tuple[dict[str, bool], list[str]]:
    """Named/hint file candidates now; returns paths_exist and the dir/glob tokens to expand."""
    paths_exist: dict[str, bool] = {}
    expand: list[str] = []
    for raw in tokens:
        tok = _norm(raw)
        if not tok or _outside(tok):
            continue
        base = "named" if tok in named else "hint"
        if tok in tracked:
            paths_exist[tok] = True
            pool.add(tok, True, base, "exists")
        elif _is_glob(tok) or tok.endswith("/") or any(p.startswith(tok + "/") for p in listing):
            expand.append(tok)
        else:
            paths_exist[tok] = False
            pool.add(tok, False, base, "new")
    return paths_exist, expand


def _expand(pool: _Pool, expand: Sequence[str], listing: list[str]) -> None:
    for tok in expand:
        if _is_glob(tok):
            hits = [p for p in listing if _glob_match(tok, p)]
        else:
            prefix = tok.rstrip("/") + "/"
            hits = [p for p in listing if p.startswith(prefix)]
        for path in hits[:MAX_PER_GLOB]:
            pool.add(path, True, "glob")


def _symbols(prose: str) -> list[str]:
    found: list[str] = []
    for m in _SYMBOL.finditer(prose):
        ticked, camel, snake = m.groups()
        name = ticked or camel or snake
        if (ticked or len(name) >= 6) and name not in found:
            found.append(name)
    return found[:MAX_SYMBOLS]


def _add_symbols(pool: _Pool, repo: str, prose: str) -> None:
    for name in _symbols(prose):
        for path in _git(repo, "grep", "-z", "-l", "-w", "-F", "-e", name)[:MAX_PER_SYMBOL]:
            pool.add(path, True, f"symbol:{name}")


def _siblings(path: str) -> list[str]:
    folder, base = os.path.split(path)
    stem = os.path.splitext(base)[0]
    names = [f"test_{stem}.py"] + [f"{stem}.{ext}" for ext in _SIBLING_EXTS]
    return [os.path.join("tests", f"test_{stem}.py")] + [os.path.join(folder, n) for n in names]


def _add_siblings(pool: _Pool, tracked: set[str]) -> None:
    for path in pool.existing():
        if scope._is_test_path(path):
            continue
        for sib in _siblings(path):
            if sib in tracked:
                pool.add(sib, True, "sibling_test")


def _candidates(pool: _Pool) -> tuple[Candidate, ...]:
    ranked = sorted(pool.items.items(), key=lambda kv: _rank(kv[1][1]))
    return tuple(Candidate(p, exists, tuple(why)) for p, (exists, why) in ranked[:MAX_CANDIDATES])


def _counts(c: Candidate) -> bool:
    """A path gives risk or sensitive evidence when it exists, or is a new path the text names."""
    return c.exists or "named" in c.why


def _text_risk(text: str) -> list[RiskItem]:
    body = prepass.prose(text)
    return [RiskItem(tag, "text", m.group(0)[:MAX_QUOTE], None)
            for tag, text_rx, _path_rx in prepass.RISK_PATTERNS for m in text_rx.finditer(body)]


def _path_risk(cands: Sequence[Candidate]) -> list[RiskItem]:
    return [RiskItem(tag, "path", None, c.path)
            for c in cands if _counts(c)
            for tag, _text_rx, path_rx in prepass.RISK_PATTERNS if path_rx.search(c.path)]


def _is_sensitive(path: str) -> bool:
    if any(scope._glob(pat).match(path) for pat in scope.SENSITIVE):
        return True
    wanted = prepass.HIGH_TAGS | prepass.MEDIUM_TAGS
    return any(tag in wanted and path_rx.search(path) for tag, _t, path_rx in prepass.RISK_PATTERNS)


def _lead_verb(text: str) -> str | None:
    sentence = re.split(r"[.!?\n]", text.strip(), maxsplit=1)[0]
    m = re.match(r"\W*([A-Za-z]+)", sentence)
    word = m.group(1).lower() if m else None
    return word if word in _VERBS else None


def _test_only(cands: Sequence[Candidate]) -> bool:
    existing = [c for c in cands if c.exists]
    named = [c for c in existing if "named" in c.why or "hint" in c.why]
    return bool(existing) and all(scope._is_test_path(c.path) for c in named)


def _type_features(text: str, cands: Sequence[Candidate]) -> dict:
    return {
        "lead_verb": _lead_verb(text),
        "test_only_scope": _test_only(cands),
        "keyword_hits": prepass.type_hits(prepass.prose(text[:300])),
    }


def build_facts(text: str, repo: str, *, hints: Sequence[str] = ()) -> Facts:
    tokens, named = _tokens(text, hints)
    listing = _git(repo, "ls-files", "-z")
    tracked = set(listing)
    pool = _Pool()
    paths_exist, expand = _add_tokens(pool, tokens, named, listing, tracked)
    _expand(pool, expand, listing)
    if listing:
        _add_symbols(pool, repo, prepass.prose(text))
    _add_siblings(pool, tracked)
    cands = _candidates(pool)
    return Facts(
        path_tokens=tokens,
        paths_exist=paths_exist,
        candidates=cands,
        risk_evidence=tuple(_text_risk(text) + _path_risk(cands)),
        sensitive_paths=tuple(sorted(c.path for c in cands if _counts(c) and _is_sensitive(c.path))),
        type_features=_type_features(text, cands),
        repo_files=len(listing),
    )


def try_build(text: str, repo: str, hints: Sequence[str] = ()):
    """build_facts, or the exception it raised. The facts stage never changes a run."""
    try:
        return build_facts(text, repo, hints=hints)
    except Exception as exc:
        return exc


def summary(facts: Facts) -> dict:
    """Counts only, for the route record and `mr route --json`."""
    by_why: dict[str, int] = {}
    for c in facts.candidates:
        for w in c.why:
            key = "symbol" if w.startswith("symbol:") else w
            by_why[key] = by_why.get(key, 0) + 1
    return {
        "path_tokens": len(facts.path_tokens),
        "exists": sum(1 for c in facts.candidates if c.exists),
        "new": sum(1 for c in facts.candidates if not c.exists),
        "candidates": len(facts.candidates),
        "by_why": by_why,
        "sensitive": len(facts.sensitive_paths),
        "repo_files": facts.repo_files,
    }


def evidence(facts: Facts) -> list[dict]:
    out: list[dict] = []
    for item in facts.risk_evidence:
        if item.source == "text":
            out.append({"tag": item.tag, "source": "text", "quote": redact.redact(item.quote or "")[0]})
        else:
            out.append({"tag": item.tag, "source": "path", "path": item.path})
    return out


def record_parts(result) -> tuple[dict, list[dict]]:
    """(facts, risk_evidence) log values for a Facts or for the exception try_build returned."""
    if isinstance(result, BaseException):
        return {"error": type(result).__name__}, []
    return summary(result), evidence(result)
