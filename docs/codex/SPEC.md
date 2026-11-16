# Codex reviewer spec `codex`

## Why `codex.why`

Rohan runs mr behind a Grok bot, and no person watches each task. Two failures count the same. The first is to accept a bad change. The second is to spend model calls on rejects and deferrals that repeat.

Signal C is one review of a candidate change by a reviewer from another model family. Today a Claude-written change gets a Grok reviewer (`G-x`), and a Grok-written change gets Sonnet (`S`). A weaker model that reviews a stronger model is the weak point of signal C.

Rohan decided to add OpenAI Codex as a third reviewer family. With this spec, mr runs the Codex CLI (`codex exec`) in a read-only sandbox for signal C only. Codex is never an implementer, a planner, a judge or a caller of mr.

## Terms `codex.terms`

- **Codex reviewer.** One `codex exec` run that reads the review prompt and replies with one `MR_REVIEW` line. Its reviewer code is `CX`.

- **Codex author.** An implementer family that the Codex reviewer can review. The config key `review.codex.authors` lists these families.

- **Fallback reviewer.** The reviewer that mr picks with Codex off. This is the reviewer of today.

- **Codex failure.** The reason that a Codex reviewer run gave no verdict. The values are in `codex.failure`.

- **Cooldown.** A time window in which mr does not start the Codex reviewer. The fallback reviewer reviews instead.

## When Codex reviews `codex.when`

The Codex reviewer takes the place of `review.map[author]` when all of these conditions are true:

- The author family is in `review.codex.authors`.

- No cooldown is active.

- The daily call cap `review.codex.calls_per_day` is not used up.

All other rules in `review.reviewer_rung` stay as they are. The rule `review.high_risk_grok` still picks the reviewer of a high-risk Grok change. A `required` Claude rung, which high-risk paths and the design route set, still reviews.

The reason is that mr has no measure of Codex against `S+`. A strength floor that mr cannot compare stays with Claude until the measure step gives a number. Rohan kept this rule (`codex.decisions`).

## Family rule `codex.family`

`review.family` maps a model that starts with `gpt-` to the family `openai`. The family `openai` is different from `grok`, `kimi`, `composer` and `claude`. Thus the Codex reviewer is cross-family for each author family of today, and AC-SIG-2 holds.

`review.codex.authors` can hold `grok`, `kimi`, `composer` and `claude`. It cannot hold `openai`, because no mr rung uses an OpenAI model.

The rule `EXCLUDED` in `mr/config.py` keeps `gpt-*` out of rungs, ladders, the `allowed` rung patterns and the classifier. This spec does not change that rule. The only door for a `gpt-*` model is one role-scoped entry, as `codex.config` says.

## Config shape `codex.config`

```yaml
paths:
  codex: codex                       # command name or absolute path
allowed: [..., "review:codex:gpt-6.1-sol"]   # role-scoped: signal C via the codex CLI only
review:
  map: {grok: S, kimi: G-h, composer: G-h, claude: G-x}   # unchanged; also the fallback reviewer
  codex:
    authors: []                      # [] = off (default). Example opt-in: [claude] or [claude, grok]
    model: gpt-6.1-sol
    effort: high                     # low | medium | high | xhigh | max
    recheck_s: 3600                  # cooldown after an auth or usage-limit failure
    calls_per_day: null              # null = no daily cap
```

The validation rules are:

- `authors` is a list. Each entry is `grok`, `kimi`, `composer` or `claude`.

- `model` matches `gpt-*` and matches one `review:codex:` entry in `allowed`.

- `effort` is `low`, `medium`, `high`, `xhigh` or `max`. mr rejects `ultra`, because that level starts sub-agents that mr does not control.

- `recheck_s` is an integer greater than 0. `calls_per_day` is null or an integer greater than 0.

- No rung in `rungs` has the code `CX`.

- `paths.codex` is an absolute path or a bare command name.

The Codex reviewer is not a rung. `config.load` builds one `Rung` object with the code `CX` and the path `codex`, and keeps it in `cfg.review.codex.rung`. It is not in `cfg.rungs`. The reason is that no ladder, planner, fallback list or judge can then name `CX`. A structure that cannot hold the wrong value replaces a list of validation checks.

