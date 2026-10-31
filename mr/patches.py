"""Patches v1: reset_tree, cumulative temp-index patches, reverse-apply reset, FINAL.md (spec §10.3, §10.2; plan §6.4, §6.5).

This is the only module that writes to the user's tree. Rules:
- all index work uses a temporary GIT_INDEX_FILE; the real index and HEAD are never touched;
- no git checkout/clean/reset/stash anywhere; the only tree write is `git apply -R` in reset();
- when in doubt, change nothing and report ok=False (caller exits 4).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass

# Fixed flags so the patch bytes never depend on the user's git config (noprefix, renames, color, ...).
_DIFF_FLAGS = ("--binary", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
               "--no-relative", "--src-prefix=a/", "--dst-prefix=b/")
_APPLY_FLAGS = ("--binary", "--whitespace=nowarn")


class PatchError(Exception):
    """A git step failed; callers treat it as unsafe (exit 4)."""


def _run(cwd: str, *args: str, index: str | None = None, stdin: bytes | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    env.pop("GIT_INDEX_FILE", None)
    if index is not None:
        env["GIT_INDEX_FILE"] = index
    out = subprocess.run(["git", *args], cwd=cwd, env=env, input=stdin, capture_output=True, check=False)
    if check and out.returncode != 0:
        raise PatchError(f"git {' '.join(args)}: {out.stderr.decode(errors='replace').strip()}")
    return out


def _top(tree: str) -> str:
    # git apply ignores paths outside cwd, so every command runs at the worktree top.
    return _run(tree, "rev-parse", "--show-toplevel").stdout.decode().strip()


class _TempIndex:
    """A throwaway index file in the system temp dir, removed on exit."""

    def __enter__(self) -> str:
        self.dir = tempfile.mkdtemp(prefix="mr-index-")
        return os.path.join(self.dir, "index")

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _stage_all(top: str, index: str) -> None:
    # Seed from HEAD so `add -A` only hashes changed files; an unborn HEAD starts empty.
    if _run(top, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0:
        _run(top, "read-tree", "HEAD", index=index)
    _run(top, "add", "-A", index=index)


def take_reset_tree(tree: str) -> str:
    top = _top(tree)
    with _TempIndex() as idx:
        _stage_all(top, idx)
        return _run(top, "write-tree", index=idx).stdout.decode().strip()


def cumulative_diff(tree: str, reset_tree: str) -> bytes:
    top = _top(tree)
    with _TempIndex() as idx:
        _stage_all(top, idx)
        return _run(top, "diff", *_DIFF_FLAGS, "--cached", reset_tree, index=idx).stdout


def _safe_name(part: str) -> str:
    s = str(part)
    if not s or "/" in s or "\\" in s or s in (".", "..") or "\0" in s:
        raise ValueError(f"unsafe path component: {s!r}")
    return s


def _task_dir(home: str, task_id: str) -> str:
    return os.path.join(home, "patches", _safe_name(task_id))


def _write_atomic(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def save_patch(home: str, task_id: str, n: int, model: str, data: bytes) -> str:
    path = os.path.join(_task_dir(home, task_id), f"r{int(n)}-{_safe_name(model)}.patch")
    return _write_atomic(path, data)


@dataclass(frozen=True)
class ResetResult:
    ok: bool
    reason: str
    saved: tuple[str, ...]


def _read(path: str | None) -> bytes:
    if not path:
        return b""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def reset(tree: str, reset_tree: str, last_patch_path: str | None, home: str, task_id: str) -> ResetResult:
    """Restore reset_tree by reverse-applying the last saved patch (v1: W5/W6/W7 alike)."""
    try:
        top = _top(tree)
        current = cumulative_diff(top, reset_tree)
        if not current:
            return ResetResult(True, "nothing to reset", ())
        last = _read(last_patch_path)
        if current != last:
            where = os.path.dirname(last_patch_path) if last_patch_path else _task_dir(home, task_id)
            human = _write_atomic(os.path.join(where, f"r-human-{int(time.time())}.patch"), current)
            saved = (human,) + ((last_patch_path,) if last else ())
            return ResetResult(False, "tree changed since last saved patch (human edit?)", saved)
        patch = os.path.abspath(last_patch_path)
        chk = _run(top, "apply", "-R", "--check", *_APPLY_FLAGS, patch, check=False)
        if chk.returncode != 0:
            return ResetResult(False, "reverse-apply check failed: "
                               + chk.stderr.decode(errors="replace").strip(), (patch,))
        app = _run(top, "apply", "-R", *_APPLY_FLAGS, patch, check=False)
        if app.returncode != 0:
            return ResetResult(False, "reverse-apply failed: "
                               + app.stderr.decode(errors="replace").strip(), (patch,))
        if cumulative_diff(top, reset_tree) != b"":
            return ResetResult(False, "reset incomplete", (patch,))
        return ResetResult(True, "reset to reset_tree", (patch,))
    except (PatchError, OSError, ValueError) as e:
        return ResetResult(False, f"reset aborted: {e}", ())


def apply_to_tree(repo: str, reset_tree: str, patch_path: str) -> str:
    """Tree sha of reset_tree + patch, built in a temp index (no worktree change)."""
    top = _top(repo)
    with _TempIndex() as idx:
        _run(top, "read-tree", reset_tree, index=idx)
        if os.path.getsize(patch_path) > 0:
            _run(top, "apply", "--cached", *_APPLY_FLAGS, os.path.abspath(patch_path), index=idx)
        return _run(top, "write-tree", index=idx).stdout.decode().strip()


def numstat(data: bytes) -> dict:
    res = {"files": 0, "add": 0, "del": 0}
    if not data.strip():
        return res
    with tempfile.TemporaryDirectory(prefix="mr-numstat-") as d:
        out = _run(d, "apply", "--numstat", "-z", "-", stdin=data).stdout
    for rec in out.split(b"\0"):
        parts = rec.split(b"\t", 2)
        if len(parts) < 3:
            continue
        res["files"] += 1
        if parts[0] != b"-":
            res["add"] += int(parts[0])
        if parts[1] != b"-":
            res["del"] += int(parts[1])
    return res


def _cell(v) -> str:
    if v is None or v == "":
        return "-"
    if isinstance(v, dict):
        v = ", ".join(f"{k}={x}" for k, x in v.items())
    elif isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v)
    return str(v).replace("|", "\\|").replace("\n", " ")


def write_final_md(home: str, task_id: str, *, outcome: str, exit_code: int, reason: str,
                   rungs: list[dict], patch: str | None, reset_tree: str | None, base: str) -> str:
    lines = [f"# mr task {task_id}: {outcome}", "",
             f"- **Outcome:** {outcome} (exit {exit_code})",
             f"- **Why it stopped:** {reason}",
             f"- **Base:** `{base}`",
             f"- **reset_tree:** `{reset_tree or '-'}`",
             f"- **Final patch:** `{patch or '-'}`", "",
             "## Rungs", "",
             "| rung | model | class | signals | patch |",
             "|---|---|---|---|---|"]
    for r in rungs:
        lines.append("| " + " | ".join(_cell(r.get(k)) for k in ("rung", "model", "class", "signals", "patch")) + " |")
    if not rungs:
        lines.append("| - | - | - | - | - |")
    lines += ["", "## How to apply", ""]
    if patch:
        lines += [f"On a tree whose content is `reset_tree` (`{reset_tree or '-'}`):", "",
                  "```sh", f"git apply --check {patch}", f"git apply {patch}", "```"]
    else:
        lines.append("No patch was produced.")
    path = os.path.join(_task_dir(home, task_id), "FINAL.md")
    return _write_atomic(path, ("\n".join(lines) + "\n").encode())
