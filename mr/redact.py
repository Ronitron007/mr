"""Redaction for any text that leaves mr (judge-spec §9.7 rules 1-10). Pure; reads os.environ only when asked."""
from __future__ import annotations

import fnmatch
import math
import os
import re
from collections import Counter
from collections.abc import Mapping

MAX_REDACTIONS = 25  # rule 10: more than this in one state means the caller aborts the send

SENSITIVE_GLOBS = (
    "**/.env*", "**/*.pem", "**/*.key", "**/*.p8", "**/*.p12", "**/*.mobileprovision", "**/*.keystore",
    "**/GoogleService-Info.plist", "**/google-services.json", "**/credentials*", "**/secrets*",
    "supabase/.temp/**", "eas.json",
)

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_MARKER = re.compile(r"<redacted:\w+>")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_JWT = re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}")
_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_-]{16,}"
    r"|gh[pous]_[A-Za-z0-9]{20,}"
    r"|github_pat_\w{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[\w-]{35}"
    r"|sbp_[a-f0-9]{40}"
    r"|(?:sk|rk|pk)_(?:live|test)_\w{16,}"
    r")"
)
_ASSIGN = re.compile(
    r"""(?i)((?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret"""
    r"""|authorization|bearer)\s*[:=]\s*['"]?)(?!<redacted:)[^\s'"]{8,}"""
)
_URL_USERINFO = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]*)?@")
_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_HEX_SHA = re.compile(r"[0-9a-fA-F]{7,40}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\d{10,}")
_ENV_NAME = re.compile(r"(?i)TOKEN|KEY|SECRET|PASSWORD")


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _is_secret_run(run: str) -> bool:
    return not _HEX_SHA.fullmatch(run) and _entropy(run) >= 4.0


def _env_values(env: Mapping[str, str]) -> list[str]:
    vals = {v for k, v in env.items() if isinstance(v, str) and len(v) >= 8 and _ENV_NAME.search(k)}
    return sorted(vals, key=len, reverse=True)


def redact(text: str, env: Mapping[str, str] | None = None) -> tuple[str, dict[str, int]]:
    """Strip ANSI, then apply rules 1-8 in order. Returns (text, {class: replacements})."""
    counts: dict[str, int] = {}

    def sub(pattern: re.Pattern, repl, cls: str, s: str) -> str:
        s, n = pattern.subn(repl, s)
        if n:
            counts[cls] = counts.get(cls, 0) + n
        return s

    text = _ANSI.sub("", text)
    text = sub(_PEM, "<redacted:private_key>", "private_key", text)
    text = sub(_JWT, "<redacted:jwt>", "jwt", text)
    text = sub(_TOKEN, "<redacted:token>", "token", text)
    text = sub(_ASSIGN, lambda m: m.group(1) + "<redacted:assignment>", "assignment", text)
    text = sub(_URL_USERINFO, lambda m: m.group(1) + "<redacted:url_userinfo>@", "url_userinfo", text)
    runs = [m for m in _RUN.finditer(text) if _is_secret_run(m.group(0))]
    for m in reversed(runs):
        text = text[:m.start()] + "<redacted:high_entropy>" + text[m.end():]
    if runs:
        counts["high_entropy"] = len(runs)
    text = sub(_EMAIL, "<redacted:email>", "email", text)
    text = sub(_PHONE, "<redacted:phone>", "phone", text)
    for value in _env_values(os.environ if env is None else env):
        parts = re.split(f"({_MARKER.pattern})", text)  # never touch a marker already inserted
        hits = 0
        for i in range(0, len(parts), 2):
            hits += parts[i].count(value)
            parts[i] = parts[i].replace(value, "<redacted:env>")
        if hits:
            counts["env"] = counts.get("env", 0) + hits
            text = "".join(parts)
    return text, counts


def redact_obj(obj, env: Mapping[str, str] | None = None) -> tuple[object, dict[str, int]]:
    """Redact every string value and list/tuple item in nested data; keys and non-strings stay as they are."""
    counts: dict[str, int] = {}

    def walk(o):
        if isinstance(o, str):
            out, got = redact(o, env)
            for k, v in got.items():
                counts[k] = counts.get(k, 0) + v
            return out
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, tuple):
            return tuple(walk(v) for v in o)
        return o

    return walk(obj), counts


def is_sensitive_path(path: str) -> bool:
    """Rule 9: repo-relative path match; a leading ``**/`` also matches at the top level."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    for glob in SENSITIVE_GLOBS:
        if fnmatch.fnmatchcase(p, glob) or (glob.startswith("**/") and fnmatch.fnmatchcase(p, glob[3:])):
            return True
    return False


def too_many(counts: Mapping[str, int]) -> bool:
    return sum(counts.values()) > MAX_REDACTIONS
