"""Read HEAD by parsing git files. No git subprocess (AC-CLI-2)."""
from __future__ import annotations

import os
import re

from mr.task import TaskError

_SHA = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")


def read_head(repo: str) -> str:
    """Return the HEAD commit. Raises TaskError if ``repo`` is not a git checkout."""
    git_path = os.path.join(repo, ".git")
    gitdir = _gitdir(git_path, repo)
    head = _read(os.path.join(gitdir, "HEAD"), repo)
    if head.startswith("ref:"):
        return _resolve(gitdir, head.split(":", 1)[1].strip(), repo)
    if _SHA.fullmatch(head):
        return head
    raise TaskError(f"not a git repo: {repo}")


def _gitdir(path: str, repo: str) -> str:
    if os.path.isdir(path):
        return path
    if os.path.isfile(path):
        line = _read(path, repo)
        if line.startswith("gitdir:"):
            raw = line.split(":", 1)[1].strip()
            if not os.path.isabs(raw):
                raw = os.path.normpath(os.path.join(os.path.dirname(path), raw))
            if os.path.isdir(raw):
                return raw
    raise TaskError(f"not a git repo: {repo}")


def _bases(gitdir: str) -> list[str]:
    found = [gitdir]
    marker = os.path.join(gitdir, "commondir")
    if not os.path.isfile(marker):
        return found
    raw = _read(marker, gitdir).strip()
    common = raw if os.path.isabs(raw) else os.path.normpath(os.path.join(gitdir, raw))
    if os.path.isdir(common) and os.path.realpath(common) != os.path.realpath(gitdir):
        found.append(common)
    return found


def _resolve(gitdir: str, ref: str, repo: str) -> str:
    bases = _bases(gitdir)
    for base in bases:
        loose = os.path.join(base, *ref.split("/"))
        if not os.path.isfile(loose):
            continue
        val = _read(loose, repo)
        if val.startswith("ref:"):
            return _resolve(base, val.split(":", 1)[1].strip(), repo)
        if _SHA.fullmatch(val):
            return val
    for base in bases:
        packed = os.path.join(base, "packed-refs")
        if not os.path.isfile(packed):
            continue
        sha = _from_packed(packed, ref)
        if sha:
            return sha
    raise TaskError(f"not a git repo: {repo}")


def _from_packed(path: str, ref: str) -> str | None:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line[0] in "#^":
                continue
            sha, _, name = line.partition(" ")
            if name == ref and _SHA.fullmatch(sha):
                return sha
    return None


def _read(path: str, repo: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        raise TaskError(f"not a git repo: {repo}") from None
