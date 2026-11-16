# Codex reviewer plan `plan`

This plan builds the [Codex reviewer](SPEC.md). Each unit below is one PR. Each PR has a check that fails on `main` today and passes on the PR.

The order is config, then the runner, then the reviewer choice, then the tree check and the records. The paid smoke run comes after the runner and before any opt-in. The docs PR comes last.

## How to run this plan `plan.rules`

- Branch each PR from `main` as `rohan/<unit name>`, with the dot changed to a dash. A PR that depends on another PR uses the branch of that PR as its base.

- Opus implements each PR. A fresh Opus reviewer checks each PR. The implementer never reviews its own PR.

- A PR merges only when its checks pass and its review has no open finding. Merge with a merge commit.

- A code PR and its docs change go in two PRs.

- No unit changes `review.codex.authors` in the defaults. The defaults keep `authors: []`.

- The smoke run makes a paid Codex call. It needs Rohan's explicit yes for the prompt, the model and the cost.

- An opt-in in Rohan's live `routes.yaml` is a live config change. It needs Rohan's explicit yes.

## Code units `plan.code`

| PR | Depends on | Change | Check that fails today |
|---|---|---|---|
| `codex.config` | none | Add `paths.codex` and the `review.codex` block with the rules of `codex.config`. Build the `CX` rung in `cfg.review.codex.rung`. Add `config.reviewer_spec`. Add the role-scoped `review:codex:` entries to `allowed`. Map `gpt-*` to the family `openai` in `review.family`. | `test_codex_config`. A config with `authors: [claude]` loads, and `reviewer_spec(cfg, "CX").path` is `codex`. A ladder, a planner rung or `review.claude_denied` that names `CX` fails. `authors: [openai]` and `effort: ultra` fail. |
| `codex.runner` | `codex.config` | Add `runners.codex_argv`, `codex_child_env`, `run_codex`, the auth preflight and `codex_failure`. Add `tests/fakes/fake_codex.py` and `gitfix.FAKE_CODEX`. | `test_codex_runner`. With the fake, the argv has each flag of `codex.run`, and neither API key reaches the child. The text comes from the `-o` file. The fake steps give each class of `codex.failure`, and a `sleep` step times out at the scaled `reviewer_s`. |
| `codex.reviewer_family` | `codex.runner` | `review.reviewer_rung` picks `CX` as `codex.when` says. `review.review` runs Codex, then the fallback reviewer on no verdict. Write and read the cooldown file and the daily count. Fill `SignalC.codex`. | `test_codex_reviewer`. A Claude change at medium risk gets `CX`. A `usage_limit` step makes `G-x` review, with one Codex call. A second review in `recheck_s` starts no Codex call. A Grok change at high risk still gets `S`. |
| `codex.review_isolation` | `codex.reviewer_family` | Take the snapshot and the tree id before and after each Codex run, as `codex.isolation` says. Stop the loop with exit 4 on a tampered review. `mr review` also exits 4. | `test_codex_tamper`. A fake step with `ignore_sandbox: true` writes a file during review. Then `mr run` exits 4 and runs no fallback reviewer. mr saves the reviewed patch. |
| `codex.route_records` | `codex.reviewer_family` | Show the Codex reviewer, the fallback and the cooldown in `mr route`, text and JSON. Write `path: codex` and `codex_failure` in the reviewer row and `cost.codex_calls` in the task record. | `test_codex_route`. With `authors: [claude]` and a Claude first rung, `mr route --json` gives `roles.reviewer` `CX` and `roles.reviewer_fallback` `G-x`. A `mr run` record has `cost.codex_calls` 1. |

`codex.review_isolation` and `codex.route_records` both depend on `codex.reviewer_family`. They do not depend on each other. Both edit `mr/loop.py`, so do `codex.review_isolation` first, and base `codex.route_records` on it.

## Paid smoke run `plan.smoke`

| Step | Depends on | Change | Check |
|---|---|---|---|
| `codex.smoke` | `codex.runner` | Run `run_codex` one time with the real CLI on a small fixture diff with one requirement. Save the raw JSONL, the `-o` file and the exit code. Then run it one time with a `CODEX_HOME` that has no login. | Paid. Rohan approves the prompt, the model, the effort and the cost first. The run passes when the reply has a valid `MR_REVIEW` line and the tree id does not change. Codex must also accept each `-c` key. |

The smoke run settles three guesses in the spec:

- The config keys `web_search` and `features.hooks` that `codex.run` passes.

- The real message texts for `auth` and `usage_limit` in `codex.failure`.

- The wall time of a review at `effort: high`, against `timeouts.reviewer_s`.

A PR named `codex.failure_fixtures` then puts the real texts in the fake steps and the `codex_failure` patterns. Its check is a fixture test on the saved texts. That test fails before the patterns change.

## Measure and opt-in `plan.optin`

| Step | Change | Check |
|---|---|---|
| `codex.measure` | Add a Codex arm to the measure harness of the split plan. Run Codex and today's reviewer on the same candidate changes. | Paid. Rohan approves it first. The report gives the false accepts, the false rejects, the re-asks and the wall time of each reviewer. |
| `codex.optin` | Set `review.codex.authors` in Rohan's live `routes.yaml`. | Rohan's explicit yes, after `codex.smoke`. `mr route` shows `CX` for the opted-in families. |

## Docs `plan.docs`

| PR | Depends on | Change |
|---|---|---|
| `codex.docs` | all code units | Add the `review.codex` block and `paths.codex` to the README config section. Add a CHANGELOG entry. Mark the units as built in this plan. |

This PR changes `README.md` and `CHANGELOG.md` at the repo root. Thus it is not a docs-only PR, and it waits for its review.

## Decisions `plan.decisions`

The answers are in `codex.decisions` in the spec. They change `codex.optin` only. The opt-in sets `review.codex.authors: [claude]`. No code unit waits for them.
