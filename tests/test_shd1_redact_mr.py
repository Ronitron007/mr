"""SHD1 locked tests: mr.redact, the redaction helper for shadow decider states (spec 9.7 rules 1-10)."""
import os
import unittest
from unittest.mock import patch


def _mod(case):
    try:
        from mr import redact
    except ImportError as exc:
        case.fail(f"mr.redact is missing: {exc}")
    return redact


# Fake secrets are split so the public-tree gate never sees a key-like literal.
X36 = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
TOKENS = (
    "sk" + "-" + "Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56Qr78St90",
    "sk" + "-or-v1-" + "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b",
    "gh" + "p_" + X36,
    "gh" + "o_" + X36,
    "gh" + "s_" + X36,
    "gh" + "u_" + X36,
    "github" + "_pat_" + "11AbCdEfGh_IjKlMnOpQrStUvWxYz012345",
    "xox" + "b-" + "Ab12-Cd34-Ef56Gh78",
    "AK" + "IA" + "IOSFODNN7EXAMPLE",
    "AI" + "za" + ("Ab3-" * 9)[:35],
    "sb" + "p_" + "a1b2c3d4e5" * 4,
    "sk" + "_live_" + "Ab12Cd34Ef56Gh78Ij90",
    "rk" + "_test_" + "Zy98Xw76Vu54Ts32Rq10",
    "pk" + "_live_" + "Mn34Op56Qr78St90Uv12",
)
EMAIL = "ops" + "@" + "example.org"
EMAIL2 = "dev" + "@" + "example.net"
GLOBS = {
    "**/.env*", "**/*.pem", "**/*.key", "**/*.p8", "**/*.p12", "**/*.mobileprovision", "**/*.keystore",
    "**/GoogleService-Info.plist", "**/google-services.json", "**/credentials*", "**/secrets*",
    "supabase/.temp/**", "eas.json",
}
SENSITIVE = (
    ".env", "app/.env.local", "certs/server.pem", "keys/deploy.key", "AuthKey_ABC.p8", "ios/dist.p12",
    "ios/App.mobileprovision", "android/app/release.keystore", "ios/App/GoogleService-Info.plist",
    "google-services.json", "credentials.json", "config/secrets.yaml", "supabase/.temp/project-ref",
    "eas.json",
)


