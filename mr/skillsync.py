"""Skills sync hand-off. Never raises, never blocks, never calls a model.

mr's agent children cannot push (runners.child_env points push URLs at file:///nonexistent), so
the skill-sync hook only marks work as pending inside them. mr's own process calls the hook once,
after _finalize, so a detached `sync.sh --record` can commit skill changes to a sync branch and
open a PR. There is no run-start call: a record that starts while agents edit could commit
half-done work. The environment is passed through unchanged (a nested mr keeps its parent's
no-push guard), except the per-repo git variables, which would point the hook's git at the
wrong repo. Off when MR_SKILLSYNC=0 (the test suite sets it) or when MR_SKILLSYNC_HOOK
is unset or does not name an executable file.
"""
import os
import subprocess

_REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
_started: list = []  # keep a reference so a still-running hook is not reported as leaked


def hook_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in _REPO_VARS}


def notify(event: str) -> bool:
    """Start the hook detached; True if it was started."""
    try:
        if os.environ.get("MR_SKILLSYNC", "1") == "0":
            return False
        hook = os.environ.get("MR_SKILLSYNC_HOOK")
        if not hook or not os.access(hook, os.X_OK):
            return False
        proc = subprocess.Popen([hook, f"mr-{event}"], env=hook_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, close_fds=True)
        _started.append(proc)
        return True
    except Exception:  # never let a hand-off break mr
        return False
