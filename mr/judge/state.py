"""Route state for the judge (spec 9.7): fixed shape, redacted before hashing, size-checked, no caller params."""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass

from mr import redact

TASK_CHARS = 4000
FILES_NAMED_MAX = 30
DEFAULT_LIMIT_TOKENS = 16000
DEFAULT_BYTES_PER_TOKEN = 3.0


@dataclass(frozen=True)
class BuiltState:
    state: dict
    state_sha: str
    state_bytes: int
    est_tokens: int
    redactions: dict
    truncated: bool
    egress: str  # "excerpts" | "metadata"
    status: str | None  # None when sendable, else "too_long" | "redaction_abort"


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _exists_in_repo(repo: str, rel: str) -> bool:
    """True only for a relative path that resolves, symlinks included, to an existing entry inside the repo."""
    if not rel or os.path.isabs(rel):
        return False
    root = os.path.realpath(repo)
    full = os.path.realpath(os.path.join(root, rel))
    return (full == root or full.startswith(root + os.sep)) and os.path.exists(full)


def build_route_state(task, features, cfg, repo_name: str, questions: dict, env=None) -> BuiltState:
    judge = getattr(cfg, "judge", None) or {}
    stack = (judge.get("stacks") or {}).get(repo_name)
    named = list(features.files_named[:FILES_NAMED_MAX])
    raw = {
        "kind": "mr.route.v1",
        "task": task.text[:TASK_CHARS],
        "repo": repo_name,
        "stack": stack if isinstance(stack, str) else None,
        "facts": {
            "files_named": named,
            "files_exist": {},
            "grep_hit_files": features.grep_hit_files,
            "risk_tags_rules": sorted(features.risk_tags),
            "risk_floor": features.risk_floor,
            "difficulty_floor": features.difficulty_floor,
            "cross_layer": features.cross_layer,
            "public_interface": features.public_interface,
            "new_module": features.new_module,
            "ambiguity_heuristic": features.ambiguity,
            "type_keyword_hits": {k: v for k, v in features.type_hits.items() if v},
        },
        "paths_sample": [],
    }
    state, counts = redact.redact_obj(raw, env)
    # redact_obj leaves dict keys raw: check existence on the raw name, key the result by the redacted name.
    files_exist: dict[str, bool] = {}
    for name in named:
        key, key_counts = redact.redact(name, env)
        files_exist[key] = files_exist.get(key, False) or _exists_in_repo(task.repo, name)
        for kind, n in key_counts.items():
            counts[kind] = counts.get(kind, 0) + n
    state["facts"]["files_exist"] = files_exist
    per_token = judge.get("token_estimate_bytes_per_token") or DEFAULT_BYTES_PER_TOKEN
    limit = (judge.get("state_limits_tokens") or {}).get("route", DEFAULT_LIMIT_TOKENS)
    q_tokens = math.ceil(len(_canon(questions)) / per_token)
    truncated = False

    def size() -> tuple[bytes, int]:
        data = _canon(state)
        return data, math.ceil(len(data) / per_token) + q_tokens

    data, est = size()
    if est > limit and state["paths_sample"]:
        state["paths_sample"] = ["[mr: truncated %d bytes]" % len(_canon(state["paths_sample"]))]
        truncated = True
        data, est = size()
    status = None
    if redact.too_many(counts):
        status = "redaction_abort"
    elif est > limit:
        status = "too_long"
    egress = "metadata" if "secrets" in features.risk_tags else judge.get("egress", "excerpts")
    return BuiltState(state, hashlib.sha256(data).hexdigest()[:16], len(data), est, counts, truncated, egress, status)


def build_route_state_v2(task, facts, cfg, repo_name: str, questions: dict, env=None, rules=None) -> BuiltState:
    """The mr.route.v2 state: the facts stage output only. It never reads task.params or task.files_named."""
    judge = getattr(cfg, "judge", None) or {}
    stack = (judge.get("stacks") or {}).get(repo_name)
    rules = rules or {}
    raw = {
        "kind": "mr.route.v2",
        "task": task.text[:TASK_CHARS],
        "repo": {"name": repo_name, "stack": stack if isinstance(stack, str) else None,
                 "size_files": facts.repo_files},
        "facts": {
            "path_tokens": list(facts.path_tokens),
            "paths_exist": {},
            "candidates": [{"path": c.path, "why": list(c.why)} for c in facts.candidates[:FILES_NAMED_MAX]],
            "risk_candidates": [{"tag": i.tag, "source": i.source, "quote": i.quote}
                                for i in facts.risk_evidence if i.source == "text"],
            "sensitive_paths": list(facts.sensitive_paths),
            "type_features": facts.type_features,
            "rules_estimate": {k: rules.get(k) for k in ("type", "difficulty", "risk")},
        },
    }
    state, counts = redact.redact_obj(raw, env)
    exist: dict[str, bool] = {}
    for name, found in facts.paths_exist.items():  # redact_obj leaves dict keys raw
        key, key_counts = redact.redact(name, env)
        exist[key] = exist.get(key, False) or bool(found)
        for kind, n in key_counts.items():
            counts[kind] = counts.get(kind, 0) + n
    state["facts"]["paths_exist"] = exist
    per_token = judge.get("token_estimate_bytes_per_token") or DEFAULT_BYTES_PER_TOKEN
    limit = (judge.get("state_limits_tokens") or {}).get("route", DEFAULT_LIMIT_TOKENS)
    data = _canon(state)
    est = math.ceil(len(data) / per_token) + math.ceil(len(_canon(questions)) / per_token)
    status = "redaction_abort" if redact.too_many(counts) else "too_long" if est > limit else None
    secret_path = any(i.source == "path" and i.tag == "secrets" for i in facts.risk_evidence)
    egress = "metadata" if secret_path else judge.get("egress", "excerpts")
    return BuiltState(state, hashlib.sha256(data).hexdigest()[:16], len(data), est, counts, False, egress, status)
