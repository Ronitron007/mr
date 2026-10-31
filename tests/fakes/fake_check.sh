#!/usr/bin/env bash
# Fake check.sh (mr-spec 14.2 and the 8 "Box fact" paragraph).
# Config JSON path comes from MR_FAKE_CHECK:
#   {"default": {...check.sh fields...},
#    "no_probe": "ok"|"junk"|"exit1"|{overrides},
#    "probe":    "ok"|"junk"|"exit1"|"fail"|{overrides},
#    "plain":    same as no_probe,
#    "after_probe": {overrides}  # merged into "default" after a good --probe
#   }
# --no-probe selects "no_probe", --probe selects "probe", no arg selects "plain".
# Every call appends its args to "<MR_FAKE_CHECK>.calls".
exec python3 - "$@" <<'PY'
import json
import os
import sys

path = os.environ["MR_FAKE_CHECK"]
with open(path, encoding="utf-8") as f:
    cfg = json.load(f)

args = sys.argv[1:]
with open(path + ".calls", "a", encoding="utf-8") as f:
    f.write(" ".join(args) + "\n")

if "--no-probe" in args:
    mode = cfg.get("no_probe", "ok")
elif "--probe" in args:
    mode = cfg.get("probe", "ok")
else:
    mode = cfg.get("plain", "ok")

default = cfg["default"]
rc = 0
success = False
if isinstance(mode, dict):
    print(json.dumps({**default, **mode}))
    success = True
elif mode == "ok":
    print(json.dumps(default))
    success = True
elif mode == "junk":
    print("not json")
elif mode == "exit1":
    print(json.dumps(default))
    rc = 1
elif mode == "fail":
    rc = 1
else:
    sys.stderr.write(f"fake_check: unknown mode {mode!r}\n")
    sys.exit(2)

if success and "--probe" in args and isinstance(cfg.get("after_probe"), dict):
    cfg["default"] = {**default, **cfg["after_probe"]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    os.replace(tmp, path)

sys.exit(rc)
PY
