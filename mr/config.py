"""Load and validate routes.yaml (spec §4)."""
from __future__ import annotations

import copy
import fnmatch
import math
import os
import re
from dataclasses import dataclass

import yaml

from mr.defaults import defaults
from mr.paths import data_dir, default_config_path

TYPES = ("code", "bugfix", "refactor", "test", "ios-native", "ui", "review", "plan", "explain", "docs")
DIFFS = ("trivial", "easy", "medium", "hard", "critical")
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
EXCLUDED = ("gpt-*", "gemini-*", "auto", "claude-haiku-*", "glm-*", "deepseek-*", "claude-fable-*", "muse-*")
CANARIES = (
    "cursor:gpt-5",
    "cursor:gemini-3-pro",
    "cursor:auto",
    "cursor:claude-haiku-5-5",
    "max:claude-haiku-5-5",
    "cursor:glm-5",
    "cursor:deepseek-v4",
    "max:claude-fable-5-1",
    "cursor:muse-1",
    "cursor:claude-sonnet-5-5-high",
    "cursor:claude-opus-5-5-high",
    "cursor:claude-sonnet-5-5",
)
_CURSOR_HAIKU = re.compile(r"cursor:.*haiku")
_HUMAN = "+human_spec_review"
_ANTHROPIC_ALIASES = frozenset({"sonnet", "opus", "haiku"})
ANTHROPIC_MSG = "Anthropic models run only via the claude CLI (hard rule)"
# droid BYOK: provider -> custom-model baseUrl and default key env var (the only place this table lives)
DROID_PROVIDERS: dict = {"anthropic": {"base_url": "https://api.anthropic.com", "key_env": "ANTHROPIC_API_KEY"}}
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def is_anthropic(model: str) -> bool:
    """Hard rule §0.3: True for claude-* models and the bare sonnet/opus/haiku aliases."""
    low = model.lower()
    return low.startswith("claude-") or low in _ANTHROPIC_ALIASES


def clamp_effort(model: str, effort: str | None) -> str | None:
    """Sonnet and Opus run only at high or xhigh. Other models are unchanged."""
    if not (fnmatch.fnmatchcase(model, "claude-sonnet-*") or fnmatch.fnmatchcase(model, "claude-opus-*")):
        return effort
    if effort == "max":
        return "xhigh"
    if effort in ("high", "xhigh"):
        return effort
    return "high"


class ConfigError(Exception):
    """Invalid routes.yaml. The message names the offending key path."""


@dataclass(frozen=True)
class Paths:
    claude: str
    check: str
    cursor_agent: str
    home: str
    droid: str = "droid"


@dataclass(frozen=True)
class Rung:
    code: str
    model: str
    path: str
    effort: str | None
    advisor: str | None = None


@dataclass(frozen=True)
class Caps:
    attempts: dict[str, int]
    claude_rungs_per_task: int
    opus_rungs_per_task: int


@dataclass(frozen=True)
class Timeouts:
    rung_s: dict[str, int]
    planner_s: int
    reviewer_s: int
    hygiene_s: int
    acceptance_s: int


@dataclass(frozen=True)
class ClaudeLimits:
    hour_max: float | None
    five_hour_max: float


@dataclass(frozen=True)
class GuardCfg:
    probe_if_sample_older_s: int
    wait_lock_s: int
    est_seed: dict[str, float]
    reservation_ttl_s: int


@dataclass(frozen=True)
class ReviewCfg:
    map: dict[str, str]
    high_risk_grok: str
    tiebreak: str
    claude_denied: str


@dataclass(frozen=True)
class PlannerCfg:
    rungs: dict[str, str]
    claude_denied: str


@dataclass(frozen=True)
class Config:
    version: int
    paths: Paths
    allowed: tuple[str, ...]
    rungs: dict[str, Rung]
    ladders: dict[str, dict[str, tuple[str, ...] | str]]
    review_required: dict
    design_route: dict
    judgment_route: dict
    escalation: dict
    hygiene: dict
    worktrees: dict
    caps: Caps
    timeouts: Timeouts
    guard: GuardCfg
    claude_limits: ClaudeLimits
    review: ReviewCfg
    planner: PlannerCfg
    fallback_claude_denied: tuple
    classifier: dict | None
    judge: dict | None
    advisor: dict | None
    acceptance: dict
    warnings: tuple[str, ...]
    source: str
    droid: dict | None = None


def load(path: str | None = None, overrides: dict | None = None) -> Config:
    """Resolve a config file, deep-merge it over defaults, then validate."""
    if path is not None:
        chosen, file_explicit = path, True
    else:
        env = os.environ.get("MR_CONFIG")
        chosen, file_explicit = (env, True) if env else (default_config_path(), False)
    overlay: dict = {}
    if file_explicit or os.path.isfile(chosen):
        if not os.path.isfile(chosen):
            raise ConfigError(f"{chosen}: config file not found")
        overlay = _read_yaml(chosen)
        raw = _deep_merge(defaults(), overlay)
        source = chosen
    else:
        raw = defaults()
        source = "<defaults>"
    if overrides:
        if isinstance(overrides, dict):
            overlay = _deep_merge(overlay, overrides)
        raw = _deep_merge(raw, overrides)
    block = overlay.get("claude_limits")
    if isinstance(block, dict):
        return from_dict(raw, source=source, limits_explicit=True, limits_keys=frozenset(block))
    return from_dict(raw, source=source, limits_explicit=False)


