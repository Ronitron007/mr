"""`mr eval`: offline report on the shadow judge and classifier. Reads the log and labels, makes no calls."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from mr import evaluation
from mr import exitcodes as ec
from mr.config import load
from mr.log import IST


def run(args) -> int:
    cfg = load(getattr(args, "config", None))
    home = cfg.paths.home
    labels = getattr(args, "labels", None)
    if labels is None:
        default = os.path.join(home, "eval", "labels.jsonl")
        labels = default if os.path.isfile(default) else None
    elif not os.path.isfile(labels):
        print(f"--labels: no such file: {labels}", file=sys.stderr)
        return ec.USAGE
    payload = evaluation.evaluate(cfg, home, getattr(args, "since", None), labels)
    today = datetime.now(IST).date().isoformat()
    out = getattr(args, "out", None) or os.path.join(home, "eval", "reports", today + ".md")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(evaluation.render(payload, today))
    except OSError as exc:
        print(f"--out: cannot write report: {exc}", file=sys.stderr)
        return ec.USAGE
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False))
        return ec.OK
    if payload["no_data"]:
        print("no shadow data in the window")
    for name, dec in payload["deciders"].items():
        print(f"{name}: " + ", ".join(f"{f} {v}" for f, v in dec["verdicts"].items()))
    if getattr(args, "sweep", False):
        print(evaluation.sweep_text(payload))
    print(f"report: {out}")
    return ec.OK
