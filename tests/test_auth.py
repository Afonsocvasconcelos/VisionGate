import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from auth import (
    AuthManager,
    TooManyAttempts,
    _read_password,
    configure_login,
    hash_password,
    verify_password,
)


class AuthenticationTests(unittest.TestCase):
    def manager(self, **overrides):
        options = {
            "username": "owner",
            "password_hash": hash_password("correct horse battery staple", n=1024),
            "session_idle_seconds": 1800,
            "session_lifetime_seconds": 28800,
            "max_attempts": 5,
            "attempt_window_seconds": 900,
        }
        options.update(overrides)
        return AuthManager(**options)

    def test_passwords_are_salted_and_verified_with_scrypt(self):
        first = hash_password("correct horse battery staple", n=1024)
        second = hash_password("correct horse battery staple", n=1024)

        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("correct horse battery staple", first))
        self.assertFalse(verify_password("wrong password", first))

    def test_login_creates_a_server_side_session_with_csrf_protection(self):
        manager = self.manager()

        login = manager.login("owner", "correct horse battery staple", "127.0.0.1")

        self.assertIsNotNone(login)
        token, csrf = login
        self.assertIsNotNone(manager.session(token))
        self.assertTrue(manager.valid_csrf(token, csrf))
        self.assertFalse(manager.valid_csrf(token, "wrong"))
        manager.logout(token)
        self.assertIsNone(manager.session(token))

    def test_repeated_failed_logins_are_rate_limited(self):
        manager = self.manager(max_attempts=2)

        self.assertIsNone(manager.login("owner", "wrong one", "192.168.1.20"))
        self.assertIsNone(manager.login("owner", "wrong two", "192.168.1.20"))
        with self.assertRaises(TooManyAttempts):
            manager.login("owner", "correct horse battery staple", "192.168.1.20")

    def test_non_ascii_username_is_rejected_without_an_error(self):
        manager = self.manager()

        self.assertIsNone(
            manager.login("öwner", "correct horse battery staple", "127.0.0.1")
        )

    def test_idle_sessions_expire(self):
        manager = self.manager(session_idle_seconds=30)
        with patch("auth.time.monotonic", return_value=10):
            token, _ = manager.login(
                "owner", "correct horse battery staple", "127.0.0.1"
            )
        with patch("auth.time.monotonic", return_value=41):
            self.assertIsNone(manager.session(token))

    def test_malformed_file_credentials_are_not_treated_as_configured(self):
        self.assertFalse(AuthManager("owner", "scrypt$broken").configured)

    def test_windows_password_prompt_shows_mask_characters(self):
        output = io.StringIO()
        with patch("auth.msvcrt") as console, redirect_stdout(output):
            console.getwch.side_effect = ["x", "\r"]
            password = _read_password("Password: ")

        self.assertEqual(password, "x")
        self.assertIn("*", output.getvalue())

    def test_file_login_accepts_a_one_character_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            with patch("builtins.input", return_value="owner"), patch(
                "auth._read_password", side_effect=["x", "x"]
            ), redirect_stdout(io.StringIO()):
                configure_login(path)

            contents = path.read_text(encoding="utf-8")
            values = dict(line.split("=", 1) for line in contents.splitlines())

        self.assertTrue(verify_password("x", values["VISIONGATE_PASSWORD_HASH"]))
        self.assertNotIn("VISIONGATE_PASSWORD=x", contents)


if __name__ == "__main__":
    unittest.main()