def from_dict(raw: dict, source: str = "<dict>", *, limits_explicit: bool | None = None,
              limits_keys: frozenset | None = None) -> Config:
    extra: list[str] = []
    if isinstance(raw, dict):
        extra = _resolve_claude_limits(raw, limits_explicit=limits_explicit, limits_keys=limits_keys)
    return _build(raw, source, validate(raw) + extra)


def validate(raw: dict) -> list[str]:
    """Return warnings. Raise ConfigError on any schema or policy violation."""
    if not isinstance(raw, dict):
        raise ConfigError("config: expected a mapping")
    if isinstance(raw.get("version"), bool) or raw.get("version") != 1:
        raise ConfigError("version: must be 1")
    _cursor_haiku(raw, "")
    _cursor_anthropic(raw, "")
    rungs = _rungs(raw)
    patterns = _allowed(raw)
    attempts = _attempts(raw)
    warnings = _ladders(raw, rungs, attempts)
    _uses(raw, rungs, patterns)
    _classifier(raw, patterns)
    _judge(raw)
    _droid(raw)
    _blocks(raw)
    _acceptance_block(raw)
    warnings.extend(_paths(raw))
    return warnings


def ladder(cfg: Config, type: str, difficulty: str) -> tuple[str, ...] | str:
    try:
        return cfg.ladders[type][difficulty]
    except KeyError:
        raise ConfigError(f"ladders.{type}.{difficulty}: missing") from None


def rung(cfg: Config, code: str) -> Rung:
    try:
        return cfg.rungs[code]
    except KeyError:
        raise ConfigError(f"rungs.{code}: rung {code!r} not defined") from None


def _deep_merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = copy.deepcopy(base)
        for key, val in over.items():
            if key in out and isinstance(out[key], dict) and isinstance(val, dict):
                out[key] = _deep_merge(out[key], val)
            else:
                out[key] = copy.deepcopy(val)
        return out
    return copy.deepcopy(over)


def _read_yaml(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except OSError as exc:
        raise ConfigError(f"{path}: {exc.strerror or exc}") from None
    except yaml.YAMLError:
        raise ConfigError(f"{path}: invalid YAML") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping")
    return data


def _model_excluded(model: str) -> str | None:
    for fam in EXCLUDED:
        if fnmatch.fnmatchcase(model, fam):
            return fam
    return None


def _pattern_excluded(model_pat: str) -> str | None:
    hit = _model_excluded(model_pat)
    if hit:
        return hit
    for fam in EXCLUDED:
        if fnmatch.fnmatchcase(fam, model_pat):
            return fam
    return None


def _match(key: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(key, pat) for pat in patterns)


def _rungs(raw: dict) -> dict:
    rungs = raw.get("rungs")
    if not isinstance(rungs, dict) or not rungs:
        raise ConfigError("rungs: missing")
    for code, spec in rungs.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"rungs.{code}: invalid")
        model, path = spec.get("model"), spec.get("path")
        if not isinstance(model, str) or not model:
            raise ConfigError(f"rungs.{code}.model: missing")
        if path not in ("cursor", "max"):
            raise ConfigError(f"rungs.{code}.path: must be cursor or max")
        if "effort" in spec and spec.get("effort") is not None and spec["effort"] not in EFFORTS:
            raise ConfigError(f"rungs.{code}.effort: must be low, medium, high, xhigh, or max")
        fam = _model_excluded(model)
        if fam or "haiku" in model:
            raise ConfigError(f"rungs.{code}.model: {model!r} is an excluded family")
        if path == "cursor" and is_anthropic(model):
            raise ConfigError(ANTHROPIC_MSG)
        if "advisor" in spec and (spec["advisor"] != ADVISOR_MODEL or path != "max"
                                  or model != "claude-sonnet-5-5"):
            raise ConfigError(f"rungs.{code}.advisor: only {ADVISOR_MODEL!r} on a max-path claude-sonnet-5-5 rung")
    return rungs


def _allowed(raw: dict) -> list[str]:
    allowed = raw.get("allowed")
    if not isinstance(allowed, list) or not allowed:
        raise ConfigError("allowed: missing")
    patterns: list[str] = []
    for i, entry in enumerate(allowed):
        if not isinstance(entry, str) or not entry:
            raise ConfigError(f"allowed[{i}]: must be a string")
        if _CURSOR_HAIKU.search(entry):
            raise ConfigError(f"allowed[{i}]: cursor-prefixed haiku")
        if entry.startswith("judge:"):
            continue
        pat = entry if entry.startswith(("cursor:", "max:")) else "cursor:" + entry
        if pat.startswith("cursor:") and is_anthropic(pat.split(":", 1)[1]):
            raise ConfigError(ANTHROPIC_MSG)
        for canary in CANARIES:
            if fnmatch.fnmatchcase(canary, pat):
                raise ConfigError(f"allowed[{i}]: {entry!r} matches canary {canary!r}")
        model = pat.split(":", 1)[1]
        fam = _pattern_excluded(model)
        if fam or (pat.startswith("cursor:") and "haiku" in model):
            raise ConfigError(f"allowed[{i}]: {entry!r} matches excluded family {fam or 'claude-haiku-*'!r}")
        patterns.append(pat)
    for code, spec in raw["rungs"].items():
        key = f"{spec['path']}:{spec['model']}"
        if not _match(key, patterns):
            raise ConfigError(f"rungs.{code}: {key!r} matches no unscoped allowed entry")
    return patterns


