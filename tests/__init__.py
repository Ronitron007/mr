import os
import tempfile

# mr run calls skillsync.notify at the end of every run; never fire the real skills hook from tests.
os.environ["MR_SKILLSYNC"] = "0"

_HOME_VARS = ("MR_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")


def _inside_temp(path: str | None) -> bool:
    if not path:
        return False
    temp = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(path)
    return real == temp or real.startswith(temp + os.sep)


if not all(_inside_temp(os.environ.get(name)) for name in _HOME_VARS):
    _root = tempfile.mkdtemp()
    for _name in _HOME_VARS:
        os.environ[_name] = _root
