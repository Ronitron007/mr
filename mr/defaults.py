"""Built-in default routes, stored inline as YAML text."""
from __future__ import annotations

import yaml

DEFAULT_YAML: str = r"""version: 1
paths:
  claude: claude                               # command name or absolute path. Never claude-real, never --bare
  check: ""
  cursor_agent: cursor-agent
  home: ""
allowed: [grok-4.7-*, composer-2.5, composer-2.5-fast, kimi-k3-low, kimi-k3-high, kimi-k3-max, kimi-k2.7-code,
          max:claude-sonnet-5-5, max:claude-opus-5-5,   # no cursor:claude-* (hard rule §0.3)
          "judge:max:claude-haiku-5-5"]   # role-scoped: judge only, claude CLI only; never a rung, never cursor-agent
rungs:
  G-l: {model: grok-4.7-low, path: cursor}
  G-m: {model: grok-4.7-medium, path: cursor}
  G-h: {model: grok-4.7-high, path: cursor}
  G-x: {model: grok-4.7-xhigh, path: cursor}
  C25: {model: composer-2.5, path: cursor}
  K-h: {model: kimi-k3-high, path: cursor}      # K-*: defined for opt-in only; no default ladder or map uses them
  K-x: {model: kimi-k3-max, path: cursor}
  S:   {model: claude-sonnet-5-5, path: max, effort: high}
  S+:  {model: claude-sonnet-5-5, path: max, effort: xhigh}
  O:   {model: claude-opus-5-5, path: max, effort: high}
  O+:  {model: claude-opus-5-5, path: max, effort: xhigh}
  S+A: {model: claude-sonnet-5-5, path: max, effort: xhigh, advisor: claude-opus-5-5}   # T20; advisor-consult-spec §2, §11; never in a ladder (takes S+'s slot at runtime)
ladders:                                    # plan §3.1, verbatim
  code:       {trivial: [G-l, G-m], easy: [G-m, G-h, S], medium: [G-h, G-x, S], hard: [G-x, S+, O], critical: design_route}
  bugfix:     {trivial: [G-m, G-h], easy: [G-h, G-x, S], medium: [G-x, S, O], hard: [G-x, S+, O], critical: [S+, O+]}
  refactor:   {trivial: [G-m, G-h], easy: [G-h, G-x], medium: [G-x, S], hard: [G-x, S+, O], critical: design_route}
  test:       {trivial: [G-l, G-m], easy: [G-m, G-h], medium: [G-h, G-x], hard: [G-x, S+], critical: [S, O]}
  ios-native: {trivial: [G-m, G-h], easy: [G-h, G-x, S], medium: [G-x, S, O], hard: [S+, O], critical: design_route}
  ui:         {trivial: [C25, G-m], easy: [G-m, G-h], medium: [G-h, G-x, S], hard: [G-x, S+], critical: design_route}
  review:     {trivial: [G-m], easy: [G-h], medium: [S], hard: [S], critical: [S]}   # single reviewer in v1; council (G-x+S, S+G-x) deferred
  plan:       {trivial: [G-m], easy: [G-h], medium: [G-x], hard: design_route, critical: design_route}
  explain:    {trivial: [G-l], easy: [G-m], medium: [G-h], hard: [G-x, S], critical: [S]}
  docs:       {trivial: [G-l], easy: [G-l, G-m], medium: [G-m, G-h], hard: [G-h, S], critical: [S]}
review_required: {bugfix_critical: true}    # [+R] cells
design_route:
  triggers: {risk_tags: [auth, payments, migration, data-deletion], cross_layer_min: 2, hard_and_ambiguous: true}
  # v1.2: no `types`/`difficulty` triggers; the table cell decides (a cell value `design_route` still routes there)
  architect: {rung: O+, mode: plan, max_per_feature: 1, max_replans: 1, max_opus_plans_per_hour: 2, est_pts: 12}
  architect_fallback: ["S+", "G-x+human_spec_review", defer]   # Cursor-path Opus removed (hard rule §0.3)
  implement: {mechanical: [C25, G-h, G-x], ui: [C25, G-h, G-x], logic: [G-h, G-x], native: [G-h, G-x], then: [S+], then_replan: true}
judgment_route: {implement: [S+], critical: [O], delegate_mechanical_subitems: true}
escalation: {skipped: raise_effort, tried_wrong: next_rung, spec_suspect: spec_author, off_track: reset}
caps: {attempts: {trivial: 3, easy: 3, medium: 4, hard: 4, critical: 4}, claude_rungs_per_task: 2, opus_rungs_per_task: 1}   # §10.6 (D18)
timeouts: {rung_s: {cursor: 900, claude: 900}, planner_s: 600, reviewer_s: 600, hygiene_s: 600, acceptance_s: 300}   # §10.4
hygiene: {commands: {}}   # per repo (§10.3); exact script names from the repo's package.json
fallback_claude_denied: [G-x]   # non-Anthropic only; cursor_same_tier removed (hard rule §0.3)
claude_limits: {hour_max: null, five_hour_max: 85}   # hour_max null = no hourly cap; five_hour_max shared by the rung guard and the judge
guard: {probe_if_sample_older_s: 600, wait_lock_s: 600,   # est held constant in v1 (§8)
        est_seed: {S: 4, S+: 6, S+A: 12, O: 10, O+: 15, PLAN_O: 12, C+: 3}, reservation_ttl_s: 3600}
review: {map: {grok: S, kimi: G-h, composer: G-h, claude: G-x}, high_risk_grok: S, tiebreak: S, claude_denied: C25}
planner: {rungs: {trivial: G-h, easy: G-h, medium: G-h, hard: O, critical: O}, claude_denied: G-h}   # rung by task difficulty; O only via the claude CLI; not a ladder rung (no claude_rungs/opus_rungs count)
worktrees: {per_repo: 3, box: 6, queue_min: 10, stale_h: 24, unmerged_keep_d: 14,
            protected: [main, master, "release/*", production], env_include: [".env", ".env.local"]}
advisor: {mode: "on", rows_off: [], injection: adv.v2, max_consults: 1, reserve_pts: 12, per_hour_box: 3, per_day_box: 10,   # T20; advisor-consult-spec §11
          transcript_cap_tokens: {early: 150000, final: 150000, audit_max: 180000}, timeouts_s: {slow: 150, stall: 300},
          monitor: {window_pairs: 20, min_pairs: 10, harm_rate_max: 0.05, relay_alarm_below: 0.5, usd_p90_max: 0.80}}
classifier: {model: grok-4.7-low, mode: off, timeout_s: 30, min_conf: 0.6, cache_days: 7, sample_n: 3, join_timeout_s: 75}   # optional block; deferred (§0.2)

judge:
  mode: "off"                     # off | shadow | on. Optional block; `off` until Phase 2 builds the judge, then `shadow`
  backend: haiku                  # the only default ("for now"); comparison backends below are off
  qset: route.v2                  # route.v1 | route.v2: the question set the shadow judge asks (v2 adds files and tag evidence)
  egress: excerpts                # off | metadata | excerpts   (§12.2; D2 closed: same vendor as the Claude rungs)
  uses:
    route:   {enabled: true, promoted: false, fields: all}             # partial promotion: list of field names
    spec:    {enabled: true, promoted: false, on_effect: annotate}   # annotate | human_gate | replan  (D5)
    postrun: {enabled: true, promoted: false, conformance: true, failure_class: true, max_extra_claude_per_day: 2}
  repos: "all"                    # "all" or a list of repo names; a repo not listed -> judge skipped for that task
  sample_n: 3                     # shadow: a task the rules are confident about is still judged 1 in n (0 = never)
  backends:
    haiku: {path: max, model: claude-haiku-5-5, effort: low,   # effort: low | null (null = omit --effort), §9.3.1
            prompt: judge.v1}                                    # via the wrapper only; never cursor-agent
    # Optional later comparison backends: pluggable, compare_only, never applied, off. Need secrets.env (§9.3.3).
    flash: {enabled: false, compare_only: true, provider: cloudflare, model_path: "@cf/cloudflare/clef-flash",
            model_field: clef-flash, context_tokens: 24576, usd_per_m_input: 0.038}
    clef:  {enabled: false, compare_only: true, provider: cloudflare, model_path: "@cf/cloudflare/clef",
            model_field: clef, context_tokens: 65536, usd_per_m_input: 0.24}
    jev:   {enabled: false, compare_only: true, provider: openrouter, model: "typesafe/jev-1.14",
            context_tokens: 65536, usd_per_m_input: 0.042}
  thresholds:
    act_p: 0.75                   # act on a soft decision (route change, type, difficulty above floor, failure class)
    raise_p: 0.60                 # act on a stricter-only signal (risk up, tag add, extra review, worktree)
    abstain_band: [0.50, 0.75]    # soft answers with confidence in this band -> unsure, logged only. No re-ask (§9.5)
    human_band_high_risk: 2       # judge vs rules difficulty gap that asks a human on high-risk tasks (plan §5.2)
  timeouts_ms: {call: 12000, decision_point: 15000, comparison: 15000}   # judge runs only at task boundaries
  state_limits_tokens: {route: 16000, spec: 16000, postrun: 16000, conformance_diff: 8000}
  token_estimate_bytes_per_token: 3.0
  budget: {calls_per_hour: null, calls_per_day: 150, max_calls_per_task: 6,    # §9.10; null = no hourly cap
           est_pts_per_call: 0.2, shadow_five_hour_max: 80}                    # shadow calls stop at this five_hour_pct
  cache: {route_ttl_days: 7, spec_ttl_days: 30, postrun_ttl_days: 30}
  breaker: {consecutive_failures: 3, open_minutes: 15}
"""


def defaults() -> dict:
    loaded = yaml.safe_load(DEFAULT_YAML)
    if not isinstance(loaded, dict):
        raise ValueError("DEFAULT_YAML did not parse to a mapping")
    return loaded
