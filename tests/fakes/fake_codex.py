#!/usr/bin/env python3
"""Fake Codex CLI (docs/codex/SPEC.md codex.tests). Scenario key "codex", step key the model.

`codex login status` uses the step key "login"; its step value `login` is chatgpt (default), api_key or none.
`codex exec` reads the flags of codex.run, prints JSONL events and writes the result text to the -o file.
Step keys beyond scenario.apply_step: error (turn.failed with that message, exit 1), event_text (agent_message
text, default the result), no_last (leave the -o file unwritten), ignore_sandbox (write files under read-only).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenario  # noqa: E402

VALUE_FLAGS = {"--color", "-o", "-m", "-c", "-s", "-C"}
LOGIN = {  # the ChatGPT line is the real text (codex-cli 0.149.1, stderr); the others are guesses
    "chatgpt": ("Logged in using ChatGPT", 0),
    "api_key": ("Logged in using an API key - sk-proj-***ABCD", 0),
    "none": ("Not logged in", 1),
}


def login_status(argv):
    scenario.record_call("codex", "login", argv, os.getcwd(), "", 0)
    text, rc = LOGIN[scenario.next_step("codex", "login").get("login", "chatgpt")]
    sys.stderr.write(text + "\n")
    return rc


def parse(argv):
    opts, positionals = {}, []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in VALUE_FLAGS:
            opts[arg] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        else:
            if not arg.startswith("-"):
                positionals.append(arg)
            i += 1
    return opts, positionals


def event(obj):
    print(json.dumps(obj), flush=True)


def run_exec(argv):
    opts, positionals = parse(argv[1:])
    model = opts.get("-m") or "default"
    tree = opts.get("-C") or os.getcwd()
    prompt = positionals[-1] if positionals else ""
    scenario.record_call("codex", model, argv, os.getcwd(), prompt, 0)
    step = scenario.next_step("codex", model)
    event({"type": "thread.started", "thread_id": "fake-codex-1"})
    event({"type": "turn.started"})
    write_files = opts.get("-s") != "read-only" or bool(step.get("ignore_sandbox"))
    scenario.apply_step(tree, step, write_files=write_files)
    if step.get("stderr"):
        sys.stderr.write(step["stderr"] + "\n")
    if step.get("error"):
        event({"type": "turn.failed", "error": {"message": step["error"]}})
        return 1
    text = scenario.result_text(step)
    event({"type": "item.completed",
           "item": {"id": "item_0", "type": "agent_message", "text": step.get("event_text", text)}})
    usage = {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50}
    usage.update(step.get("usage") or {})
    event({"type": "turn.completed", "usage": usage})
    if opts.get("-o") and not step.get("no_last"):
        with open(opts["-o"], "w", encoding="utf-8") as fh:
            fh.write(text)
    return int(step.get("rc", 0))


def main(argv):
    if argv[:2] == ["login", "status"]:
        return login_status(argv)
    if argv[:1] == ["exec"]:
        return run_exec(argv)
    sys.stderr.write(f"fake codex: unsupported argv {argv!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
