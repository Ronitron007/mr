"""`mr gc`: minimal dry-run cleanup (mr-spec §2.1, AC-ISO-2)."""
from __future__ import annotations

import fcntl
import glob
import os
import subprocess
import sys

from mr import config, exitcodes as ec

_GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0"}


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(_GIT_ENV)
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _stale_lock_paths(home: str) -> list[str]:
    state_dir = os.path.join(home, "state")
    found: list[str] = []
    for path in sorted(glob.glob(os.path.join(state_dir, "tree-*.lock"))):
        try:
            if os.path.getsize(path) == 0:
                continue
        except OSError:
            continue
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            raw = os.read(fd, 4096).decode("utf-8", "replace").strip()
            if raw:
                found.append(path)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return found


def _merged_mr_branches(repo: str) -> list[str]:
    cur = _git(repo, "branch", "--show-current")
    current = cur.stdout.strip() if cur.returncode == 0 else None
    out = _git(repo, "branch", "--merged", "HEAD", "--list", "mr/*")
    if out.returncode != 0:
        return []
    branches: list[str] = []
    for line in out.stdout.splitlines():
        name = line.strip().lstrip("*").strip()
        if not name or not name.startswith("mr/"):
            continue
        if name == current:
            continue
        branches.append(name)
    return sorted(branches)


def _scan(cfg, repo: str | None) -> list[str]:
    home = cfg.paths.home
    items: list[str] = []
    for lock in _stale_lock_paths(home):
        items.append(f"lock {lock}")
    if repo:
        repo = os.path.abspath(repo)
        for branch in _merged_mr_branches(repo):
            items.append(f"branch {branch}")
    return items


def run(args) -> int:
    if not getattr(args, "dry_run", False):
        print("gc: only --dry-run is built in v1", file=sys.stderr)
        return ec.HUMAN

    cfg = config.load(args.config)
    lock_path = os.path.join(cfg.paths.home, "state", "gc.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        items = _scan(cfg, getattr(args, "repo", None))
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    for line in items:
        print(line)
    return ec.OK
