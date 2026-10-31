# Changelog

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
