"""Acceptance signal A: locked tests, red-on-base, vitest JSON (spec §5.9, §7.5a, §10.3)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
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
_RAN = re.compile(r"^Ran (\d+) tests? in \S+s$")
_VERDICT = re.compile(r"^(?:OK|FAILED|NO TESTS RAN)\b")
_FIXTURES = frozenset({"setUpModule", "tearDownModule", "setUpClass", "tearDownClass"})
_TOTALS = ("numTotalTests", "numPassedTests", "numFailedTests")


@dataclass(frozen=True)
class CollectionError:
    file: str | None
    kind: str  # module_not_found | other
    module: str | None
    detail: str


class NamedRun(dict):
    """``(file, test name) -> status``, plus how the test process ended: ``collection_errors``,
    ``returncode`` (None when no process ran), ``timed_out`` and ``problems``: ``(file or None, text)`` for a
    run error that the statuses do not show (a runner summary that is missing or does not match the statuses, a
    vitest file that failed with no failed test)."""

    def __init__(self, statuses=None, collection_errors=(), returncode=None, timed_out=False, problems=()):
        super().__init__(statuses or {})
        self.collection_errors = tuple(collection_errors)
        self.returncode = returncode
        self.timed_out = timed_out
        self.problems = tuple(problems)


@dataclass(frozen=True)
class SignalA:
    ok: bool
    passed: tuple
    failed: tuple
    missing: tuple
    lock_violations: tuple
    detail: str
    run_errors: tuple = ()  # why the run is unhealthy, as text: never a test id
    other_failed: tuple = ()  # (file, test) pairs: failed tests in a locked file that the checklist does not name


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
    counts = dict.fromkeys(_TOTALS, 0)
    failed_file = False
    problems: list[tuple] = []
    for result in report.get("testResults") or []:
        if not isinstance(result, dict):
            continue
        rel = _report_rel(tree, str(result.get("name") or ""))
        failed_file = failed_file or result.get("status") == "failed"
        assertions = result.get("assertionResults") or []
        if assertions:
            for item in assertions:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status") or "failed")
                counts["numTotalTests"] += 1
                counts["numPassedTests"] += status == "passed"
                counts["numFailedTests"] += status == "failed"
                if item.get("title"):
                    statuses[(rel, str(item["title"]))] = status
            if result.get("status") == "failed" and not any(
                    isinstance(item, dict) and item.get("status") == "failed" for item in assertions):
                why = str(result.get("message") or "").strip()[:500] or "no message"
                problems.append((rel, f"{rel}: the test file failed with no failed test ({why})"))
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
    return NamedRun(statuses, errors, problems=problems + _vitest_totals(report, counts, failed_file, statuses))


def _vitest_totals(report: dict, counts: dict, failed_file: bool, statuses: dict) -> list[tuple]:
    """The report's own totals must be there and match its test results and the parsed statuses."""
    if not all(type(report.get(key)) is int for key in _TOTALS) or not isinstance(report.get("success"), bool):
        return [(None, "vitest report has no totals (" + ", ".join(_TOTALS) + ", success)")]
    wrong = [f"{key} {report[key]} but {got} in the results" for key, got in counts.items() if report[key] != got]
    if wrong:
        return [(None, "vitest totals do not match its test results: " + ", ".join(wrong))]
    kept = sum(status == "failed" for status in statuses.values())
    if kept < counts["numFailedTests"]:
        return [(None, f"vitest has {counts['numFailedTests']} failed tests but only {kept} failed statuses "
                       "after tests with the same file and title merge")]
    if report["success"] is False and not counts["numFailedTests"] and not failed_file:
        return [(None, "vitest reported success false with no failed test or file")]
    return []


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


def _unittest_summary(text: str, results: int, rc: int) -> list[tuple]:
    """unittest's own end of run: the last ``Ran N tests`` line, then OK, FAILED or NO TESTS RAN. A run cut short
    has none. N must be the number of test results printed, and only OK goes with exit code 0."""
    lines = [line.strip() for line in text.splitlines()]
    ran = [index for index, line in enumerate(lines) if _RAN.match(line)]
    if not ran:
        return [(None, "no unittest summary: the test process ended before unittest printed 'Ran N tests'")]
    count = int(_RAN.match(lines[ran[-1]]).group(1))
    verdict = next((line for line in lines[ran[-1] + 1:] if line), "")
    if not _VERDICT.match(verdict):
        return [(None, f"no unittest summary: no OK or FAILED line after 'Ran {count} tests'")]
    problems = []
    if count != results:
        problems.append((None, f"unittest summary says {count} tests ran but the output has {results} results"))
    if verdict.startswith("OK") != (rc == 0):
        problems.append((None, f"unittest summary {verdict!r} but exit code {rc}"))
    return problems


