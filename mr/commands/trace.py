"""`mr trace`: story, HTML snapshot, scrubbed demo, and live viewer."""
from __future__ import annotations

import json
import os
import sys

from mr import config, exitcodes as ec


def _resolve_home(args) -> str:
    if args.home:
        return args.home
    env = os.environ.get("MR_HOME")
    if env:
        return env
    return config.load(args.config).paths.home


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print("wrote", path)


def run(args) -> int:
    from mr.trace import load, narrate, render, scrub, serve

    home = _resolve_home(args)
    cmd = args.cmd
    if cmd == "serve":
        serve.serve(home, args.host, args.port)
        return ec.OK
    traces = load.build_traces(home)
    if cmd == "build":
        _write(args.out, render.page(render.payload(traces)))
    elif cmd == "demo":
        clean = scrub.scrub_traces(traces)
        data = render.payload(clean)
        bad = scrub.find_secrets(data)
        blob = json.dumps(data, default=str)
        leaks = [
            x
            for t in traces
            for x in (t.get("title"), t.get("why"), t.get("task"))
            if x and len(str(x)) >= 12 and not str(x).startswith("t-") and str(x) in blob
        ]
        if leaks:
            print(
                f"refusing: {len(leaks)} real task titles/labels survived scrubbing",
                file=sys.stderr,
            )
            return ec.NOT_ACCEPTED
        if bad:
            print(
                f"refusing: {len(bad)} secret-looking strings survived scrubbing",
                file=sys.stderr,
            )
            return ec.NOT_ACCEPTED
        _write(args.out, render.page(data))
        _write(args.data, json.dumps(clean, indent=1, default=str))
    elif cmd == "show":
        t = next((x for x in traces if x["task"] == args.task), None)
        if not t:
            print("no such task", file=sys.stderr)
            return ec.NOT_ACCEPTED
        if args.json:
            print(json.dumps(t, default=str))
        else:
            print(f"{t['task']}  {t.get('title') or ''}\n{narrate.headline(t)}")
            for i, st in enumerate(t["steps"], 1):
                print(f"  {i}. {narrate.step_line(st)}")
    else:
        print(f"mr trace: unknown command {cmd!r}", file=sys.stderr)
        return ec.USAGE
    return ec.OK
