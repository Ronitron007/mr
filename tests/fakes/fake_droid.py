#!/usr/bin/env python3
"""Fake droid CLI for tests (never the real droid, no network).

Env:
  FAKE_DROID_STEPS  path of a JSON list of steps; call N (counting `exec` calls) uses step N, the last one repeats.
  FAKE_DROID_LOG    path of a JSONL file; every call appends {"argv", "cwd", "keys"} ("keys" are the names of
                    ANTHROPIC_API_KEY / FACTORY_API_KEY present in the environment, never their values).
Step keys: files {relative path: text} written under --cwd, result (final text), is_error, rc, num_turns.
`--version` prints a version line and exits 0.
"""
import json
import os
import sys

KEY_VARS = ("ANTHROPIC_API_KEY", "FACTORY_API_KEY")
DEFAULT_RESULT = "done\nMR_RESULT " + json.dumps(
    {"item": None, "status": "done", "requirements": [], "deviations": [], "pushback": None, "confidence": 0.8})


def _calls(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main(argv):
    log = os.environ.get("FAKE_DROID_LOG")
    prior = sum(1 for c in _calls(log) if c["argv"][:1] == ["exec"])
    if log:
        record = {"argv": argv, "cwd": os.getcwd(), "keys": [k for k in KEY_VARS if os.environ.get(k)]}
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    if argv[:1] == ["--version"]:
        print("droid 1.1.0")
        return 0
    steps = []
    if os.environ.get("FAKE_DROID_STEPS"):
        with open(os.environ["FAKE_DROID_STEPS"], encoding="utf-8") as fh:
            steps = json.load(fh)
    step = steps[min(prior, len(steps) - 1)] if steps else {}
    tree = argv[argv.index("--cwd") + 1] if "--cwd" in argv else os.getcwd()
    for rel, text in (step.get("files") or {}).items():
        dest = os.path.join(tree, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text)
    is_error = bool(step.get("is_error", False))
    usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "factory_credits": 0, "thinking_tokens": 5, "ttft_ms": 300}
    print(json.dumps({"type": "result", "subtype": "failure" if is_error else "success", "is_error": is_error,
                      "duration_ms": 1200, "num_turns": step.get("num_turns", 3),
                      "result": step.get("result", DEFAULT_RESULT), "session_id": "fake-droid-1", "usage": usage}))
    return int(step.get("rc", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
