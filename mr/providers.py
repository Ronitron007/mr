"""Provider registry for `mr setup`. No account data is stored."""
from __future__ import annotations

import copy
import os
import shutil

from mr.defaults import defaults

_CLAUDE_RUNGS = ("S", "S+", "O", "O+", "S+A")
_CURSOR_TO_S = ("G-l", "G-m", "G-h", "C25", "K-h", "K-x")
_ANTHROPIC_RUNGS = frozenset(_CLAUDE_RUNGS)
_LEAVE = frozenset({"design_route", "defer"})
_HUMAN_SUFFIX = "+human_spec_review"

PROVIDERS = {
    "cursor": {
        "binary": "cursor-agent",
        "config_key": "cursor_agent",
        "rung_path": "cursor",
        "hint": "install Cursor CLI, then connect it: cursor-agent status",
    },
    "claude": {
        "binary": "claude",
        "config_key": "claude",
        "rung_path": "max",
        "hint": "install Claude Code, then run claude once to connect it",
    },
}


def _executable(path) -> str | None:
    if not isinstance(path, str) or path == "":
        return None
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return os.path.abspath(path)
    return None


def detect(overrides=None) -> dict:
    """Resolved absolute binary per provider, or None when it is not usable."""
    overrides = overrides or {}
    found = {}
    for pid, spec in PROVIDERS.items():
        if pid in overrides:
            found[pid] = _executable(overrides[pid])
        else:
            located = shutil.which(spec["binary"])
            found[pid] = os.path.abspath(located) if located else None
    return found


def rung_map(enabled) -> dict:
    """Substitution for rung codes whose provider is not enabled."""
    chosen = set(enabled)
    mapping: dict[str, str] = {}
    if "claude" not in chosen:
        for code in _CLAUDE_RUNGS:
            mapping[code] = "G-x"
    if "cursor" not in chosen:
        for code in _CURSOR_TO_S:
            mapping[code] = "S"
        mapping["G-x"] = "S+"
    return mapping


def _rewrite_scalar(value, mapping: dict, keep_anthropic: bool):
    if not isinstance(value, str):
        return value
    if value in _LEAVE or value.endswith(_HUMAN_SUFFIX):
        return value
    mapped = mapping.get(value, value)
    if keep_anthropic and mapped in _ANTHROPIC_RUNGS:
        return value
    return mapped


def _rewrite_list(items, mapping: dict, keep_anthropic: bool = False) -> list:
    out = []
    for item in items:
        code = _rewrite_scalar(item, mapping, keep_anthropic)
        if out and out[-1] == code:
            continue
        out.append(code)
    return out


def _rewrite(doc: dict, mapping: dict) -> None:
    for table in doc["ladders"].values():
        for diff, cell in list(table.items()):
            if isinstance(cell, (list, tuple)):
                table[diff] = _rewrite_list(cell, mapping)
            else:
                table[diff] = _rewrite_scalar(cell, mapping, False)
    design = doc["design_route"]
    design["architect"]["rung"] = _rewrite_scalar(design["architect"]["rung"], mapping, False)
    design["architect_fallback"] = _rewrite_list(design["architect_fallback"], mapping)
    for key, val in list(design["implement"].items()):
        if isinstance(val, (list, tuple)):
            design["implement"][key] = _rewrite_list(val, mapping)
    judgment = doc["judgment_route"]
    for key in ("implement", "critical"):
        judgment[key] = _rewrite_list(judgment[key], mapping)
    doc["fallback_claude_denied"] = _rewrite_list(doc["fallback_claude_denied"], mapping)
    review = doc["review"]
    review["map"] = {name: _rewrite_scalar(code, mapping, False) for name, code in review["map"].items()}
    review["high_risk_grok"] = _rewrite_scalar(review["high_risk_grok"], mapping, False)
    review["tiebreak"] = _rewrite_scalar(review["tiebreak"], mapping, False)
    review["claude_denied"] = _rewrite_scalar(review["claude_denied"], mapping, True)
    planner = doc["planner"]
    planner["rungs"] = {diff: _rewrite_scalar(code, mapping, False) for diff, code in planner["rungs"].items()}
    planner["claude_denied"] = _rewrite_scalar(planner["claude_denied"], mapping, True)


def build_config(enabled, binaries) -> dict:
    """User routes.yaml overlay. Both providers: version and paths only."""
    chosen = set(enabled)
    paths = {}
    for pid in ("cursor", "claude"):
        if pid in chosen:
            paths[PROVIDERS[pid]["config_key"]] = binaries[pid]
    if "cursor" in chosen and "claude" in chosen:
        return {"version": 1, "paths": paths}
    doc = copy.deepcopy(defaults())
    _rewrite(doc, rung_map(chosen))
    doc["version"] = 1
    doc["paths"] = paths
    if "claude" not in chosen:
        doc["advisor"] = {"mode": "off"}
    doc.pop("droid", None)  # droid stays off and comes from defaults at load; keeps key-env names out of the file
    return doc
