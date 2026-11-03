"""`mr droid check [--probe]` and `mr droid reset`: the BYOK droid backend for rungs S and S+."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time

from mr import config, droid, runners
from mr import exitcodes as ec

PROBE_PROMPT = "Reply with the single word ok."
PROBE_TIMEOUT_S = 90
VERSION_TIMEOUT_S = 20
# exit codes of `mr droid check` (besides 0): 1 other probe error, 2 no binary, 3 no key, 4 auth, 5 credits
ERROR, MISSING_BINARY, NO_KEY, AUTH, CREDITS = 1, 2, 3, 4, 5
_KEYS = ("installed", "version", "enabled", "key_present", "settings", "state", "status",
         "latency_ms", "usage", "detail")


def _version(cfg) -> str | None:
    """First line of `droid --version`, or None."""
    try:
        done = subprocess.run([cfg.paths.droid, "--version"], capture_output=True, text=True, check=False,
                              timeout=VERSION_TIMEOUT_S, env=runners.child_env(False), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (done.stdout or done.stderr).strip().splitlines()
    return lines[0][:100] if lines else None


def _probe(cfg, doc: dict) -> int:
    """One tiny call on twin S (read-only, no --auto). Ignores the sticky state, then updates it."""
    twins = cfg.droid["twins"]
    if not twins:
        doc.update(status="error", detail="no droid twins configured")
        return ERROR
    tree = tempfile.mkdtemp(prefix="mr-droid-probe-")
    try:
        rr = runners.run_droid("S", twins.get("S") or next(iter(twins.values())), PROBE_PROMPT, tree,
                               PROBE_TIMEOUT_S, cfg=cfg, task="droid-probe", attempt=1, probe=True)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    doc.update(latency_ms=int(rr.wall_s * 1000), usage=rr.usage)
    if not rr.is_error:
        droid.reset_state(cfg)
        doc["status"] = "ok"
        return ec.OK
    detail = " ".join((rr.result_text or rr.stderr_tail or f"rc={rr.rc}").split())[:droid.DETAIL_CAP]
    doc["detail"] = detail
    code, status, sticky = {"credits": (CREDITS, "credits", "credits_exhausted"),
                            "auth": (AUTH, "auth", "auth_failed")}.get(rr.droid_failure, (ERROR, "error", None))
    if sticky:
        droid.write_state(cfg, sticky, time.time(), detail)
    doc["status"] = status
    return code


def check(cfg, probe: bool, doc: dict) -> int:
    """Fill doc and return the exit code. The version call is the only spawn before the key check."""
    envs = droid.key_envs(cfg)
    doc.update(enabled=bool(cfg.droid.get("enabled")), installed=droid.binary_path(cfg) is not None,
               key_present=bool(envs) and len(droid.key_values(cfg)) == len(envs))
    if not doc["installed"]:
        doc["status"] = "missing_binary"
        return MISSING_BINARY
    doc["version"] = _version(cfg)
    if not doc["key_present"]:
        doc["status"] = "no_key"
        return NO_KEY
    try:
        doc["settings"] = droid.ensure_settings(cfg)
    except config.ConfigError as exc:
        doc.update(settings="invalid", status="invalid_settings", detail=str(exc))
        return ERROR
    if probe:
        return _probe(cfg, doc)
    doc["status"] = "ready"
    return ec.OK


def _print(doc: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps({k: doc[k] for k in _KEYS}, ensure_ascii=False))
        return
    for key in _KEYS:
        if doc[key] is not None and key != "state":
            print(f"{key}: {doc[key]}")
    print(f"state: {doc['state']['status']}")


def run(args) -> int:
    cfg = config.load(args.config)
    if not cfg.droid:
        raise config.ConfigError("droid: block missing")
    if args.droid_cmd == "reset":
        droid.reset_state(cfg)
        print(json.dumps({"state": droid.read_state(cfg)}) if args.json else "droid state reset")
        return ec.OK
    doc = dict.fromkeys(_KEYS)
    code = check(cfg, args.probe, doc)
    doc["state"] = droid.read_state(cfg)
    _print(doc, args.json)
    return code
