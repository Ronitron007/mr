#!/usr/bin/env python3
"""Fake cursor-agent (mr-spec 14.2, plan 1.2).

Accepts: -p/--print, --output-format json|stream-json, --model, --mode ask|plan,
--trust, --force, --workspace PATH, and the prompt as a positional argument
(or piped stdin). File writes land in --workspace if given, else the cwd.
--mode ask/plan is read-only unless the step says "ignore_mode": true.
"""

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenario  # noqa: E402

VALUE_FLAGS = {"--output-format", "--model", "--mode", "--workspace"}
BOOL_FLAGS = {"-p", "--print", "--trust", "--force"}


def parse(argv):
    opts = {"--output-format": "json"}
    positionals = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--") and "=" in arg and arg.split("=", 1)[0] in VALUE_FLAGS:
            key, val = arg.split("=", 1)
            opts[key] = val
            i += 1
        elif arg in VALUE_FLAGS:
            opts[arg] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        elif arg in BOOL_FLAGS:
            i += 1
        else:
            positionals.append(arg)
            i += 1
    return opts, positionals


def main():
    argv = sys.argv[1:]
    opts, positionals = parse(argv)
    model = opts.get("--model") or "default"
    mode = opts.get("--mode")
    tree = opts.get("--workspace") or os.getcwd()
    prompt, stdin_len = scenario.read_prompt(positionals[-1] if positionals else "")

    scenario.record_call("cursor", model, argv, os.getcwd(), prompt, stdin_len)
    step = scenario.next_step("cursor", model)

    read_only = mode in ("ask", "plan")
    write_files = (not read_only) or bool(step.get("ignore_mode"))
    scenario.apply_step(tree, step, write_files=write_files)

    if step.get("stderr"):
        sys.stderr.write(step["stderr"])
        if not step["stderr"].endswith("\n"):
            sys.stderr.write("\n")

    if step.get("no_envelope"):
        print("FAKE cursor-agent: raw non-JSON output")
    else:
        usage = {
            "inputTokens": 100,
            "outputTokens": 50,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
        }
        usage.update(step.get("usage") or {})
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": bool(step.get("is_error", False)),
            "duration_ms": 123,
            "result": scenario.result_text(step),
            "session_id": "fake-cursor-" + uuid.uuid4().hex[:8],
            "usage": usage,
        }
        if opts["--output-format"] == "stream-json":
            print(json.dumps({"type": "system", "subtype": "init", "model": model}))
        print(json.dumps(envelope))
    return int(step.get("rc", 0))


if __name__ == "__main__":
    sys.exit(main())
