"""Versioned judge question sets (spec 9.4): load them in file order and fingerprint them."""
from __future__ import annotations

import hashlib
import json
import os

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "questions")


def load(qset: str = "route.v1") -> dict:
    with open(os.path.join(_DIR, qset + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def qset_sha(questions: dict) -> str:
    raw = json.dumps(questions, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:12]
