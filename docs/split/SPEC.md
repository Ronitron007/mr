# Split mode spec `split`

## Why `split.why`

Rohan runs mr behind a Grok bot. Grok is a weak model. It fails when it must build a feature from scratch. It does well on a small change with a clear test.

Two failures count the same. The first is to accept a bad change when no person watches. The second is to pay Opus for work that a cheap model can do.

Split mode puts the strong model where it pays. A strong planner splits the task into small units. Each unit has a locked test. Grok implements one unit at a time. Only a failed unit goes to Sonnet. A strong reviewer from another model family checks the full change at the end.

## Terms `split.terms`

- **Unit.** One requirement from the planner checklist, with its locked tests and its files. The id of a unit is the requirement id, for example `R2`.

- **Kind.** The type of work in a unit. The values are `mechanical`, `logic`, `ui` and `native`.

- **Unit ladder.** The rungs that one unit can use, cheapest first. The kind of the unit selects the unit ladder.

- **Landed.** A unit lands when its checks pass. Its changes stay in the tree for the next unit.

- **Split task.** A task that mr runs in split mode.

## When split mode runs `split.when`

Split mode runs when all of these conditions are true:

- `split.mode` is `on`.

- The route is `cascade`, or `design` mapped to cascade.

- The difficulty is `medium`, `hard` or `critical`.

- The planner returns a valid checklist with two or more requirements.

Trivial and easy tasks keep the current path. They get no planner and no strong model before the first rung. A checklist with one requirement runs on the current ladder.

## Data shape `split.data`

The checklist keeps its current keys. Each requirement gets one new key, `kind`. The requirement order is the unit order.

```python
Kind = Literal["mechanical", "logic", "ui", "native"]

@dataclass(frozen=True)
class Unit:
    id: str                          # requirement id, "R1"
    kind: Kind
    text: str                        # the SHALL sentence
    examples: tuple[dict, ...]       # given / when / then
    files: tuple[str, ...]           # files_map[id]
    tests: tuple[tuple[str, str], ...]  # (file, test name) from every acceptance item that covers id

UnitStatus = Literal["pending", "running", "landed", "failed"]

@dataclass(frozen=True)
class UnitResult:
    unit: str
    status: UnitStatus
    rungs: tuple[str, ...]           # rungs tried, in order
    patch: str | None                # saved patch for this unit
    signals: dict                    # D, H, A for the last attempt
```

`units_from(checklist) -> tuple[Unit, ...]` is a pure function. It has no other input.

## Unit ladders `split.ladders`

The defaults are in `split.ladders`. Opus never implements a unit.

| Kind | Unit ladder |
|---|---|
| `mechanical` | `G-h`, `S` |
| `logic` | `G-x`, `S` |
| `ui` | `C25`, `G-h`, `S` |
| `native` | `S`, `S+` |

The planner rung for a split task is stronger than today. The defaults are `S` for medium, `O` for hard and `O+` for critical. The reason is that the quality of the split decides the result.

## Flow `split.flow`

1. The planner writes the checklist. mr locks the tests. Each named test must fail on the base tree.

2. mr checks each unit before any executor runs. Each unit must have one or more tests and one or more files. A unit that fails this check stops the task with exit 3.

3. mr records the hygiene baseline on the base tree, before it writes the planner tests.

4. For each unit in order, mr runs the first rung of its unit ladder.

5. After each attempt, mr runs signal D on the unit files and signal H against the base baseline. mr also runs the tests of this unit and of every landed unit.

6. If all checks pass, the unit lands. mr goes to the next unit.

7. If a check fails, mr resets the tree to the state before this unit. Then mr runs the next rung of the unit ladder with a handoff.

8. If the last rung of a unit ladder fails, the task stops with exit 1. mr saves the patch of each landed unit.

9. After the last unit lands, mr runs all locked tests, hygiene and signal C. Signal C is one review of the full change against the task text and all requirements.

10. If signal C fails, the task stops with exit 1. mr does not run a second round of units.

## Executor brief `split.brief`

The implementer prompt for a unit holds these items:

- The task text, for context only.

- The text and the examples of this unit.

- The files of this unit, which are its scope.

- The locked tests of this unit, by file and name.

- The ids and files of the landed units, as read-only context.

- The handoff from the last failed attempt on this unit, if there is one.

The brief does not hold the other pending units. Parsel, Factory and Amp give their executors their own unit only.

## Caps `split.caps`

- A unit gets one attempt per rung on its unit ladder. An infra retry does not count.

- The task caps `claude_rungs_per_task` and `opus_rungs_per_task` count across all units. Planner and reviewer calls do not count, as today.

- When a cap stops the next Sonnet rung, the task stops with exit 1 and keeps the landed units.

## Records `split.records`

- The task record gets `split: true` and one `units` list of `UnitResult` rows.

- Each attempt row gets `unit: <id>`.

- `mr trace` shows one band per unit.

## Out of scope `split.out`

- Units that run at the same time. Units run one after another on one tree. A parallel version needs one worktree per unit.

- A re-plan after a unit fails. PaT re-plans on failure. mr stops instead, and the measure step decides if a re-plan pays.

- Changes to the intake judge. The route fixes in the plan are separate PRs.

## Prior art `split.prior_art`

No package combines a planner, locked tests per unit, a cheap executor and escalation per unit. So mr builds split mode on its own checklist.

| Source | Rule it gives split mode |
|---|---|
| [Parsel](https://arxiv.org/abs/2212.10561) | Small units with tests work. The quality of the split decides the result. |
| [PaT](https://www.microsoft.com/en-us/research/wp-content/uploads/2026/05/acl26_PaTCodeGen2.pdf) | The closest match, with the order reversed. A weak executor raises cost. Use a stop rule. |
| [Factory Missions](https://factory.com/news/missions-architecture) | Write the test contract before the work. Start each worker fresh on one unit. Check the full change at the end. |
| [Agentless](https://arxiv.org/html/2407.01489) | Generated tests are often wrong. Check them before you trust them. |
| [Aider architect](https://aider.chat/2024/09/26/architect.html) | A strong planner with a cheap editor scores higher than one model alone. |
| [Scrouting](https://huggingface.co/papers/2608.04804) | A checked handoff carries the result more than the router does. |

No source escalates the executor per unit after a failed test. The measure step must test this rule.
