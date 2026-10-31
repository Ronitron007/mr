"""Turn traces into the single-file viewer (data, CSS and JS inlined; opens from disk)."""
from __future__ import annotations

import hashlib
import json
import os
import time

from . import load, narrate

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def payload(traces: list[dict], live: bool = False) -> dict:
    for t in traces:
        t["headline"] = narrate.headline(t)
        t["shape"] = narrate.shape(t)
        for s in t.get("steps") or []:
            s["line"] = narrate.step_line(s)
    body = json.dumps(traces, sort_keys=True, default=str)
    return {
        "generated": time.time(),
        "live": live,
        "sig": hashlib.sha1(body.encode()).hexdigest()[:12],
        "rungOrder": load.RUNG_ORDER,
        "failureMeaning": narrate.FAILURE_MEANING,
        "traces": traces,
    }


def page(data: dict) -> str:
    def read(n):
        with open(os.path.join(WEB, n), encoding="utf-8") as f:
            return f.read()
    blob = json.dumps(data, default=str).replace("</", "<\\/")
    return (read("viewer.html")
            .replace("/*__CSS__*/", read("viewer.css"))
            .replace("/*__JS__*/", read("viewer.js"))
            .replace("/*__DATA__*/", blob))
