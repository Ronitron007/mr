"""`mr queue`: the tasks that wait for a check judge (mr resume <task> continues one)."""
from __future__ import annotations

import json

from mr import config, exitcodes as ec, state

_KEYS = ("task", "repo", "check", "queued_at", "frees_hint")


def run(args) -> int:
    cfg = config.load(args.config)
    waiting = [{k: entry.get(k) for k in _KEYS} for entry in state.read_queue(cfg.paths.home)]
    if getattr(args, "json", False):
        print(json.dumps(waiting, ensure_ascii=False))
        return ec.OK
    for e in waiting:
        print(f"{e['task']}  {e['check']}  {e['repo']}  queued {e['queued_at']}  frees {e['frees_hint'] or '-'}")
    return ec.OK
