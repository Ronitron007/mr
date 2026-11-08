OK = 0
NOT_ACCEPTED = 1
USAGE = 2
HUMAN = 3
UNSAFE_GIT = 4
TREE_MOVED = 6
GUARD_DENY = 10
JUDGE_UNAVAILABLE = 69
INTERNAL = 70
DEFERRED = 75
SIGINT = 130
SIGTERM = 143

_PRECEDENCE = (UNSAFE_GIT, HUMAN, DEFERRED, NOT_ACCEPTED)


def precedence(codes: list[int]) -> int:
    if not codes or all(c == OK for c in codes):
        return OK
    for code in _PRECEDENCE:
        if code in codes:
            return code
    return max(codes)
