"""Public-repo names kept by the trace demo scrubber."""
import os
import unittest

from mr.trace import scrub


def _trace(repo):
    return {
        "task": "t-1",
        "repo": repo,
        "title": "Fix the thing",
        "type": "code",
        "difficulty": "easy",
    }


class PublicRepos(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("MR_TRACE_PUBLIC_REPOS")
        os.environ.pop("MR_TRACE_PUBLIC_REPOS", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MR_TRACE_PUBLIC_REPOS", None)
        else:
            os.environ["MR_TRACE_PUBLIC_REPOS"] = self._saved

    def test_public_repos_constant_only_scratch_repo(self):
        self.assertEqual(scrub.PUBLIC_REPOS, ("scratch-repo",))

    def test_public_repos_reads_env_at_call_time(self):
        os.environ["MR_TRACE_PUBLIC_REPOS"] = " alpha , , beta ,"
        fn = getattr(scrub, "public_repos", None)
        self.assertTrue(callable(fn))
        self.assertEqual(tuple(fn()), ("scratch-repo", "alpha", "beta"))
        os.environ["MR_TRACE_PUBLIC_REPOS"] = "gamma"
        self.assertEqual(tuple(fn()), ("scratch-repo", "gamma"))
        os.environ["MR_TRACE_PUBLIC_REPOS"] = ""
        self.assertEqual(tuple(fn()), ("scratch-repo",))
        os.environ.pop("MR_TRACE_PUBLIC_REPOS", None)
        self.assertEqual(tuple(fn()), ("scratch-repo",))

    def test_unset_env_keeps_only_scratch_repo(self):
        out = scrub.scrub_traces(
            [
                _trace("scratch-repo"),
                _trace("mr-trace"),
                _trace("skills-repo"),
                _trace("example-app"),
            ]
        )
        self.assertEqual(
            [t["repo"] for t in out],
            ["scratch-repo", "private-repo-1", "private-repo-2", "private-repo-3"],
        )

    def test_env_alpha_beta_keeps_those_names(self):
        first = scrub.scrub_traces([_trace("alpha")])
        os.environ["MR_TRACE_PUBLIC_REPOS"] = "alpha, beta"
        second = scrub.scrub_traces(
            [
                _trace("alpha"),
                _trace("beta"),
                _trace("scratch-repo"),
                _trace("mr-trace"),
            ]
        )
        self.assertEqual(first[0]["repo"], "private-repo-1")
        self.assertEqual(
            [t["repo"] for t in second],
            ["alpha", "beta", "scratch-repo", "private-repo-1"],
        )
        overridden = scrub.scrub_traces(
            [_trace("alpha"), _trace("kept")],
            public_repos=("kept",),
        )
        self.assertEqual([t["repo"] for t in overridden], ["private-repo-1", "kept"])

    def test_none_argument_reads_env_public_repos(self):
        os.environ["MR_TRACE_PUBLIC_REPOS"] = "alpha, beta"
        try:
            out = scrub.scrub_traces(
                [_trace("alpha"), _trace("mr-trace")],
                public_repos=None,
            )
        except TypeError:
            self.fail("scrub_traces(public_repos=None) raised TypeError")
        self.assertEqual([t["repo"] for t in out], ["alpha", "private-repo-1"])


if __name__ == "__main__":
    unittest.main()
