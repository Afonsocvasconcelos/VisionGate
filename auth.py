from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import os
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows launcher uses the masked path below
    msvcrt = None

from dotenv import load_dotenv


SCRYPT_N = 131_072
_USERNAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, *, n: int = SCRYPT_N) -> str:
    if not 1 <= len(password) <= 1024:
        raise ValueError("Password must be between 1 and 1024 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=8, p=1, maxmem=256 << 20, dklen=32
    )
    return f"scrypt${n}$8$1${_encode(salt)}${_encode(digest)}"


def _password_parts(encoded: str) -> tuple[int, bytes, bytes] | None:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        cost = int(n)
        decoded_salt = _decode(salt)
        decoded_expected = _decode(expected)
        if (
            algorithm != "scrypt"
            or not 2 <= cost <= 1_048_576
            or cost & (cost - 1)
            or (int(r), int(p)) != (8, 1)
            or len(decoded_salt) < 16
            or len(decoded_expected) != 32
        ):
            return None
        return cost, decoded_salt, decoded_expected
    except (ValueError, TypeError):
        return None


def verify_password(password: str, encoded: str) -> bool:
    parts = _password_parts(encoded)
    if not parts or len(password) > 1024:
        return False
    try:
        cost, salt, expected = parts
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=cost,
            r=8,
            p=1,
            maxmem=256 << 20,
            dklen=32,
        )
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class TooManyAttempts(Exception):
    def __init__(self, retry_after: int):
        super().__init__("Too many login attempts")
        self.retry_after = max(1, retry_after)


@dataclass(slots=True)
class Session:
    username: str
    csrf: str
    created: float
    last_seen: float


class AuthManager:
    def __init__(
        self,
        username: str,
        password_hash: str,
        *,
        session_idle_seconds: int = 1800,
        session_lifetime_seconds: int = 28800,
        max_attempts: int = 5,
        attempt_window_seconds: int = 900,
    ):
        self.username = username.strip()
        self.password_hash = password_hash.strip()
        self.session_idle_seconds = session_idle_seconds
        self.session_lifetime_seconds = session_lifetime_seconds
        self.max_attempts = max_attempts
        self.attempt_window_seconds = attempt_window_seconds
        # ponytail: the bundled launcher is single-worker; use a shared session store before adding workers.
        self._sessions: dict[str, Session] = {}
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> AuthManager:
        return cls(
            os.getenv("VISIONGATE_USERNAME", ""),
            os.getenv("VISIONGATE_PASSWORD_HASH", ""),
            session_idle_seconds=int(os.getenv("VISIONGATE_SESSION_IDLE_SECONDS", "1800")),
            session_lifetime_seconds=int(os.getenv("VISIONGATE_SESSION_LIFETIME_SECONDS", "28800")),
        )

    @property
    def configured(self) -> bool:
        return bool(_USERNAME.fullmatch(self.username) and _password_parts(self.password_hash))

    @staticmethod
    def _session_key(token: str) -> str:
        return hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()

    def _attempt_keys(self, valid_username: bool, client_key: str) -> tuple[str, ...]:
        keys = [f"ip:{client_key}"]
        if valid_username:
            keys.append(f"account:{self.username.casefold()}")
        return tuple(keys)

    def _check_limit(self, keys: tuple[str, ...], now: float) -> None:
        for key in keys:
            attempts = self._attempts.setdefault(key, deque())
            while attempts and attempts[0] <= now - self.attempt_window_seconds:
                attempts.popleft()
            if len(attempts) >= self.max_attempts:
                raise TooManyAttempts(int(attempts[0] + self.attempt_window_seconds - now) + 1)

    def _purge(self, now: float) -> None:
        cutoff = now - self.attempt_window_seconds
        for key, attempts in list(self._attempts.items()):
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if not attempts:
                self._attempts.pop(key, None)
        for key, session in list(self._sessions.items()):
            if (
                now - session.last_seen > self.session_idle_seconds
                or now - session.created > self.session_lifetime_seconds
            ):
                self._sessions.pop(key, None)

    def login(self, username: str, password: str, client_key: str) -> tuple[str, str] | None:
        now = time.monotonic()
        valid_username = secrets.compare_digest(
            username.encode("utf-8"), self.username.encode("utf-8")
        )
        keys = self._attempt_keys(valid_username, client_key)
        with self._lock:
            self._purge(now)
            self._check_limit(keys, now)
            for key in keys:
                self._attempts.setdefault(key, deque()).append(now)
        valid_password = verify_password(password, self.password_hash)
        with self._lock:
            if not (valid_username and valid_password):
                return None
            for key in keys:
                self._attempts.pop(key, None)
            token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            self._sessions[self._session_key(token)] = Session(
                username=self.username, csrf=csrf, created=now, last_seen=now
            )
        return token, csrf

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        now = time.monotonic()
        key = self._session_key(token)
        with self._lock:
            session = self._sessions.get(key)
            if not session:
                return None
            if (
                now - session.last_seen > self.session_idle_seconds
                or now - session.created > self.session_lifetime_seconds
            ):
                self._sessions.pop(key, None)
                return None
            session.last_seen = now
            return session

    def valid_csrf(self, token: str | None, csrf: str | None) -> bool:
        session = self.session(token)
        return bool(session and csrf and secrets.compare_digest(session.csrf, csrf))

    def logout(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(self._session_key(token), None)


def _set_env_values(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_password(prompt: str) -> str:
    if msvcrt is None:
        return getpass.getpass(f"{prompt}(typing is hidden) ")

    print(f"{prompt}(type normally; * will appear) ", end="", flush=True)
    characters: list[str] = []
    while True:
        character = msvcrt.getwch()
        if character in ("\r", "\n"):
            print()
            return "".join(characters)
        if character == "\x03":
            raise KeyboardInterrupt
        if character == "\b":
            if characters:
                characters.pop()
                print("\b \b", end="", flush=True)
            continue
        if character in ("\x00", "\xe0"):
            msvcrt.getwch()
            continue
        if character.isprintable():
            characters.append(character)
            print("*", end="", flush=True)


def configure_login(path: Path) -> None:
    current = os.getenv("VISIONGATE_USERNAME", "admin")
    while True:
        username = input(f"Login username [{current}]: ").strip() or current
        if _USERNAME.fullmatch(username):
            break
        print("Use 1-64 letters, numbers, dots, dashes, or underscores.")
    while True:
        password = _read_password("Login password: ")
        confirmation = _read_password("Confirm password: ")
        if not password:
            print("Password cannot be blank.")
        elif len(password) > 128:
            print("Password cannot be longer than 128 characters.")
        elif not secrets.compare_digest(password, confirmation):
            print("The passwords did not match.")
        else:
            break
    _set_env_values(
        path,
        {"VISIONGATE_USERNAME": username, "VISIONGATE_PASSWORD_HASH": hash_password(password)},
    )
    print("VisionGate login saved. The password itself was not stored.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure VisionGate's file-only login")
    parser.add_argument("--ensure", action="store_true", help="prompt only if login is missing")
    arguments = parser.parse_args()
    path = Path(__file__).resolve().parent / ".env"
    load_dotenv(path)
    if arguments.ensure and AuthManager.from_environment().configured:
        return 0
    print("\nVisionGate login must be configured before the web app can start.")
    configure_login(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
