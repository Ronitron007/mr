"""`mr guard`: one guard decision as JSON (mr-spec §2.1, §8)."""
from __future__ import annotations

import json
from dataclasses import asdict

from mr import config, guard
from mr import exitcodes as ec


def run(args) -> int:
    cfg = config.load(args.config)
    res = guard.check(getattr(args, "rung", None) or "S", cfg)
    out = {k: v for k, v in asdict(res).items() if k != "sample"}
    print(json.dumps(out, ensure_ascii=False))
    return ec.OK if res.allow else ec.GUARD_DENY
