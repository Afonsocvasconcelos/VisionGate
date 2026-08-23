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
class ProfileSample:
    id: int
    profile_id: int
    label: str
    embedding: np.ndarray
    thumbnail: bytes | None
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


@dataclass(frozen=True, slots=True)
class EWeLinkDevice:
    device_id: str
    name: str
    model: str
    device_key: str
    uiid: int | None
    host: str
    port: int
    params: dict
    capabilities: list[dict]
    online: bool | None
    available: bool
    created_at: str
    updated_at: str
    last_seen: str | None
    last_sync: str


@dataclass(frozen=True, slots=True)
class Automation:
    id: int
    name: str
    enabled: bool
    revision: int
    graph: dict
    next_run_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AutomationRun:
    id: int
    automation_id: int | None
    revision: int
    trigger: dict
    status: str
    started_at: str
    finished_at: str | None
    result: dict


class Database:
    SCHEMA_VERSION = 4

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        legacy_migration = self._backup_legacy_database()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            statements = (
                """CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    label TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    created_at TEXT NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS cameras (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    stream_url TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS events (
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
                )""",
                """CREATE TABLE IF NOT EXISTS profile_samples (
                    id INTEGER PRIMARY KEY,
                    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    thumbnail BLOB,
                    created_at TEXT NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS ewelink_devices (
                    device_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    device_key TEXT NOT NULL,
                    uiid INTEGER,
                    host TEXT NOT NULL DEFAULT '',
                    port INTEGER NOT NULL DEFAULT 8081,
                    params TEXT NOT NULL DEFAULT '{}',
                    capabilities TEXT NOT NULL DEFAULT '[]',
                    online INTEGER,
                    available INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen TEXT,
                    last_sync TEXT NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS automations (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    graph TEXT NOT NULL,
                    next_run_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS automation_runs (
                    id INTEGER PRIMARY KEY,
                    automation_id INTEGER REFERENCES automations(id) ON DELETE SET NULL,
                    revision INTEGER NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    result TEXT NOT NULL DEFAULT '{}'
                )""",
            )
            for statement in statements:
                db.execute(statement)
            db.execute(
                "CREATE INDEX IF NOT EXISTS profile_samples_profile ON profile_samples(profile_id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS automation_runs_automation ON automation_runs(automation_id, id DESC)"
            )
            db.execute(
                """INSERT INTO profile_samples(profile_id, label, embedding, thumbnail, created_at)
                   SELECT p.id, p.label, p.embedding, NULL, p.created_at
                   FROM profiles p
                   WHERE NOT EXISTS (
                       SELECT 1 FROM profile_samples s WHERE s.profile_id = p.id
                   )"""
            )
            if legacy_migration:
                self._migrate_legacy_configuration(db)
            self._upgrade_automations(db)
            db.execute(
                """INSERT INTO settings(key, value) VALUES ('schema_version', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (json.dumps(self.SCHEMA_VERSION),),
            )

    def _backup_legacy_database(self) -> bool:
        if not self.path.exists() or not self.path.stat().st_size:
            return False
        source = sqlite3.connect(self.path, timeout=5)
        try:
            tables = {
                row[0]
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "profiles" not in tables or "profile_samples" in tables:
                return False
            backup_path = self.path.with_name(self.path.name + ".pre-automation.bak")
            created = not backup_path.exists()
            target = sqlite3.connect(backup_path, timeout=5)
            try:
                if created:
                    source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("The pre-automation database backup failed verification")
            finally:
                target.close()
            return True
        finally:
            source.close()

    @staticmethod
    def _migrate_legacy_configuration(db: sqlite3.Connection) -> None:
        from automation import default_device_graph, validate_graph

        settings = {
            key: json.loads(value)
            for key, value in db.execute("SELECT key, value FROM settings")
        }
        now = datetime.now(timezone.utc).isoformat()
        device_id = str(settings.get("ewelink_device_id") or "").strip()
        device_key = str(settings.get("ewelink_device_key") or "").strip()
        if device_id and device_key:
            channels = sorted(
                {
                    int(settings.get("ewelink_open_channel", 1)),
                    int(settings.get("ewelink_close_channel", 2)),
                }
            )
            capabilities = [
                {
                    "id": "switches",
                    "type": "channels",
                    "channels": channels,
                    "writable": True,
                }
            ]
            db.execute(
                """INSERT INTO ewelink_devices
                   (device_id, name, model, device_key, uiid, host, port, params,
                    capabilities, online, available, created_at, updated_at, last_seen,
                    last_sync)
                   VALUES (?, ?, ?, ?, NULL, ?, ?, '{}', ?, NULL, 1,
                           ?, ?, NULL, ?)""",
                (
                    device_id,
                    str(settings.get("ewelink_model") or "eWeLink device"),
                    str(settings.get("ewelink_model") or "SONOFF device"),
                    device_key,
                    str(settings.get("ewelink_host") or ""),
                    int(settings.get("ewelink_port") or 8081),
                    json.dumps(capabilities, separators=(",", ":")),
                    now,
                    now,
                    now,
                ),
            )

        if device_id and not db.execute("SELECT 1 FROM automations LIMIT 1").fetchone():
            graph = validate_graph(
                default_device_graph(
                    device_id,
                    float(settings.get("auto_close_seconds", 5)),
                    int(settings.get("ewelink_open_channel", 1)),
                    int(settings.get("ewelink_close_channel", 2)),
                    float(settings.get("pulse_seconds", 1)),
                )
            )
            db.execute(
                """INSERT INTO automations
                   (name, enabled, revision, graph, next_run_at, created_at, updated_at)
                   VALUES (?, 1, 1, ?, NULL, ?, ?)""",
                (graph["name"], json.dumps(graph, separators=(",", ":")), now, now),
            )

    @staticmethod
    def _upgrade_automations(db: sqlite3.Connection) -> None:
        from automation import upgrade_automation_graph, validate_graph

        settings = {
            key: json.loads(value)
            for key, value in db.execute("SELECT key, value FROM settings")
        }
        rows = db.execute("SELECT id, enabled, revision, graph FROM automations").fetchall()
        for automation_id, enabled, revision, raw_graph in rows:
            graph, changed = upgrade_automation_graph(json.loads(raw_graph), settings)
            condition = next((node for node in graph.get("nodes", []) if node.get("id") == "still-away"), None)
            if condition and condition.get("config", {}).get("camera_id") == "event":
                condition["config"]["camera_id"] = "*"
                changed = True
            if not changed:
                continue
            graph["revision"] = revision + 1
            graph["enabled"] = bool(enabled)
            graph = validate_graph(graph)
            db.execute(
                "UPDATE automations SET name = ?, revision = ?, graph = ?, updated_at = ? WHERE id = ?",
                (
                    graph["name"],
                    revision + 1,
                    json.dumps(graph, separators=(",", ":")),
                    datetime.now(timezone.utc).isoformat(),
                    automation_id,
                ),
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
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
            db.execute(
                """INSERT INTO profile_samples
                   (profile_id, label, embedding, thumbnail, created_at)
                   VALUES (?, ?, ?, NULL, ?)""",
                (profile_id, label, vector.tobytes(), created_at),
            )
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

    @staticmethod
    def _sample(row) -> ProfileSample:
        return ProfileSample(
            row[0],
            row[1],
            row[2],
            np.frombuffer(row[3], np.float32),
            row[4],
            row[5],
        )

    def profile_samples(self, profile_id: int | None = None) -> list[ProfileSample]:
        query = """SELECT id, profile_id, label, embedding, thumbnail, created_at
                   FROM profile_samples"""
        values: tuple = ()
        if profile_id is not None:
            query += " WHERE profile_id = ?"
            values = (profile_id,)
        query += " ORDER BY id"
        with self._connect() as db:
            rows = db.execute(query, values).fetchall()
        return [self._sample(row) for row in rows]

    def profile_sample_counts(self) -> dict[int, int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT profile_id, COUNT(*) FROM profile_samples GROUP BY profile_id"
            ).fetchall()
        return {int(profile_id): int(count) for profile_id, count in rows}

    def matching_profiles(self) -> list[Profile]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT p.id, p.name, s.label, s.embedding, p.created_at
                   FROM profiles p JOIN profile_samples s ON s.profile_id = p.id
                   ORDER BY p.id, s.id"""
            ).fetchall()
        return [
            Profile(row[0], row[1], row[2], np.frombuffer(row[3], np.float32), row[4])
            for row in rows
        ]

    def add_sample(
        self,
        profile_id: int,
        label: str,
        embedding: np.ndarray,
        thumbnail: bytes | None,
    ) -> ProfileSample:
        vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            profile = db.execute(
                "SELECT label FROM profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if not profile:
                raise ValueError("Identity not found")
            if profile[0] != label:
                raise ValueError("Sample must use the same object class as the identity")
            count = db.execute(
                "SELECT COUNT(*) FROM profile_samples WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()[0]
            if count >= 64:
                raise ValueError("An identity can contain at most 64 samples")
            cursor = db.execute(
                """INSERT INTO profile_samples
                   (profile_id, label, embedding, thumbnail, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (profile_id, label, vector.tobytes(), thumbnail, created_at),
            )
            sample_id = cursor.lastrowid
        return ProfileSample(
            sample_id, profile_id, label, vector, thumbnail, created_at
        )

    def add_enrollment_samples(
        self,
        *,
        label: str,
        samples: list[tuple[np.ndarray, bytes | None]],
        profile_id: int | None = None,
        name: str = "",
        duplicate_threshold: float = 0.985,
    ) -> tuple[Profile, list[ProfileSample], int]:
        """Atomically create/extend an identity, omitting near-identical samples."""
        if not samples:
            raise ValueError("Select at least one sample")
        vectors = [np.asarray(vector, dtype=np.float32).reshape(-1) for vector, _ in samples]
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            row = None
            if profile_id is not None:
                row = db.execute(
                    "SELECT id, name, label, embedding, created_at FROM profiles WHERE id = ?",
                    (profile_id,),
                ).fetchone()
                if not row:
                    raise ValueError("Identity not found")
                if row[2] != label:
                    raise ValueError("Samples must use the same object class as the identity")
                profile = Profile(
                    row[0], row[1], row[2], np.frombuffer(row[3], np.float32), row[4]
                )
                saved = [
                    np.frombuffer(item[0], np.float32)
                    for item in db.execute(
                        "SELECT embedding FROM profile_samples WHERE profile_id = ? ORDER BY id",
                        (profile_id,),
                    ).fetchall()
                ]
            else:
                name = name.strip()
                if not name:
                    raise ValueError("A name is required for a new identity")
                saved = []

            accepted: list[tuple[np.ndarray, bytes | None]] = []
            skipped = 0
            for vector, (_, thumbnail) in zip(vectors, samples):
                if any(
                    cosine_similarity(previous, vector) >= duplicate_threshold
                    for previous in [*saved, *(item[0] for item in accepted)]
                ):
                    skipped += 1
                    continue
                accepted.append((vector, thumbnail))
            if len(saved) + len(accepted) > 64:
                raise ValueError("An identity can contain at most 64 samples")
            if profile_id is None:
                if not accepted:
                    raise ValueError("Select at least one distinct sample")
                cursor = db.execute(
                    "INSERT INTO profiles(name, label, embedding, created_at) VALUES (?, ?, ?, ?)",
                    (name, label, accepted[0][0].tobytes(), created_at),
                )
                profile_id = cursor.lastrowid
                profile = Profile(profile_id, name, label, accepted[0][0], created_at)
            added: list[ProfileSample] = []
            for vector, thumbnail in accepted:
                cursor = db.execute(
                    """INSERT INTO profile_samples
                       (profile_id, label, embedding, thumbnail, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (profile_id, label, vector.tobytes(), thumbnail, created_at),
                )
                added.append(
                    ProfileSample(
                        cursor.lastrowid,
                        profile_id,
                        label,
                        vector,
                        thumbnail,
                        created_at,
                    )
                )
        return profile, added, skipped

    def delete_sample(self, profile_id: int, sample_id: int) -> bool:
        with self._connect() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM profile_samples WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()[0]
            if count <= 1:
                return False
            return (
                db.execute(
                    "DELETE FROM profile_samples WHERE id = ? AND profile_id = ?",
                    (sample_id, profile_id),
                ).rowcount
                > 0
            )

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

    @staticmethod
    def _ewelink_device(row) -> EWeLinkDevice:
        return EWeLinkDevice(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            json.loads(row[7]),
            json.loads(row[8]),
            None if row[9] is None else bool(row[9]),
            bool(row[10]),
            row[11],
            row[12],
            row[13],
            row[14],
        )

    def sync_ewelink_devices(self, devices: list[dict]) -> list[EWeLinkDevice]:
        from ewelink_cloud import device_capabilities

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("UPDATE ewelink_devices SET available = 0, updated_at = ?", (now,))
            for device in devices:
                params = device.get("params")
                params = params if isinstance(params, dict) else {}
                capabilities = device.get("capabilities")
                if not isinstance(capabilities, list):
                    capabilities = device_capabilities(device.get("uiid"), params)
                online = device.get("online")
                online_value = None if online is None else int(bool(online))
                last_seen = now if online is True else None
                db.execute(
                    """INSERT INTO ewelink_devices
                       (device_id, name, model, device_key, uiid, host, port, params,
                        capabilities, online, available, created_at, updated_at, last_seen,
                        last_sync)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                       ON CONFLICT(device_id) DO UPDATE SET
                         name = excluded.name,
                         model = excluded.model,
                         device_key = excluded.device_key,
                         uiid = excluded.uiid,
                         host = CASE WHEN excluded.host <> '' THEN excluded.host ELSE ewelink_devices.host END,
                         port = CASE WHEN excluded.host <> '' THEN excluded.port ELSE ewelink_devices.port END,
                         params = excluded.params,
                         capabilities = excluded.capabilities,
                         online = excluded.online,
                         available = 1,
                         updated_at = excluded.updated_at,
                         last_seen = COALESCE(excluded.last_seen, ewelink_devices.last_seen),
                         last_sync = excluded.last_sync""",
                    (
                        str(device["id"]),
                        str(device.get("name") or device["id"]),
                        str(device.get("model") or "SONOFF device"),
                        str(device.get("device_key") or ""),
                        int(device["uiid"]) if device.get("uiid") is not None else None,
                        str(device.get("host") or ""),
                        int(device.get("port") or 8081),
                        json.dumps(params, separators=(",", ":")),
                        json.dumps(capabilities, separators=(",", ":")),
                        online_value,
                        now,
                        now,
                        last_seen,
                        now,
                    ),
                )
        return self.ewelink_devices()

    def ewelink_devices(self) -> list[EWeLinkDevice]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT device_id, name, model, device_key, uiid, host, port,
                          params, capabilities, online, available, created_at, updated_at,
                          last_seen, last_sync
                   FROM ewelink_devices ORDER BY name COLLATE NOCASE, device_id"""
            ).fetchall()
        return [self._ewelink_device(row) for row in rows]

    def ewelink_device(self, device_id: str) -> EWeLinkDevice | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT device_id, name, model, device_key, uiid, host, port,
                          params, capabilities, online, available, created_at, updated_at,
                          last_seen, last_sync
                   FROM ewelink_devices WHERE device_id = ?""",
                (device_id,),
            ).fetchone()
        return self._ewelink_device(row) if row else None

    def update_ewelink_device_state(
        self, device_id: str, params: dict, online: bool | None
    ) -> EWeLinkDevice | None:
        from ewelink_cloud import device_capabilities

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT uiid FROM ewelink_devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if not row:
                return None
            db.execute(
                """UPDATE ewelink_devices
                   SET params = ?, capabilities = ?, online = ?, updated_at = ?,
                       last_seen = CASE WHEN ? = 1 THEN ? ELSE last_seen END,
                       last_sync = ? WHERE device_id = ?""",
                (
                    json.dumps(params, separators=(",", ":")),
                    json.dumps(device_capabilities(row[0], params), separators=(",", ":")),
                    None if online is None else int(online),
                    now,
                    None if online is None else int(online),
                    now,
                    now,
                    device_id,
                ),
            )
        return self.ewelink_device(device_id)

    @staticmethod
    def _automation(row) -> Automation:
        return Automation(
            row[0], row[1], bool(row[2]), row[3], json.loads(row[4]), row[5], row[6], row[7]
        )

    def create_automation(self, name: str, graph: dict, enabled: bool = False) -> Automation:
        from automation import validate_graph

        name = name.strip()
        document = validate_graph(
            {**graph, "name": name, "enabled": bool(enabled), "revision": 1}
        )
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO automations
                   (name, enabled, revision, graph, next_run_at, created_at, updated_at)
                   VALUES (?, ?, 1, ?, NULL, ?, ?)""",
                (name, int(enabled), json.dumps(document, separators=(",", ":")), now, now),
            )
            automation_id = cursor.lastrowid
        return self.automation(automation_id)

    def update_automation(
        self, automation_id: int, name: str, graph: dict, enabled: bool
    ) -> Automation | None:
        from automation import validate_graph

        current = self.automation(automation_id)
        if not current:
            return None
        revision = current.revision + 1
        name = name.strip()
        document = validate_graph(
            {
                **graph,
                "name": name,
                "enabled": bool(enabled),
                "revision": revision,
            }
        )
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """UPDATE automations SET name = ?, enabled = ?, revision = ?, graph = ?,
                          next_run_at = NULL, updated_at = ? WHERE id = ?""",
                (
                    name,
                    int(enabled),
                    revision,
                    json.dumps(document, separators=(",", ":")),
                    now,
                    automation_id,
                ),
            )
        return self.automation(automation_id)

    def automation(self, automation_id: int) -> Automation | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT id, name, enabled, revision, graph, next_run_at, created_at, updated_at
                   FROM automations WHERE id = ?""",
                (automation_id,),
            ).fetchone()
        return self._automation(row) if row else None

    def automations(self, enabled: bool | None = None) -> list[Automation]:
        query = """SELECT id, name, enabled, revision, graph, next_run_at, created_at, updated_at
                   FROM automations"""
        values: tuple = ()
        if enabled is not None:
            query += " WHERE enabled = ?"
            values = (int(enabled),)
        query += " ORDER BY id"
        with self._connect() as db:
            rows = db.execute(query, values).fetchall()
        return [self._automation(row) for row in rows]

    def delete_automation(self, automation_id: int) -> bool:
        with self._connect() as db:
            return (
                db.execute("DELETE FROM automations WHERE id = ?", (automation_id,)).rowcount
                > 0
            )

    def set_automation_next_run(
        self, automation_id: int, next_run_at: datetime | str | None
    ) -> None:
        if isinstance(next_run_at, datetime):
            if next_run_at.tzinfo is None:
                raise ValueError("Next run time must include a time zone")
            next_run_at = next_run_at.astimezone(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "UPDATE automations SET next_run_at = ? WHERE id = ?",
                (next_run_at, automation_id),
            )

    @staticmethod
    def _automation_run(row) -> AutomationRun:
        return AutomationRun(
            row[0],
            row[1],
            row[2],
            json.loads(row[3]),
            row[4],
            row[5],
            row[6],
            json.loads(row[7]),
        )

    def start_automation_run(
        self,
        automation_id: int | None,
        revision: int,
        trigger: dict,
        status: str = "running",
    ) -> AutomationRun:
        if status not in {"running", "dropped"}:
            raise ValueError("New automation run must be running or dropped")
        now = datetime.now(timezone.utc).isoformat()
        finished = now if status == "dropped" else None
        result = {"reason": "concurrency limit reached"} if status == "dropped" else {}
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO automation_runs
                   (automation_id, revision, trigger, status, started_at, finished_at, result)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    automation_id,
                    revision,
                    json.dumps(trigger, separators=(",", ":")),
                    status,
                    now,
                    finished,
                    json.dumps(result, separators=(",", ":")),
                ),
            )
            run_id = cursor.lastrowid
            db.execute(
                """DELETE FROM automation_runs WHERE id NOT IN
                   (SELECT id FROM automation_runs ORDER BY id DESC LIMIT 1000)"""
            )
        return self.automation_run(run_id)

    def finish_automation_run(self, run_id: int, status: str, result: dict) -> AutomationRun:
        if status not in {"completed", "failed", "canceled"}:
            raise ValueError("Automation run has an invalid final status")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            changed = db.execute(
                """UPDATE automation_runs SET status = ?, finished_at = ?, result = ?
                   WHERE id = ? AND status = 'running'""",
                (status, now, json.dumps(result, separators=(",", ":")), run_id),
            ).rowcount
        run = self.automation_run(run_id)
        if not changed or not run:
            raise ValueError("Automation run is not active")
        return run

    def cancel_active_automation_runs(self, reason: str = "Application restarted") -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            return db.execute(
                """UPDATE automation_runs SET status = 'canceled', finished_at = ?, result = ?
                   WHERE status = 'running'""",
                (now, json.dumps({"reason": reason}, separators=(",", ":"))),
            ).rowcount

    def automation_run(self, run_id: int) -> AutomationRun | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT id, automation_id, revision, trigger, status, started_at,
                          finished_at, result FROM automation_runs WHERE id = ?""",
                (run_id,),
            ).fetchone()
        return self._automation_run(row) if row else None

    def automation_runs(
        self, automation_id: int | None = None, limit: int = 100
    ) -> list[AutomationRun]:
        limit = min(1000, max(1, limit))
        query = """SELECT id, automation_id, revision, trigger, status, started_at,
                          finished_at, result FROM automation_runs"""
        values: tuple
        if automation_id is None:
            query += " ORDER BY id DESC LIMIT ?"
            values = (limit,)
        else:
            query += " WHERE automation_id = ? ORDER BY id DESC LIMIT ?"
            values = (automation_id, limit)
        with self._connect() as db:
            rows = db.execute(query, values).fetchall()
        return [self._automation_run(row) for row in rows]

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
    by_identity: dict[int, Match] = {}
    for profile in profiles:
        if profile.label != label:
            continue
        candidate = Match(profile, profile_similarity(profile.embedding, embedding))
        previous = by_identity.get(profile.id)
        if previous is None or candidate.similarity > previous.similarity:
            by_identity[profile.id] = candidate
    candidates = sorted(
        by_identity.values(), key=lambda item: item.similarity, reverse=True
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


def _ewelink_lan_request(
    host: str,
    port: int,
    device_id: str,
    device_key: str,
    command: str,
    data: dict,
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
    from Crypto.Cipher import AES

    plaintext = json.dumps(data, separators=(",", ":")).encode()
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
        f"http://{url_host}:{port}/zeroconf/{command}",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json;charset=UTF-8"},
        method="POST",
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
    if channel not in {1, 2, 3, 4}:
        raise ValueError("eWeLink channel must be between 1 and 4")
    if state not in {"on", "off"}:
        raise ValueError("eWeLink state must be on or off")
    return _ewelink_lan_request(
        host,
        port,
        device_id,
        device_key,
        "switches",
        {"switches": [{"switch": state, "outlet": channel - 1}]},
        sequence=sequence,
        iv=iv,
    )


def ewelink_info_request(
    host: str,
    port: int,
    device_id: str,
    device_key: str,
    *,
    sequence: str | None = None,
    iv: bytes | None = None,
) -> Request:
    """Build an encrypted stock-firmware relay status request."""
    return _ewelink_lan_request(
        host, port, device_id, device_key, "info", {}, sequence=sequence, iv=iv
    )


def ewelink_response_data(payload: dict, device_key: str) -> dict:
    """Decrypt the data object returned by stock eWeLink LAN firmware."""
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    if not payload.get("encrypt") or not isinstance(data, str):
        raise ValueError("eWeLink returned invalid relay data")
    try:
        from Crypto.Cipher import AES

        iv = base64.b64decode(payload["iv"], validate=True)
        encrypted = base64.b64decode(data, validate=True)
        plaintext = AES.new(
            hashlib.md5(device_key.encode()).digest(), AES.MODE_CBC, iv
        ).decrypt(encrypted)
        padding = plaintext[-1]
        if padding < 1 or padding > 16 or plaintext[-padding:] != bytes([padding]) * padding:
            raise ValueError
        decoded = json.loads(plaintext[:-padding])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("eWeLink returned invalid relay data") from error
    if not isinstance(decoded, dict):
        raise ValueError("eWeLink returned invalid relay data")
    return decoded


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
