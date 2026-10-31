"""Diff-scope signal D (spec §5.9, §10.6, §9.7 step 9)."""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass

# §9.7 step 9. "**" matches across "/"; a single "*" does not.
SENSITIVE = (
    "**/.env*", "**/*.pem", "**/*.key", "**/*.p8", "**/*.p12", "**/*.mobileprovision",
    "**/*.keystore", "**/GoogleService-Info.plist", "**/google-services.json",
    "**/credentials*", "**/secrets*", "supabase/.temp/**", "eas.json",
)
# Basename globs, plus anything under ios/ or android/.
DEP_CONFIG = (
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "Podfile", "Podfile.lock", "app.json", "app.config.*", "tsconfig*.json",
    "vitest.config.*", "vite.config.*", "babel.config.*", "metro.config.*",
    ".eslintrc*", "eslint.config.*",
)
# trivial/easy: spec §10.6. medium+ are a v1 decision — L0 `run --once` may run
# without a planner, so those difficulties still need an est_loc and a file cap.
NO_CHECKLIST_LIMITS = {
    "trivial": (40, 2), "easy": (120, 4), "medium": (300, 8), "hard": (400, 12), "critical": (400, 12),
}
_GLOBS: dict[str, re.Pattern[str]] = {}


@dataclass(frozen=True)
class SignalD:
    ok: bool
    reasons: tuple[str, ...]
    files: tuple[str, ...]
    loc: int
    est_loc: int


def _glob(pat: str) -> re.Pattern[str]:
    if pat not in _GLOBS:
        i, parts = 0, ["^"]
        while i < len(pat):
            if pat.startswith("**/", i):
                parts.append("(?:.*/)?")
                i += 3
            elif pat.startswith("**", i):
                parts.append(".*")
                i += 2
            elif pat[i] == "*":
                parts.append("[^/]*")
                i += 1
            elif pat[i] == "?":
                parts.append("[^/]")
                i += 1
            else:
                parts.append(re.escape(pat[i]))
                i += 1
        _GLOBS[pat] = re.compile("".join(parts) + "$")
    return _GLOBS[pat]


_TEST_SEGMENTS = frozenset({"tests", "test", "__tests__"})
_TEST_BASES = ("test_*.py", "*_test.py", "*.test.*", "*.spec.*")


def _is_test_path(path: str) -> bool:
    """True when signal D should omit this path from the loc total."""
    parts = [part for part in path.replace("\\", "/").split("/") if part and part != "."]
    if any(part in _TEST_SEGMENTS for part in parts):
        return True
    base = parts[-1] if parts else ""
    return any(fnmatch.fnmatchcase(base, pat) for pat in _TEST_BASES)


def _is_dep(path: str) -> bool:
    base, dirs = path.rsplit("/", 1)[-1], path.split("/")[:-1]
    return any(fnmatch.fnmatchcase(base, pat) for pat in DEP_CONFIG) or "ios" in dirs or "android" in dirs


def _rename_dest(path: str) -> str:
    braced = re.fullmatch(r"(.*)\{(.*?) => (.*?)\}(.*)", path)
    if braced:
        return braced.group(1) + braced.group(3) + braced.group(4)
    return path.split(" => ", 1)[1].strip() if " => " in path else path


def changed_files(patch: bytes) -> list[tuple[str, int, int]]:
    """(path, added, deleted) via `git apply --numstat`. Binary files count as (0, 0)."""
    if not patch or not patch.strip():
        return []
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env["GIT_CONFIG_GLOBAL"] = env["GIT_CONFIG_SYSTEM"] = os.devnull
    with tempfile.TemporaryDirectory(prefix="mr-scope-") as tmp:
        proc = subprocess.run(["git", "apply", "--numstat", "-"], cwd=tmp, env=env,
                              input=patch, capture_output=True, check=False)
    rows: list[tuple[str, int, int]] = []
    for raw in proc.stdout.splitlines():
        parts = raw.decode("utf-8", "replace").split("\t")
        if len(parts) < 3:
            continue
        path = _rename_dest(parts[2].strip().strip('"'))
        if path:
            rows.append((path, 0 if parts[0] == "-" else int(parts[0]),
                         0 if parts[1] == "-" else int(parts[1])))
    return rows


def scope(patch: bytes, *, checklist: dict | None, difficulty: str,
          files_named: tuple[str, ...], locked_files: tuple[str, ...] = (),
          forbidden: tuple[str, ...] = ()) -> SignalD:
    """One threshold. Every breach is recorded; ok is true only when the list is empty."""
    rows = changed_files(patch)
    files = tuple(dict.fromkeys(path for path, _, _ in rows))
    loc = sum(added + deleted for path, added, deleted in rows if not _is_test_path(path))
    named = tuple(files_named)
    globs = list(forbidden)
    if checklist:
        block = checklist.get("scope") or {}
        globs = list(block.get("forbidden") or []) + list(forbidden)
        modify = set(block.get("modify") or [])
        allowed = modify | set(block.get("create") or [])
        est, cap = int(checklist["est_loc"]), None
    else:
        est, cap = NO_CHECKLIST_LIMITS[difficulty]
        modify, allowed = set(), set()
    named_set, dirs, locked = set(named), {os.path.dirname(p) for p in named}, set(locked_files)
    reasons: list[str] = []
    for path in files:
        if path in locked:
            reasons.append(f"locked test edited: {path}")
        if any(_glob(pat).match(path) for pat in globs):
            reasons.append(f"forbidden: {path}")
        if checklist:
            if path not in allowed:
                reasons.append(f"out of scope: {path}")
            if _is_dep(path) and path not in modify:
                reasons.append(f"dependency/config file: {path}")
            continue
        in_scope = (not named) or path in named_set or os.path.dirname(path) in dirs
        if not in_scope:
            reasons.append(f"out of scope: {path}")
        if path not in named_set and any(_glob(pat).match(path) for pat in SENSITIVE):
            reasons.append(f"sensitive path: {path}")
        if path not in named_set and _is_dep(path):
            reasons.append(f"dependency/config file: {path}")
    if cap is not None and len(files) > cap:
        reasons.append(f"too many files: {len(files)} > {cap}")
    if loc > 2 * est:
        reasons.append("too large: loc > 2×est")
    return SignalD(not reasons, tuple(reasons), files, loc, est)
