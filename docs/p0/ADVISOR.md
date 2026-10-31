# P0-ADV: advisor decision fixture table

Expected-output table for `advisor.decide`, derived **only** from
`docs/spec/advisor-consult-spec.md` (§3.1 inputs, §3.2 decision table, §4 gates,
§4.3 estimates, §11 config, 30 Oct 2026 changelog: caps 150k/150k, `audit_max`
180k, mode default `on`). No code was consulted.

Fixture: `tests/fixtures/advisor/decide_table.json` (50 rows, V01–V50).

## Row schema

```json
{"id":"V01",
 "inputs":{"mode":"on|off|shadow|trial","flag":"auto|off|force","consults_flag":1|2,"slot_flag":null|"early"|"final",
   "budget":"normal|free|low|high","rung":"S+|S|O|O+|G-h|K-h|C25","route":"cascade|design|judgment",
   "item_kind":null|"logic"|"mechanical"|"ui"|"native","type":"code|bugfix|refactor|docs|explain|test|review|ui|ios-native",
   "difficulty":"trivial|easy|medium|hard|critical","risk":"low|medium|high","ambiguity":"low|medium|high",
   "load_bearing":true,"sf_tags":[],"hist":[{"model":"grok-4.7-high","cls":"tried_wrong","failing":["A1"]}],
   "guard":{"hour":0,"reserved":0,"five_hour_pct":30},"adv_hour":0,"adv_day":0,
   "est_consult_tokens":{"early":45000,"final":70000},"rows_off":[],"env_ok":true,"demoted":false,"breaker":false,
   "sa_used_in_task":false},
 "expect":{"row":"N5","consult":false,"slots":[],"gate":"ok","run_rung":"S+"},
 "note":"why"}
```

Input mapping to spec names: `mode` = `advisor.mode`; `flag` = `--advisor
off|auto|force`; `consults_flag` = `--advisor-consults 1|2`; `slot_flag` =
`--advisor-slot early|final`; `budget` = `--budget`; `demoted` =
`state/advisor-demoted.json` present (§9.4); `breaker` = `advisor_breaker` open
(§7 launch error, §8 `over_cap`); `sa_used_in_task` = an `S+A` rung already ran
in this task (G3); `env_ok` = neither `DISABLE_TELEMETRY` nor
`CLAUDE_CODE_DISABLE_ADVISOR_TOOL` set (G6).

## Conventions applied

- The §3.2 table is evaluated **first match wins**, in the listed order:
  N0, N1, N2, N3, N4, N5, N6, F1, Y1, Y2, Y3, Y4, Y5, N7.
- Gates run **only when the table says yes** (F1/Y rows). For every N row:
  `consult=false`, `slots=[]`, `gate="ok"`, `run_rung` = the input rung
  unchanged — even when gate inputs would fail (row V45).
- `gate` ∈ `ok | budget | hourly_cap | transcript | mode | unsupported |
  task_cap`. Gate order (§4, "checked in order"): **G1, G3, G4, G5, G6, G7**;
  the first failing gate wins (row V49: G1+G4 both fail → `budget`).
- Gate-blocked rows: `consult=false`, `slots=[]`, `run_rung="S+"` — except G1
  when plain S+ does not fit either, then `run_rung=null` (normal
  Claude-denied fallback, mr-spec §8).
- `run_rung="S+A"` exactly when `consult=true`.

## Exact gate formulas applied

- **G1 budget** (12-point S+A reservation, §4): pass iff
  `hour + reserved + 12 + 5 ≤ 40` **and** `five_hour_pct + 12 ≤ 92`.
  On failure: `gate="budget"`. Fallback rung: `"S+"` iff
  `hour + reserved + 6 + 5 ≤ 40` **and** `five_hour_pct + 6 ≤ 92`, else `null`.
  Boundaries (AC-ADV-3): hour 23/reserved 0 → 40 allowed; hour 24 → 41 denied;
  five_hour 80 → 92 allowed; 81 → 93 denied.
- **G3 task**: pass iff `sa_used_in_task == false`; else `gate="task_cap"`,
  run plain S+ (at most 1 S+A rung per task/item).
- **G4 box**: pass iff `adv_hour < 3` **and** `adv_day < 10`; else
  `gate="hourly_cap"`. `adv_hour == 3` refuses because this run would be the
  4th advisor run within the rolling hour (AC-ADV-3). Caps from §11
  (`per_hour_box: 3`, `per_day_box: 10`).
- **G5 transcript** (caps raised 30 Oct 2026): a slot is allowed iff
  `est_consult_tokens[slot] ≤ 150000`. If the primary slot is `final` and over
  the cap but `early` fits → switch to `["early"]` (still `consult=true`,
  `gate="ok"`). If both are over → `consult=false`, `gate="transcript"`, plain
  S+ (O stays as the later rung). 150000 exactly is allowed; 150001 is over.