The helper `config.reviewer_spec(cfg, code)` returns `cfg.rungs[code]`, or the `CX` rung. The reviewer attempt row, `_row_guarded` and `mr route` use this helper.

The defaults keep `authors: []`. Thus a user who does not opt in gets the same reviewer as today, and `mr route` shows the same output. To opt in is a change to the live config. It needs Rohan's explicit yes.

## Data shape `codex.data`

```python
CodexFailure = Literal["auth", "auth_api_key", "usage_limit", "timeout", "crash", "contract"]
SkipReason = Literal["cooldown", "day_cap"]
@dataclass(frozen=True)
class CodexCfg:
    authors: frozenset[str]
    model: str
    effort: str
    recheck_s: int
    calls_per_day: int | None
    rung: Rung                       # code "CX", path "codex"
```

`RunResult` gets the backend value `codex` and one new field, `codex_failure: CodexFailure | None`.

`SignalC` gets two new fields:

- `codex: dict | None` holds `{"calls": int, "failure": CodexFailure | None, "skipped": SkipReason | None}`. It is None when Codex is off for the author family.

- `tampered: str | None` holds the reason when the Codex reviewer changed the tree. It is None otherwise.

## How mr runs Codex `codex.run`

`runners.run_codex(cfg, prompt, tree, timeout_s, *, task, attempt)` is the only entry point. `runners.codex_argv` is the only place that builds the argv.

```
codex exec --json --color never -o <raw_base>.last
  -m <model> -c model_reasoning_effort="<effort>"
  -s read-only -c approval_policy="never"
  -c web_search="disabled" -c features.hooks=false
  --ignore-user-config --ignore-rules --ephemeral
  -C <tree> <prompt>
```

Each flag has one reason:

- `--json` gives JSONL events on stdout. mr reads the usage and the failure text from them.

- `-o <raw_base>.last` writes the final message to a file. mr reads the review text from this file.

- `-m` and `model_reasoning_effort` come from `review.codex`. mr never uses the model or the effort of the user config.

- `-s read-only` stops edits to the tree. The exec default is also read-only, but mr does not rely on a default.

- `approval_policy="never"` stops a wait for an approval that no person gives. A command that needs approval fails at once.

- `web_search="disabled"` keeps the reviewer on the prompt and the tree. The only network use is the model call.

- `features.hooks=false`, `--ignore-user-config` and `--ignore-rules` keep the hooks, MCP servers, plugins and rules of the user config out of the run. Auth still comes from `CODEX_HOME`.

- `--ephemeral` writes no session files.

- `-C <tree>` and the process working directory are the task tree. Codex can read the files of the tree, so it can give `path:line` evidence.

mr does not pass `--skip-git-repo-check`. The tree is always a git repo. The check then catches a wrong working directory.

The prompt is the last argv element, as for `cursor-agent` and `claude`. It is `review._prompt` from `mr/prompts/reviewer.v1.txt`, with no change. It holds the task text, the requirements and the patch, truncated at 60,000 characters. This size is much smaller than the macOS argv limit. Thus `exec_child` needs no stdin path.

Codex reads `AGENTS.md` from `CODEX_HOME` and from the repo. mr keeps this, because the Claude reviewer also reads the repo `CLAUDE.md`. The `MR_REVIEW` final-line rule and the re-ask catch a reply that does not obey the contract.

## Environment `codex.env`

`runners.codex_child_env()` is `child_env(False)` with two more keys removed, `OPENAI_API_KEY` and `CODEX_API_KEY`. The push block of `child_env` stays. Thus a `git push` from a child goes to `file:///nonexistent`.

The reason is the same as for the Claude rungs. Codex must use the ChatGPT plan login and never bill the OpenAI API.

## Auth preflight `codex.auth`

Before each review, mr runs `codex login status` with a timeout of 30 seconds, scaled by `MR_TEST_TIME_SCALE`. This command makes no model call.

- If the command fails or prints no login, the Codex failure is `auth`.

- If the output says that the login uses an API key, the Codex failure is `auth_api_key`. An API key login bills the OpenAI API, so mr refuses it.

- If the output says that the login uses ChatGPT, mr runs the review.

mr starts no `codex exec` after an auth failure.

## Output and verdict `codex.output`

