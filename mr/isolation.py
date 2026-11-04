"""Isolation v1: worktree decision table, git snapshots, writer lock (spec §5.8, §10.1, §10.3)."""
from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

BUILT = {"W0", "W5", "W6", "W7"}
IST = timezone(timedelta(hours=5, minutes=30))
_WHY = {
    "W1": "parallel writers or another writer",
    "W2": "foreign dirty tree",
    "W3": "experiment",
    "W4": "high risk or dependency change",
}
_HIGH_TAGS = frozenset({"auth", "payments", "migration", "data-deletion", "deps", "native-config"})


@dataclass(frozen=True)
class IsoInputs:
    RO: bool
    OTHER_WRITER: bool
    DIRTY_FOREIGN: bool
    PARALLEL: bool
    EXPERIMENT: bool
    HIGH_RISK: bool
    DEPS: bool
    PROTECTED: bool
    OWNED_BRANCH: bool
    NEEDS_BRANCH: bool = False


def decide_isolation(i: IsoInputs, high_isolation: str = "W5") -> str:
    """First matching rule wins (plan §6.2). High risk, a dependency change or a branch-sized change runs on
    W5 while W4 is not built; ``high_isolation="W4"`` keeps the old mapping."""
    if i.RO:
        return "W0"
    if i.PARALLEL or i.OTHER_WRITER:
        return "W1"
    if i.DIRTY_FOREIGN:
        return "W2"
    if i.EXPERIMENT:
        return "W3"
    if i.HIGH_RISK or i.DEPS or i.NEEDS_BRANCH:
        return "W4" if high_isolation == "W4" else "W5"
    if i.PROTECTED:
        return "W5"
    if i.OWNED_BRANCH:
        return "W6"
    return "W7"


def unsupported_reason(code: str) -> str | None:
    if code in BUILT:
        return None
    return f"{code} ({_WHY.get(code, 'unknown')}) needs a worktree; worktrees are not built in v1"


# ---- git helpers -------------------------------------------------------------

