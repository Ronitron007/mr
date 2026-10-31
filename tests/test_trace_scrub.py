import copy
import unittest

from mr.trace import scrub


def trace(repo, title="Fix the thing"):
    return {"task": "t-1", "repo": repo, "title": title, "type": "code", "difficulty": "easy", "dirs": ["src"],
            "commit": "abc", "caller": "x", "events": [{"kind": "guard", "t": 1, "label": "ok", "detail": {"hour": 3}}],
            "steps": [{"role": "implementer", "summary": "did it in /home/alice/x", "patch": "/home/alice/p", "raw": "/home/alice/r",
                       "session_id": "s", "pushback": {"x": 1}, "requirements": [], "task_hint": "h",
                       "tools": [{"name": "Read", "kind": "read", "hint": "/home/alice/secret.txt"}, {"name": "say", "kind": "say", "hint": "hi"}],
                       "model": "grok", "note": "token=abcdef123456"}]}  # gitleaks:allow (fake test value)


class Scrub(unittest.TestCase):
    def test_private_repo_aliased_and_title_generic(self):
        out = scrub.scrub_traces([trace("example-app"), trace("other"), trace("example-app")])
        self.assertEqual([t["repo"] for t in out], ["private-repo-1", "private-repo-2", "private-repo-1"])
        self.assertEqual(out[0]["title"], "Code task, easy, 1 attempt (demo)")

    def test_public_repo_kept_but_title_still_synthetic(self):
        out = scrub.scrub_traces([trace("scratch-repo")])
        self.assertEqual(out[0]["repo"], "scratch-repo")
        self.assertNotIn("Fix the thing", str(out))

    def test_standalone_review_ids_aliased(self):
        t = trace(None)
        t["task"] = "review-secret-feature"
        out = scrub.scrub_traces([t])[0]
        self.assertEqual(out["task"], "review-1")
        self.assertNotIn("secret-feature", str(out))

    def test_drops_and_tools(self):
        s = scrub.scrub_traces([trace("scratch-repo")])[0]
        st = s["steps"][0]
        for k in scrub.STEP_DROP:
            self.assertIsNone(st[k])
        self.assertEqual(st["tools"], [{"name": "Read", "kind": "read"}])
        self.assertNotIn("detail", s["events"][0])
        self.assertIsNone(s["dirs"])

    def test_secret_and_paths(self):
        s = scrub.scrub_traces([trace("scratch-repo")])[0]
        self.assertEqual(s["steps"][0]["note"], "[redacted]")
        self.assertEqual(scrub.find_secrets(s), [])
        self.assertEqual(scrub.find_secrets({"a": ["ghp_abcdefghijkl"]}), ["ghp_abcdefghijkl"])

    def test_input_not_mutated(self):
        t = trace("example-app")
        before = copy.deepcopy(t)
        scrub.scrub_traces([t])
        self.assertEqual(t, before)

    def test_none_safe(self):
        self.assertEqual(scrub.scrub_traces(None), [])
        self.assertEqual(len(scrub.scrub_traces([{}])), 1)


if __name__ == "__main__":
    unittest.main()
