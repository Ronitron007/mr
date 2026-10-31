#!/usr/bin/env python3
"""Public-tree gate: fail on machine-specific home paths, key-like strings and extra private patterns.

Usage: scripts/check_public_tree.py [ROOT]
Extra patterns (one regex per line, case-insensitive) can be supplied in a file named by
MR_PRIVATE_PATTERNS_FILE, kept outside the repo so the private names themselves are never committed.
"""
import os
import re
import subprocess
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKS = {
    "home path": re.compile(r"/(?:home|Users)/(?!alice\b|user\b|you\b|runner\b)[a-z][\w.-]*/", re.I),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "openai-style key": re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}"),
    "aws key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "cloudflare token": re.compile(r"(?i)\b(?:cf|cloudflare)[_-]?(?:api[_-]?)?token\s*[:=]\s*['\"]?[A-Za-z0-9_-]{30,}"),
}
extra = os.environ.get("MR_PRIVATE_PATTERNS_FILE")
if extra:
    with open(extra, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if line.strip() and not line.startswith("#"):
                CHECKS[f"private pattern #{i + 1}"] = re.compile(line.strip(), re.I)

SKIP = {"scripts/check_public_tree.py"}
files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
hits = 0
for rel in files:
    if rel in SKIP:
        continue
    try:
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
        continue
    for name, rx in CHECKS.items():
        for m in rx.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            print(f"{rel}:{line}: {name}")  # never print the match itself
            hits += 1
print(f"{hits} hit(s) in {len(files)} tracked files")
sys.exit(1 if hits else 0)
