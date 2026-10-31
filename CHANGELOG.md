# Changelog

## Unreleased

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