class RedactRulesTest(unittest.TestCase):
    def test_pem_private_key_block(self):
        r = _mod(self)
        pem = ("-----BEGIN " + "RSA PRIVATE KEY-----\n" + "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX\n"
               + "-----END " + "RSA PRIVATE KEY-----")
        out, counts = r.redact("before\n" + pem + "\nafter", env={})
        self.assertEqual(out, "before\n<redacted:private_key>\nafter")
        self.assertEqual(counts.get("private_key"), 1)

    def test_jwt(self):
        r = _mod(self)
        jwt = ("eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiJ1c2VyLTEifQ" + "."
               + "c2lnbmF0dXJlLXZhbHVlLWhlcmU")
        out, counts = r.redact(f"x {jwt} y", env={})
        self.assertEqual(out, "x <redacted:jwt> y")
        self.assertEqual(counts.get("jwt"), 1)

    def test_token_prefix_families(self):
        r = _mod(self)
        for tok in TOKENS:
            out, counts = r.redact(tok, env={})
            self.assertEqual(out, "<redacted:token>", tok[:8])
            self.assertEqual(counts.get("token"), 1, tok[:8])
        out, counts = r.redact(" ".join(TOKENS), env={})
        self.assertEqual(out, " ".join(["<redacted:token>"] * len(TOKENS)))
        self.assertEqual(counts.get("token"), len(TOKENS))

    def test_assignment_keeps_key(self):
        r = _mod(self)
        out, counts = r.redact("export PASSWORD=hunter2hunter2", env={})
        self.assertEqual(out, "export PASSWORD=<redacted:assignment>")
        self.assertEqual(counts.get("assignment"), 1)
        out2, counts2 = r.redact("client_secret=Zx9Yw8Vu7Ts6", env={})  # gitleaks:allow (fake test value)
        self.assertEqual(out2, "client_secret=<redacted:assignment>")
        self.assertEqual(counts2.get("assignment"), 1)

    def test_url_userinfo(self):
        r = _mod(self)
        url = "https://" + "bob:s3cretpw" + "@" + "git.example.com/repo.git"
        out, counts = r.redact(f"clone {url} now", env={})
        self.assertNotIn("s3cretpw", out)
        self.assertIn("<redacted:url_userinfo>", out)
        self.assertTrue(out.endswith("git.example.com/repo.git now"), out)
        self.assertEqual(counts.get("url_userinfo"), 1)
        self.assertEqual(counts.get("email", 0), 0)

    def test_high_entropy_run(self):
        r = _mod(self)
        blob = "aB3dE5gH7jK9mN1pQ2rS4tU6vW8xY0zCfLiO"
        out, counts = r.redact(f"blob {blob} end", env={})
        self.assertEqual(out, "blob <redacted:high_entropy> end")
        self.assertEqual(counts.get("high_entropy"), 1)

    def test_git_sha_kept(self):
        r = _mod(self)
        sha = "0a1b2c3d4e5f6789" + "9876f5e4d3c2b1a0"
        text = f"commit {sha} and short {sha[:7]}"
        out, counts = r.redact(text, env={})
        self.assertEqual(out, text)
        self.assertEqual(sum(counts.values()), 0)

    def test_email_phone_counts(self):
        r = _mod(self)
        out, counts = r.redact(f"a {EMAIL} b {EMAIL2} c 5551234567", env={})
        self.assertEqual(out, "a <redacted:email> b <redacted:email> c <redacted:phone>")
        self.assertEqual(counts.get("email"), 2)
        self.assertEqual(counts.get("phone"), 1)
        self.assertEqual(sum(counts.values()), 3)

    def test_env_values(self):
        r = _mod(self)
        env = {"MY_API_TOKEN": "plainvalue-xyz", "HOME_DIR": "otherval-123", "SHORT_KEY": "abc12"}
        out, counts = r.redact("a plainvalue-xyz b otherval-123 c abc12", env=env)
        self.assertEqual(out, "a <redacted:env> b otherval-123 c abc12")
        self.assertEqual(counts.get("env"), 1)
        lower = {"db_password": "lowercase-pw-1"}
        self.assertEqual(r.redact("x lowercase-pw-1", env=lower)[0], "x <redacted:env>")
        with patch.dict(os.environ, {"SHD1_TEST_SECRET": "zz-envvalue-77"}):
            self.assertEqual(r.redact("x zz-envvalue-77 y")[0], "x <redacted:env> y")

    def test_ansi_stripped(self):
        r = _mod(self)
        out, _counts = r.redact("\x1b[31mred\x1b[0m text", env={})
        self.assertEqual(out, "red text")


class RedactHelpersTest(unittest.TestCase):
    def test_redact_obj_nested(self):
        r = _mod(self)
        obj = {"a": ["mail " + EMAIL, ("ok", 5)], "b": {"c": "call 5551234567"}, EMAIL: "plain",
               "n": 3, "f": None}
        out, counts = r.redact_obj(obj, env={})
        self.assertEqual(out["a"][0], "mail <redacted:email>")
        self.assertEqual(list(out["a"][1]), ["ok", 5])
        self.assertEqual(out["b"]["c"], "call <redacted:phone>")
        self.assertEqual(out[EMAIL], "plain")
        self.assertEqual((out["n"], out["f"]), (3, None))
        self.assertEqual(counts.get("email"), 1)
        self.assertEqual(counts.get("phone"), 1)
        self.assertEqual(obj["a"][0], "mail " + EMAIL)

    def test_sensitive_paths(self):
        r = _mod(self)
        self.assertEqual(set(r.SENSITIVE_GLOBS), GLOBS)
        for path in SENSITIVE:
            self.assertTrue(r.is_sensitive_path(path), path)
        self.assertFalse(r.is_sensitive_path("src/app.py"))
        self.assertFalse(r.is_sensitive_path("docs/README.md"))

    def test_too_many_threshold(self):
        r = _mod(self)
        self.assertEqual(r.MAX_REDACTIONS, 25)
        self.assertFalse(r.too_many({"email": 25}))
        self.assertTrue(r.too_many({"email": 20, "phone": 6}))
        self.assertFalse(r.too_many({}))


if __name__ == "__main__":
    unittest.main()
