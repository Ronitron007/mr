"""CLI entry and argument parsing for mr."""
from __future__ import annotations

import argparse
import datetime
import importlib
import os
import re
import sys
import traceback

from mr import exitcodes as ec
from mr.config import ConfigError

_DEFERRED = frozenset({"plan", "spec", "judge", "mark"})
_TYPE = ("code", "bugfix", "refactor", "test", "ios-native", "ui", "review", "plan", "explain", "docs")
_DIFF = ("trivial", "easy", "medium", "hard", "critical")
_ROUTE = ("design", "judgment", "cascade")
_BUDGET = ("free", "low", "normal", "high")
_LAT = ("normal", "fast")
_JUDGE = ("off", "shadow", "on")
_CLASS = ("off", "shadow", "on")
_PREFER = frozenset(
    {"G-l", "G-m", "G-h", "G-x", "C25", "K-h", "K-x", "S", "S+", "O", "O+"}
)
_GUARD_RUNG = ("S", "S+", "O", "O+", "PLAN_O", "C+")
_REVIEW_FAM = ("grok", "kimi", "composer", "claude")
_REVIEW_RISK = ("low", "medium", "high")


class _UsageError(Exception):
    pass


class _MrParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _prefer(value: str) -> str:
    if value not in _PREFER:
        raise argparse.ArgumentTypeError(f"invalid --prefer value: {value!r}")
    return value