def _git(tree: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(["git", *args], cwd=tree, env=env, capture_output=True, text=True, check=False)


def _lines(tree: str, *args: str) -> tuple[str, ...]:
    out = _git(tree, *args)
    return tuple(ln for ln in out.stdout.splitlines() if ln) if out.returncode == 0 else ()


def _branch(tree: str) -> str | None:
    out = _git(tree, "symbolic-ref", "-q", "--short", "HEAD")
    return out.stdout.strip() or None if out.returncode == 0 else None


@dataclass(frozen=True)
class GitSnap:
    head: str | None
    branch: str | None
    worktrees: tuple[str, ...]
    status: tuple[str, ...]
    refs: tuple[str, ...]
    stash: tuple[str, ...]


def snapshot(tree: str) -> GitSnap:
    head = _git(tree, "rev-parse", "HEAD")
    wts = tuple(ln[len("worktree "):] for ln in _lines(tree, "worktree", "list", "--porcelain") if ln.startswith("worktree "))
    refs = _lines(tree, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes", "refs/tags")
    return GitSnap(
        head=head.stdout.strip() if head.returncode == 0 and head.stdout.strip() else None,
        branch=_branch(tree),
        worktrees=wts,
        status=_lines(tree, "status", "--porcelain"),
        refs=tuple(sorted(refs)),
        stash=_lines(tree, "stash", "list"),
    )


def violation(before: GitSnap, after: GitSnap, *, allow_new_branch: str | None = None) -> str | None:
    """Short reason if a rung touched refs/HEAD/worktrees/stash; None otherwise. Status is not checked."""
    if before.head != after.head:
        return "head moved"
    if before.branch != after.branch and not (allow_new_branch and after.branch == allow_new_branch):
        return f"branch switched {before.branch or '(detached)'}->{after.branch or '(detached)'}"
    if before.worktrees != after.worktrees:
        return "worktree list changed"
    allowed = f"refs/heads/{allow_new_branch}" if allow_new_branch else None
    old, new = set(before.refs), set(after.refs)
    added = sorted(new - old - {allowed})
    if added:
        return "ref added: " + ", ".join(added[:3])
    removed = sorted(old - new)
    if removed:
        return "ref removed: " + ", ".join(removed[:3])
    if before.stash != after.stash:
        return "stash changed"
    return None


def protected(branch: str | None, patterns) -> bool:
    if branch is None:
        return True
    return any(fnmatch.fnmatchcase(branch, p) for p in patterns)


def compute_inputs(repo, *, ro: bool, risk: str, difficulty: str, risk_tags, experiment: bool,
                   est_files: int = 0, est_loc: int = 0, parallel: bool = False,
                   owned_branch: str | None = None, other_writer: bool, cfg, path_tags=()) -> IsoInputs:
    """Only path evidence (``path_tags``) sets HIGH_RISK or DEPS; risk, risk_tags and est_files never do."""
    tags = set(path_tags or ())
    branch = _branch(repo)
    return IsoInputs(
        RO=ro,
        OTHER_WRITER=other_writer,
        DIRTY_FOREIGN=bool(_lines(repo, "status", "--porcelain")),
        PARALLEL=parallel,
        EXPERIMENT=experiment,
        HIGH_RISK=bool(tags & _HIGH_TAGS),
        DEPS="deps" in tags,
        PROTECTED=protected(branch, cfg.worktrees["protected"]),
        OWNED_BRANCH=branch is not None and branch == owned_branch and branch.startswith("mr/"),
        NEEDS_BRANCH=difficulty == "critical" or est_loc >= 400,
    )


def branch_name(task_id: str, text: str, now: datetime | None = None) -> str:
    now = now or datetime.now(IST)
    now = now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:24].strip("-") or "task"
    return f"mr/{now:%Y%m%d-%H%M}-{slug}-{task_id[-4:]}"


def create_branch_w5(repo, task_id: str, text: str) -> tuple[str, str]:
    """`git switch -c` a new mr/* branch. Returns (original branch or detached HEAD sha, new branch)."""
    orig = _branch(repo)
    if orig is None:
        orig = _git(repo, "rev-parse", "HEAD").stdout.strip()
    name = branch_name(task_id, text)
    out = _git(repo, "switch", "-c", name)
    if out.returncode != 0:
        raise RuntimeError(f"git switch -c {name} failed: {out.stderr.strip()}")
    return orig, name


# ---- writer lock -------------------------------------------------------------

class StaleLock(Exception):
    def __init__(self, task, pid, path):
        super().__init__(f"stale writer lock {path}: task={task} pid={pid}")
        self.task, self.pid, self.path = task, pid, path


def lock_path(cfg, repo) -> str:
    digest = hashlib.sha1(os.path.realpath(repo).encode()).hexdigest()
    return os.path.join(cfg.paths.home, "state", f"tree-{digest}.lock")


_lock_path = lock_path


class WriterLock:
    def __init__(self, cfg, repo, task: str):
        self.path = _lock_path(cfg, repo)
        self.task = task
        self._fd: int | None = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        try:
            raw = os.read(fd, 4096).decode("utf-8", "replace").strip()
            if raw:
                try:
                    info = json.loads(raw)
                except ValueError:
                    info = {}
                info = info if isinstance(info, dict) else {}
                raise StaleLock(info.get("task"), info.get("pid"), self.path)
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid(), "task": self.task}).encode())
            os.fsync(fd)
        except BaseException:
            os.close(fd)  # closing drops the flock
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self) -> "WriterLock":
        self.held = self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def is_held(cfg, repo) -> bool:
    """True if another open file description holds the flock. Never creates or modifies the file."""
    try:
        fd = os.open(_lock_path(cfg, repo), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def leftover_w7(cfg, repo) -> str | None:
    """Placeholder: state-based check for a previous W7 task's uncommitted changes comes with T12."""
    return None
