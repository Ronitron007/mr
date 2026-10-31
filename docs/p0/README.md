# P0 spec inputs (AC-PRE-1, AC-ISO-1, AC-SIG-3, AC-ESC-1, AC-REV-1, §7.5a)

Authored 30 Oct 2026 (IST) against mr-spec v1.2 and model-routing-plan. These are fixed expected outputs: implementers must not edit them to make a test pass; fix the code or raise a spec issue. The spec wins over this file, and this file wins where the spec is silent. Regexes are Python `re` with `re.IGNORECASE`. A "word list" `a|b c` means `\b(?:a|b c)\b`. Files hold one JSON array, one row per line.

## 1. `tests/fixtures/p0/prepass_table.json` (AC-PRE-1, §5.2, plan §4.2)
Row: `{id, text, files_named: list[str]|null, expect:{type, risk_tags: sorted list[str], risk_floor: low|medium|high, difficulty_floor: trivial|medium|hard, confident: bool}}`. There is no repo, so `grep_hit_files` = 0 and is ignored.
- **Path tokens.** Split `text` on whitespace. Strip leading ``( [ { " ' ` `` and trailing ``) ] } " ' ` . , ; : ! ?`` characters. A stripped token is a path if it fullmatches one of these: `[\w.@*~-]*/[\w.@*~/-]*` (contains a slash); `[\w.@*~-]*\.(ts|tsx|js|jsx|mjs|cjs|json|sql|md|yml|yaml|plist|entitlements|swift|kt|gradle|toml|lock|css|html|sh|pem|key|p8|p12|keystore|mobileprovision|env)`; `\.env[\w.-]*`; `(Podfile|Gemfile)(\.lock)?`.
- `files_named` = the row value when it is not null; otherwise the path tokens, deduplicated in first-seen order. `n = len(files_named)`.
- **Prose** = `text` with every whitespace token that is a path token removed. Type keywords, text risk regexes and hedge words run on the prose only, so `foo.test.ts` is not a "test" keyword.
- **Type.** `type_hits[t]` = `len(re.findall(word_list_t, prose))`. `type` = the type with the most hits. Ties go to the type listed first below. No hits → `code`.
  - code: `implement|implements|implementing|feature|endpoint|integrate|wire up`
  - bugfix: `fix|fixes|fixed|fixing|bug|bugs|crash|crashes|crashing|broken|regression`
  - refactor: `refactor|refactoring|rename|extract|simplify|dedupe|clean up`
  - test: `test|tests|testing|coverage|vitest`
  - ios-native: `ios|xcode|swift|swiftui|cocoapods|pod install|native module`
  - ui: `ui|screen|screens|button|buttons|layout|styling|animation|modal|dark mode`
  - review: `review|audit`
  - plan: `plan|roadmap|proposal|design doc`
  - explain: `explain|explains|how does|what does|why does|walk me through`
  - docs: `docs|documentation|readme|changelog|jsdoc`
- **Risk tags** = text tags (word list on the prose) ∪ path tags (`re.search` on each entry of `files_named`).
  - auth: text `auth|authn|authz|authentication|authorization|login|logout|log in|log out|sign[- ]?in|sign[- ]?up|password|passwords|oauth|jwt|rls`; path `(^|/)(auth|login|signin|signup)(/|\.|$)`
  - payments: text `payment|payments|billing|stripe|checkout|subscription|subscriptions|invoice|invoices|refund|refunds|in-app purchases?|iap|revenuecat`; path `(^|/)(payments?|billing|stripe|checkout|subscriptions?|iap)(/|\.|$)`
  - migration: text `migration|migrations|migrate|alter table|schema change`; path `(^|/)migrations?(/|$)`
  - data-deletion: text `delete|deletes|deleted|deleting|deletion|purge|truncate|wipe|drop table`; path `(^|/)(delete|deletion|purge)[\w-]*(/|\.|$)`
  - native-config: text `entitlement|entitlements|cocoapods|pod install|config plugins?|bundle identifier|provisioning profile`; path `(^|/)(ios|android)/|(^|/)podfile$|(^|/)app\.json$|(^|/)app\.config\.\w+$|\.entitlements$|(^|/)info\.plist$`
  - deps: text `dependency|dependencies|devdependency|devdependencies|npm install|yarn add|pnpm add|expo install|upgrade|bump|lockfile`; path `(^|/)(package\.json|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|podfile|podfile\.lock)$`
  - secrets: text `secret|secrets|api key|api keys|private key|credential|credentials|env var|env vars|environment variables?`; path `(^|/)\.env[^/]*$|\.(pem|key|p8|p12|keystore|mobileprovision)$|(^|/)(googleservice-info\.plist|google-services\.json|eas\.json)$|(^|/)(credentials|secrets)[^/]*$|(^|/)supabase/\.temp/` (the §9.7 step 9 list)
- **Floors.** Any of auth, payments, migration or data-deletion → risk `high` and difficulty floor `hard`. Otherwise any of native-config, deps or secrets → `medium`/`medium`. Otherwise `low`/`trivial`. Then, if `n ≥ 8`, difficulty_floor = `hard` (risk is unchanged).
- **Ambiguity** (0..3) = `[n == 0] + [prose matches hedge list] + ["?" in raw text]`. Hedge list: `maybe|perhaps|probably|somehow|something|stuff|etc|whatever|not sure|figure out|tbd|improve|nicer`.
- **confident** = (exactly one type has ≥ 1 hit) ∧ (risk_tags ≠ ∅ ∨ n ≥ 8 ∨ n ≤ 2) ∧ ambiguity ≤ 1. The middle term is plan §4.2's "difficulty band is clear", so only 3..7 files with no risk tag counts as unclear.

## 2. `tests/fixtures/p0/isolation_table.json` (AC-ISO-1, plan §6.2)
Row: `{id, inputs:{RO, OTHER_WRITER, DIRTY_FOREIGN, PARALLEL, EXPERIMENT, HIGH_RISK, DEPS, PROTECTED, OWNED_BRANCH: bool}, expect: "W0".."W7", note}`. The rules are checked in this order, and the first true one wins: W0 `RO`; W1 `PARALLEL ∨ OTHER_WRITER`; W2 `DIRTY_FOREIGN`; W3 `EXPERIMENT`; W4 `HIGH_RISK ∨ DEPS`; W5 `PROTECTED`; W6 `OWNED_BRANCH`; W7 otherwise. `decide_isolation` returns the raw code. Mapping W1–W4 to exit 4 in v1 (§0.2) is the caller's job.

## 3. `tests/fixtures/p0/failure_table.json` (AC-SIG-3, §5.9, §10.4, plan §7.3a)
Row: `{id, inputs:{model: str, rc: int, is_error, timed_out, wrapper_exit_75, hook_block, has_envelope: bool, diff_files: list[str], diff_loc: int, mapped_files_touched, H, D: bool, A_failing: list[str], C: "pass"|"fail"|"unclear"|null, mr_result: object|null, prior_attempts: [{model, complete: bool, A_failing}]}, expect:{cls, hard: bool, confident: bool}, note}`. `cls` ∈ `skipped|tried_wrong|spec_suspect|off_track|infra|guard_deny`. `C` null = C not run. `prior_attempts` holds only attempts made since the last re-plan (§10.6), oldest first, without the current rung.
**Check order (exact):**
1. `wrapper_exit_75 ∨ hook_block` → `guard_deny` (hard false, confident true). This comes before infra, because a wrapper 75 also has rc ≠ 0 and no envelope.
2. `is_error ∨ timed_out ∨ (rc ≠ 0 ∧ ¬has_envelope)` → `infra` (hard false, confident true).
3. Evaluate the four §7.3a detectors independently into a set M. Definitions: `s` = `mr_result.status`, or null when mr_result is null; `base(m)` = m with a trailing `-(low|medium|high|xhigh|max)` removed.
   - `complete` := mapped_files_touched ∧ H ∧ s = done.
   - `TWO` := complete ∧ A_failing ≠ [] ∧ ∃p ∈ prior_attempts: p.complete ∧ base(p.model) ≠ base(model) ∧ p.A_failing ∩ A_failing ≠ ∅.
   - `valid_pb` := s = pushback ∧ pushback is an object ∧ pushback.type ∈ {spec_vs_code, wrong_test, scope_needed, interface_impossible} ∧ item and evidence are non-empty strings.
   - `ev` := some requirements[].evidence is non-empty, or pushback is an object with non-empty evidence.
   - **off_track** := ¬D.
   - **spec_suspect** := TWO ∨ valid_pb.
   - **skipped** := mr_result = null ∨ (s = blocked ∧ ¬ev) ∨ (s = pushback ∧ ¬valid_pb) ∨ (s = done ∧ diff_files = []) ∨ (s ≠ pushback ∧ ¬mapped_files_touched) ∨ ¬H.
   - **tried_wrong** := mapped_files_touched ∧ H ∧ s = done ∧ (A_failing ≠ [] ∨ C ∈ {fail, unclear}) ∧ ¬TWO.
4. `cls` = the first member of M in the order off_track, spec_suspect, skipped, tried_wrong. If M = ∅, `cls` = tried_wrong (the rules fallback). `confident` = (|M| = 1). `hard` = (cls = off_track) ∨ (cls = spec_suspect ∧ TWO).

## 4. `tests/fixtures/p0/escalation_table.json` (AC-ESC-1, §10.2, §10.4, §10.6; used in L1/T16)
Row: `{id, ladder: list[rung], difficulty, used_rungs, current_rung, cls, attempts, claude_rungs, opus_rungs, infra_retries_this_rung, effort_up_used: list[base model], guard_allows: bool, cursor_paid_left: int, replans: int, expect:{action: rung|replan|exit, rung: code|null, exit: int|null, reset: bool, effort_up: bool}, note}`.
- Counters are taken **after** the failed current rung: it is already in `used_rungs` and already counted in `attempts`, `claude_rungs` and `opus_rungs` (Claude reviewer calls count toward `claude_rungs`). `guard_allows` is the guard's answer for the Claude rung that would be launched next.
- Constants: `cap` = {trivial 3, easy 3, medium 4, hard 4, critical 4}. Claude rungs: S, S+, O, O+. Opus rungs: O, O+. Paid rungs: K-h, K-x. `up` = {G-l→G-m, G-m→G-h, G-h→G-x, K-h→K-x, S→S+, O→O+}. C25, G-x, K-x, S+ and O+ have no `up`. `base`: G-* → grok-4.7, C25 → composer-2.5, K-* → kimi-k3, S/S+ → claude-sonnet-5-5, O/O+ → claude-opus-5-5.
- `ok(r)` := r ∉ used_rungs ∧ (r not paid ∨ cursor_paid_left > 0) ∧ (r not Claude ∨ claude_rungs < 2) ∧ (r not Opus ∨ opus_rungs < 1).
- `fallback()` := the first r in [G-x, K-x] with ok(r) ∧ attempts < cap, returned as `rung r`. If there is none → `exit 75`.
- `launch(r)` := if r is Claude ∧ ¬guard_allows → `fallback()`; else `rung r`.

Steps (the first one that applies decides):
1. `infra`: if infra_retries_this_rung = 0 → `launch(current_rung)`. This is not an attempt, so it skips the cap, ok() and paid checks; only the guard applies. If ≥ 1 → current is Claude ? `fallback()` : `exit 1`.
2. `guard_deny` → `fallback()`. Never retried.
3. `spec_suspect` → replans = 0 ∧ attempts < cap ? `replan` (rung null, reset true) : `exit 3`.
4. attempts ≥ cap → `exit 1`.
5. `off_track` → reset = true, then continue as tried_wrong.
6. `skipped` → u = up[current]. If u exists ∧ base(current) ∉ effort_up_used ∧ ok(u) → `launch(u)` with effort_up = true (false if launch fell back). Otherwise continue as tried_wrong.
7. `tried_wrong` → `launch(first r in ladder order with ok(r))`. If there is none → `exit 1`.

On an exit, rung is null and reset and effort_up are false. reset is true only for off_track (on a launched rung) and for replan. Resets caused by a broad failure (plan §6.4: > 3 failing A items or > 10 H errors) are decided outside next_step, so this table does not cover them.

## 5. `tests/fixtures/p0/checklist.example.json` (§7.5a)
This is a valid planner output for a small example app task. Validity rules used:
- the keys are exactly those in §7.5a;
- every requirement is in some acceptance `covers`;
- every `files_map` path is in scope.modify ∪ scope.create;
- every acceptance `file` is a `tests[].path` and is in scope.create;
- every `tests[]` name equals an `it()` title in that file (A matches on the vitest JSON `title`, not `fullName`).

On base the spec fails with "module not found" for `src/features/chat/muteSettings.ts`, which is in scope.create, so it counts as red.

## 6. `tests/fixtures/p0/mr_review_fixtures.json` (AC-REV-1)
Single rows: `{id, text, expect: object|null, note}`. Retry pairs: `{id, attempts: [text1, text2], expect_overall, note}`.
**Parse:**
1. `lines = text.splitlines()[-50:]`.
2. Candidates are the lines that start with `MR_REVIEW ` at column 0. The last candidate wins; an earlier valid one is never used as a fallback.
3. Run `json.loads` on the rest of the line.
4. The result must be an object with `items` (a list of objects with `id` str, `verdict` ∈ pass|fail and `evidence` str) and `overall` ∈ pass|fail|unclear. Anything else → null.

Pair rule: if the first attempt is null, the reviewer is re-asked once. If the second attempt is also null → `overall: unclear`; otherwise use the second attempt's overall. MR_RESULT (§7.3) uses the same line rules.

## 7. Prompts `mr/prompts/{implementer,planner,reviewer}.v1.txt` (§7.5a)
These are `str.format` templates. The only fields are: implementer `{goal} {scope} {locked_files} {handoff} {task_id}`; planner `{task_text} {files_named} {repo_tree_excerpt} {task_id}`; reviewer `{item} {requirements} {patch} {task_id}`. Literal braces are doubled. The golden test pins the sha256 of each file as stored.

## Decisions (the spec was silent or underspecified)
- D-P1 Type ties are broken by list order (code first). "Exactly one type keyword matched" means exactly one *type* has hits; repeated keywords of one type are fine.
- D-P2 With no type keyword the type is `code` (§5.2) and the row is never confident: zero types matched.
- D-P3 Keywords and text risk regexes ignore path tokens; `?` is checked on the raw text. Known limits: `and/or` and `Node.js` count as paths.
- D-P4 Risk floor comes from tags only. ≥ 8 files raises only difficulty_floor (§5.2: "from risk tags / file counts").
- D-P5 The `delete` words tag data-deletion even for code deletes. This errs strict, which is allowed because rules may only raise risk. `Podfile` tags both native-config and deps (plan §6.2 DEPS).
- D-F1 Added the input `model` (the current rung's model id), because "two *different* models" cannot be decided without it. Models are compared by `base()`, so an effort-up (grok high→xhigh) is the same model.
- D-F2 C `unclear` counts as a review fail in classify. Classify only runs on a rung the loop has already failed, and §0.2 low-risk `unclear` is a pass that never reaches it.
- D-F3 A valid pushback is spec_suspect but **not hard**. §5.9 reserves hard for D and for the two-model rule. Pushback skips the diff-based skipped detectors, because pushback normally comes with no diff.
- D-F4 `blocked` with evidence and mapped files touched matches no detector, so it is tried_wrong with confident false (the §5.9 example). An empty M falls back to tried_wrong, which escalates rather than repeats.
- D-F5 When several classes match, the priority is off_track > spec_suspect > skipped > tried_wrong (hard classes first; skipped is cheaper to act on).
- D-E1 Added the input `replans` and the action `replan`. "Re-plan once, then exit 3" needs a counter, and a re-plan is not a rung.
- D-E2 spec_suspect with no attempts left exits 3, not 1: the spec is the problem, and exit 3 > 1 in §2.3.
- D-E3 Capped or paid-capped ladder entries are skipped, not terminal, like `paid_cap` in §10.6. The fallback chain also obeys the paid cap and the attempts cap. If the chain is empty after a guard deny (or a second Claude infra) → 75 (§2.3: 75 when the last blocker was a guard deny).
- D-E4 An infra retry of a Claude rung goes through the guard again. A deny then takes the fallback chain. An infra retry of K-* does not check the paid ledger.
- D-E5 Effort-up O→O+ exists (O+ is in §10.2's "no higher" list), but the Opus cap of 1 always blocks it in v1.
- D-E6 `reset` is false on exits: the patch is saved and FINAL.md is written, and the tree is left as it is.
- D-R1 MR_REVIEW is checked against its schema; a schema mismatch counts as invalid, like bad JSON. The tag must start at column 0.
