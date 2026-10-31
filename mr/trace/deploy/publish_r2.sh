#!/usr/bin/env bash
# Optional: build a REAL-data mr trace snapshot and upload it to your own private R2 bucket behind worker.js.
# Needs an env file outside any repo (mode 600, never committed), named by MR_TRACE_ENV, with
#   MR_TRACE_UPLOAD_URL=https://<your-worker-host>/upload
#   MR_TRACE_UPLOAD_TOKEN=<your upload token>
# Refuses to upload if the snapshot contains anything that looks like a key or token.
# Real snapshots contain your task titles: treat the view link like a password.
set -euo pipefail
ENV_FILE="${MR_TRACE_ENV:?set MR_TRACE_ENV to your upload env file}"
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
case "$MR_TRACE_UPLOAD_URL" in *.r2.dev*) echo "refusing: public r2.dev target" >&2; exit 1;; esac
out="$(mktemp --suffix=.html)"; trap 'rm -f "$out"' EXIT
mr trace build -o "$out" >/dev/null
python3 - "$out" <<'PY'
import sys
from mr.trace import scrub
hits = [m.group(0)[:12] + "…" for m in scrub.SECRET.finditer(open(sys.argv[1]).read())]
if hits:
    sys.exit(f"refusing to upload: {len(hits)} secret-looking strings, e.g. {hits[:3]}")
PY
resp="$(curl -sS --fail-with-body -X PUT -H "Authorization: Bearer ${MR_TRACE_UPLOAD_TOKEN}" \
  -H "Content-Type: text/html" --data-binary @"$out" "$MR_TRACE_UPLOAD_URL")"
echo "$(date '+%d %b %H:%M %Z') uploaded snapshot: $resp"