`run_codex` makes a `RunResult` from the run:

- `result_text` is the text of `<raw_base>.last`. If that file has no text, mr uses the last `item.completed` event with an `agent_message` item.

- `usage` is the usage object of the last `turn.completed` event.

- `is_error` is true when the exit code is not 0, the run timed out, a `turn.failed` or `error` event came, or `result_text` is empty.

- `stderr_tail` and the raw `.out` and `.err` files are as for the other runners.

`review.review` reads `result_text` with `parse_review` and `verdict`, as for every reviewer. The last `MR_REVIEW` line of the last 50 lines decides. mr does not use `--output-schema`. The reason is that one parser and one prompt file then serve every reviewer, the fakes and the fixtures.

## Codex failures `codex.failure`

`runners.codex_failure(text, stderr_tail, timed_out)` gives the class of a failed run. The text is the message of the `turn.failed` or `error` event.

| Class | When | Re-ask | Cooldown |
|---|---|---|---|
| `auth` | The preflight finds no login, or the message says 401, unauthorized or login. | No | Yes |
| `auth_api_key` | The preflight finds an API key login. | No | Yes |
| `usage_limit` | The message says usage limit, rate limit, quota or 429. | No | Yes |
| `timeout` | The run passed `timeouts.reviewer_s`. | Yes, once | No |
| `crash` | Any other run with `is_error` true. | Yes, once | No |
| `contract` | The run worked, but `verdict` returns None. | Yes, once | No |

The message patterns are a guess. The paid smoke run `codex.smoke` records the real messages, and the fixtures use them.

A re-ask uses the attempt slot that `review.review` has today. Thus Codex runs at most two times for one review.

## Fallback and cooldown `codex.fallback`

When the Codex reviewer gives no verdict, the fallback reviewer reviews the same prompt in the same `review.review` call. The fallback reviewer gets its own two attempts. Its rules are those of today, which include the `review.claude_denied` path and the `defer` flag.

When the fallback reviewer also gives no verdict, the result is `UNAVAILABLE`. The loop then parks the attempt with exit 75, as `_defer_review` does today. Thus a Codex failure alone never defers a task.

The reason is that the fallback reviewer is the bar of today. A Codex failure then makes signal C no weaker than it is today. A deferral costs a later full review, and an unattended bot can resume into the same failure again.

After an `auth`, `auth_api_key` or `usage_limit` failure, mr writes `<home>/state/codex-cooldown.json`. The file holds `until`, which is the time now plus `recheck_s`, and the failure class. While `until` is in the future, `reviewer_rung` skips Codex, and `SignalC.codex.skipped` is `cooldown`. The reason is that each review would otherwise start a Codex run that fails in the same way.

## Tree check `codex.isolation`

The read-only sandbox must keep the tree as it is. mr checks this and does not trust the sandbox alone.

1. Before the Codex run, mr takes `isolation.snapshot(tree)` and `patches.take_reset_tree(tree)`.

2. After the run, mr takes both again.

3. If `isolation.violation` returns a reason, or the two tree ids differ, mr sets `SignalC.tampered`.

4. A tampered review gives no verdict. The loop stops the task with exit 4, `UNSAFE_GIT`. mr saves the reviewed patch and says what changed.

5. `mr review` also exits 4 for a tampered review.

mr does not run the fallback reviewer after a tampered review. A tree that changed under review is not safe to review again.

This check covers the Codex reviewer only. Today mr takes no snapshot around any reviewer call. That gap is a separate issue, outside this spec.

## Cost and quota `codex.cost`

Codex calls count against the usage limits of Rohan's ChatGPT plan. That plan has a rolling 5-hour window and a weekly window, and the limits change with the plan. The bot shares these limits with Rohan's own use of Codex. Because mr cannot read these limits, mr has no guard for Codex.

mr bounds the Codex calls in these ways:

- One review starts at most two Codex runs.

- The number of reviews in a task is at most the attempts cap of its difficulty, as today.

- A cooldown stops all Codex runs after an auth or usage-limit failure.

- `calls_per_day` stops Codex runs after that number of runs in one IST day. mr keeps the count in `<home>/state/codex-calls.json`. The fallback reviewer reviews after the cap.