- **G6 environment**: pass iff `env_ok == true`; else `gate="unsupported"`.
- **G7 mode**: fail (`gate="mode"`, plain S+) iff `mode == "shadow"` **or**
  `demoted` **or** `breaker` **or** the matched row ∈ `rows_off`. Otherwise
  pass. `off` never reaches G7 (it is N0).

## Slots rules applied

- F1: `[slot_flag]` if given, else `["early"]`.
- Y1: `["early"]`; `["early","final"]` only when `consults_flag == 2`
  (explicit `--advisor-consults 2`, G2).
- Y2, Y4, Y5: `["early"]`. Y3: `["final"]`.
- `consults_flag == 2` on any non-Y1 row has no effect (G2: 2 consults only
  under Y1 + explicit flag) — row V48.
- Gates can only shrink or block slots (G5 switch/drop), never add them.

## Decisions (spec was silent or ambiguous)

- **D-A1 — `force` does not beat the N rows.** §3.2 says the table is "first
  match wins" and F1 sits after N0–N6, but the precedence bullet says
  "explicit params (`--advisor off/force`) beat the table". Resolved in favour
  of the table order: `--advisor force` only converts an N7 outcome into a
  consult; N0–N6 (mode/budget off, non-S+ rung, design item, mechanical/docs,
  judgment taste, trivial/easy, routine medium) still yield no consult
  (row V46). This is also the only reading consistent with AC-ADV-2's
  never-rows and with "force does not bypass" the hard limits. The literal
  precedence-bullet reading would make row F1 redundant.
- **D-A2 — `item_kind` survives a design-route override.** N2 requires
  `route == design`; when the design route is skipped (`--route cascade`, per
  Y2's parenthetical) the item keeps its `item_kind`, so `mechanical` still
  hits N3 (V12) while `logic` can hit Y2 (V24).
- **D-A3 — `trial` mode passes G7.** G7's failure list covers `shadow`,
  demotion, breaker and `rows_off`; `trial` ("only inside `mr eval
  advisor-trial`") behaves like `on` for the decision table (V47).
- **D-A4 — gate-blocked rows report `slots=[]`.** No consult is scheduled, so
  no slots are promised; the would-have-been slot is recoverable from the row
  id. (The §7 log records `consult_requested=true, consult_made=false`.)
- **D-A5 — G5 on two-slot Y1.** Each slot is checked against its own estimate.
  If `final` is over the cap but `early` fits, the `final` slot is dropped and
  `["early"]` remains (V50); the spec's switch rule is generalised from the
  single-primary-slot wording. If the primary `early` estimate itself is over
  the cap, the result is `transcript` (the spec only defines a final→early
  switch, and `final` is never cheaper).
- **D-A6 — two-model same-check is N7, not Y5.** Y5 requires *exactly one*
  `tried_wrong` by another model; two different models failing the same check
  is `spec_suspect` (goes to the spec author, mr-spec §10.2). At medium
  difficulty nothing else matches, so the row is N7 (V28).
- **D-A7 — N2 ignores difficulty and signals.** Any design-route spec item is
  N2 regardless of criticality, sf_tags or risk (V11), because Opus already
  planned it.
- **D-A8 — enum readings.** Y5's "difficulty ≥ medium" = {medium, hard,
  critical}; "risk ≥ medium" = {medium, high}.
- **D-A9 — `budget: high` is not an N0 condition.** N0 lists only
  `free|low`; `high` behaves as `normal` (V21).
- **D-A10 — G4 is strict.** "At most 3 per rolling 60 min / 10 per IST day"
  plus AC-ADV-3 ("the 4th advisor run within an hour is refused") means
  `adv_hour < 3` and `adv_day < 10`, not `≤`.

## Spec contradictions / tensions found

1. **Precedence bullet vs. table order (the only real one).** §3.2's
   precedence list says explicit params beat the table, while the table itself
   is "first match wins" with F1 in position 8. Taken literally, `--advisor
   force` would consult on easy tasks and non-S+ rungs, conflicting with
   AC-ADV-2's never-rows ("even with judge raises") and making the F1 row
   pointless. Resolved per D-A1; an implementing model may read it the other
   way, so this should be clarified with the maintainer.
2. **N4's first clause is redundant, not contradictory.** "`route == judgment`
   and the rung is `O`" is already covered by N1, as the spec itself notes;
   harmless overlap.
3. **Worked example ambiguity is benign.** §3.2's worked example says the
   trial task is "N5 or N6"; both give no consult, so the expected outcome is
   the same. The fixture pins the easy reading (N5) per the task brief (V16).

*Spec-only derivation; no implementation code
was read.*