def _attempts(raw: dict) -> dict[str, int]:
    caps = raw.get("caps")
    if not isinstance(caps, dict):
        raise ConfigError("caps: missing")
    attempts = caps.get("attempts")
    if not isinstance(attempts, dict):
        raise ConfigError("caps.attempts: missing")
    for diff in DIFFS:
        if diff not in attempts:
            raise ConfigError(f"caps.attempts.{diff}: missing")
        num = attempts[diff]
        if isinstance(num, bool) or not isinstance(num, int) or num < 1:
            raise ConfigError(f"caps.attempts.{diff}: must be int >= 1")
    for key in ("claude_rungs_per_task", "opus_rungs_per_task"):
        num = caps.get(key)
        if isinstance(num, bool) or not isinstance(num, int):
            raise ConfigError(f"caps.{key}: must be an int")
    return attempts


def _ladders(raw: dict, rungs: dict, attempts: dict[str, int]) -> list[str]:
    ladders = raw.get("ladders")
    if not isinstance(ladders, dict):
        raise ConfigError("ladders: missing")
    warnings: list[str] = []
    for kind in list(ladders):
        if kind not in TYPES:
            raise ConfigError(f"ladders.{kind}: unknown type")
    for kind in TYPES:
        table = ladders.get(kind)
        if not isinstance(table, dict):
            raise ConfigError(f"ladders.{kind}: missing")
        for diff in list(table):
            if diff not in DIFFS:
                raise ConfigError(f"ladders.{kind}.{diff}: unknown difficulty")
        for diff in DIFFS:
            if diff not in table:
                raise ConfigError(f"ladders.{kind}.{diff}: missing")
            cell = table[diff]
            if cell == "design_route":
                continue
            if not isinstance(cell, (list, tuple)) or not cell:
                raise ConfigError(f"ladders.{kind}.{diff}: must be a non-empty list of rung codes or 'design_route'")
            for i, code in enumerate(cell):
                if code not in rungs:
                    raise ConfigError(f"ladders.{kind}.{diff}[{i}]: rung {code!r} not defined")
            if len(cell) > attempts[diff]:
                warnings.append(f"ladders.{kind}.{diff}: {len(cell)} rungs > caps.attempts.{diff}={attempts[diff]}")
    return warnings


def _uses(raw: dict, rungs: dict, patterns: list[str]) -> None:
    review = raw.get("review")
    if not isinstance(review, dict):
        raise ConfigError("review: missing")
    mapping = review.get("map")
    if not isinstance(mapping, dict):
        raise ConfigError("review.map: missing")
    for name, code in mapping.items():
        _code(code, f"review.map.{name}", rungs)
    _code(review.get("high_risk_grok"), "review.high_risk_grok", rungs)
    _code(review.get("tiebreak"), "review.tiebreak", rungs)
    _code(review.get("claude_denied"), "review.claude_denied", rungs)
    _non_anthropic(review["claude_denied"], "review.claude_denied", rungs)
    if rungs[review["claude_denied"]]["model"].startswith("grok-"):
        # grok output gets the Claude reviewer by default; its denied fallback must be another family
        raise ConfigError("review.claude_denied: must be a different family from grok")
    _planner(raw, rungs)
    denied = raw.get("fallback_claude_denied")
    if not isinstance(denied, (list, tuple)):
        raise ConfigError("fallback_claude_denied: missing")
    for i, code in enumerate(denied):
        _code(code, f"fallback_claude_denied[{i}]", rungs)
    judgment = raw.get("judgment_route")
    if not isinstance(judgment, dict):
        raise ConfigError("judgment_route: missing")
    for key in ("implement", "critical"):
        _code_list(judgment.get(key), f"judgment_route.{key}", rungs)
    design = raw.get("design_route")
    if not isinstance(design, dict):
        raise ConfigError("design_route: missing")
    architect = design.get("architect")
    if not isinstance(architect, dict):
        raise ConfigError("design_route.architect: missing")
    _code(architect.get("rung"), "design_route.architect.rung", rungs)
    implement = design.get("implement")
    if not isinstance(implement, dict):
        raise ConfigError("design_route.implement: missing")
    for key, val in implement.items():
        if isinstance(val, (list, tuple)):
            _code_list(val, f"design_route.implement.{key}", rungs)
    fallback = design.get("architect_fallback")
    if not isinstance(fallback, (list, tuple)):
        raise ConfigError("design_route.architect_fallback: missing")
    for i, entry in enumerate(fallback):
        path = f"design_route.architect_fallback[{i}]"
        if entry == "defer":
            continue
        if not isinstance(entry, str):
            raise ConfigError(f"{path}: invalid entry")
        if entry.startswith("cursor:"):
            if _CURSOR_HAIKU.search(entry) or _model_excluded(entry.split(":", 1)[1]):
                raise ConfigError(f"{path}: {entry!r} is an excluded family")
            if not _match(entry, patterns):
                raise ConfigError(f"{path}: {entry!r} matches no unscoped allowed entry")
            continue
        if entry.endswith(_HUMAN):
            _code(entry[: -len(_HUMAN)], path, rungs)
            continue
        _code(entry, path, rungs)


