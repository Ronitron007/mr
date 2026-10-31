"""`mr review`: read-only cross-family review of the working tree (mr-spec §2.1, §5.9).

Never writes to the repo: the diff uses patches' temporary index only.
"""
from __future__ import annotations

import json
import subprocess
import sys

from mr import config, patches, review
from mr import exitcodes as ec


def _git(repo: str, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout.strip()


def _filter_diff(data: bytes, paths: list[str]) -> bytes:
    """Keep per-file sections whose b-path matches a given path or dir prefix."""
    if not paths:
        return data
    kept = []
    for section in data.split(b"diff --git "):
        if not section.strip():
            continue
        header = section.split(b"\n", 1)[0].decode(errors="replace")
        bpath = header.split(" b/", 1)[-1]
        if any(bpath == p or bpath.startswith(p.rstrip("/") + "/") for p in paths):
            kept.append(b"diff --git " + section)
    return b"".join(kept)


def run(args) -> int:
    cfg = config.load(args.config)
    try:
        repo = args.repo or _git(".", "rev-parse", "--show-toplevel")
        base_tree = _git(repo, "rev-parse", f"{args.base or 'HEAD'}^{{tree}}")
        patch = _filter_diff(patches.cumulative_diff(repo, base_tree), args.paths)
    except (RuntimeError, patches.PatchError) as e:
        print(f"mr review: {e}", file=sys.stderr)
        return ec.USAGE
    if not patch.strip():
        print("nothing to review")
        return ec.OK
    fam = args.author_family or "grok"
    sig = review.review(patch.decode("utf-8", "replace"), None, None, f"{fam}-author",
                        args.risk or "medium", cfg=cfg, tree=repo,
                        task_id="review", attempt=1)
    doc = {"verdict": sig.verdict, "ok": sig.ok, "reviewer": sig.reviewer,
           "items": list(sig.items), "attempts": sig.attempts, "warning": sig.warning}
    if args.json:
        print(json.dumps(doc, ensure_ascii=False))
    else:
        line = f"review: {sig.verdict} (reviewer {sig.reviewer}, attempts {sig.attempts})"
        if sig.warning:
            line += f" - {sig.warning}"
        if sig.denied:
            line += " [claude denied; fallback reviewer]"
        print(line)
    return ec.OK if sig.ok else ec.NOT_ACCEPTED
