# Split mode plan `plan`

This plan builds [split mode](SPEC.md) and the fixes that it needs first. Each unit below is one PR. Each PR has a check that fails on `main` today and passes on the PR.

The order is gates, then route fixes, then split mode, then the measure step. Split mode needs correct gates. A unit that lands on a false green breaks every unit after it.

## How to run this plan `plan.rules`

- Branch each PR from `main` as `rohan/<unit name>`. A PR that depends on another PR uses the branch of that PR as its base.

- Opus implements each PR. A fresh Opus reviewer checks each PR. The implementer never reviews its own PR.

- A PR merges only when its checks pass and its review has no open finding. Merge with a merge commit.

- A code PR and its docs change go in two PRs.

- Changes to defaults, such as `split.mode` on, need Rohan's explicit yes.

- The measure step makes paid model calls. It needs Rohan's explicit yes for each run.

## Gate fixes `plan.gates`

These PRs fix the checks that pass a bad change. Issue [Ronitron007/mr#1](https://github.com/Ronitron007/mr/issues/1) has the reproductions. The script `mr-review-reproduce.py` from that issue gives each PR its failing check.

| PR | Change | Check that fails today |
|---|---|---|
| `gate.hygiene_baseline` | Record the hygiene baseline before the planner writes its tests. Give each failure a stable id. Count a nonzero exit with no parsed failure as a failure. | The `regression-hidden-by-red-baseline` and `missing-tsc-command-passes-hygiene` reproductions accept the change. |
| `gate.acceptance_health` | Keep the return code, the timeout and the collection errors of a test run. Signal A fails when the run is not healthy. A plan with no requirement or no test is not valid. | The `suite-failure-accepted` and `empty-checklist-valid` reproductions return A.ok true. |
| `gate.review_contract` | The reviewer must give one verdict for each requirement id. mr computes `overall` from the items. A reviewer crash is an infra failure, not `unclear`. Each review prompt holds the task text. | The `review-verdict-not-crosschecked`, `reviewer-crash-accepted-low-risk` and `review-prompt-no-task` reproductions accept or omit. |
| `gate.self_report` | Handle `blocked` and `pushback` before mr accepts a patch. | The `incomplete-self-report-accepted` reproductions exit 0. |
| `gate.caps` | Pass the configured caps to `next_step` and check them before the first launch. Show planner and reviewer rungs in `mr route`. | The `zero-claude-cap-ignored` reproduction picks `S`. |
| `gate.ref_targets` | Record the object id of each ref in the snapshot and compare it. | The `existing-ref-move-invisible` reproduction returns no violation. |
| `gate.no_checks` | `mr run` stops with exit 4 when the repo has no hygiene commands. The flag `--no-checks` allows the run. | A trivial task with no hygiene config exits 0 with H, A and C all empty. |

`gate.review_contract` and `gate.self_report` both edit `mr/loop.py`. Do them in that order, and base the second on the first.

## Route fixes `plan.route`

These PRs fix what the live Haiku run showed on 2026-11-09. They do not depend on the gate fixes.

| PR | Change | Check that fails today |
|---|---|---|
| `route.judge_bars` | Keep one table of bars in `mr/judge/policy.py`. A stricter answer needs `raise_p`, a neutral one needs `act_p`, and a looser one needs `act_p` and a quote. The reader and the merge both use it. | Haiku says difficulty `medium` at 0.70, and mr keeps `trivial`. |
| `route.uncertain_floor` | When the judge is unsure, unavailable or behind an open breaker, difficulty for `code`, `bugfix` and `refactor` is at least `medium`. mr prints one stderr line and writes `judge_absent` in the task record. | A race fix with the judge unavailable gets the ladder `G-m, G-h` and no review. |
| `route.word_difficulty` | When the judge drops a text risk tag, the difficulty base comes from the remaining tags. | "delete the unused helper" stays `hard` after the judge drops the tag. |

## Split mode `plan.split`

These PRs build split mode behind `split.mode: off`. They depend on all gate fixes.

| PR | Depends on | Change | Check that fails today |
|---|---|---|---|
| `split.units` | gates | Add `kind` to each requirement in the planner prompt and in `planner.validate`. Add `mr/units.py` with `Unit`, `UnitResult` and `units_from`. Log the units in the planner row. No run changes. | `units_from` on the checklist fixture returns one unit per requirement with the right tests and files. |
| `split.loop` | `split.units` | Run the units in order with their unit ladders, as `split.flow` says. Reset to the tree before the unit on a failure. Use the stronger split planner rungs. | With fake CLIs, Grok fails unit R2 and Sonnet passes it. R1 stays landed and only R2 reaches Sonnet. |
| `split.integration` | `split.loop` | After each unit, run the tests of all landed units and hygiene. After the last unit, run all locked tests and one review of the full change. | With fake CLIs, unit R2 breaks the test of R1. The task does not accept. |
| `split.trace` | `split.loop` | Show one band per unit in `mr trace`. | The trace page of a split task shows no unit bands. |

## Measure step `plan.measure`

| PR | Change | Check |
|---|---|---|
| `measure.harness` | Add a task set of 20 to 30 real tasks from Rohan's repos at fixed commits. Each task has hidden tests that mr does not see. Add a script that runs three arms and writes a report. | The script runs on fake CLIs and writes a report with all three arms. |
| `measure.run` | Run the harness with real models. Arm A is always Sonnet with an Opus review. Arm B is mr with split mode on. Arm C is mr with split mode off. | Paid. Rohan approves the prompts, the models and the cost first. |
| `split.default_on` | Set `split.mode: on` in the defaults. | Only when arm B passes as many hidden tests as arm A and uses less quota. |

The report gives these numbers for each arm:

- Hidden tests passed and false accepts.

- Claude quota points, Cursor minutes and wall time.

- Quota per accepted task.

## Open question `plan.open`

Where does the Grok bot run? If it runs on this Mac, units must stay sequential. If it runs on a server, a later plan can run independent units at the same time. Each unit then needs its own worktree.
