#!/usr/bin/env python3
"""Fake claude CLI (mr-spec 14.2, plan 1.2).

Accepts: -p [prompt], --output-format, --model, --effort, --permission-mode,
--allowedTools, --disallowedTools, --no-session-persistence, --json-schema,
and a positional prompt (or piped stdin). Prints the Claude Code JSON result.

Special behaviours:
- "--bare" anywhere in argv: exit 99, stderr "--bare forbidden".
- step "exit": 75: wrapper refusal; prints the hourly-limit message to stderr,
  exits 75 with no stdout, does not consume or apply the step.
- step "hook_block": true: consumes the step, applies nothing, exit 2.
"""

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenario  # noqa: E402

VALUE_FLAGS = {
    "--output-format",
    "--model",
    "--effort",
    "--permission-mode",
    "--allowedTools",
    "--disallowedTools",
    "--json-schema",
    "--advisor",
    "--append-system-prompt",
}
BOOL_FLAGS = {"--no-session-persistence", "--bare", "--verbose"}

ADVISOR_MODEL = "claude-opus-5-5"


def _ts_plus(ts, seconds):
    from datetime import datetime, timedelta

    return (datetime.fromisoformat(ts.replace("Z", "+00:00")) + timedelta(seconds=seconds)
            ).isoformat().replace("+00:00", "Z")


def advisor_events(step):
    """stream-json advisor blocks for a step with an "advisor" key (advisor-consult-spec §8).

    Step keys: ts (ISO, default 2026-10-30T10:00:00Z), latency_s (default 20),
    error (an advisor_tool_result_error code; omit for a redacted success).
    """
    adv = step.get("advisor")
    if adv is None:
        return []
    ts0 = adv.get("ts") or "2026-10-30T10:00:00Z"
    content = ({"type": "advisor_tool_result_error", "error_code": adv["error"]} if adv.get("error")
               else {"type": "advisor_redacted_result", "encrypted_content": "x"})
    return [
        {"type": "assistant", "timestamp": ts0,
         "message": {"id": "m-adv-1", "content": [
             {"type": "server_tool_use", "id": "srv1", "name": "advisor", "input": {}}]}},
        {"type": "assistant", "timestamp": _ts_plus(ts0, float(adv.get("latency_s", 20))),
         "message": {"id": "m-adv-2", "content": [
             {"type": "advisor_tool_result", "tool_use_id": "srv1", "content": content}]}},
    ]


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
        elif arg == "-p":
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                positionals.append(argv[i + 1])
                i += 2
            else:
                i += 1
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
    prompt, stdin_len = scenario.read_prompt(positionals[-1] if positionals else "")

    scenario.record_call("claude", model, argv, os.getcwd(), prompt, stdin_len)

    if "--bare" in argv:
        sys.stderr.write("--bare forbidden\n")
        return 99

    step = scenario.next_step("claude", model, consume=False)
    if int(step.get("exit", 0)) == 75:
        sys.stderr.write("claude: refusing to start - hourly limit reached\n")
        return 75
    step = scenario.next_step("claude", model, consume=True)

    if step.get("hook_block"):
        sys.stderr.write("blocked by hook\n")
        return 2

    scenario.apply_step(os.getcwd(), step, write_files=True)

    if step.get("stderr"):
        sys.stderr.write(step["stderr"])
        if not step["stderr"].endswith("\n"):
            sys.stderr.write("\n")

    if step.get("no_envelope"):
        print("FAKE claude: raw non-JSON output")
    else:
        cost = float(step.get("cost", 0.0))
        served = step.get("model_served") or model
        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        usage.update(step.get("usage") or {})
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": bool(step.get("is_error", False)),
            "result": scenario.result_text(step),
            "session_id": "fake-claude-" + uuid.uuid4().hex[:8],
            "num_turns": 3,
            "total_cost_usd": cost,
            "usage": usage,
            "modelUsage": {
                served: {
                    "inputTokens": usage["input_tokens"],
                    "outputTokens": usage["output_tokens"],
                    "costUSD": cost,
                }
            },
            "stop_reason": "end_turn",
        }
        adv = step.get("advisor")
        if adv is not None:  # modelUsage carries the Opus consult tokens (advisor-consult-spec §8)
            envelope["modelUsage"][ADVISOR_MODEL] = {
                "inputTokens": int(adv.get("inputTokens", 40000)),
                "outputTokens": int(adv.get("outputTokens", 1500)),
                "costUSD": float(adv.get("costUSD", 0.19)),
            }
        if opts.get("--output-format") == "stream-json":
            print(json.dumps({"type": "system", "subtype": "init", "model": served}))
            for event in advisor_events(step):
                print(json.dumps(event))
        print(json.dumps(envelope))
    return int(step.get("rc", 0))


if __name__ == "__main__":
    sys.exit(main())
