"""Judge client: one guarded claude call and the status mapping of mr-spec §9.3.2 steps 1-2. No retries."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from mr import guard


@dataclass
class RawResult:
    status: str  # ok | guard_yield | timeout | cli_unavailable | cli_error | model_drift
    result_text: str | None = None
    exit_code: int | None = None
    latency_ms: int = 0
    duration_api_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    usd_equiv: float | None = None
    model_served: str | None = None


def _int(val) -> int | None:
    return int(val) if isinstance(val, (int, float)) and not isinstance(val, bool) else None


def _write_demoted(cfg, served: list[str]) -> None:
    folder = os.path.join(cfg.paths.home, "state")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "judge-demoted.json")
    tmp = f"{path}.{os.getpid()}.tmp"
    doc = {"reason": "model_drift", "served": served, "ts": datetime.now(timezone.utc).isoformat()}
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)


def call(prompt: str, cfg, *, mode: str, timeout_ms: int | None = None, exec_fn=None) -> RawResult:
    if timeout_ms is None:
        timeout_ms = ((cfg.judge or {}).get("timeouts_ms") or {}).get("call", 12000)
    run = guard.run_claude_judge(prompt, cfg, timeout_ms / 1000, mode=mode, exec_fn=exec_fn)
    latency = int(run.wall_s * 1000)
    if run.status != "ran":
        return RawResult(run.status, exit_code=run.rc, latency_ms=latency)
    raw = RawResult("cli_error", exit_code=run.rc, latency_ms=latency)
    if run.rc != 0:
        return raw
    try:
        env = json.loads(run.stdout)
    except ValueError:
        return raw
    if not isinstance(env, dict) or env.get("is_error") is True:
        return raw
    usage = env.get("usage") if isinstance(env.get("usage"), dict) else {}
    cost = env.get("total_cost_usd")
    served = env.get("modelUsage")
    served = sorted(served) if isinstance(served, dict) else []
    text = env.get("result")
    raw.result_text = text if isinstance(text, str) else None
    raw.duration_api_ms = _int(env.get("duration_api_ms"))
    raw.input_tokens, raw.output_tokens = _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))
    raw.usd_equiv = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    raw.model_served = ",".join(served)
    if served != [guard.JUDGE_MODEL]:
        _write_demoted(cfg, served)
        raw.status = "model_drift"
    else:
        raw.status = "ok"
    return raw