def _parse_unittest(text: str, files: list[str], rc: int | None = None) -> NamedRun:
    """Statuses from ``unittest -v`` output. With ``rc`` the summary is checked too (``_unittest_summary``)."""
    statuses: dict[tuple[str, str], str] = {}
    failed_modules: list[str] = []
    pending: tuple[str, str] | None = None
    results = 0
    for line in text.splitlines():
        stripped = line.strip()
        match = _HEADER.match(stripped)
        if match:
            pending = None
            name, qual = match.group("name"), match.group("qual")
            results += name not in _FIXTURES
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
    return NamedRun(statuses, errors, problems=() if rc is None else _unittest_summary(text, results, rc))


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def purge_bytecode(tree: str) -> None:
    """Remove the untracked pycs under ``__pycache__`` in ``tree``, for a command that drops the environment
    (``python -E``, ``env -i``, tox) and so ignores ``PYTHONPYCACHEPREFIX``. Tracked files stay."""
    try:
        done = subprocess.run(
            ["git", "-C", tree, "ls-files", "-z", "--others", "--", ":(glob)**/__pycache__/*.pyc"],
            capture_output=True, check=True, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return
    for rel in filter(None, done.stdout.split(b"\0")):
        try:
            os.remove(os.path.join(os.fsencode(tree), rel))
        except OSError:
            pass


@contextmanager
def fresh_bytecode(tree: str, env: dict | None = None):
    """A copy of ``env`` (default: ``os.environ``) whose Python reads and writes bytecode only in a new empty
    directory, removed on exit. CPython reuses a ``__pycache__`` pyc whose source has the same size and whole-second
    mtime, so a same-second edit in the tree would run the old code. ``PYTHONDONTWRITEBYTECODE`` still reads that pyc;
    pytest's assertion rewrite also honors the prefix."""
    purge_bytecode(tree)
    prefix = tempfile.mkdtemp(prefix="mr-pycache-")
    try:
        yield {**(os.environ if env is None else env), "PYTHONPYCACHEPREFIX": prefix}
    finally:
        shutil.rmtree(prefix, ignore_errors=True)


def _run_argv(argv: list[str], cwd: str, timeout_s: float, env: dict | None = None) -> tuple[int, str, bool]:
    with fresh_bytecode(cwd, env) as run_env:
        return _run_argv_in(argv, cwd, timeout_s, run_env)


def _run_argv_in(argv: list[str], cwd: str, timeout_s: float, env: dict) -> tuple[int, str, bool]:
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
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
                    return NamedRun(run, run.collection_errors, rc, timed, run.problems)
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
    run = _parse_unittest(text, files, None if timed else rc)
    return NamedRun(run, run.collection_errors, rc, timed, run.problems)


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
    """Empty when every named test is red on the current tree (AC-A-2) and the run has no other error: no runner
    summary problem and no fixture error (such as ``tearDownModule``) in a locked file, except in a file whose
    import fails on a module the task creates."""
    create = _create_paths(checklist)
    locked = set(_acceptance_paths(checklist))
    errors: list[str] = ["collection error: timeout"] if getattr(run, "timed_out", False) else []
    red_files: set[str] = set()
    for err in getattr(run, "collection_errors", ()):
        if _acceptable(err, create, err.file):
            if err.file:
                red_files.add(err.file)
        else:
            errors.append(f"collection error: {err.detail}")
    errors += [f"run error on base: {text}" for file, text in getattr(run, "problems", ())
               if file is None or file in locked]
    errors += [f"{rel}::{name} failed on base: a locked test file must not fail outside its named tests"
               for rel, name in _other_failed(checklist, run) if name in _FIXTURES and rel not in red_files]
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


def _other_failed(checklist: dict, run) -> tuple:
    """Failed tests in a locked file that the checklist does not name, such as a ``tearDownModule`` error."""
    named = {(item.get("file"), name) for item in checklist.get("acceptance") or [] if isinstance(item, dict)
             for name in item.get("tests") or []}
    locked = set(_acceptance_paths(checklist))
    return tuple((rel, name) for (rel, name), status in run.items()
                 if status == "failed" and rel in locked and (rel, name) not in named)


def run_errors(checklist: dict, run) -> tuple[str, ...]:
    """Why a final run of the locked tests is not healthy apart from its test statuses: a timeout, a collection
    error, a runner summary that is missing or does not match (``NamedRun.problems``), or a nonzero exit that no
    failed status explains. Empty when the run is healthy. A failed test that is not named is ``_other_failed``."""
    locked = set(_acceptance_paths(checklist))
    errors = ["timeout"] if getattr(run, "timed_out", False) else []
    errors += [f"collection error: {err.detail}" for err in getattr(run, "collection_errors", ())]
    errors += [text for file, text in getattr(run, "problems", ()) if file is None or file in locked]
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
    run = statuses if statuses is not None else {}
    other = () if violations else _other_failed(checklist, run)
    bad_run = () if violations else run_errors(checklist, run)
    if violations:
        detail = "locked files changed: " + ", ".join(violations)
    elif not (passed or failed or missing):
        detail = "no named acceptance tests"
    else:
        detail = f"{len(failed)} failed, {len(missing)} missing" if failed or missing else f"{len(passed)} passed"
        if other or bad_run:
            detail += "; unhealthy run: " + ", ".join([f"{f}::{t}" for f, t in other] + list(bad_run))
    ok = not violations and bool(passed) and not failed and not missing and not other and not bad_run
    return SignalA(ok, tuple(passed), tuple(failed), tuple(missing), violations, detail, bad_run, other)


def acceptance(tree: str, checklist: dict, lockdoc: dict, *, cfg, repo_name: str) -> SignalA:
    """Signal A. A lock violation fails the signal (AC-SIG-1)."""
    violations = verify_lock(tree, lockdoc)
    if violations:
        return signal_from_statuses(checklist, {}, violations)
    files = _acceptance_paths(checklist)
    run = run_named(tree, files, runner_for(cfg, repo_name), cfg.timeouts.acceptance_s)
    return signal_from_statuses(checklist, run)