Codex calls do not count toward `caps.claude_rungs_per_task` or `caps.opus_rungs_per_task`. Reviewer calls do not count toward those caps today. When Codex reviews a Grok change, mr saves the Claude points that `S` would use.

Each task record gets `cost.codex_calls`, the number of Codex runs that started. `claude_pts` stays null for a Codex row.

If the ChatGPT plan buys credits after the cap, a Codex call past the cap costs money. No mr code can see this setting. Rohan says the plan does not buy credits (`codex.decisions`).

## mr route and records `codex.route`

`mr route` shows the reviewer that `mr run` would pick. With Codex on for the author family, the line is this:

```
reviewer: CX (codex gpt-6.1-sol high; fallback G-x)
```

During a cooldown, the line is `reviewer: G-x (CX cooling down until <time>)`. After the daily cap, the line is `reviewer: G-x (CX daily cap used)`. The JSON output gets `roles.reviewer` and a new key, `roles.reviewer_fallback`.

The reviewer attempt row for a Codex run has `rung: CX`, `path: codex` and the model. It also gets `codex_failure`. `mr trace` shows the row as for any reviewer.

## Tests `codex.tests`

`tests/fakes/fake_codex.py` is a fake Codex CLI in the style of `fake_cursor_agent.py`. `tests/gitfix.py` gets `FAKE_CODEX`. The fake uses the scenario engine with the CLI key `codex` and the model as the step key.

The fake supports these items:

- `codex login status` prints the step value of `login`, which is `chatgpt`, `api_key` or `none`.

- `codex exec` reads the flags of `codex.run` and writes the step result text to the `-o` file. It prints JSONL events.

- A step with `error` prints a `turn.failed` event with that message and exits 1.

- A step with `sleep` waits that many seconds, for the timeout tests.

- The fake writes files only when the step has `ignore_sandbox: true` or the sandbox is not `read-only`.

- Each call records the argv, and whether `OPENAI_API_KEY` and `CODEX_API_KEY` reached the child.

Each unit in [PLAN.md](PLAN.md) names its check. Each check fails on `main` today.

## Out of scope `codex.out`

- Codex as an implementer, a planner, a check judge or the intake judge.

- Two reviewers for one change. The council of reviewers stays deferred.

- `codex review` and `codex exec review`. They use their own rubric and their own diff base, and they give no verdict for each requirement id.

- `--output-schema` for the reply. A later PR can test whether it cuts re-asks.

- A probe of the ChatGPT plan limits. The Codex CLI has no documented command for this in non-interactive mode.

- An OpenAI API key backend for Codex.

- A tree check around the Claude and Cursor reviewer calls. This is a separate issue.

## Prior art `codex.prior_art`

The research notes are in the session scratch file `lbyb.md`. No package wraps `codex exec` with a custom verdict contract. So mr builds a thin runner beside `run_droid`, on `exec_child`.

| Source | What it gives this spec |
|---|---|
| [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) | The default sandbox of `codex exec` is read-only. It gives `--json` events, `-o`, `--output-schema` and `--ephemeral`. |
| [Codex sandboxing](https://learn.chatgpt.com/docs/sandboxing) | The sandbox modes and the approval policies `on-request` and `never`. macOS uses Seatbelt. |
| [Codex config reference](https://learn.chatgpt.com/docs/config-file/config-reference) | `model_reasoning_effort`, `features.hooks`, `shell_environment_policy` and the web search settings. |
| [Codex AGENTS.md guide](https://learn.chatgpt.com/docs/agent-configuration/agents-md) | Codex reads `AGENTS.md` from `CODEX_HOME` and from the repo. No documented switch turns this off. |
| [openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc) | Codex reviews the work of another agent, read-only, on the local login and its usage limits. |

## Decisions `codex.decisions`

Rohan answered the open questions on 2026-11-16.

- The opt-in value is `authors: [claude]`. Codex reviews changes from Claude rungs only. The reviews of Grok changes stay with Sonnet.

- The Codex reviewer does not take the place of a `required` `S+` review. The `high_risk_grok` and `required` reviews stay with Claude.

- The ChatGPT plan of the bot does not buy credits after the cap. Codex stops at the plan limit, and the cooldown in `codex.failure` handles it. `calls_per_day` stays `null`.
