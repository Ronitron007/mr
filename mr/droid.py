"""Droid (Factory CLI, BYOK) backend for the Sonnet rungs S and S+: settings file, sticky state, availability.

The API key is never read into a file or a record: the settings file holds ``${VAR}`` references only
and the key reaches the droid child through its environment (see runners.droid_child_env).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time

from mr import config
from mr.config import ConfigError

STATUSES = ("ok", "credits_exhausted", "auth_failed")
DETAIL_CAP = 200
KEY_MASK = "<redacted:key>"
_REF = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}\Z")
_CREDITS = re.compile(r"credit balance is too low|\b402\b|billing|insufficient", re.I)
_AUTH = re.compile(r"\b401\b|\b403\b|authentication_error|permission_error", re.I)
_TRANSIENT = re.compile(r"\b429\b|\b529\b|rate_limit_error|overloaded", re.I)


# --- settings file ------------------------------------------------------------------

def model_id(twin: dict) -> str:
    """The id droid gives a custom model: custom:<displayName>-<index>; mr keeps names unique, so index 0."""
    return f"custom:{twin['name']}-0"


def settings_doc(cfg) -> dict:
    """The customModels document, one entry per twin. apiKey is always an environment reference."""
    block = cfg.droid
    models = []
    for twin in block["twins"].values():
        provider = twin["provider"]
        models.append({
            "model": twin["model"], "displayName": twin["name"],
            "baseUrl": config.DROID_PROVIDERS[provider]["base_url"],
            "apiKey": "${" + block["provider_key_env"][provider] + "}",
            "provider": provider, "maxOutputTokens": twin["max_output"],
            "enableThinking": True, "thinkingMaxTokens": twin["thinking_tokens"]})
    return {"customModels": models}


def _literal_key(node) -> bool:
    if isinstance(node, dict):
        return any((k == "apiKey" and not (isinstance(v, str) and _REF.match(v))) or _literal_key(v)
                   for k, v in node.items())
    if isinstance(node, list):
        return any(_literal_key(v) for v in node)
    return False


def _write_private(path: str, text: str) -> None:
    """Atomic write, mode 0600 (mkstemp creates the file private)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def ensure_settings(cfg) -> str:
    """Write the settings file when missing or different. 'written' or 'ok'.

    An existing file with an apiKey that is not a ${VAR} reference is refused and left untouched.
    """
    path, want = cfg.droid["settings"], settings_doc(cfg)
    have = None
    try:
        with open(path, encoding="utf-8") as fh:
            have = json.load(fh)
    except (OSError, ValueError):
        pass
    if _literal_key(have):
        raise ConfigError(f"droid settings file {path}: an apiKey is not a ${{VAR}} reference; "
                          "remove the literal key (mr never uses one)")
    if have == want:
        return "ok"
    _write_private(path, json.dumps(want, indent=2) + "\n")
    return "written"


# --- keys ------------------------------------------------------------------------------

def key_envs(cfg) -> list[str]:
    """Environment variable names that hold the provider keys the twins use."""
    block = cfg.droid
    names = {block["provider_key_env"][t["provider"]] for t in block["twins"].values()}
    return sorted(names)


def key_values(cfg) -> list[str]:
    return [v for v in (os.environ.get(n) for n in key_envs(cfg)) if v]


def scrub(text: str, keys=()) -> str:
    """Mask every key value in text (plain and JSON-escaped)."""
    for key in keys:
        for form in (key, json.dumps(key)[1:-1]):
            text = text.replace(form, KEY_MASK)
    return text


# --- sticky state --------------------------------------------------------------------------

def state_path(cfg) -> str:
    return os.path.join(cfg.paths.home, "state", "droid.json")


def read_state(cfg) -> dict:
    """{status, since, recheck_after, detail}. A missing or unreadable file means ok."""
    try:
        with open(state_path(cfg), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or doc.get("status") not in STATUSES:
        return {"status": "ok", "since": 0.0, "recheck_after": 0.0, "detail": ""}
    return {"status": doc["status"], "since": doc.get("since") or 0.0,
            "recheck_after": doc.get("recheck_after") or 0.0, "detail": str(doc.get("detail") or "")}


def write_state(cfg, status: str, now: float, detail: str = "") -> dict:
    if status not in STATUSES:
        raise ValueError(f"unknown droid status: {status!r}")
    text = " ".join(scrub(detail or "", key_values(cfg)).split())[:DETAIL_CAP]
    doc = {"status": status, "since": now, "recheck_after": now + (cfg.droid["recheck_s"] if status != "ok" else 0),
           "detail": text}
    _write_private(state_path(cfg), json.dumps(doc) + "\n")
    return doc


def reset_state(cfg) -> None:
    try:
        os.unlink(state_path(cfg))
    except FileNotFoundError:
        pass


# --- availability and failure classes -------------------------------------------------------

def binary_path(cfg) -> str | None:
    """The resolvable droid command, or None."""
    cmd = cfg.paths.droid
    if os.path.isabs(cmd):
        return cmd if os.path.isfile(cmd) and os.access(cmd, os.X_OK) else None
    return shutil.which(cmd)


def available(cfg, now: float | None = None) -> tuple[bool, str]:
    """(True, '') when mr may run a rung on droid now, else (False, reason)."""
    block = cfg.droid
    if not block or not block.get("enabled"):
        return False, "droid is not enabled"
    if binary_path(cfg) is None:
        return False, f"droid binary not found: {cfg.paths.droid}"
    for name in key_envs(cfg):
        if not os.environ.get(name):
            return False, f"{name} is empty or unset"
    state = read_state(cfg)
    when = time.time() if now is None else now
    if state["status"] != "ok" and when < state["recheck_after"]:
        return False, f"{state['status']} until {state['recheck_after']:.0f} (epoch seconds)"
    return True, ""


def classify_failure(result_text: str, stderr: str) -> str:
    """'credits' | 'auth' | 'transient' | 'other' from the final text and stderr of a failed droid run."""
    text = f"{result_text or ''}\n{stderr or ''}"
    for kind, pat in (("credits", _CREDITS), ("auth", _AUTH), ("transient", _TRANSIENT)):
        if pat.search(text):
            return kind
    return "other"
