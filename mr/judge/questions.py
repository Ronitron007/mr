"""Versioned judge question sets (spec 9.4): load them in file order and fingerprint them."""
from __future__ import annotations

import copy
import hashlib
import json
import os

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "questions")
MAX_OPTIONS = 30


def load(qset: str = "route.v1") -> dict:
    with open(os.path.join(_DIR, qset + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def expand_v2(base: dict, facts) -> dict:
    """The concrete route.v2 questions for one task: fixed ones, a tag question per text-evidence tag, the files ones."""
    templates = base["templates"]
    out = {qid: copy.deepcopy(q) for qid, q in base.items() if qid != "templates"}
    tags = dict.fromkeys(item.tag for item in facts.risk_evidence if item.source == "text")
    for tag in tags:
        q = copy.deepcopy(templates["tag"])
        q["instructions"] = q["instructions"].replace("<tag>", tag)
        out["tag." + tag] = q
    for qid in ("files.change", "files.new", "files.complete"):
        out[qid] = copy.deepcopy(templates[qid])
    out["files.change"]["options"] = [c.path for c in facts.candidates[:MAX_OPTIONS]]
    return out


def qset_sha(questions: dict) -> str:
    raw = json.dumps(questions, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:12]