def _code(code, path: str, rungs: dict) -> None:
    if isinstance(code, str) and code not in rungs and (_model_excluded(code) or "haiku" in code):
        raise ConfigError(f"{path}: {code!r} is an excluded family")
    if code not in rungs:
        raise ConfigError(f"{path}: rung {code!r} not defined")


def _non_anthropic(code: str, path: str, rungs: dict) -> None:
    if is_anthropic(rungs[code]["model"]):
        raise ConfigError(f"{path}: {code!r} is an Anthropic rung; the denied fallback must be non-Anthropic")


def _planner(raw: dict, rungs: dict) -> None:
    block = raw.get("planner")
    if not isinstance(block, dict):
        raise ConfigError("planner: missing")
    by_diff = block.get("rungs")
    if not isinstance(by_diff, dict):
        raise ConfigError("planner.rungs: missing")
    for diff in list(by_diff):
        if diff not in DIFFS:
            raise ConfigError(f"planner.rungs.{diff}: unknown difficulty")
    for diff in DIFFS:
        if diff not in by_diff:
            raise ConfigError(f"planner.rungs.{diff}: missing")
        _code(by_diff[diff], f"planner.rungs.{diff}", rungs)
        if rungs[by_diff[diff]].get("advisor"):
            raise ConfigError(f"planner.rungs.{diff}: {by_diff[diff]!r} is an advisor rung")
    _code(block.get("claude_denied"), "planner.claude_denied", rungs)
    _non_anthropic(block["claude_denied"], "planner.claude_denied", rungs)


def _code_list(items, path: str, rungs: dict) -> None:
    if not isinstance(items, (list, tuple)):
        raise ConfigError(f"{path}: must be a list of rung codes")
    for i, code in enumerate(items):
        _code(code, f"{path}[{i}]", rungs)


def _classifier(raw: dict, patterns: list[str]) -> None:
    block = raw.get("classifier")
    if block is None:
        return
    if not isinstance(block, dict) or not isinstance(block.get("model"), str):
        raise ConfigError("classifier.model: missing")
    model = block["model"]
    if _model_excluded(model) or "haiku" in model:
        raise ConfigError(f"classifier.model: {model!r} is an excluded family")
    if is_anthropic(model):
        raise ConfigError(ANTHROPIC_MSG)
    key = f"cursor:{model}"
    if not _match(key, patterns):
        raise ConfigError(f"classifier.model: {key!r} matches no unscoped allowed entry")
    if "mode" in block:
        _canon_mode(block["mode"], "classifier.mode")
    n = block.get("sample_n", 0)
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ConfigError(f"classifier.sample_n: {n!r} must be an integer >= 0")
    wait = block.get("join_timeout_s", 1)
    if not isinstance(wait, (int, float)) or isinstance(wait, bool) or not math.isfinite(wait) or wait <= 0:
        raise ConfigError(f"classifier.join_timeout_s: {wait!r} must be a number > 0")


def _canon_mode(value, path: str) -> str:
    if value is False:
        value = "off"
    elif value is True:
        value = "on"
    if value not in ("off", "shadow", "on"):
        raise ConfigError(f"{path}: must be off, shadow, or on")
    return value


def _judge(raw: dict) -> None:
    block = raw.get("judge")
    if block is None:
        return
    if not isinstance(block, dict):
        raise ConfigError("judge: expected a mapping")
    _canon_mode(block.get("mode"), "judge.mode")
    if block.get("backend") != "haiku":
        raise ConfigError("judge.backend: must be 'haiku'")
    backends = block.get("backends")
    haiku = backends.get("haiku") if isinstance(backends, dict) else None
    if not isinstance(haiku, dict) or haiku.get("path") != "max" or haiku.get("model") != "claude-haiku-5-5":
        raise ConfigError("judge.backends.haiku: must be path 'max' and model 'claude-haiku-5-5'")
    if "qset" in block and block["qset"] not in ("route.v1", "route.v2"):
        raise ConfigError(f"judge.qset: {block['qset']!r} must be 'route.v1' or 'route.v2'")
    n = block.get("sample_n", 3)
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ConfigError(f"judge.sample_n: {n!r} must be an integer >= 0")
    budget = block.get("budget")
    if isinstance(budget, dict):
        per_hour = budget.get("calls_per_hour")
        if per_hour is not None and (_plain_number(per_hour) is None or per_hour <= 0):
            raise ConfigError("judge.budget.calls_per_hour: must be null or a number > 0")
        if "shadow_five_hour_max" in budget:
            cap = _plain_number(budget["shadow_five_hour_max"])
            if cap is None or not 0 <= cap <= 100:
                raise ConfigError("judge.budget.shadow_five_hour_max: must be a number from 0 to 100")


def _pos_int(val) -> bool:
    return isinstance(val, int) and not isinstance(val, bool) and val > 0


