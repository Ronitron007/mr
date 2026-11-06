"""Task identity, loading and fingerprint (mr-spec §5.1, §7.1)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, fields, replace

from mr.paths import data_dir

_TEXT_LIMIT = 8000
_TRAIL_PUNCT = ".,;:!?"
PIN_FIELDS = ("type", "difficulty", "route", "risk", "risk_tags", "load_bearing", "needs_review", "files")


class TaskError(Exception):
    """Bad task input. Callers map this to exit 2."""


@dataclass(frozen=True)
class Params:
    """Explicit user params. None means the caller did not set the field."""

    type: str | None = None
    difficulty: str | None = None
    route: str | None = None
    prefer: str | None = None
    budget: str = "normal"
    latency: str = "normal"
    experiment: bool = False
    judge: str | None = None
    classifier: str | None = None
    yes: bool = False
    wait_lock_s: int = 600
    pin: tuple[str, ...] = ()


@dataclass(frozen=True, init=False)
class Task:
    id: str
    text: str
    repo: str
    base: str
    params: Params
    hint_files: tuple[str, ...]
    source: str
    deprecated: tuple[str, ...]

    def __init__(self, id, text, repo, base, params, hint_files=None, source="cli", deprecated=(), *,
                 files_named=None):
        """``files_named`` is the old keyword for ``hint_files`` (kept for one release)."""
        given = hint_files if hint_files is not None else files_named
        for name, value in (("id", id), ("text", text), ("repo", repo), ("base", base), ("params", params),
                            ("hint_files", tuple(given or ())), ("source", source),
                            ("deprecated", tuple(deprecated))):
            object.__setattr__(self, name, value)

    @property
    def files_named(self) -> tuple[str, ...]:
        return self.hint_files

    @property
    def fingerprint(self) -> str:
        return fingerprint(self)


def new_task_id(text: str, ts: float, home: str) -> str:
    """Return ``t-`` plus 8 hex chars. Retry with ts+1e-6 when patches/<id> exists."""
    stamp = ts
    for _ in range(100):
        digest = hashlib.sha1((text + repr(stamp)).encode()).hexdigest()[:8]
        tid = f"t-{digest}"
        if not os.path.exists(f"{home}/patches/{tid}"):
            return tid
        stamp += 1e-6
    raise TaskError("task id collision")


def normalise(text: str) -> str:
    """Lowercase, collapse whitespace, strip, drop trailing punctuation."""
    collapsed = re.sub(r"\s+", " ", text.lower()).strip()
    return collapsed.rstrip(_TRAIL_PUNCT).strip()


def fingerprint(task: Task) -> str:
    repo_name = os.path.basename(task.repo.rstrip("/"))
    raw = normalise(task.text) + repo_name + task.base
    return hashlib.sha1(raw.encode()).hexdigest()


def _read_task_file(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskError(f"cannot read task file: {path}") from exc
    if not isinstance(data, dict):
        raise TaskError("task file must be a JSON object")
    return data


def _merge_params(cli: Params, raw: object) -> Params:
    """File values fill only fields the CLI left at None or the dataclass default."""
    if raw is None:
        return cli
    if not isinstance(raw, dict):
        raise TaskError("task file params must be an object")
    defaults = Params()
    updates: dict[str, object] = {}
    for field in fields(Params):
        if field.name not in raw:
            continue
        current = getattr(cli, field.name)
        if current is None or current == getattr(defaults, field.name):
            updates[field.name] = raw[field.name]
    if "pin" in updates:
        updates["pin"] = _pin(updates["pin"])
    return replace(cli, **updates) if updates else cli


def _pin(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or not all(isinstance(name, str) for name in raw):
        raise TaskError("task file params.pin must be a list of field names")
    bad = [name for name in raw if name not in PIN_FIELDS]
    if bad:
        raise TaskError(f"task file params.pin: unknown field {bad[0]!r} (use {', '.join(PIN_FIELDS)})")
    return tuple(raw)


def _git_out(repo: str, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", repo, *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise TaskError(f"git rev-parse failed: {repo}") from exc
    out = done.stdout.strip()
    if not out:
        raise TaskError(f"git rev-parse failed: {repo}")
    return out


def _string_list(raw: object, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise TaskError(f"{label} must be a list of strings")
    return tuple(raw)


def _hint_files(data: dict | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(hints.files, deprecated field names) from a task file. ``files_named`` is the old name for hints.files."""
    if data is None:
        return (), ()
    hints = data.get("hints")
    if hints is not None and not isinstance(hints, dict):
        raise TaskError("task file hints must be an object")
    old = data.get("files_named")
    deprecated = ("files_named",) if old is not None else ()
    listed = _string_list(old, "files_named") if old is not None else ()
    if hints is not None and hints.get("files") is not None:
        return _string_list(hints["files"], "hints.files"), deprecated
    return listed, deprecated


def load_task(
    argv_text: str | None,
    task_file: str | None,
    params: Params,
    repo: str | None,
    home: str | None = None,
) -> Task:
    data = _read_task_file(task_file) if task_file else None
    if argv_text is not None:
        text: object = argv_text
        source = "cli"
    elif data is not None:
        text = data.get("text")
        source = "file"
    else:
        text = None
        source = "cli"
    if not isinstance(text, str) or text.strip() == "":
        raise TaskError("task text missing")
    if len(text) > _TEXT_LIMIT:
        raise TaskError("task text exceeds 8000 characters")
    if data is not None:
        params = _merge_params(params, data.get("params"))
    chosen = repo or (data.get("repo") if data else None)
    if not isinstance(chosen, str) or chosen.strip() == "":
        raise TaskError("repo missing")
    top = _git_out(chosen, "rev-parse", "--show-toplevel")
    base = _git_out(top, "rev-parse", "HEAD")
    hint_files, deprecated = _hint_files(data)
    return Task(
        id=new_task_id(text, time.time(), data_dir() if home is None else home),
        text=text,
        repo=top,
        base=base,
        params=params,
        hint_files=hint_files,
        source=source,
        deprecated=deprecated,
    )


def make_task(
    text: str,
    repo: str,
    base: str,
    params: Params,
    hint_files: tuple[str, ...] | None = None,
    source: str = "cli",
    *,
    files_named: tuple[str, ...] | None = None,
) -> Task:
    """Build a Task without git. ``load_task`` is unchanged."""
    if not isinstance(text, str) or text.strip() == "":
        raise TaskError("task text missing")
    if len(text) > _TEXT_LIMIT:
        raise TaskError("task text exceeds 8000 characters")
    if not isinstance(repo, str) or repo.strip() == "":
        raise TaskError("repo missing")
    if not isinstance(base, str) or base.strip() == "":
        raise TaskError("base missing")
    if hint_files is None:
        hint_files = files_named
    return Task(
        id=new_task_id(text, time.time(), data_dir()),
        text=text,
        repo=repo,
        base=base,
        params=params,
        hint_files=tuple(hint_files or ()),
        source=source,
    )
