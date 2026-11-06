# Changelog

## Unreleased

- A broken plan no longer ends a task with exit 3 (`plan.judge: on`, the default; `off` keeps exit 3). `mr` first fixes what it can by itself (a "create" file that already exists, a missing "modify" file, `./` and absolute paths, a test missing from the create list, files outside the scope, ids out of order), then asks the planner again with the exact remaining errors, then asks one stronger planner (G-h, then S, O, O+). If that plan is also invalid, the task runs without a plan, with a required S+ review, and prints `plan rejected twice; running without a plan` (the task record gets `plan_rejected: true`). If the planner cannot answer at all (timeouts or no JSON, also from the stronger one), the task is deferred with exit 75 and "planner unavailable" instead of exit 3. Planner records in the run log gain `status` (valid, repaired or rejected), `repairs` and `retry_errors`, and `invalid_plan` is no longer a failure class when the judge is on.

- Callers no longer have to name the files. `mr` now finds the files itself (named paths, your hints, the judge's picks) and the planner fixes the final scope, so a task file takes an optional `hints.files` list (paths you already know) and `mr` no longer copies path words out of the task text into the task. `files_named` still works for one release as an alias for `hints.files`: `mr run` prints `files_named is deprecated; use hints.files` once and the task record gets `deprecated_field: files_named`. The task record now always has `files_src` (how many files came from the text, a hint, the judge or the planner). The planner prompt lists each file with its source. Without a plan, a file only the judge picked that lies outside the named files is a warning in the attempt record (`d_warnings`) instead of a failure; sensitive paths and dependency files still fail.

- A risk word in the task text alone (auth, payments, migration, deleting data) no longer sends a task to the design route (exit 3) or to the unbuilt W4 isolation (exit 4); only real paths do. A design decision now runs as a cascade with an Opus plan (O+ when critical) and an S+ review (`design_route.map_to_cascade: true`; set it to false to get the old exit 3). High risk from a path (auth, payments, migration, data deletion, deps, native config) runs on a `mr/*` branch (`risk.high_isolation: W5`, W4 restores the old mapping) with a planner of at least S+ and a strong review, is committed on the branch and never merged ("review and merge by hand"), tells you migrations need a human go, and adds a do-not-install line to the prompt for deps. Critical or large (400+ lines) changes also get a branch. If the guard denies that strong review, the task stops with exit 75 and the patch is kept instead of falling back to a weaker reviewer. The route record gains `design_mapped_to_cascade`, `required_review` and `isolation_inputs`.

- The judge is now on by default (`judge.mode: on`). Every `mr run` and `mr route` asks the Haiku judge once at intake (at most 3 judge calls per task, `judge.budget.max_calls_per_task_on`) and runs the merged decision: safety floors first, then the judge, then your hints, then the rules. Each hint the judge changes prints a `judge override:` line and is logged in the route record (with the new `judge`, `hints`, `overrides`, `files`, `judge_absent` and `fallback` keys). If the judge cannot answer (timeout, guard yield, budget, breaker, bad reply), `mr` uses its cached answer, else asks the grok classifier once (it can only make things stricter), else falls back to the rules, and the task still runs. For debugging, `--judge off` (or `judge.mode: off`) gives the old rules-only routing; the flag is logged as `judge_off_by_flag` and printed as `judge off by flag (debug)`. `mr route -f FILE` now reads the task text from the file.

- Internal: the rules that merge the judge's answers with safety floors, caller hints and the rules-only decision (stricter answers at 0.60, neutral at 0.75, looser only with a quote found in the task text), plus a repeatable `--pin FIELD` flag on `mr run` and `mr route` (and `params.pin` in a task file) to keep your value for a field. Not active yet: no decision changes.

- New optional droid backend (`droid:` config block, off by default). With `droid.enabled: true` and your Anthropic key in `ANTHROPIC_API_KEY`, the Sonnet rungs `S` and `S+` run on the Factory `droid` CLI first (Sonnet only, billed to your API key, outside the Claude usage gate). When droid reports no credits or a bad key, mr restores the tree and runs the same attempt on the `claude` CLI, and keeps using it until `droid.recheck_s` has passed; the key never reaches `claude` or `cursor-agent` children. New `mr droid check [--probe]` and `mr droid reset`.

- The shadow judge now asks question set `route.v2` by default (`judge.qset`, `route.v1` or `route.v2`): it also answers which files the change edits and creates, whether the candidate list is complete, and quotes the words that make a risk tag apply. The answers are logged only; no routing decision changes.

- New facts stage, logged only. For every task `mr` now works out which files it is about (named files, whether they
  exist, directory and glob matches, files that mention a backticked or CamelCase/snake_case name, and sibling
  tests), which risk words came from the text and which from file paths, which candidate paths are sensitive, and a
  few type features. The route record gets `facts` (counts) and `risk_evidence` (risk words with redacted quotes, or
  paths); `mr route --json` adds the same two keys and the text output adds a `facts:` line, also with `--no-models`.
  It makes no model call and never changes a decision, ladder, prompt or exit code; a failure is logged as an error.
- New `mr eval` command: an offline report on the shadow judge and classifier. It reads the run log (and an optional
  labels file, by default `eval/labels.jsonl` in the data directory) and writes a Markdown report with the rules,
  judge, classifier and judge+classifier arms, accuracy, calibration, threshold sweeps (`--sweep`), ops numbers and a
  PROMOTE / KEEP SHADOW / DEMOTE verdict per field. Flags: `--since YYYY-MM-DD`, `--labels PATH`, `--out PATH`,
  `--sweep`; with `--json` it prints the same data as one JSON object. It makes no model calls, no network requests and
  no subprocesses, writes only the report, and never changes config or state: promotion stays a human decision.
- The shadow judge now runs: `mr run --judge shadow` (or `judge.mode: shadow`) asks Haiku the routing questions in the
  background for unsure tasks and for one in `judge.sample_n` (default 3) confident ones, logs one `judge` record per
  call (answers, readings, budget, cache hit or the reason it was skipped), links it from the route record and adds
  its route to the task record. Answers never change routing, rungs, review or exits. `mr route --judge shadow`
  prints a `judge (shadow):` line and adds a `judge_shadow` key to `--json`. `judge.repos` now defaults to `all`
  and `--judge on` runs as shadow.

- Call path for the shadow judge, still not wired into `mr run`: one guarded Haiku call through the claude wrapper
  (no lock, no reservation, no probe; it yields when the usage check is high, unreadable or, in shadow, at
  `judge.budget.shadow_five_hour_max`, default 80), a status mapping that never retries, a call budget (150 a day,
  6 a task, no hourly cap unless `judge.budget.calls_per_hour` is set; the day rolls at midnight IST), a shared
  circuit breaker and a seven-day answer cache. The rung guard now counts recent judge calls in its reserve.

- Building blocks for the shadow judge: the 19 routing questions (`route.v1`), a fixed prompt that shows the model
  only neutral `q01...` keys, a redacted and size-checked task state with no caller options in it, a strict reader
  for the model's JSON reply (anything but one exact object is rejected; bad single answers become "unsure"), and a
  reader that turns answers into act, raise, unsure or ignored. These are pure functions: nothing calls them yet, so
  routing is unchanged.

- Shadow classifier wired into `mr run` and `mr route`. With `--classifier shadow` (or `classifier.mode: shadow`) a
  background thread asks the cheap model beside the rules; its answer is logged and joined at task end (timeout
  `classifier.join_timeout_s`, default 75 s) and never changes routing, rungs, review or exit codes. Confident tasks are
  sampled 1 in `classifier.sample_n` (default 3). `on` runs as shadow with a stderr note. `mr route` prints a
  `classifier (shadow):` line and a `classifier_shadow` JSON key, and no longer prints "judge/classifier not built".
  The default mode stays off.

- Classifier client for the shadow classifier: a fixed prompt template, a strict reader for the cheap model's JSON
  answer (one repair retry), a seven-day answer cache and a log record. It runs the model with no file access from an
  empty work folder and never with a Claude model. Nothing calls it yet, so routing is unchanged.

- Logging seam for the shadow judge and classifier: new redaction helper (secrets, tokens, emails and phone-like
  numbers become `<redacted:...>` markers), route records can carry shadow answers, and the task record now has
  `text_redacted` (redacted task text, first 4000 characters), `feature_hash` and the shadow summaries. No model
  calls and no routing change yet.

- No hourly Claude cap by default: `claude_limits.hour_max` defaults to null (no hourly check in the rung guard,
  advisor gate or judge yield; a missing hourly reading is not a deny when unset). `five_hour_max: 85`,
  reservations, probes and the Claude lock are unchanged; setting `hour_max` explicitly still enables the cap.

## v0.1.0 (2026-10-31)

First public release.

- Routing: rules-based prepass (type, difficulty, risk tags, files named) picks a ladder of model rungs, cheapest
  first; `mr route --explain` shows the decision without calling a model.
- Runs: planner checklist with locked acceptance tests, implementer rungs through `cursor-agent` or the `claude`
  CLI, verification by repo hygiene commands, acceptance tests and a cross-family reviewer, and escalation by
  failure class with per-task caps.
- `mr setup`: detects `cursor-agent` and `claude` on PATH and writes a user config; works with one provider or both;
  `mr setup --check` validates.
- Portable paths: config under `$XDG_CONFIG_HOME/mr`, data under `$XDG_DATA_HOME/mr` (or `MR_HOME`); the Claude
  usage-check script is optional.
- `mr trace`: local viewer for runs (`serve`, `build`, `show`, `demo`), with a scrubbed demo mode that refuses to
  write if a secret or real title survives. Task records carry a short title and the caller.
- Optional Cloudflare Worker and upload script for a private, secret-link hosted viewer.
- Commands: `run`, `route`, `setup`, `review`, `log`, `trace`, `gc`, `unlock`, `guard`.
