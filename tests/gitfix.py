"""Git fixtures for mr tests (mr-spec 14.2).

All git calls go through subprocess with a fixed author environment and the
global/system git config pointed at /dev/null, so nothing depends on the
host's ~/.gitconfig. Temp repos live under tempfile dirs chosen by the tests.
"""

import json
import os
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_CURSOR = os.path.join(_HERE, "fakes", "fake_cursor_agent.py")
FAKE_CLAUDE = os.path.join(_HERE, "fakes", "fake_claude.py")
FAKE_CHECK = os.path.join(_HERE, "fakes", "fake_check.sh")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "mr test",
    "GIT_AUTHOR_EMAIL": "mr-test@example.com",
    "GIT_COMMITTER_NAME": "mr test",
    "GIT_COMMITTER_EMAIL": "mr-test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(repo, *args, check=True):
    env = dict(os.environ)
    env.update(_GIT_ENV)
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _write(root, files):
    for rel, content in files.items():
        dest = os.path.join(root, rel)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)


def make_repo(root, files=None, branch="main"):
    """Init a repo at root with a README, a .gitignore and one commit."""
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-b", branch)
    base = {"README.md": "# test repo\n", ".gitignore": "build/\nnode_modules/\n"}
    base.update(files or {})
    _write(root, base)
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial")
    return root


def commit(repo, files, msg):
    """Write files, commit them, return the new HEAD sha."""
    _write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", msg)
    return head(repo)


def head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def add_branch(repo, name, checkout=False):
    git(repo, "branch", name)
    if checkout:
        git(repo, "checkout", name)


def make_foreign_dirty(repo, path="foreign.txt", tracked=False):
    """Add a dirty file not owned by mr: modified-tracked or untracked."""
    if tracked:
        commit(repo, {path: "original\n"}, "add foreign")
        _write(repo, {path: "modified by someone else\n"})
    else:
        _write(repo, {path: "untracked foreign\n"})
    return os.path.join(repo, path)


def add_worktree(repo, path, branch):
    git(repo, "worktree", "add", "-b", branch, path)
    return path


def add_decoys(repo, home):
    """Paths/branches that look like mr's but must be ignored.

    home is a temp dir supplied by the test - never the real ~.
    """
    add_branch(repo, "alice/wip")
    os.makedirs(os.path.join(repo, ".claude", "worktrees", "x"), exist_ok=True)
    os.makedirs(
        os.path.join(home, ".cursor", "worktrees", os.path.basename(repo.rstrip("/")), "y"),
        exist_ok=True,
    )


def write_scenario(dir, scenario):
    path = os.path.join(dir, "scenario.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(scenario, f)
    return path


def write_check(dir, cfg):
    path = os.path.join(dir, "check.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


def fake_env(scenario_path, check_path):
    env = dict(os.environ)
    env.update(
        {
            "MR_FAKE_SCENARIO": scenario_path,
            "MR_FAKE_CHECK": check_path,
            "MR_TEST_TIME_SCALE": "0.01",
            "MR_TEST_ALLOW_FAKE_PATHS": "1",
        }
    )
    return env