def _droid_twin(code: str, twin, raw: dict, names: set, enabled: bool) -> None:
    """One droid twin: a Sonnet max-path rung at high/xhigh effort, no advisor, a known provider.

    The rung effort rule applies only while droid is enabled: the shipped (disabled) block must not turn a
    rung effort that clamp_effort only warns about into a config error.
    """
    path = f"droid.twins.{code}"
    spec = raw["rungs"].get(code)
    if spec is None:
        raise ConfigError(f"{path}: rung {code!r} not defined")
    if not isinstance(twin, dict):
        raise ConfigError(f"{path}: expected a mapping")
    if spec.get("path") != "max":
        raise ConfigError(f"{path}: only a max-path rung can have a droid twin")
    for label, model in (("rung", spec["model"]), ("twin", twin.get("model"))):
        if not isinstance(model, str) or not fnmatch.fnmatchcase(model, "claude-sonnet-*"):
            raise ConfigError(f"{path}: the {label} model must match claude-sonnet-* (droid is Sonnet only)")
    if enabled and spec.get("effort") not in ("high", "xhigh"):  # the raw effort, before clamp_effort
        raise ConfigError(f"{path}: rungs.{code}.effort must be high or xhigh for a droid twin")
    if spec.get("advisor"):
        raise ConfigError(f"{path}: {code!r} is an advisor rung")
    if twin.get("provider", "anthropic") not in DROID_PROVIDERS:
        raise ConfigError(f"{path}.provider: must be one of {', '.join(DROID_PROVIDERS)}")
    name = twin.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{path}.name: missing")
    if name in names:
        raise ConfigError(f"{path}.name: {name!r} is used by another twin")
    names.add(name)
    for key in ("thinking_tokens", "max_output"):
        if not _pos_int(twin.get(key)):
            raise ConfigError(f"{path}.{key}: must be an int > 0")


def _droid(raw: dict) -> None:
    """Optional ``droid`` block (BYOK Sonnet backend for rungs S and S+)."""
    block = raw.get("droid")
    if block is None:
        return
    if not isinstance(block, dict):
        raise ConfigError("droid: expected a mapping")
    if not isinstance(block.get("enabled"), bool):
        raise ConfigError("droid.enabled: must be true or false")
    if not isinstance(block.get("settings", ""), str):
        raise ConfigError("droid.settings: must be a path string")
    if block.get("auto", "medium") not in ("low", "medium"):
        raise ConfigError("droid.auto: must be low or medium")
    if not _pos_int(block.get("recheck_s", 10800)):
        raise ConfigError("droid.recheck_s: must be an int > 0")
    envs = block.get("provider_key_env", {})
    if not isinstance(envs, dict) or any(not isinstance(v, str) or not _ENV_NAME.match(v) for v in envs.values()):
        raise ConfigError("droid.provider_key_env: must map a provider to an environment variable name")
    twins = block.get("twins", {})
    if not isinstance(twins, dict):
        raise ConfigError("droid.twins: expected a mapping")
    names: set = set()
    for code, twin in twins.items():
        _droid_twin(code, twin, raw, names, block["enabled"])


def _droid_copy(raw: dict) -> dict | None:
    """Deep copy of the droid block with the settings path, key env names and provider filled in."""
    block = _copy_block(raw, "droid")
    if block is None:
        return None
    if not block.get("settings"):
        block["settings"] = os.path.join(raw["paths"]["home"], "droid", "settings.json")
    block.setdefault("auto", "medium")
    block.setdefault("recheck_s", 10800)
    block["provider_key_env"] = {**{k: v["key_env"] for k, v in DROID_PROVIDERS.items()},
                                 **block.get("provider_key_env", {})}
    block.setdefault("twins", {})
    for twin in block["twins"].values():
        twin.setdefault("provider", "anthropic")
    return block


def _blocks(raw: dict) -> None:
    for key in ("review_required", "escalation", "hygiene", "worktrees"):  # cursor_paid (retired) is ignored if present
        if not isinstance(raw.get(key), dict):
            raise ConfigError(f"{key}: missing")
    _advisor_block(raw)
    timeouts = raw.get("timeouts")
    if not isinstance(timeouts, dict) or not isinstance(timeouts.get("rung_s"), dict):
        raise ConfigError("timeouts.rung_s: missing")
    for key in ("cursor", "claude"):
        _req_int(timeouts["rung_s"], key, f"timeouts.rung_s.{key}")
    if "droid" in timeouts["rung_s"]:
        _req_int(timeouts["rung_s"], "droid", "timeouts.rung_s.droid")
    for key in ("planner_s", "reviewer_s", "hygiene_s", "acceptance_s"):
        _req_int(timeouts, key, f"timeouts.{key}")
    _claude_limits_block(raw)
    guard = raw.get("guard")
    if not isinstance(guard, dict):
        raise ConfigError("guard: missing")
    for key in ("probe_if_sample_older_s", "wait_lock_s", "reservation_ttl_s"):
        _req_int(guard, key, f"guard.{key}")
    seeds = guard.get("est_seed")
    if not isinstance(seeds, dict) or not seeds:
        raise ConfigError("guard.est_seed: missing")
    for key, val in seeds.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ConfigError(f"guard.est_seed.{key}: must be a number")


def _req_int(obj: dict, key: str, path: str) -> int:
    if key not in obj:
        raise ConfigError(f"{path}: missing")
    val = obj[key]
    if isinstance(val, bool) or not isinstance(val, int):
        raise ConfigError(f"{path}: must be an int")
    return val


