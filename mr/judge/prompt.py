"""Judge prompt (judge.v1): render with neutral q01.. keys and strictly read the reply (spec 9.3.2 steps 3-5)."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from mr import redact

_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "judge.v1.txt")
_TEMPLATE_V2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "judge.v2.txt")
MAX_PICKS = 12
MAX_NEW = 6


@dataclass
class ParsedAnswers:
    status: str  # ok | ok_partial | invalid_response
    answers: dict  # original id -> compact form
    unsure: tuple[str, ...]


def _template_bytes(path: str = _TEMPLATE) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def prompt_sha(version: int = 1) -> str:
    return hashlib.sha256(_template_bytes(_TEMPLATE_V2 if version == 2 else _TEMPLATE)).hexdigest()[:12]


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
    return _parse(result_text, qmap, questions, _one)


def _parse(result_text: str, qmap: dict, questions: dict, one) -> ParsedAnswers:
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
        compact = one(questions[qid], given[key]) if key in given else None
        if compact is None:
            unsure.append(qid)
        else:
            answers[qid] = compact
    return ParsedAnswers("ok_partial" if unsure else "ok", answers, tuple(unsure))


# --- judge.v2: multi-choice, path-list and evidence answers ----------------------------------------------------

def render_prompt_v2(state: dict, questions: dict) -> tuple[str, dict]:
    qmap = {"q%02d" % i: qid for i, qid in enumerate(questions, 1)}
    shown = {}
    for key, qid in qmap.items():
        shown[key] = {k: ("yes/no" if k == "type" and v == "noul" else v) for k, v in questions[qid].items()}
    shown, _counts = redact.redact_obj(shown)  # a token in a candidate path must not reach the prompt
    text = _template_bytes(_TEMPLATE_V2).decode("utf-8")
    text = text.replace("{STATE}", json.dumps(state, ensure_ascii=False))
    text = text.replace("{QUESTIONS}", json.dumps(shown, ensure_ascii=False))
    return text, qmap


def _squash(text: str) -> str:
    return " ".join(text.lower().split())


def _confidence(raw: dict, keys: set):
    """The confidence of an answer object with exactly these keys, or None when keys or value are wrong."""
    if set(raw) != keys:
        return None
    conf = raw["confidence"]
    return conf if _number(conf) and 0.5 <= conf <= 1.0 else None


def _string_list(raw, limit: int):
    ok = isinstance(raw, list) and len(raw) <= limit and all(isinstance(x, str) for x in raw)
    return list(dict.fromkeys(raw)) if ok else None


def _new_path_ok(path: str, facts) -> bool:
    if not path or path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/"):
        return False
    if facts.paths_exist.get(path):
        return False
    return not any(c.path == path and c.exists for c in facts.candidates)


def _listed(q: dict, raw, facts):
    """Compact form of a files.change or files.new answer, or None."""
    if not isinstance(raw, dict):
        return None
    conf = _confidence(raw, {"answer", "confidence"})
    if conf is None:
        return None
    if q["type"] == "multi":
        items = _string_list(raw["answer"], MAX_PICKS)
        return None if items is None else {"picks": [p for p in items if p in q.get("options", ())], "conf": conf}
    items = _string_list(raw["answer"], MAX_NEW)
    return None if items is None else {"paths": [p for p in items if _new_path_ok(p, facts)], "conf": conf}


def _evidence(raw, task_text: str):
    if not isinstance(raw, dict):
        return None
    conf = _confidence(raw, {"answer", "confidence", "evidence"})
    ans, ev = raw.get("answer"), raw.get("evidence")
    if conf is None or not isinstance(ans, bool) or not (ev is None or isinstance(ev, str)):
        return None
    found = ev is not None and _squash(ev) != "" and _squash(ev) in _squash(task_text)
    return {"p": conf if ans else 1 - conf, "evidence": ev, "quote_found": found}


def parse_answers_v2(result_text: str, qmap: dict, questions: dict, facts, task_text: str) -> ParsedAnswers:
    def one(q: dict, raw):
        if q.get("type") in ("multi", "paths"):
            return _listed(q, raw, facts)
        if q.get("evidence"):
            return _evidence(raw, task_text)
        return _one(q, raw)

    return _parse(result_text, qmap, questions, one)
