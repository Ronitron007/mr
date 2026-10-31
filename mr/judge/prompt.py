"""Judge prompt (judge.v1): render with neutral q01.. keys and strictly read the reply (spec 9.3.2 steps 3-5)."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "judge.v1.txt")


@dataclass
class ParsedAnswers:
    status: str  # ok | ok_partial | invalid_response
    answers: dict  # original id -> compact form
    unsure: tuple[str, ...]


def _template_bytes() -> bytes:
    with open(_TEMPLATE, "rb") as fh:
        return fh.read()


def prompt_sha() -> str:
    return hashlib.sha256(_template_bytes()).hexdigest()[:12]


def render_prompt(state: dict, questions: dict) -> tuple[str, dict]:
    qmap = {"q%02d" % i: qid for i, qid in enumerate(questions, 1)}
    shown = {}
    for key, qid in qmap.items():
        shown[key] = {k: ("yes/no" if k == "type" and v == "noul" else v) for k, v in questions[qid].items()}
    text = _template_bytes().decode("utf-8")
    text = text.replace("{STATE}", json.dumps(state, ensure_ascii=False))
    text = text.replace("{QUESTIONS}", json.dumps(shown, ensure_ascii=False))
    return text, qmap


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _one(q: dict, raw):
    """Compact form for one answer, or None when it is unusable (the question becomes unsure)."""
    if not isinstance(raw, dict) or set(raw) != {"answer", "confidence"}:
        return None
    ans, conf = raw["answer"], raw["confidence"]
    if not _number(conf) or not 0.5 <= conf <= 1.0:
        return None
    kind = q.get("type")
    if kind == "noul":
        return {"p": conf if ans is True else 1 - conf} if isinstance(ans, bool) else None
    if kind == "choice":
        return {"c": ans, "conf": conf} if isinstance(ans, str) and ans in q.get("criteria", {}) else None
    if kind == "score":
        ok = isinstance(ans, int) and not isinstance(ans, bool) and 0 <= ans < len(q.get("criteria", ()))
        return {"s": ans, "conf": conf} if ok else None
    return None


def parse_answers(result_text: str, qmap: dict, questions: dict) -> ParsedAnswers:
    bad = ParsedAnswers("invalid_response", {}, ())
    try:
        obj = json.loads(result_text.strip())
    except ValueError:
        return bad
    if not isinstance(obj, dict) or set(obj) != {"answers"} or not isinstance(obj["answers"], dict):
        return bad
    given = obj["answers"]
    if any(k not in qmap for k in given):
        return bad
    answers, unsure = {}, []
    for key, qid in qmap.items():
        compact = _one(questions[qid], given[key]) if key in given else None
        if compact is None:
            unsure.append(qid)
        else:
            answers[qid] = compact
    return ParsedAnswers("ok_partial" if unsure else "ok", answers, tuple(unsure))