def _wait_lock(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int for --wait-lock-s: {value!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError("--wait-lock-s must be >= 0")
    return n


def _setup_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--provider", action="append", choices=("cursor", "claude"))
    p.add_argument("--cursor-agent")
    p.add_argument("--claude")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--print", action="store_true")
    p.add_argument("--check", action="store_true")


def _run_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--type", choices=_TYPE)
    p.add_argument("--difficulty", choices=_DIFF)
    p.add_argument("--route", choices=_ROUTE)
    p.add_argument("--prefer", type=_prefer)
    p.add_argument("--budget", choices=_BUDGET, default="normal")
    p.add_argument("--latency", choices=_LAT)
    p.add_argument("--judge", choices=_JUDGE)
    p.add_argument("--classifier", choices=_CLASS)
    p.add_argument("--experiment", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--wait-lock-s", type=_wait_lock, default=600)
    p.add_argument("--advisor", choices=("off", "auto", "force"), default="auto")
    p.add_argument("--advisor-consults", type=int, choices=(1, 2), default=1)
    p.add_argument("--advisor-slot", choices=("early", "final"))
    p.add_argument("--repo")


def _split_globals(argv: list[str]) -> tuple[str, bool, bool, list[str]]:
    config = None  # None = config.load() resolves MR_CONFIG, then the default path (absent default -> built-in defaults)
    json_out = verbose = False
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--config":
            if i + 1 >= len(argv):
                raise ValueError("--config requires an argument")
            config = argv[i + 1]
            i += 2
        elif a == "--json":
            json_out = True
            i += 1
        elif a == "-v":
            verbose = True
            i += 1
        else:
            rest.append(a)
            i += 1
    return config, json_out, verbose, rest


def _since(value: str) -> str:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(value)
        datetime.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid --since value {value!r}: use YYYY-MM-DD") from None
    return value


def _eval_flags(p) -> None:
    p.add_argument("--since", type=_since, help="only tasks routed on or after this IST date (YYYY-MM-DD)")
    p.add_argument("--labels", help="labels JSONL file (default: <home>/eval/labels.jsonl when present)")
    p.add_argument("--out", help="report path (default: <home>/eval/reports/<IST date>.md)")
    p.add_argument("--sweep", action="store_true", help="print the threshold sweep table")


def _droid_subparsers(p) -> None:
    sub = p.add_subparsers(dest="droid_cmd", required=True, parser_class=_MrParser)
    sub.add_parser("check", help="Binary, key, settings and state; --probe makes one tiny call"
                   ).add_argument("--probe", action="store_true")
    sub.add_parser("reset", help="Clear the sticky credits/auth state")


def build_parser() -> argparse.ArgumentParser:
    p = _MrParser(prog="mr", description="Model routing CLI")
    p.add_argument("--config", default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", action="store_true")
    sp = p.add_subparsers(dest="command", metavar="COMMAND")
    run = sp.add_parser("run", help="Route and run one task")
    run.add_argument("task", nargs="?", default=None)
    run.add_argument("-f", "--file", dest="task_file")
    _run_flags(run)
    run.add_argument("--rung")
    run.add_argument("--once", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    route = sp.add_parser("route", help="Dry run routing")
    route.add_argument("task", nargs="?", default=None)
    route.add_argument("-f", "--file", dest="task_file")
    _run_flags(route)
    route.add_argument("--explain", action="store_true")
    route.add_argument("--no-models", action="store_true")
    guard = sp.add_parser("guard", help="Claude gate decision")
    guard.add_argument("--rung", choices=_GUARD_RUNG, default="S")
    review = sp.add_parser("review", help="Cross-family review")
    review.add_argument("--base")
    review.add_argument("--repo")
    review.add_argument("--author-family", choices=_REVIEW_FAM)
    review.add_argument("--risk", choices=_REVIEW_RISK)
    review.add_argument("paths", nargs="*")
    gc = sp.add_parser("gc", help="Worktree and branch cleanup")
    gc.add_argument("--dry-run", action="store_true")
    gc.add_argument("--repo")
    unlock = sp.add_parser("unlock", help="Clear crashed writer lock")
    unlock.add_argument("--repo", required=True)
    log = sp.add_parser("log", help="Pretty-print JSONL log")
    log.add_argument("--task")
    log.add_argument("--role")
    log.add_argument("--tail", type=int)
    sp.add_parser("trace", help="Trace viewer")
    setup = sp.add_parser("setup", help="Configure provider CLIs")
    _setup_flags(setup)
    _eval_flags(sp.add_parser("eval", help="Offline shadow judge and classifier report"))
    _droid_subparsers(sp.add_parser("droid", help="BYOK droid backend for S and S+"))
    for name in sorted(_DEFERRED):
        sp.add_parser(name, help=f"{name} (deferred in v1)")
    return p


def _cmd_parser(cmd: str) -> argparse.ArgumentParser:
    p = _MrParser(prog=f"mr {cmd}")
    if cmd == "run":
        p.add_argument("task", nargs="?", default=None)
        p.add_argument("-f", "--file", dest="task_file")
        _run_flags(p)
        p.add_argument("--rung")
        p.add_argument("--once", action="store_true")
        p.add_argument("--dry-run", action="store_true")
    elif cmd == "route":
        p.add_argument("task", nargs="?", default=None)
        p.add_argument("-f", "--file", dest="task_file")
        _run_flags(p)
        p.add_argument("--explain", action="store_true")
        p.add_argument("--no-models", action="store_true")
    elif cmd == "guard":
        p.add_argument("--rung", choices=_GUARD_RUNG, default="S")
    elif cmd == "review":
        p.add_argument("--base")
        p.add_argument("--repo")
        p.add_argument("--author-family", choices=_REVIEW_FAM)
        p.add_argument("--risk", choices=_REVIEW_RISK)
        p.add_argument("paths", nargs="*")
    elif cmd == "gc":
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--repo")
    elif cmd == "unlock":
        p.add_argument("--repo", required=True)
    elif cmd == "log":
        p.add_argument("--task")
        p.add_argument("--role")
        p.add_argument("--tail", type=int)
    elif cmd == "trace":
        p.add_argument("--home", default=None)
        sub = p.add_subparsers(dest="cmd", required=True, parser_class=_MrParser)
        serve_p = sub.add_parser("serve", help="Live trace page")
        serve_p.add_argument("--port", type=int, default=8765)
        serve_p.add_argument("--host", default="127.0.0.1")
        build_p = sub.add_parser("build", help="Self-contained HTML snapshot")
        build_p.add_argument("-o", "--out", default="mr-trace.local.html")
        demo_p = sub.add_parser("demo", help="Scrubbed snapshot safe to publish")
        demo_p.add_argument("-o", "--out", default="docs/index.html")
        demo_p.add_argument("--data", default="docs/demo-traces.json")
        show_p = sub.add_parser("show", help="Plain-English story for one task")
        show_p.add_argument("task")
    elif cmd == "setup":
        _setup_flags(p)
    elif cmd == "eval":
        _eval_flags(p)
    elif cmd == "droid":
        _droid_subparsers(p)
    return p


def _parse_argv(cmd: str, argv: list[str]) -> argparse.Namespace:
    return _cmd_parser(cmd).parse_args(argv)


def main(argv: list[str] | None) -> int:
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw in (["-h"], ["--help"]) or (len(raw) == 1 and raw[0] in ("-h", "--help")):
        build_parser().print_help()
        return ec.OK
    try:
        config, json_out, verbose, rest = _split_globals(raw)
    except ValueError as e:
        print(e, file=sys.stderr)
        return ec.USAGE
    if not rest:
        print("mr: command required", file=sys.stderr)
        return ec.USAGE
    cmd, cmd_argv = rest[0], rest[1:]
    if cmd in ("-h", "--help"):
        build_parser().print_help()
        return ec.OK
    if cmd in _DEFERRED:
        print(f"mr {cmd}: not built in v1 L0", file=sys.stderr)
        return ec.USAGE
    if cmd not in ("run", "route", "guard", "review", "gc", "unlock", "log", "trace", "setup", "eval", "droid"):
        print(f"mr: unknown command {cmd!r}", file=sys.stderr)
        return ec.USAGE
    if cmd_argv in (["-h"], ["--help"]):
        _cmd_parser(cmd).print_help()
        return ec.OK
    try:
        args = _parse_argv(cmd, cmd_argv)
    except _UsageError as e:
        print(e, file=sys.stderr)
        return ec.USAGE
    args.config = config
    args.json = json_out
    args.verbose = verbose
    args.command = cmd
    try:
        mod = importlib.import_module("mr.commands." + cmd)
        return int(mod.run(args))
    except ModuleNotFoundError as e:
        if e.name == "mr.commands." + cmd:
            print(f"mr {cmd}: not built yet", file=sys.stderr)
            return ec.USAGE
        print(traceback.format_exc(), file=sys.stderr)
        return ec.INTERNAL
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return ec.USAGE
    except Exception:
        print(traceback.format_exc(), file=sys.stderr)
        return ec.INTERNAL
