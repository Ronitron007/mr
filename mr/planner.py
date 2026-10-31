"""Cascade planner: checklist.json, locked tests, red-on-base (spec §7.5a, §10.3).

parse_document accepts one JSON object as bare JSON, a single ```json fence,
or prose around exactly one top-level object that contains "goal". Several
objects return None unless repo_root and runner are passed, in which case the
first object that passes validate is returned.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import NamedTuple

from mr import acceptance, config, patches, runners, state

_PROMPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
_FENCE = re.compile(r"^```(?:json)?[ \t]*\n(.*)\n```[ \t]*$", re.DOTALL | re.IGNORECASE)
_KEYS = {"goal", "scope", "requirements", "acceptance", "est_loc", "files_map", "tests"}
_SCOPE_KEYS = {"modify", "create", "forbidden"}
_REQ_KEYS = {"id", "text", "examples"}
_EX_KEYS = {"given", "when", "then"}
_ACC_KEYS = {"id", "covers", "file", "tests"}
_TEST_KEYS = {"path", "content"}
_UNIT_DEF = r"(?m)^\s*(?:async\s+)?def\s+{name}\s*\("
_VITEST_TITLE = r"""(?:\bit|\btest)\s*\(\s*(['"`]){title}\1"""


class PlanResult(NamedTuple):
    ok: bool
    checklist: dict | None
    reset_tree: str | None
    errors: list[str]
    attempts: int
    raw_outputs: list[str]
    rung: str = ""  # the rung the last attempt actually ran on
    spend: tuple = ()  # (RunResult, GuardedRun) per Claude planner call, for the task cost block
    notes: tuple = ()  # why an attempt left the configured rung (Claude denied)


def _as_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value) if value else "(none)"
    return "" if value is None else str(value)


def build_prompt(task_text, files_named, repo_tree_excerpt, task_id, runner) -> str:
    """Format the vitest or unittest planner template."""
    name = "planner.v1.txt" if runner == "vitest" else "planner.unittest.v1.txt"
    if runner not in ("vitest", "unittest"):
        raise ValueError(f"unknown runner {runner!r}")
    with open(os.path.join(_PROMPTS, name), encoding="utf-8") as handle:
        template = handle.read()
    return template.format(
        task_text=_as_text(task_text),
        files_named=_as_text(files_named),
        repo_tree_excerpt=_as_text(repo_tree_excerpt),
        task_id=_as_text(task_id),
    )


def _one_goal_object(body: str):
    """Last top-level dict that contains "goal", if that match is unique."""
    decoder = json.JSONDecoder()
    found = []
    index = 0
    while index < len(body):
        start = body.find("{", index)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(body, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(obj, dict) and "goal" in obj:
            found.append(obj)
        index = end if end > start else start + 1
    if len(found) != 1:
        return None
    return found[0]


def _prepared_body(text: str) -> str | None:
    if not isinstance(text, str):
        return None
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        return fenced.group(1).strip()
    if body.startswith("```"):
        return None
    return body


def _legacy_document(body: str):
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        return _one_goal_object(body)
    return doc if isinstance(doc, dict) else None


def _top_level_dicts(body: str) -> list:
    decoder = json.JSONDecoder()
    found = []
    index = 0
    while index < len(body):
        start = body.find("{", index)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(body, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        index = end if end > start else start + 1
    return found


def _json_dicts(body: str) -> list:
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        return _top_level_dicts(body)
    return [doc] if isinstance(doc, dict) else []


def parse_document(text: str, *, repo_root: str | None = None, runner: str | None = None):
    """One checklist, or, with repo_root and runner, the first object that validates."""
    body = _prepared_body(text)
    if body is None:
        return None
    if repo_root is not None and runner is not None:
        docs = _json_dicts(body)
        if len(docs) > 1:
            for doc in docs:
                if not validate(doc, repo_root=repo_root, runner=runner):
                    return doc
            return None
    return _legacy_document(body)


def _unparsed_errors(text: str, *, repo_root: str, runner: str) -> list[str]:
    """Retry text when a reply is not one usable checklist."""
    body = _prepared_body(text)
    docs = _json_dicts(body) if body is not None else []
    if len(docs) > 1:
        messages: list[str] = []
        for doc in docs:
            messages.extend(validate(doc, repo_root=repo_root, runner=runner))
        head = f"reply had {len(docs)} JSON documents; none valid"
        if messages:
            return [f"{head}: {'; '.join(messages)}"]
        return [head]
    return ["planner output is not one JSON document"]


def _norm_rel(path) -> str | None:
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path:
        return None
    if os.path.isabs(path) or path.startswith("/"):
        return None
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts) if parts else None


def _pattern_ok(path: str, runner: str) -> bool:
    if runner == "vitest":
        return path.endswith(".mr.spec.ts") and "/.mr.spec.ts" not in "/" + path
    if runner == "unittest":
        return re.fullmatch(r"tests/test_[^/]*_mr\.py", path) is not None
    return False


def _title_in(content: str, title: str, runner: str) -> bool:
    if runner == "unittest":
        return re.search(_UNIT_DEF.format(name=re.escape(title)), content) is not None
    return re.search(_VITEST_TITLE.format(title=re.escape(title)), content) is not None


def _exact(obj, allowed: set[str], label: str, errors: list[str]) -> bool:
    if not isinstance(obj, dict):
        errors.append(f"keys: {label or 'checklist'} must be an object")
        return False
    ok = True
    prefix = f"{label}." if label else ""
    for key in sorted(set(obj) - allowed):
        errors.append(f"keys: unexpected {prefix}{key}")
        ok = False
    for key in sorted(allowed - set(obj)):
        errors.append(f"keys: missing {prefix}{key}")
        ok = False
    return ok


def _string_list(value, label: str, errors: list[str]) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append(f"keys: {label} must be a list of strings")
        return None
    return value


def validate(doc, *, repo_root: str, runner: str) -> list[str]:
    """Return checklist problems. An empty list means the document is valid."""
    errors: list[str] = []
    if runner not in ("vitest", "unittest"):
        errors.append(f"runner: {runner!r} is not vitest or unittest")
    if not isinstance(doc, dict):
        return ["keys: checklist must be an object"]
    _exact(doc, _KEYS, "", errors)
    if not isinstance(doc.get("goal"), str) or not doc.get("goal"):
        errors.append("keys: goal must be a string")
    if isinstance(doc.get("est_loc"), bool) or not isinstance(doc.get("est_loc"), int):
        errors.append("keys: est_loc must be an int")

    scope = doc.get("scope")
    modify = create = None
    if _exact(scope, _SCOPE_KEYS, "scope", errors) if isinstance(scope, dict) else errors.append("keys: scope must be an object") or False:
        modify = _string_list(scope.get("modify"), "scope.modify", errors)
        create = _string_list(scope.get("create"), "scope.create", errors)
        _string_list(scope.get("forbidden"), "scope.forbidden", errors)
    modify = modify or []
    create = create or []
    scope_set: set[str] = set()
    for label, paths, must_exist in (("modify", modify, True), ("create", create, False)):
        for path in paths:
            rel = _norm_rel(path)
            if rel is None:
                errors.append(f"path: {path} is outside the repo")
                continue
            scope_set.add(rel)
            exists = os.path.lexists(os.path.join(repo_root, rel))
            if must_exist and not exists:
                errors.append(f"modify: {path} does not exist")
            if not must_exist and exists:
                errors.append(f"create: {path} already exists")

    requirements = doc.get("requirements")
    req_ids: list[str] = []
    if not isinstance(requirements, list):
        errors.append("keys: requirements must be a list")
        requirements = []
    for index, req in enumerate(requirements, start=1):
        if not _exact(req, _REQ_KEYS, "requirements", errors):
            continue
        expect = f"R{index}"
        if req.get("id") != expect:
            errors.append(f"id: requirement id {req.get('id')!r} must be {expect}")
        elif isinstance(req.get("id"), str):
            req_ids.append(req["id"])
        if not isinstance(req.get("text"), str):
            errors.append("keys: requirements.text must be a string")
        examples = req.get("examples")
        if not isinstance(examples, list) or not examples:
            errors.append("keys: requirements.examples must be a non-empty list")
            continue
        for example in examples:
            _exact(example, _EX_KEYS, "examples", errors)

    acceptance_items = doc.get("acceptance")
    if not isinstance(acceptance_items, list):
        errors.append("keys: acceptance must be a list")
        acceptance_items = []
    covered: set[str] = set()
    acc_ids_ok = True
    for index, item in enumerate(acceptance_items, start=1):
        if not _exact(item, _ACC_KEYS, "acceptance", errors):
            acc_ids_ok = False
            continue
        expect = f"A{index}"
        if item.get("id") != expect:
            errors.append(f"id: acceptance id {item.get('id')!r} must be {expect}")
        covers = item.get("covers")
        if not isinstance(covers, list) or not all(isinstance(c, str) for c in covers):
            errors.append("keys: acceptance.covers must be a list of strings")
        else:
            covered.update(covers)
        names = item.get("tests")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            errors.append("keys: acceptance.tests must be a list of strings")

    tests = doc.get("tests")
    contents: dict[str, str] = {}
    test_paths: set[str] = set()
    if not isinstance(tests, list):
        errors.append("keys: tests must be a list")
        tests = []
    for item in tests:
        if not _exact(item, _TEST_KEYS, "tests", errors):
            continue
        rel = _norm_rel(item.get("path"))
        if rel is None:
            errors.append(f"path: {item.get('path')} is outside the repo")
            continue
        test_paths.add(rel)
        if not _pattern_ok(rel, runner):
            errors.append(f"pattern: {item.get('path')} does not match {runner}")
        if rel not in { _norm_rel(path) for path in create }:
            errors.append(f"tests path: {item.get('path')} is not in scope.create")
        if not isinstance(item.get("content"), str):
            errors.append("keys: tests.content must be a string")
        else:
            contents[rel] = item["content"]

    for req_id in req_ids:
        if req_id not in covered:
            errors.append(f"covered: {req_id} is not covered")

    files_map = doc.get("files_map")
    if not isinstance(files_map, dict):
        errors.append("keys: files_map must be an object")
        files_map = {}
    else:
        map_ids = set(files_map)
        for req_id in req_ids:
            if req_id not in map_ids:
                errors.append(f"files_map: missing {req_id}")
        for key in sorted(map_ids - set(req_ids)):
            errors.append(f"files_map: {key} is not a requirement")
        for key, paths in files_map.items():
            listed = _string_list(paths, f"files_map.{key}", errors)
            if listed is None:
                continue
            for path in listed:
                rel = _norm_rel(path)
                if rel is None:
                    errors.append(f"path: {path} is outside the repo")
                elif rel not in scope_set:
                    errors.append(f"files_map: {path} is outside scope")

    create_norm = {rel for path in create if (rel := _norm_rel(path))}
    if acc_ids_ok:
        for item in acceptance_items:
            if not isinstance(item, dict):
                continue
            raw = item.get("file")
            rel = _norm_rel(raw)
            if rel is None:
                errors.append(f"path: {raw} is outside the repo")
                continue
            if not _pattern_ok(rel, runner):
                errors.append(f"pattern: {raw} does not match {runner}")
            if rel not in test_paths:
                errors.append(f"acceptance file: {raw} is not in tests")
            if rel not in create_norm:
                errors.append(f"acceptance file: {raw} is not in scope.create")
            content = contents.get(rel)
            names = item.get("tests")
            if not isinstance(content, str) or not isinstance(names, list):
                continue
            for name in names:
                if isinstance(name, str) and not _title_in(content, name, runner):
                    errors.append(f"test name: {name} not found in {raw}")
    return errors


def _excerpt(tree: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", tree, "ls-files"], capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "(unavailable)"
    lines = done.stdout.splitlines()
    if len(lines) > 200:
        lines = lines[:200] + ["…"]
    return "\n".join(lines)


def _save_raw(home: str, task_id: str, n: int, text: str) -> None:
    dest = os.path.join(state.task_dir(home, task_id), f"planner-a{n}.txt")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as handle:
        handle.write(text)


def _write_json(path: str, doc: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")


def _inside(tree: str, rel: str) -> str:
    root = os.path.realpath(tree)
    dest = os.path.normpath(os.path.join(root, rel))
    if os.path.commonpath([root, dest]) != root:
        raise ValueError(f"refusing to write {rel}")
    return dest


def _write_tests(tree: str, doc: dict) -> list[str]:
    written = []
    for item in doc["tests"]:
        rel = _norm_rel(item["path"])
        if rel is None:
            raise ValueError(f"unsafe test path {item['path']!r}")
        dest = _inside(tree, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write(item["content"])
        written.append(rel)
    return written


def _delete_tests(tree: str, rels: list[str]) -> None:
    for rel in rels:
        try:
            os.remove(os.path.join(tree, rel))
        except FileNotFoundError:
            pass


def _with_errors(prompt: str, errors: list[str]) -> str:
    lines = "\n".join(f"- {err}" for err in errors)
    return (
        prompt
        + "\n\nThe previous checklist was invalid:\n"
        + lines
        + "\nReturn ONE corrected JSON document and nothing else.\n"
    )


def _run(code: str, prompt: str, tree: str, *, cfg, task_id: str, attempt: int):
    """One planner call on rung ``code``. Returns (RunResult | None, GuardedRun | None)."""
    rung = cfg.rungs[code]
    if rung.path == "cursor":
        if config.is_anthropic(rung.model):
            raise RuntimeError(f"cursor planner {code} is Anthropic (hard rule)")
        return runners.run_cursor(rung.model, prompt, tree, "ask", cfg.timeouts.planner_s,
                                  cfg=cfg, task=task_id, attempt=attempt), None
    guarded, res = runners.run_claude(code, prompt, tree, "plan", None, cfg.timeouts.planner_s,
                                      cfg=cfg, task=task_id, attempt=attempt)
    return res, guarded


def plan(task, *, cfg, tree: str, task_id: str, attempt: int, difficulty: str | None = None) -> PlanResult:
    """Run the planner rung for the task's difficulty. Invalid output is retried once, then ok=False."""
    repo_name = os.path.basename(os.path.abspath(tree).rstrip(os.sep))
    runner = acceptance.runner_for(cfg, repo_name)
    primary = cfg.planner.rungs[difficulty or getattr(task, "difficulty", None) or "medium"]
    base = build_prompt(task.text, task.files_named, _excerpt(tree), task_id, runner)
    raws: list[str] = []
    errors: list[str] = []
    spend: list = []
    notes: list[str] = []
    used = primary
    for n in (1, 2):
        prompt = base if n == 1 else _with_errors(base, errors)
        used = primary
        result, guarded = _run(used, prompt, tree, cfg=cfg, task_id=task_id, attempt=attempt)
        if guarded is not None and (result is None or guarded.wrapper_denied):
            if result is not None:  # wrapper deny ran; a pre-check deny (result is None) did not
                spend.append((result, guarded))
            reason = guarded.guard.reason if result is None else "wrapper refused (exit 75)"
            used = cfg.planner.claude_denied
            notes.append(f"planner {primary} denied ({reason}); attempt {n} ran on {used}")
            result, guarded = _run(used, prompt, tree, cfg=cfg, task_id=task_id, attempt=attempt)
            if guarded is not None and result is not None:
                spend.append((result, guarded))
        elif guarded is not None:
            spend.append((result, guarded))
        text = result.result_text or ""
        raws.append(text)
        _save_raw(cfg.paths.home, task_id, n, text)
        if result.timed_out:
            errors = [f"planner timed out after {cfg.timeouts.planner_s}s"]
            continue
        doc = parse_document(text, repo_root=tree, runner=runner)
        if doc is None:
            errors = _unparsed_errors(text, repo_root=tree, runner=runner)
            continue
        errors = validate(doc, repo_root=tree, runner=runner)
        if errors:
            continue
        written = _write_tests(tree, doc)
        run = acceptance.run_named(
            tree, [item["path"] for item in doc["tests"]], runner, cfg.timeouts.acceptance_s,
        )
        errors = acceptance.base_errors(doc, run)
        if errors:
            _delete_tests(tree, written)
            continue
        _write_json(os.path.join(state.task_dir(cfg.paths.home, task_id), "checklist.json"), doc)
        acceptance.lock(tree, doc, cfg, repo_name, task_id=task_id)
        return PlanResult(True, doc, patches.take_reset_tree(tree), [], n, raws,
                          used, tuple(spend), tuple(notes))
    return PlanResult(False, None, None, errors, len(raws), raws, used, tuple(spend), tuple(notes))
