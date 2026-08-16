from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request

import numpy as np


def _route_ipv4() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            return str(probe.getsockname()[0])
    except OSError:
        return ""


def local_ipv4_addresses() -> list[str]:
    candidates = {_route_ipv4()}
    try:
        candidates.update(
            item[4][0]
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        )
    except OSError:
        pass
    addresses = []
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if (
            address.version == 4
            and address.is_private
            and not address.is_loopback
            and not address.is_link_local
        ):
            addresses.append(str(address))
    return sorted(set(addresses), key=ipaddress.ip_address)


@dataclass(frozen=True, slots=True)
class Profile:
    id: int
    name: str
    label: str
    embedding: np.ndarray
    created_at: str


@dataclass(frozen=True, slots=True)
class Match:
    profile: Profile
    similarity: float


@dataclass(frozen=True, slots=True)
class Camera:
    id: int
    name: str
    stream_url: str
    username: str
    password: str
    enabled: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    created_at: str
    kind: str
    message: str
    camera_id: int | None
    camera_name: str | None
    profile_id: int | None
    profile_name: str | None
    label: str | None
    similarity: float | None


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    label TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cameras (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    stream_url TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    camera_id INTEGER,
                    camera_name TEXT,
                    profile_id INTEGER,
                    profile_name TEXT,
                    label TEXT,
                    similarity REAL
                );"""
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def add(self, name: str, label: str, embedding: np.ndarray) -> Profile:
        vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO profiles(name, label, embedding, created_at) VALUES (?, ?, ?, ?)",
                (name.strip(), label, vector.tobytes(), created_at),
            )
            profile_id = cursor.lastrowid
        return Profile(profile_id, name.strip(), label, vector, created_at)

    def all(self) -> list[Profile]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, name, label, embedding, created_at FROM profiles ORDER BY id"
            ).fetchall()
        return [
            Profile(row[0], row[1], row[2], np.frombuffer(row[3], np.float32), row[4])
            for row in rows
        ]

    def delete(self, profile_id: int) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,)).rowcount > 0

    def add_camera(
        self,
        name: str,
        stream_url: str,
        username: str = "",
        password: str = "",
        enabled: bool = True,
    ) -> Camera:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO cameras
                   (name, stream_url, username, password, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name.strip(), stream_url.strip(), username, password, int(enabled), now, now),
            )
            camera_id = cursor.lastrowid
        return self.camera(camera_id)

    def update_camera(
        self,
        camera_id: int,
        name: str,
        stream_url: str,
        username: str,
        password: str,
        enabled: bool,
    ) -> Camera | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            changed = db.execute(
                """UPDATE cameras SET name = ?, stream_url = ?, username = ?, password = ?,
                   enabled = ?, updated_at = ? WHERE id = ?""",
                (
                    name.strip(),
                    stream_url.strip(),
                    username,
                    password,
                    int(enabled),
                    now,
                    camera_id,
                ),
            ).rowcount
        return self.camera(camera_id) if changed else None

    @staticmethod
    def _camera(row) -> Camera:
        return Camera(row[0], row[1], row[2], row[3], row[4], bool(row[5]), row[6], row[7])

    def camera(self, camera_id: int) -> Camera | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT id, name, stream_url, username, password, enabled, created_at, updated_at
                   FROM cameras WHERE id = ?""",
                (camera_id,),
            ).fetchone()
        return self._camera(row) if row else None

    def cameras(self) -> list[Camera]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT id, name, stream_url, username, password, enabled, created_at, updated_at
                   FROM cameras ORDER BY id"""
            ).fetchall()
        return [self._camera(row) for row in rows]

    def delete_camera(self, camera_id: int) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM cameras WHERE id = ?", (camera_id,)).rowcount > 0

    def settings(self) -> dict:
        with self._connect() as db:
            rows = db.execute("SELECT key, value FROM settings").fetchall()
        return {key: json.loads(value) for key, value in rows}

    def update_settings(self, values: dict) -> dict:
        with self._connect() as db:
            db.executemany(
                """INSERT INTO settings(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                [(key, json.dumps(value)) for key, value in values.items()],
            )
        return self.settings()

    def delete_settings(self, *keys: str) -> None:
        if keys:
            with self._connect() as db:
                db.executemany("DELETE FROM settings WHERE key = ?", [(key,) for key in keys])

    def add_event(
        self,
        kind: str,
        message: str,
        camera: Camera | None = None,
        profile_id: int | None = None,
        profile_name: str | None = None,
        label: str | None = None,
        similarity: float | None = None,
    ) -> Event:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO events
                   (created_at, kind, message, camera_id, camera_name, profile_id,
                    profile_name, label, similarity)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    created_at,
                    kind,
                    message,
                    camera.id if camera else None,
                    camera.name if camera else None,
                    profile_id,
                    profile_name,
                    label,
                    similarity,
                ),
            )
            event_id = cursor.lastrowid
            # ponytail: retain 1000 access events; add archival when longer history matters.
            db.execute(
                "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)"
            )
            row = db.execute(
                """SELECT id, created_at, kind, message, camera_id, camera_name,
                   profile_id, profile_name, label, similarity
                   FROM events WHERE id = ?""",
                (event_id,),
            ).fetchone()
        return Event(*row)

    def events(self, limit: int = 100) -> list[Event]:
        limit = min(1000, max(1, limit))
        with self._connect() as db:
            rows = db.execute(
                """SELECT id, created_at, kind, message, camera_id, camera_name,
                   profile_id, profile_name, label, similarity
                   FROM events ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [Event(*row) for row in rows]

    def clear_events(self) -> int:
        with self._connect() as db:
            return db.execute("DELETE FROM events").rowcount


def cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    left = np.asarray(left, dtype=np.float32).reshape(-1)
    right = np.asarray(right, dtype=np.float32).reshape(-1)
    if left.shape != right.shape:
        return -1.0
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    return float(np.dot(left, right) / denominator) if denominator else -1.0


def profile_similarity(profile_embedding: np.ndarray, live_embedding: np.ndarray) -> float:
    """Compare current descriptors while retaining legacy global-only profiles."""
    profile = np.asarray(profile_embedding, dtype=np.float32).reshape(-1)
    live = np.asarray(live_embedding, dtype=np.float32).reshape(-1)
    if live.size > profile.size:
        live = live[: profile.size]
    return cosine_similarity(profile, live)


def best_match(
    profiles: list[Profile],
    label: str,
    embedding: np.ndarray,
    threshold: float,
    ambiguity_margin: float = 0.0,
) -> Match | None:
    candidates = sorted(
        (
            Match(profile, profile_similarity(profile.embedding, embedding))
            for profile in profiles
            if profile.label == label
        ),
        key=lambda item: item.similarity,
        reverse=True,
    )
    if not candidates or candidates[0].similarity < threshold:
        return None
    if (
        len(candidates) > 1
        and candidates[0].similarity - candidates[1].similarity
        < max(0.0, ambiguity_margin)
    ):
        return None
    return candidates[0]


def reid_regions(crop: np.ndarray, label: str) -> list[np.ndarray]:
    """Split a target into stable regions so appearance is more than one average color."""
    height, width = crop.shape[:2]
    if label == "person":
        return [
            crop,
            crop[: int(height * 0.62)],
            crop[int(height * 0.38) :],
            crop[
                int(height * 0.15) : int(height * 0.85),
                int(width * 0.1) : int(width * 0.9),
            ],
        ]
    return [
        crop,
        crop[:, : int(width * 0.65)],
        crop[:, int(width * 0.35) :],
        crop[
            int(height * 0.1) : int(height * 0.9),
            int(width * 0.1) : int(width * 0.9),
        ],
    ]


def reid_eligible(label: str, box: tuple[int, int, int, int]) -> bool:
    width, height = box[2] - box[0], box[3] - box[1]
    return (width >= 32 and height >= 64) if label == "person" else (
        width >= 48 and height >= 32
    )


class AccessGate:
    def __init__(self, confirmations: int, cooldown_seconds: float):
        self.confirmations = max(1, confirmations)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self._pending: dict[int, tuple[int, int]] = {}
        self._opened_tracks: set[int] = set()
        self._last_open = float("-inf")

    def observe(self, track_id: int, profile_id: int | None, now: float) -> bool:
        if profile_id is None:
            self._pending.pop(track_id, None)
            return False
        previous_profile, count = self._pending.get(track_id, (profile_id, 0))
        count = count + 1 if previous_profile == profile_id else 1
        self._pending[track_id] = (profile_id, count)
        if (
            count < self.confirmations
            or track_id in self._opened_tracks
            or now - self._last_open < self.cooldown_seconds
        ):
            return False
        self._opened_tracks.add(track_id)
        self._last_open = now
        return True

    def retain(self, active_track_ids: set[int]) -> None:
        self._pending = {
            track_id: state
            for track_id, state in self._pending.items()
            if track_id in active_track_ids
        }
        self._opened_tracks.intersection_update(active_track_ids)


def select_track(
    tracks: list[dict], x: float, y: float, width: int, height: int
) -> dict | None:
    if not (0 <= x <= 1 and 0 <= y <= 1) or width <= 0 or height <= 0:
        return None
    px, py = x * width, y * height
    hits = [
        track
        for track in tracks
        if track.get("embedding") is not None
        and track["box"][0] <= px <= track["box"][2]
        and track["box"][1] <= py <= track["box"][3]
    ]
    return min(
        hits,
        key=lambda track: (track["box"][2] - track["box"][0])
        * (track["box"][3] - track["box"][1]),
        default=None,
    )


def ewelink_request(
    host: str,
    port: int,
    device_id: str,
    device_key: str,
    channel: int,
    state: str,
    *,
    sequence: str | None = None,
    iv: bytes | None = None,
) -> Request:
    """Build an encrypted stock-firmware eWeLink LAN command."""
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError as error:
        raise ValueError("eWeLink host must be a local IP address") from error
    if not (address.is_private or address.is_loopback):
        raise ValueError("eWeLink host must be on the local network")
    if not 1 <= port <= 65535:
        raise ValueError("eWeLink port must be between 1 and 65535")
    if not re.fullmatch(r"[A-Za-z0-9]{6,32}", device_id):
        raise ValueError("eWeLink device ID must contain 6-32 letters or numbers")
    if not device_key or len(device_key) > 128:
        raise ValueError("eWeLink device key is required")
    if channel not in {1, 2, 3, 4}:
        raise ValueError("eWeLink channel must be between 1 and 4")
    if state not in {"on", "off"}:
        raise ValueError("eWeLink state must be on or off")

    from Crypto.Cipher import AES

    plaintext = json.dumps(
        {"switches": [{"switch": state, "outlet": channel - 1}]},
        separators=(",", ":"),
    ).encode()
    padding = 16 - len(plaintext) % 16
    plaintext += bytes([padding]) * padding
    iv = iv or os.urandom(16)
    if len(iv) != 16:
        raise ValueError("eWeLink IV must contain 16 bytes")
    # Stock eWeLink firmware specifies MD5 here as its AES key derivation step.
    key = hashlib.md5(device_key.encode()).digest()
    encrypted = AES.new(key, AES.MODE_CBC, iv).encrypt(plaintext)
    payload = {
        "sequence": sequence or str(time.time_ns() // 1_000_000),
        "deviceid": device_id,
        "selfApikey": "123",
        "encrypt": True,
        "iv": base64.b64encode(iv).decode(),
        "data": base64.b64encode(encrypted).decode(),
    }
    url_host = f"[{address}]" if address.version == 6 else str(address)
    return Request(
        f"http://{url_host}:{port}/zeroconf/switches",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json;charset=UTF-8"},
        method="POST",
    )


def rtsp_url_from_text(text: str) -> str:
    stream_match = re.search(
        r'(?mi)^\s*stream link\s*:\s*["\']?([^"\'\r\n]+)', text
    )
    if not stream_match:
        return ""
    stream_url = stream_match.group(1).strip()

    def field(name: str) -> str:
        match = re.search(
            rf'(?mi)^\s*{name}\s*:\s*["\']?([^"\'\r\n]+)', text
        )
        return match.group(1).strip() if match else ""

    username, password = field("username"), field("password")
    if not username or not password:
        return stream_url
    return camera_stream_url(stream_url, username, password)


def camera_stream_url(stream_url: str, username: str, password: str) -> str:
    parsed = urlsplit(stream_url)
    if parsed.scheme != "rtsp" or not parsed.hostname or not username or not password:
        return stream_url
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    credentials = f"{quote(username, safe='')}:{quote(password, safe='')}"
    return urlunsplit(
        (
            parsed.scheme,
            f"{credentials}@{host}:{parsed.port or 554}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )
