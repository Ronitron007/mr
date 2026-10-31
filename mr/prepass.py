"""Deterministic prepass (mr-spec §5.2, docs/p0/README.md §1)."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mr.task import Task

_LEAD = "([{" + "\"'`"
_TRAIL = ")]}" + "\"'`.,;:!?"

_DIFF_ORDER = ("trivial", "easy", "medium", "hard", "critical")

_TYPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("code", "implement|implements|implementing|feature|endpoint|integrate|wire up"),
    ("bugfix", "fix|fixes|fixed|fixing|bug|bugs|crash|crashes|crashing|broken|regression"),
    ("refactor", "refactor|refactoring|rename|extract|simplify|dedupe|clean up"),
    ("test", "test|tests|testing|coverage|vitest"),
    ("ios-native", "ios|xcode|swift|swiftui|cocoapods|pod install|native module"),
    ("ui", "ui|screen|screens|button|buttons|layout|styling|animation|modal|dark mode"),
    ("review", "review|audit"),
    ("plan", "plan|roadmap|proposal|design doc"),
    ("explain", "explain|explains|how does|what does|why does|walk me through"),
    ("docs", "docs|documentation|readme|changelog|jsdoc"),
)

_RISK_TEXT: tuple[tuple[str, str], ...] = (
    (
        "auth",
        "auth|authn|authz|authentication|authorization|login|logout|log in|log out"
        "|sign[- ]?in|sign[- ]?up|password|passwords|oauth|jwt|rls",
    ),
    (
        "payments",
        "payment|payments|billing|stripe|checkout|subscription|subscriptions"
        "|invoice|invoices|refund|refunds|in-app purchases?|iap|revenuecat",
    ),
    ("migration", "migration|migrations|migrate|alter table|schema change"),
    (
        "data-deletion",
        "delete|deletes|deleted|deleting|deletion|purge|truncate|wipe|drop table",
    ),
    (
        "native-config",
        "entitlement|entitlements|cocoapods|pod install|config plugins?"
        "|bundle identifier|provisioning profile",
    ),
    (
        "deps",
        "dependency|dependencies|devdependency|devdependencies|npm install|yarn add"
        "|pnpm add|expo install|upgrade|bump|lockfile",
    ),
    (
        "secrets",
        "secret|secrets|api key|api keys|private key|credential|credentials"
        "|env var|env vars|environment variables?",
    ),
)

_RISK_PATH: dict[str, str] = {
    "auth": r"(^|/)(auth|login|signin|signup)(/|\.|$)",
    "payments": r"(^|/)(payments?|billing|stripe|checkout|subscriptions?|iap)(/|\.|$)",
    "migration": r"(^|/)migrations?(/|$)",
    "data-deletion": r"(^|/)(delete|deletion|purge)[\w-]*(/|\.|$)",
    "native-config": (
        r"(^|/)(ios|android)/|(^|/)podfile$|(^|/)app\.json$|(^|/)app\.config\.\w+$"
        r"|\.entitlements$|(^|/)info\.plist$"
    ),
    "deps": (
        r"(^|/)(package\.json|package-lock\.json|yarn\.lock|pnpm-lock\.yaml"
        r"|bun\.lockb?|podfile|podfile\.lock)$"
    ),
    "secrets": (
        r"(^|/)\.env[^/]*$|\.(pem|key|p8|p12|keystore|mobileprovision)$"
        r"|(^|/)(googleservice-info\.plist|google-services\.json|eas\.json)$"
        r"|(^|/)(credentials|secrets)[^/]*$"
        r"|(^|/)supabase/\.temp/"
    ),
}

_HIGH = frozenset({"auth", "payments", "migration", "data-deletion"})
_MED = frozenset({"native-config", "deps", "secrets"})

_LAYERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^supabase/", re.IGNORECASE),
    re.compile(r"(^|/)(screens|navigation)/|^app/", re.IGNORECASE),
    re.compile(r"^(ios|android|plugins)/|(^|/)app\.(json|config\.\w+)$", re.IGNORECASE),
    re.compile(r"(^|/)(store|stores|state|api|queries|services)/", re.IGNORECASE),
)

_PUBLIC = re.compile(
    r"\b(exported? (types?|interfaces?)|public api|api contract|db tables?|data model)\b",
    re.IGNORECASE,
)
_NEW_MODULE = re.compile(
    r"\b(new (module|package|screen|native module)|create (a |the )?(module|package))\b",
    re.IGNORECASE,
)
_JUDGMENT = re.compile(
    r"\b(feel|animation|animations|design|flow|polish|look and feel|ux)\b",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"\b(?:maybe|perhaps|probably|somehow|something|stuff|etc|whatever"
    r"|not sure|figure out|tbd|improve|nicer)\b",
    re.IGNORECASE,
)

_PATH_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"[\w.@*~-]*/[\w.@*~/-]*", re.IGNORECASE),
    re.compile(
        r"[\w.@*~-]*\.(?:ts|tsx|js|jsx|mjs|cjs|json|sql|md|yml|yaml|plist"
        r"|entitlements|swift|kt|gradle|toml|lock|css|html|sh|pem|key|p8|p12"
        r"|keystore|mobileprovision|env)",
        re.IGNORECASE,
    ),
    re.compile(r"\.env[\w.-]*", re.IGNORECASE),
    re.compile(r"(?:Podfile|Gemfile)(?:\.lock)?", re.IGNORECASE),
)


def _words(alternation: str) -> re.Pattern[str]:
    return re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)


_TYPE_RES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, _words(alts)) for name, alts in _TYPES
)
_RISK_RES: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = tuple(
    (name, _words(alts), re.compile(_RISK_PATH[name], re.IGNORECASE))
    for name, alts in _RISK_TEXT
)


def _strip_token(token: str) -> str:
    return token.lstrip(_LEAD).rstrip(_TRAIL)


def _is_path(token: str) -> bool:
    return bool(token) and any(rx.fullmatch(token) for rx in _PATH_RES)


def path_tokens(text: str) -> tuple[str, ...]:
    seen: list[str] = []
    for token in text.split():
        stripped = _strip_token(token)
        if not _is_path(stripped) or stripped in seen:
            continue
        seen.append(stripped)
    return tuple(seen)


def prose(text: str) -> str:
    kept = [token for token in text.split() if not _is_path(_strip_token(token))]
    return " ".join(kept)


def type_hits(prose: str) -> dict[str, int]:
    return {name: len(rx.findall(prose)) for name, rx in _TYPE_RES}


def _chosen_type(hits: dict[str, int]) -> str:
    best = 0
    chosen = "code"
    for name, _rx in _TYPE_RES:
        count = hits.get(name, 0)
        if count > best:
            best = count
            chosen = name
    return chosen


def risk_tags(prose: str, files: Sequence[str]) -> frozenset[str]:
    found: set[str] = set()
    for name, text_rx, path_rx in _RISK_RES:
        if text_rx.search(prose) or any(path_rx.search(path) for path in files):
            found.add(name)
    return frozenset(found)


def floors(tags: frozenset[str], n: int) -> tuple[str, str]:
    if tags & _HIGH:
        risk, difficulty = "high", "hard"
    elif tags & _MED:
        risk, difficulty = "medium", "medium"
    else:
        risk, difficulty = "low", "trivial"
    if n >= 8:
        difficulty = "hard"
    return risk, difficulty


def ambiguity(text: str, prose: str, n: int) -> int:
    return int(n == 0) + int(_HEDGE.search(prose) is not None) + int("?" in text)


def confident(hits: dict[str, int], tags: frozenset[str], n: int, amb: int) -> bool:
    one_type = sum(1 for count in hits.values() if count >= 1) == 1
    band_clear = bool(tags) or n >= 8 or n <= 2
    return one_type and band_clear and amb <= 1


def _difficulty_est(floor: str, n: int) -> str:
    if n <= 1:
        band = "trivial"
    elif n == 2:
        band = "easy"
    elif n <= 7:
        band = "medium"
    else:
        band = "hard"
    return floor if _DIFF_ORDER.index(floor) >= _DIFF_ORDER.index(band) else band


def _cross_layer(files: Sequence[str]) -> int:
    return sum(1 for rx in _LAYERS if any(rx.search(path) for path in files))


@dataclass(frozen=True)
class Features:
    type_hits: dict[str, int]
    files_named: tuple[str, ...]
    grep_hit_files: int
    risk_tags: frozenset[str]
    risk_floor: str
    difficulty_floor: str
    difficulty_est: str
    cross_layer: int
    public_interface: bool
    new_module: bool
    ambiguity: int
    judgment_keywords: bool
    confident: bool
    type: str
    n_files: int


def _assemble(text: str, files: tuple[str, ...], grep_hits: int) -> Features:
    body = prose(text)
    hits = type_hits(body)
    tags = risk_tags(body, files)
    n = len(files)
    risk, difficulty = floors(tags, n)
    amb = ambiguity(text, body, n)
    return Features(
        type_hits=hits,
        files_named=files,
        grep_hit_files=grep_hits,
        risk_tags=tags,
        risk_floor=risk,
        difficulty_floor=difficulty,
        difficulty_est=_difficulty_est(difficulty, n),
        cross_layer=_cross_layer(files),
        public_interface=_PUBLIC.search(body) is not None,
        new_module=_NEW_MODULE.search(body) is not None,
        ambiguity=amb,
        judgment_keywords=_JUDGMENT.search(body) is not None,
        confident=confident(hits, tags, n, amb),
        type=_chosen_type(hits),
        n_files=n,
    )


def extract_from(
    text: str, files_named: Sequence[str] | None
) -> Features:
    files = path_tokens(text) if files_named is None else tuple(files_named)
    return _assemble(text, files, 0)


def _grep_hit_files(repo: str | None, files: tuple[str, ...]) -> int:
    if repo is None or not files:
        return 0
    try:
        done = subprocess.run(
            ["git", "-C", repo, "ls-files", "--", *files],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return 0
    found = set((done.stdout or "").splitlines())
    return sum(1 for path in files if path in found)


def extract(task: Task, repo: str | None) -> Features:
    files = tuple(task.files_named)
    return _assemble(task.text, files, _grep_hit_files(repo, files))
