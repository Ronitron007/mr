"""`mr setup`: write a user routes.yaml from detected provider CLIs."""
from __future__ import annotations

import json
import os
import shutil
import sys

import yaml

from mr import exitcodes as ec
from mr.config import ConfigError, load
from mr.paths import default_config_path
from mr.providers import PROVIDERS, build_config, detect

_YES = frozenset({"y", "Y", "yes", "YES"})
_ORDER = ("cursor", "claude")


def _target(args) -> str:
    config = getattr(args, "config", None)
    if config:
        return config
    env = os.environ.get("MR_CONFIG")
    if env:
        return env
    return default_config_path()


def _binary_found(value: str) -> bool:
    if os.path.isabs(value):
        return os.path.isfile(value) and os.access(value, os.X_OK)
    return shutil.which(value) is not None


_NON_CODE = frozenset({"design_route", "defer"})


def _referenced_codes(cfg) -> list:
    """Rung codes the loaded config actually schedules. Non-codes are left in."""
    codes = []
    for table in cfg.ladders.values():
        for cell in table.values():
            if isinstance(cell, (list, tuple)):
                codes.extend(cell)
    codes.extend(cfg.review.map.values())
    codes.append(cfg.review.high_risk_grok)
    codes.append(cfg.review.tiebreak)
    codes.extend(cfg.planner.rungs.values())
    # Denied fallbacks run only when the Claude gate denies a call. An empty
    # paths.check never denies, so those cursor rungs are not in use.
    if cfg.paths.check:
        codes.append(cfg.review.claude_denied)
        codes.append(cfg.planner.claude_denied)
        codes.extend(cfg.fallback_claude_denied)
    for value in cfg.judgment_route.values():
        if isinstance(value, (list, tuple)):
            codes.extend(value)
    architect = cfg.design_route.get("architect") or {}
    if isinstance(architect, dict):
        codes.append(architect.get("rung"))
    implement = cfg.design_route.get("implement") or {}
    if isinstance(implement, dict):
        for value in implement.values():
            if isinstance(value, (list, tuple)):
                codes.extend(value)
    return codes


def _used_paths(cfg) -> set[str]:
    used = set()
    for code in _referenced_codes(cfg):
        if not isinstance(code, str) or code in _NON_CODE:
            continue
        rung = cfg.rungs.get(code)
        if rung is not None:
            used.add(rung.path)
    return used


def _advisor_on(cfg) -> bool:
    mode = (cfg.advisor or {}).get("mode")
    return mode is True or mode == "on"


def _provider_used(cfg, pid: str, used_paths: set[str]) -> bool:
    if PROVIDERS[pid]["rung_path"] in used_paths:
        return True
    return pid == "claude" and _advisor_on(cfg)


def _check(args) -> int:
    try:
        cfg = load(getattr(args, "config", None))
    except ConfigError as exc:
        line = str(exc).splitlines()[0].strip() if str(exc).strip() else "config error"
        print(line)
        return ec.NOT_ACCEPTED
    ok = True
    used_paths = _used_paths(cfg)
    for pid in _ORDER:
        if not _provider_used(cfg, pid, used_paths):
            print(f"{pid}: not used")
            continue
        value = getattr(cfg.paths, PROVIDERS[pid]["config_key"])
        found = bool(value) and _binary_found(value)
        if not found:
            ok = False
        print(f"{pid}: {'found' if found else 'missing'}")
    return ec.OK if ok else ec.NOT_ACCEPTED


def _overrides(args) -> dict:
    overrides = {}
    cursor = getattr(args, "cursor_agent", None)
    claude = getattr(args, "claude", None)
    if cursor is not None:
        overrides["cursor"] = cursor
    if claude is not None:
        overrides["claude"] = claude
    return overrides


def _enabled(args, found: dict) -> list[str] | None:
    """Enabled provider ids, or None when the command must exit USAGE."""
    named = list(getattr(args, "provider", None) or [])
    if named:
        missing = [pid for pid in named if not found.get(pid)]
        if missing:
            for pid in missing:
                print(f"{pid}: not detected. {PROVIDERS[pid]['hint']}", file=sys.stderr)
            return None
        return [pid for pid in _ORDER if pid in named]
    detected = [pid for pid in _ORDER if found.get(pid)]
    printing = bool(getattr(args, "print", False))
    if getattr(args, "yes", False) or printing or not sys.stdin.isatty():
        return detected
    enabled = []
    for pid in detected:
        answer = input(f"Enable {pid}? [y/N] ")
        if isinstance(answer, str) and answer.strip() in _YES:
            enabled.append(pid)
    return enabled


def _dump(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _emit(args, path: str, providers: list[str], written: bool, document: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps({"path": path, "providers": providers, "written": written}))
        return
    if bool(getattr(args, "print", False)):
        sys.stdout.write(document)
        return
    print(path)
    for pid in providers:
        print(pid)
    print(f"add hygiene.commands.<repo> to {path}: mr run exits 4 for a repo with no hygiene commands "
          "unless you pass --no-checks")
    print('mr route --explain "<task>"')
    print("mr run")


def run(args) -> int:
    if getattr(args, "check", False):
        return _check(args)
    found = detect(_overrides(args) or None)
    enabled = _enabled(args, found)
    if enabled is None:
        return ec.USAGE
    if not enabled:
        for pid in _ORDER:
            spec = PROVIDERS[pid]
            print(f"{spec['binary']}: {spec['hint']}", file=sys.stderr)
        return ec.USAGE
    path = _target(args)
    printing = bool(getattr(args, "print", False))
    if not printing and os.path.exists(path) and not getattr(args, "force", False):
        print(path, file=sys.stderr)
        return ec.USAGE
    doc = build_config(enabled, found)
    document = _dump(doc)
    if printing:
        _emit(args, path, enabled, False, document)
        return ec.OK
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(document)
    _emit(args, path, enabled, True, document)
    return ec.OK
