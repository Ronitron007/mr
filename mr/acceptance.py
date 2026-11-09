"""Acceptance signal A: locked tests, red-on-base, vitest JSON (spec §5.9, §7.5a, §10.3)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass

_VITEST_PAT = re.compile(r"(?:.+/)?[^/]+\.mr\.spec\.ts\Z")
_HEADER = re.compile(
    r"^(?P<name>\S+) \((?P<qual>\S+)\) \.\.\.(?P<rest>.*)$"
)
_STATUS = {"ok": "passed", "FAIL": "failed", "ERROR": "failed", "skipped": "skipped"}
_MISSING_MOD = re.compile(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]")
_FAILED_IMPORT = re.compile(r"Failed to import test module: (\S+)")
_CANNOT_FIND = re.compile(
    r"""(?:Cannot find module|Failed to resolve import|Cannot find package)\s+['"]([^'"]+)['"]""",
    re.I,
)
_SETUP_STRINGS = re.compile(r"""['"]([^'"]+)['"]""")
_EXT = re.compile(r"\.(py|ts|tsx|js|mjs|cjs)$")


@dataclass(frozen=True)
class CollectionError:
    file: str | None
    kind: str  # module_not_found | other
    module: str | None
    detail: str


class NamedRun(dict):
    """``(file, test name) -> status``, plus how the test process ended: ``collection_errors``,
    ``returncode`` (None when no process ran) and ``timed_out``."""

    def __init__(self, statuses=None, collection_errors=(), returncode=None, timed_out=False):
        super().__init__(statuses or {})
        self.collection_errors = tuple(collection_errors)
        self.returncode = returncode
        self.timed_out = timed_out


@dataclass(frozen=True)
class SignalA:
    ok: bool
    passed: tuple
    failed: tuple
    missing: tuple
    lock_violations: tuple
    detail: str
    run_errors: tuple = ()


def runner_for(cfg, repo_name: str) -> str:
    block = getattr(cfg, "acceptance", None) or {}
    runners = block.get("runners") or {}
    if repo_name in runners:
        return runners[repo_name]
    return block.get("default") or "vitest"


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scripts_bytes(tree: str) -> bytes | None:
    path = os.path.join(tree, "package.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.loads(handle.read())
    except (OSError, json.JSONDecodeError):
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        scripts = {}
    return json.dumps(scripts, sort_keys=True, separators=(",", ":")).encode()


def _hash_entry(tree: str, rel: str, entry: dict) -> str | None:
    path = os.path.join(tree, rel)
    if entry.get("part") == "scripts":
        blob = _scripts_bytes(tree)
        if blob is None:
            return None if not os.path.isfile(path) else _sha_file(path)
        return _sha_bytes(blob)
    if not os.path.isfile(path):
        return None
    return _sha_file(path)


def _acceptance_paths(checklist: dict) -> list[str]:
    seen: list[str] = []
    for item in checklist.get("acceptance") or []:
        if isinstance(item, dict) and isinstance(item.get("file"), str) and item["file"] not in seen:
            seen.append(item["file"])
    return seen


def _vitest_configs(tree: str) -> list[str]:
    found = []
    try:
        names = os.listdir(tree)
    except OSError:
        return []
    for name in names:
        if name.startswith("vitest.config.") and os.path.isfile(os.path.join(tree, name)):
            found.append(name)
    return sorted(found)


def _setup_files(tree: str, config_rel: str) -> list[str]:
    path = os.path.join(tree, config_rel)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return []
    mark = re.search(r"setupFiles\b", text)
    if not mark:
        return []
    window = text[mark.end():mark.end() + 500]
    base = os.path.dirname(path)
    found: list[str] = []
    for spec in _SETUP_STRINGS.findall(window):
        if spec.startswith(("http://", "https://")):
            continue
        abs_path = spec if os.path.isabs(spec) else os.path.normpath(os.path.join(base, spec))
        if not os.path.isfile(abs_path):
            continue
        rel = os.path.relpath(abs_path, tree).replace("\\", "/")
        if rel.startswith("..") or rel in found:
            continue
        found.append(rel)
    return found


def lock(tree: str, checklist: dict, cfg, repo_name: str, *, task_id: str) -> dict:
    """Hash acceptance tests and, for vitest, test-config files. Write tests.lock."""
    from mr import state

    files: dict[str, dict] = {}
    for rel in _acceptance_paths(checklist):
        digest = _hash_entry(tree, rel, {})
        if digest is None:
            raise FileNotFoundError(rel)
        files[rel] = {"sha256": digest}
    if runner_for(cfg, repo_name) == "vitest":
        for rel in _vitest_configs(tree):
            files[rel] = {"sha256": _sha_file(os.path.join(tree, rel))}
            for setup in _setup_files(tree, rel):
                files.setdefault(setup, {"sha256": _sha_file(os.path.join(tree, setup))})
        if os.path.isfile(os.path.join(tree, "package.json")) and _scripts_bytes(tree) is not None:
            files["package.json"] = {"sha256": _hash_entry(tree, "package.json", {"part": "scripts"}), "part": "scripts"}
    doc = {"files": files}
    dest = os.path.join(state.task_dir(cfg.paths.home, task_id), "tests.lock")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")
    return doc


def verify_lock(tree: str, lockdoc: dict) -> list[str]:
    """Paths whose locked hash changed, or that are missing."""
    bad: list[str] = []
    for rel, entry in (lockdoc.get("files") or {}).items():
        if not isinstance(entry, dict):
            bad.append(rel)
            continue
        digest = _hash_entry(tree, rel, entry)
        if digest is None or digest != entry.get("sha256"):
            bad.append(rel)
    return bad


def _report_rel(tree: str, name: str) -> str:
    name = str(name).replace("\\", "/")
    if os.path.isabs(name):
        rel = os.path.relpath(name, tree)
    else:
        rel = name
    rel = rel.replace("\\", "/")
    return rel[2:] if rel.startswith("./") else rel


def _specifier_name(spec: str) -> str:
    spec = spec.split("?", 1)[0].replace("\\", "/").rstrip("/")
    base = spec.rsplit("/", 1)[-1]
    return _EXT.sub("", base)


def parse_vitest_report(report: dict, *, tree: str) -> NamedRun:
    """Parse a vitest ``--reporter=json`` document. Does not run vitest."""
    statuses: dict[tuple[str, str], str] = {}
    errors: list[CollectionError] = []
    for result in report.get("testResults") or []:
        if not isinstance(result, dict):
            continue
        rel = _report_rel(tree, str(result.get("name") or ""))
        assertions = result.get("assertionResults") or []
        if assertions:
            for item in assertions:
                if not isinstance(item, dict) or not item.get("title"):
                    continue
                statuses[(rel, str(item["title"]))] = str(item.get("status") or "failed")
            continue
        message = str(result.get("message") or "")
        found = _CANNOT_FIND.search(message)
        if found:
            spec = next(group for group in found.groups() if group)
            errors.append(CollectionError(
                rel, "module_not_found", _specifier_name(spec), message.strip()[:500] or spec,
            ))
        elif message or result.get("status") not in (None, "passed"):
            errors.append(CollectionError(rel, "other", None, message.strip()[:500] or "collection failed"))
    return NamedRun(statuses, errors)


def module_matches(module: str, paths) -> bool:
    """True when ``module`` is the import name of a path the task will create."""
    for path in paths:
        norm = str(path).replace("\\", "/").lstrip("./")
        stem = _EXT.sub("", norm)
        dotted = stem.replace("/", ".")
        base = stem.rsplit("/", 1)[-1]
        if module == dotted or module == base or dotted.endswith("." + module):
            return True
    return False


def _dotted(path: str) -> str:
    rel = path[:-3] if path.endswith(".py") else path
    return rel.replace("\\", "/").replace("/", ".")


def _file_for_module(module: str, files: list[str]) -> str | None:
    best, best_len = None, -1
    for raw in files:
        rel = str(raw).replace("\\", "/")
        if not rel.endswith(".py"):
            continue
        mod = rel[:-3].replace("/", ".")
        if module == mod or module.startswith(mod + "."):
            if len(mod) > best_len:
                best, best_len = rel, len(mod)
    if best:
        return best
    guess = module.replace(".", "/") + ".py"
    for raw in files:
        if str(raw).replace("\\", "/") == guess:
            return guess
    return files[0] if len(files) == 1 else None


def _closing_status(text: str) -> str | None:
    """A status line is exactly ok, FAIL, or ERROR, or starts with skipped."""
    stripped = text.strip()
    if stripped in ("ok", "FAIL", "ERROR"):
        return stripped
    if stripped.startswith("skipped"):
        return "skipped"
    return None


def _store_status(statuses: dict, name: str, qual: str, status: str, files: list[str]) -> None:
    rel = _file_for_module(qual, files) or _file_for_module(name, files)
    if rel is None:
        return
    statuses[(rel, name)] = _STATUS[status]


def _parse_unittest(text: str, files: list[str]) -> NamedRun:
    statuses: dict[tuple[str, str], str] = {}
    failed_modules: list[str] = []
    pending: tuple[str, str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        match = _HEADER.match(stripped)
        if match:
            pending = None
            name, qual = match.group("name"), match.group("qual")
            if "_FailedTest" in qual or "_FailedTest" in name:
                failed_modules.append(name)
                continue
            status = _closing_status(match.group("rest"))
            if status is None:
                pending = (name, qual)
            else:
                _store_status(statuses, name, qual, status, files)
            continue
        if pending is None:
            continue
        status = _closing_status(stripped)
        if status is None:
            continue
        name, qual = pending
        pending = None
        _store_status(statuses, name, qual, status, files)
    for mod in _FAILED_IMPORT.findall(text):
        if mod not in failed_modules:
            failed_modules.append(mod)
    missing = []
    for mod in _MISSING_MOD.findall(text):
        if mod not in missing:
            missing.append(mod)
    errors: list[CollectionError] = []
    seen: set[tuple] = set()
    targets = failed_modules or ([None] if missing else [])
    for mod in targets:
        rel = _file_for_module(mod, files) if mod else (files[0] if len(files) == 1 else None)
        own = [m for m in missing if m != mod]
        if own:
            for name in own:
                key = (rel, "module_not_found", name)
                if key in seen:
                    continue
                seen.add(key)
                errors.append(CollectionError(rel, "module_not_found", name, f"No module named {name!r}"))
        elif mod is not None:
            key = (rel, "other", mod)
            if key not in seen:
                seen.add(key)
                errors.append(CollectionError(rel, "other", None, f"failed to import {mod}"))
    return NamedRun(statuses, errors)


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _run_argv(argv: list[str], cwd: str, timeout_s: float, env: dict | None = None) -> tuple[int, str, bool]:
    proc = subprocess.Popen(
        argv, cwd=cwd, env=os.environ.copy() if env is None else env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    timed = False
    try:
        out_b, err_b = proc.communicate(timeout=float(timeout_s))
    except subprocess.TimeoutExpired:
        timed = True
        _killpg(proc.pid, signal.SIGTERM)
        try:
            out_b, err_b = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _killpg(proc.pid, signal.SIGKILL)
            out_b, err_b = proc.communicate()
    text = (out_b or b"").decode("utf-8", "replace") + "\n" + (err_b or b"").decode("utf-8", "replace")
    return proc.returncode if proc.returncode is not None else 1, text, timed


def run_named(tree: str, files: list[str], runner: str, timeout) -> NamedRun:
    """Run named test files. Statuses are ``(file, name) -> status``."""
    files = [str(path).replace("\\", "/") for path in files]
    if runner == "vitest":
        import tempfile
        handle = tempfile.NamedTemporaryFile(prefix="mr-vitest-", suffix=".json", delete=False)
        out_path = handle.name
        handle.close()
        try:
            argv = ["npx", "vitest", "run", *files, "--reporter=json", f"--outputFile={out_path}"]
            rc, text, timed = _run_argv(argv, tree, timeout)
            if os.path.isfile(out_path) and os.path.getsize(out_path):
                try:
                    with open(out_path, encoding="utf-8") as handle:
                        report = json.loads(handle.read())
                except (OSError, json.JSONDecodeError):
                    report = None
                if isinstance(report, dict):
                    run = parse_vitest_report(report, tree=tree)
                    return NamedRun(run, run.collection_errors, rc, timed)
            if timed:
                return NamedRun({}, (), rc, True)
            detail = text.strip()[:500] or "vitest produced no JSON report"
            return NamedRun({}, [CollectionError(files[0] if files else None, "other", None, detail)], rc)
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass
    if runner != "unittest":
        return NamedRun({}, [CollectionError(None, "other", None, f"unknown runner {runner!r}")])
    modules = [_dotted(path) for path in files]
    child_env = os.environ.copy()
    child_env["PYTHONWARNINGS"] = "ignore"
    rc, text, timed = _run_argv(
        [sys.executable, "-m", "unittest", "-v", *modules], tree, timeout, env=child_env,
    )
    run = _parse_unittest(text, files)
    return NamedRun(run, run.collection_errors, rc, timed)


def _create_paths(checklist: dict) -> list[str]:
    scope = checklist.get("scope") if isinstance(checklist, dict) else None
    create = scope.get("create") if isinstance(scope, dict) else None
    return [path for path in create or [] if isinstance(path, str)]


def _acceptable(err: CollectionError, create: list[str], test_file: str | None) -> bool:
    if err.kind != "module_not_found" or not err.module:
        return False
    paths = [path for path in create if path != test_file]
    return module_matches(err.module, paths)


def base_errors(checklist: dict, run: NamedRun) -> list[str]:
    """Empty when every named test is red on the current tree (AC-A-2)."""
    create = _create_paths(checklist)
    errors: list[str] = ["collection error: timeout"] if getattr(run, "timed_out", False) else []
    red_files: set[str] = set()
    for err in getattr(run, "collection_errors", ()):
        if _acceptable(err, create, err.file):
            if err.file:
                red_files.add(err.file)
        else:
            errors.append(f"collection error: {err.detail}")
    for item in checklist.get("acceptance") or []:
        if not isinstance(item, dict):
            continue
        rel = item.get("file")
        for name in item.get("tests") or []:
            status = run.get((rel, name))
            if status == "failed":
                continue
            if status == "passed":
                errors.append(f"{rel}::{name} passed on base")
            elif status is None and rel in red_files:
                continue
            else:
                errors.append(f"{rel}::{name} did not fail at test level ({status})")
    return errors


def run_errors(checklist: dict, run) -> tuple[str, ...]:
    """Why a final run of the locked tests is not healthy apart from its named tests: a timeout, a collection
    error, a failed test that is not named (a teardown error) in a locked file, or a nonzero exit that no
    failed status explains. Empty when the run is healthy."""
    named = {(item.get("file"), name) for item in checklist.get("acceptance") or [] if isinstance(item, dict)
             for name in item.get("tests") or []}
    locked = set(_acceptance_paths(checklist))
    errors = ["timeout"] if getattr(run, "timed_out", False) else []
    errors += [f"collection error: {err.detail}" for err in getattr(run, "collection_errors", ())]
    errors += [f"{rel}::{name}" for (rel, name), status in run.items()
               if status == "failed" and rel in locked and (rel, name) not in named]
    returncode = getattr(run, "returncode", None)
    if returncode and not errors and "failed" not in run.values():
        errors.append(f"test process exited {returncode}")
    return tuple(errors)


def signal_from_statuses(checklist: dict, statuses, lock_violations=()) -> SignalA:
    """Signal A from a run. It passes only when at least one named test ran, every named test passed and the
    run is healthy (``run_errors``)."""
    violations = tuple(lock_violations)
    passed, failed, missing = [], [], []
    if not violations:
        for item in checklist.get("acceptance") or []:
            if not isinstance(item, dict):
                continue
            rel = item.get("file")
            for name in item.get("tests") or []:
                status = statuses.get((rel, name)) if statuses is not None else None
                pair = (rel, name)
                if status == "passed":
                    passed.append(pair)
                elif status is None:
                    missing.append(pair)
                else:
                    failed.append(pair)
    bad_run = () if violations else run_errors(checklist, statuses if statuses is not None else {})
    if violations:
        detail = "locked files changed: " + ", ".join(violations)
    elif not (passed or failed or missing):
        detail = "no named acceptance tests"
    else:
        detail = f"{len(failed)} failed, {len(missing)} missing" if failed or missing else f"{len(passed)} passed"
        if bad_run:
            detail += "; unhealthy run: " + ", ".join(bad_run)
    ok = not violations and bool(passed) and not failed and not missing and not bad_run
    return SignalA(ok, tuple(passed), tuple(failed), tuple(missing), violations, detail, bad_run)


def acceptance(tree: str, checklist: dict, lockdoc: dict, *, cfg, repo_name: str) -> SignalA:
    """Signal A. A lock violation fails the signal (AC-SIG-1)."""
    violations = verify_lock(tree, lockdoc)
    if violations:
        return signal_from_statuses(checklist, {}, violations)
    files = _acceptance_paths(checklist)
    run = run_named(tree, files, runner_for(cfg, repo_name), cfg.timeouts.acceptance_s)
    return signal_from_statuses(checklist, run)
