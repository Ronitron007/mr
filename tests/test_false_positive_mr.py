import re
import unittest
from pathlib import Path

from mr.advisor import est_consult
from mr.trace.scrub import SECRET, find_secrets

ROOT = Path(__file__).resolve().parents[1]
HOME_USER = re.compile(r"/(home|Users)/[A-Za-z]")
GENERIC = "/(home|Users)/[A-Za-z]"
DOC = "§4.3 consult transcript estimate (base 45k until the EWMA has 10 samples)."


def _uses_generic_home(text):
    return re.search(
        r"re\.(?:search|compile)\([\s\S]{0,120}" + re.escape(GENERIC),
        text,
    )


class TestScrubSkFalsePositive(unittest.TestCase):
    def test_word_internal_sk_is_not_a_secret(self):
        src = (ROOT / "mr" / "trace" / "scrub.py").read_text(encoding="utf-8")
        self.assertEqual(find_secrets("mr-task-title-and-caller"), [])
        self.assertIsNone(SECRET.search("mr-task-title-and-caller"))
        self.assertEqual(find_secrets("the task-title"), [])
        self.assertIsNone(SECRET.search("the task-title"))
        self.assertIn("(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}", src)
        self.assertEqual(src.count("sk-[A-Za-z0-9_-]{8,}"), 1)
        for piece in (
            "gh[opsu]_[A-Za-z0-9]{8,}",
            "github_pat_[A-Za-z0-9_]{8,}",
            "xox[bpas]-[A-Za-z0-9-]{8,}",
            "AKIA[0-9A-Z]{16}",
            r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}",
            "-----BEGIN [A-Z ]*PRIVATE KEY-----",
            r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S{6,}",
        ):
            self.assertIn(piece, src)
        self.assertTrue(SECRET.flags & re.IGNORECASE)
        for sample in (
            "sk-abcdefgh12345678",
            " sk-abcdefgh12345678",
            "=sk-abcdefgh12345678",
            '"sk-abcdefgh12345678',
        ):
            match = SECRET.search(sample)
            self.assertIsNotNone(match, sample)
            self.assertIn("sk-abcdefgh12345678", match.group(0))
            self.assertTrue(find_secrets(sample), sample)


class TestConsultEstimateDoc(unittest.TestCase):
    def test_est_consult_docstring_uses_public_base(self):
        self.assertEqual(est_consult.__doc__, DOC)


class TestGenericHomePathChecks(unittest.TestCase):
    def test_paths_mr_uses_generic_home_regex(self):
        text = (ROOT / "tests" / "test_paths_mr.py").read_text(encoding="utf-8")
        self.assertIsNone(HOME_USER.search(text))
        self.assertIsNotNone(_uses_generic_home(text))
        self.assertIn("def test_builtin_yaml_is_public", text)
        self.assertIn("def test_existing_tests_drop_machine_pins", text)
        self.assertIn('argv[0], "claude"', text)
        self.assertIn("cursor:claude-haiku-5-5", text)

    def test_paths_callers_uses_generic_home_regex(self):
        text = (ROOT / "tests" / "test_paths_callers_mr.py").read_text(encoding="utf-8")
        self.assertIsNone(HOME_USER.search(text))
        self.assertIsNotNone(_uses_generic_home(text))
        for name in (
            "def test_skillsync_notify_false_when_hook_unset",
            "def test_cli_has_no_machine_config_constant",
            "def test_run_all_uses_portable_python",
            "def test_cli_route_log_uses_data_dir",
        ):
            self.assertIn(name, text)
        self.assertIn("DEFAULT_HOOK", text)
        self.assertIn("${PYTHON:-python3}", text)
        self.assertIn("data_dir()", text)
        self.assertIn("-m unittest discover -s tests -t .", text)
        self.assertIn("_DEFAULT_CONFIG", text)


if __name__ == "__main__":
    unittest.main()