def _plain_number(val):
    """A real number, never a bool. None when the value cannot be mapped."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return val


def _keep_mapped(key: str, val: float, label: str, warnings: list[str]) -> bool:
    """Drop a legacy mapping that would fail the claude_limits check. The default stays."""
    if not math.isfinite(val) or val <= 0:
        warnings.append(f"{label}: ignored, mapped {key}={val} is not a positive number; keeping default")
        return False
    if key == "five_hour_max" and val > 100:
        warnings.append(f"{label}: ignored, mapped five_hour_max={val} is above 100; keeping default")
        return False
    return True


def _claude_limits_block(raw: dict) -> None:
    block = raw.get("claude_limits")
    if not isinstance(block, dict):
        raise ConfigError("claude_limits: missing")
    for key in ("hour_max", "five_hour_max"):
        val = block.get(key)
        if key == "hour_max" and val is None:
            continue  # null/absent = no hourly cap
        if _plain_number(val) is None or val <= 0:
            raise ConfigError(f"claude_limits.{key}: must be a positive number")
    if block["five_hour_max"] > 100:
        raise ConfigError("claude_limits.five_hour_max: must be <= 100")


def _guard_derived(guard: dict) -> tuple[dict, list[str]]:
    """Map legacy guard.limit/margin/five_hour_max. Non-numbers are ignored, not fatal.

    hour_max = limit - (margin or 0). Margin alone does not invent a limit, and a
    missing margin is 0, not the old default of 5. A mapped value that is not a
    legal claude_limits number is warned and dropped, so the default for that key stays.
    """
    out: dict = {}
    warnings: list[str] = []
    if "limit" in guard:
        limit = _plain_number(guard.get("limit"))
        if "margin" in guard:
            margin = _plain_number(guard["margin"])
            if margin is None:
                warnings.append("guard.margin: ignored, not a number")
                margin = 0
        else:
            margin = 0
        if limit is None:
            warnings.append("guard.limit: ignored, not a number")
        elif _keep_mapped("hour_max", limit - margin, "guard.limit/margin", warnings):
            out["hour_max"] = limit - margin
    if "five_hour_max" in guard:
        five = _plain_number(guard.get("five_hour_max"))
        if five is None:
            warnings.append("guard.five_hour_max: ignored, not a number")
        elif _keep_mapped("five_hour_max", five, "guard.five_hour_max", warnings):
            out["five_hour_max"] = five
    return out, warnings


def _judge_derived(budget: dict) -> tuple[dict, list[str]]:
    out: dict = {}
    warnings: list[str] = []
    for src, key in (("yield_at_hour_usage", "hour_max"), ("yield_at_five_hour_pct", "five_hour_max")):
        if src not in budget:
            continue
        val = _plain_number(budget.get(src))
        if val is None:
            warnings.append(f"judge.budget.{src}: ignored, not a number")
        elif _keep_mapped(key, val, f"judge.budget.{src}", warnings):
            out[key] = val
    return out, warnings


def _resolve_claude_limits(raw: dict, *, limits_explicit: bool | None,
                           limits_keys: frozenset | None = None) -> list[str]:
    """Fold legacy guard/judge knobs into claude_limits. Never raises on legacy keys.

    ``limits_explicit`` is True when the user file or overrides set ``claude_limits``.
    None means infer it from ``raw`` (``from_dict`` of a complete document).
    ``limits_keys`` is the keys the user actually set. A conflict warning compares
    only those keys, so a default filled in for an omitted key is not a conflict.
    """
    guard = raw.get("guard") if isinstance(raw.get("guard"), dict) else {}
    judge = raw.get("judge") if isinstance(raw.get("judge"), dict) else {}
    budget = judge.get("budget") if isinstance(judge.get("budget"), dict) else {}
    guard_map, warnings = _guard_derived(guard)
    judge_map, judge_warnings = _judge_derived(budget)
    warnings.extend(judge_warnings)
    if limits_explicit is None:
        limits_explicit = isinstance(raw.get("claude_limits"), dict)
        if limits_keys is None and isinstance(raw.get("claude_limits"), dict):
            limits_keys = frozenset(raw["claude_limits"])
    if limits_explicit:
        limits = raw.get("claude_limits")
        keys = limits_keys if limits_keys is not None else frozenset(limits or {})
        if isinstance(limits, dict) and _legacy_disagrees(limits, guard_map, judge_map, keys):
            warnings.append(
                "claude_limits: legacy guard.limit/margin/five_hour_max or "
                "judge.budget.yield_at_* disagree; claude_limits wins"
            )
        return warnings
    if not guard_map and not judge_map:
        return warnings
    chosen: dict = {}
    conflict = False
    for key in ("hour_max", "five_hour_max"):
        gval, jval = guard_map.get(key), judge_map.get(key)
        if key in guard_map and key in judge_map and gval != jval:
            conflict = True
        if key in guard_map:
            chosen[key] = gval
        elif key in judge_map:
            chosen[key] = jval
    base = dict(raw["claude_limits"]) if isinstance(raw.get("claude_limits"), dict) else {}
    base.update(chosen)
    raw["claude_limits"] = base
    if conflict:
        warnings.append(
            "claude_limits: legacy guard values and judge.budget.yield_at_* disagree; guard values win"
        )
    return warnings


def _legacy_disagrees(limits: dict, guard_map: dict, judge_map: dict, keys: frozenset) -> bool:
    for derived in (guard_map, judge_map):
        for key, val in derived.items():
            if key in keys and limits.get(key) != val:
                return True
    return False


def _command_path(value: str, key: str) -> None:
    """Absolute path, or a bare command name with no slash."""
    if os.path.isabs(value) or "/" not in value:
        return
    raise ConfigError(f"paths.{key}: must be absolute or a bare command name")


def _paths(raw: dict) -> list[str]:
    paths = raw.get("paths")
    if not isinstance(paths, dict):
        raise ConfigError("paths: missing")
    for key in ("claude", "check", "cursor_agent", "home"):
        if not isinstance(paths.get(key), str):
            raise ConfigError(f"paths.{key}: missing")
    for key in ("claude", "cursor_agent"):
        if not paths[key]:
            raise ConfigError(f"paths.{key}: missing")
        _command_path(paths[key], key)
    if "droid" in paths:
        if not isinstance(paths["droid"], str) or not paths["droid"]:
            raise ConfigError("paths.droid: missing")
        _command_path(paths["droid"], "droid")
    if paths["check"] and not os.path.isabs(paths["check"]):
        raise ConfigError("paths.check: must be absolute")
    if not paths["home"]:
        paths["home"] = data_dir()
    if not os.path.isabs(paths["home"]):
        raise ConfigError("paths.home: must be absolute")
    warnings: list[str] = []
    if os.environ.get("MR_TEST_ALLOW_FAKE_PATHS") == "1":
        warnings.append("MR_TEST_ALLOW_FAKE_PATHS set")
    return warnings


def _cursor_haiku(obj, path: str) -> None:
    if isinstance(obj, str):
        if _CURSOR_HAIKU.search(obj):
            raise ConfigError(f"{path or 'config'}: cursor-prefixed haiku")
        return
    if isinstance(obj, dict):
        for key, val in obj.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and _CURSOR_HAIKU.search(key):
                raise ConfigError(f"{child}: cursor-prefixed haiku")
            _cursor_haiku(val, child)
    elif isinstance(obj, (list, tuple)):
        for i, val in enumerate(obj):
            _cursor_haiku(val, f"{path}[{i}]")


def _cursor_anthropic(obj, path: str) -> None:
    """Hard rule §0.3: no literal cursor:<anthropic> string anywhere in the config."""
    if isinstance(obj, str):
        if obj.startswith("cursor:") and is_anthropic(obj.split(":", 1)[1]):
            raise ConfigError(ANTHROPIC_MSG)
        return
    if isinstance(obj, dict):
        for key, val in obj.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.startswith("cursor:") and is_anthropic(key.split(":", 1)[1]):
                raise ConfigError(ANTHROPIC_MSG)
            _cursor_anthropic(val, child)
    elif isinstance(obj, (list, tuple)):
        for i, val in enumerate(obj):
            _cursor_anthropic(val, f"{path}[{i}]")


def _build(raw: dict, source: str, warnings: list[str]) -> Config:
    rungs: dict[str, Rung] = {}
    for code, spec in raw["rungs"].items():
        model = spec["model"]
        old = spec.get("effort") if spec.get("effort") is not None else None
        new = clamp_effort(model, old)
        if new != old:
            warnings.append(
                f"rungs.{code}.effort: {old!r} raised to {new!r} ({model} runs only at high or xhigh)"
            )
        rungs[code] = Rung(code, model, spec["path"], new, spec.get("advisor"))
    ladders: dict[str, dict[str, tuple[str, ...] | str]] = {}
    for kind in TYPES:
        ladders[kind] = {}
        for diff in DIFFS:
            cell = raw["ladders"][kind][diff]
            ladders[kind][diff] = cell if cell == "design_route" else tuple(cell)
    judge = _copy_block(raw, "judge")
    if judge is not None:
        judge["mode"] = _canon_mode(judge.get("mode"), "judge.mode")
    classifier = _copy_block(raw, "classifier")
    if classifier is not None and "mode" in classifier:
        classifier["mode"] = _canon_mode(classifier["mode"], "classifier.mode")
    guard = raw["guard"]
    timeouts = raw["timeouts"]
    return Config(
        version=1,
        paths=Paths(raw["paths"]["claude"], raw["paths"]["check"], raw["paths"]["cursor_agent"], raw["paths"]["home"],
                    raw["paths"].get("droid", "droid")),
        allowed=tuple(raw["allowed"]),
        rungs=rungs,
        ladders=ladders,
        review_required=copy.deepcopy(raw["review_required"]),
        design_route=copy.deepcopy(raw["design_route"]),
        judgment_route=copy.deepcopy(raw["judgment_route"]),
        escalation=copy.deepcopy(raw["escalation"]),
        hygiene=copy.deepcopy(raw["hygiene"]),
        worktrees=copy.deepcopy(raw["worktrees"]),
        caps=Caps(
            attempts={k: raw["caps"]["attempts"][k] for k in DIFFS},
            claude_rungs_per_task=raw["caps"]["claude_rungs_per_task"],
            opus_rungs_per_task=raw["caps"]["opus_rungs_per_task"],
        ),
        timeouts=Timeouts(
            rung_s={"droid": 1800, **timeouts["rung_s"]},
            planner_s=timeouts["planner_s"],
            reviewer_s=timeouts["reviewer_s"],
            hygiene_s=timeouts["hygiene_s"],
            acceptance_s=timeouts["acceptance_s"],
        ),
        guard=GuardCfg(
            probe_if_sample_older_s=guard["probe_if_sample_older_s"],
            wait_lock_s=guard["wait_lock_s"],
            est_seed={k: float(v) for k, v in guard["est_seed"].items()},
            reservation_ttl_s=guard["reservation_ttl_s"],
        ),
        claude_limits=ClaudeLimits(
            hour_max=raw["claude_limits"].get("hour_max"),
            five_hour_max=raw["claude_limits"]["five_hour_max"],
        ),
        review=ReviewCfg(
            map=dict(raw["review"]["map"]),
            high_risk_grok=raw["review"]["high_risk_grok"],
            tiebreak=raw["review"]["tiebreak"],
            claude_denied=raw["review"]["claude_denied"],
        ),
        planner=PlannerCfg(
            rungs=dict(raw["planner"]["rungs"]),
            claude_denied=raw["planner"]["claude_denied"],
        ),
        fallback_claude_denied=tuple(raw["fallback_claude_denied"]),
        classifier=classifier,
        judge=judge,
        advisor=_advisor_block(raw),
        acceptance=_acceptance_block(raw),
        warnings=tuple(warnings),
        source=source,
        droid=_droid_copy(raw),
    )


ADVISOR_MODEL = "claude-opus-5-5"
ADVISOR_DEFAULTS: dict = {  # advisor-consult-spec §11
    "mode": "on", "rows_off": [], "injection": "adv.v2", "max_consults": 1, "reserve_pts": 12,
    "per_hour_box": 3, "per_day_box": 10,
    "transcript_cap_tokens": {"early": 150000, "final": 150000, "audit_max": 180000},
    "timeouts_s": {"slow": 150, "stall": 300},
    "sample": {"shadow_pair": 0.5, "on_control": 0.2, "max_hour_usage_for_pairs": 10},
    "monitor": {"window_pairs": 20, "min_pairs": 10, "harm_rate_max": 0.05, "relay_alarm_below": 0.5,
                "usd_p90_max": 0.80},
}
_ADVISOR_MODES = ("off", "shadow", "trial", "on")
_ADVISOR_ROWS = frozenset({"F1", "Y1", "Y2", "Y3", "Y4", "Y5"})


def _advisor_block(raw: dict) -> dict:
    """Optional ``advisor`` block, merged over the §11 defaults. Absent means ``mode: on`` with defaults."""
    block = raw.get("advisor")
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise ConfigError("advisor: expected a mapping")
    for key in block:
        if key not in ADVISOR_DEFAULTS:
            raise ConfigError(f"advisor.{key}: unknown key")
    out = _deep_merge(ADVISOR_DEFAULTS, block)
    mode = out["mode"]
    out["mode"] = "on" if mode is True else "off" if mode is False else mode
    if out["mode"] not in _ADVISOR_MODES:
        raise ConfigError("advisor.mode: must be off, shadow, trial, or on")
    rows = out["rows_off"]
    if not isinstance(rows, list) or any(r not in _ADVISOR_ROWS for r in rows):
        raise ConfigError("advisor.rows_off: must be a list of F1/Y1-Y5 row ids")
    if out["injection"] != "adv.v2":
        raise ConfigError("advisor.injection: must be adv.v2")
    if out["max_consults"] not in (1, 2) or isinstance(out["max_consults"], bool):
        raise ConfigError("advisor.max_consults: must be 1 or 2")
    for key in ("reserve_pts", "per_hour_box", "per_day_box"):
        if _req_int(out, key, f"advisor.{key}") < 0:
            raise ConfigError(f"advisor.{key}: must be >= 0")
    caps = out["transcript_cap_tokens"]
    for key in ("early", "final", "audit_max"):
        if _req_int(caps, key, f"advisor.transcript_cap_tokens.{key}") <= 0:
            raise ConfigError(f"advisor.transcript_cap_tokens.{key}: must be > 0")
    if caps["audit_max"] < max(caps["early"], caps["final"]):
        raise ConfigError("advisor.transcript_cap_tokens.audit_max: must be >= the early and final caps")
    times = out["timeouts_s"]
    for key in ("slow", "stall"):
        _req_int(times, key, f"advisor.timeouts_s.{key}")
    if not 0 < times["slow"] < times["stall"]:
        raise ConfigError("advisor.timeouts_s: need 0 < slow < stall")
    for sub in ("sample", "monitor"):
        if not isinstance(out[sub], dict):
            raise ConfigError(f"advisor.{sub}: expected a mapping")
        for key, val in out[sub].items():
            if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0:
                raise ConfigError(f"advisor.{sub}.{key}: must be a number >= 0")
    return out


_RUNNERS = frozenset({"vitest", "unittest"})


def _acceptance_block(raw: dict) -> dict:
    """Optional ``acceptance`` block. Absent means ``{runners: {}, default: vitest}``."""
    block = raw.get("acceptance")
    if block is None:
        return {"runners": {}, "default": "vitest"}
    if not isinstance(block, dict):
        raise ConfigError("acceptance: expected a mapping")
    runners = block.get("runners", {})
    if not isinstance(runners, dict):
        raise ConfigError("acceptance.runners: expected a mapping")
    clean: dict[str, str] = {}
    for key, val in runners.items():
        if not isinstance(key, str) or not key:
            raise ConfigError("acceptance.runners: keys must be repo names")
        if val not in _RUNNERS:
            raise ConfigError(f"acceptance.runners.{key}: must be vitest or unittest")
        clean[key] = val
    default = block.get("default", "vitest")
    if default not in _RUNNERS:
        raise ConfigError("acceptance.default: must be vitest or unittest")
    return {"runners": clean, "default": default}


def _copy_block(raw: dict, key: str) -> dict | None:
    block = raw.get(key)
    return None if block is None else copy.deepcopy(block)
