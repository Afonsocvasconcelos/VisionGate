from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import urlopen

import cv2
import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator

from auth import AuthManager, TooManyAttempts
from automation import AutomationEngine, GraphValidationError, default_door_graph, validate_graph
from core import (
    AccessGate,
    Camera,
    Database,
    Match,
    best_match,
    camera_stream_url,
    ewelink_info_request,
    ewelink_request,
    ewelink_response_data,
    local_ipv4_addresses,
    reid_eligible,
    reid_regions,
    rtsp_url_from_text,
    select_track,
)
from ewelink_cloud import (
    EWeLinkCloud,
    EWeLinkCloudError,
    ImportSessions,
    add_lan_addresses,
    cloud_status_request,
    cloud_switch_request,
)
from ewelink_devices import EWeLinkDeviceManager
from enrollment import EnrollmentManager


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("visiongate")
AUTH = AuthManager.from_environment()
SESSION_COOKIE = "vg"
APP_PORT = 83


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    value = float(os.getenv(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data")).resolve()
DEFAULT_SETTINGS = {
    "app_name": "VisionGate",
    "brand_palette": "teal",
    "performance_mode": "auto",
    "yolo_model": os.getenv("YOLO_MODEL", "yolo11n.pt").strip(),
    "yolo_imgsz": int(_number("YOLO_IMGSZ", 640, 320, 1280)),
    "detection_confidence": _number("DETECTION_CONFIDENCE", 0.35, 0.05, 0.95),
    "embed_every": int(_number("EMBED_EVERY", 5, 1, 60)),
    "match_threshold": _number("MATCH_THRESHOLD", 0.82, 0.1, 0.999),
    "match_margin": _number("MATCH_MARGIN", 0.04, 0, 0.25),
    "match_confirmations": int(_number("MATCH_CONFIRMATIONS", 3, 1, 20)),
    "open_cooldown_seconds": _number("OPEN_COOLDOWN_SECONDS", 20, 0, 3600),
    "jpeg_quality": int(_number("JPEG_QUALITY", 82, 40, 100)),
    "ewelink_model": os.getenv("EWELINK_MODEL", "SONOFF 4CH Pro R2").strip(),
    "ewelink_host": os.getenv("EWELINK_HOST", "").strip(),
    "ewelink_port": int(_number("EWELINK_PORT", 8081, 1, 65535)),
    "ewelink_device_id": os.getenv("EWELINK_DEVICE_ID", "").strip(),
    "ewelink_device_key": os.getenv("EWELINK_DEVICE_KEY", "").strip(),
    "ewelink_cloud_token": "",
    "ewelink_cloud_app_id": "",
    "ewelink_cloud_region": "",
    "ewelink_cloud_user_apikey": "",
    "ewelink_open_channel": int(_number("EWELINK_OPEN_CHANNEL", 1, 1, 4)),
    "ewelink_close_channel": int(_number("EWELINK_CLOSE_CHANNEL", 2, 1, 4)),
    "pulse_seconds": _number("DOOR_PULSE_SECONDS", 1, 0.1, 30),
    "auto_close_seconds": _number("AUTO_CLOSE_SECONDS", 5, 0, 3600),
}


@dataclass(frozen=True, slots=True)
class Config:
    data_dir: Path
    performance_mode: str
    yolo_model: str
    yolo_imgsz: int
    confidence: float
    embed_every: int
    match_threshold: float
    match_margin: float
    confirmations: int
    cooldown: float
    jpeg_quality: int
    ewelink_model: str
    ewelink_host: str
    ewelink_port: int
    ewelink_device_id: str
    ewelink_device_key: str
    ewelink_cloud_token: str
    ewelink_cloud_app_id: str
    ewelink_cloud_region: str
    ewelink_open_channel: int
    ewelink_close_channel: int
    pulse_seconds: float
    auto_close_seconds: float
    disable_vision: bool


def _config(settings: dict) -> Config:
    return Config(
        data_dir=DATA_DIR,
        performance_mode=str(settings["performance_mode"]),
        yolo_model=str(settings["yolo_model"]),
        yolo_imgsz=int(settings["yolo_imgsz"]),
        confidence=float(settings["detection_confidence"]),
        embed_every=int(settings["embed_every"]),
        match_threshold=float(settings["match_threshold"]),
        match_margin=float(settings["match_margin"]),
        confirmations=int(settings["match_confirmations"]),
        cooldown=float(settings["open_cooldown_seconds"]),
        jpeg_quality=int(settings["jpeg_quality"]),
        ewelink_model=str(settings["ewelink_model"]).strip(),
        ewelink_host=str(settings["ewelink_host"]).strip(),
        ewelink_port=int(settings["ewelink_port"]),
        ewelink_device_id=str(settings["ewelink_device_id"]).strip(),
        ewelink_device_key=str(settings["ewelink_device_key"]).strip(),
        ewelink_cloud_token=str(settings["ewelink_cloud_token"]).strip(),
        ewelink_cloud_app_id=str(settings["ewelink_cloud_app_id"]).strip(),
        ewelink_cloud_region=str(settings["ewelink_cloud_region"]).strip(),
        ewelink_open_channel=int(settings["ewelink_open_channel"]),
        ewelink_close_channel=int(settings["ewelink_close_channel"]),
        pulse_seconds=float(settings["pulse_seconds"]),
        auto_close_seconds=float(settings["auto_close_seconds"]),
        disable_vision=os.getenv("DISABLE_VISION", "0") == "1",
    )


def _camera_parts(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "rtsp" or not parsed.hostname:
        raise ValueError("Camera stream must be a valid rtsp:// URL")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    clean_url = urlunsplit(
        (
            parsed.scheme,
            f"{host}:{parsed.port or 554}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )
    return clean_url, unquote(parsed.username or ""), unquote(parsed.password or "")


def _video_capture(url: str):
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    capture = cv2.VideoCapture(
        url,
        cv2.CAP_FFMPEG,
        [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
            10_000,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC,
            10_000,
        ],
    )
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return capture


def _camera_connection_error() -> str:
    addresses = local_ipv4_addresses()
    location = f" This PC is on {addresses[0]}." if addresses else ""
    return (
        "Camera did not accept the RTSP connection."
        f"{location} Check the camera power, address, and network."
    )


EWELINK_CLOUD = EWeLinkCloud()
EWELINK_IMPORTS = ImportSessions()
DATABASE = Database(DATA_DIR / "whitelist.db")
stored_settings = DATABASE.settings()
missing_settings = {
    key: value for key, value in DEFAULT_SETTINGS.items() if key not in stored_settings
}
if missing_settings:
    stored_settings = DATABASE.update_settings(missing_settings)
DATABASE.delete_settings("ha_url", "ha_token", "ha_entity")
stored_settings = DATABASE.settings()
CONFIG = _config(stored_settings)

if not DATABASE.cameras():
    source = os.getenv("RTSP_URL", "").strip()
    if not source and (info := ROOT / "info.md").exists():
        source = rtsp_url_from_text(info.read_text(encoding="utf-8"))
    if source:
        clean_url, username, password = _camera_parts(source)
        DATABASE.add_camera("Camera 1", clean_url, username, password)


class DoorController:
    POLL_SECONDS = 60

    def __init__(self, config: Config, database: Database):
        self.database = database
        self._busy = threading.Lock()
        self._guard = threading.Lock()
        self._config = config
        self._last_trigger = float("-inf")
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._last_state_check: float | None = None
        self._state_check_error = ""
        self.last_event = "Waiting for an approved target"
        self.last_error = ""
        settings = database.settings()
        saved_command = str(
            settings.get("door_last_command") or settings.get("door_last_state") or ""
        )
        self._last_command = {
            "open": "open",
            "closed": "close",
            "close": "close",
        }.get(saved_command)
        self._state = "unknown"
        if self._last_command is None:
            recent_command = next(
                (
                    event.kind
                    for event in database.events(1000)
                    if event.kind in {"door_open", "door_close"}
                ),
                "",
            )
            self._last_command = {
                "door_open": "open",
                "door_close": "close",
            }.get(recent_command)
        if self._last_command:
            database.update_settings({"door_last_command": self._last_command})
        self._validate(config)
        self._state_source = "momentary_relay"

    @staticmethod
    def _configured(config: Config) -> bool:
        lan = all(
            (config.ewelink_host, config.ewelink_device_id, config.ewelink_device_key)
        )
        cloud = all(
            (
                config.ewelink_device_id,
                config.ewelink_cloud_token,
                config.ewelink_cloud_app_id,
                config.ewelink_cloud_region,
            )
        )
        return lan or cloud

    @staticmethod
    def _validate(config: Config) -> None:
        identity = (config.ewelink_device_id, config.ewelink_device_key)
        cloud = (
            config.ewelink_cloud_token,
            config.ewelink_cloud_app_id,
            config.ewelink_cloud_region,
        )
        if any(identity) and not all(identity):
            raise ValueError("eWeLink device ID and device key are required together")
        if config.ewelink_host and not all(identity):
            raise ValueError("eWeLink IP requires a device ID and device key")
        if any(cloud) and not all(cloud):
            raise ValueError("eWeLink cloud authorization is incomplete; import the device again")
        if all(identity) and not (config.ewelink_host or all(cloud)):
            raise ValueError("Enter a relay IP or import the device again for cloud control")
        if config.ewelink_open_channel == config.ewelink_close_channel:
            raise ValueError("open and close must use different eWeLink channels")
        if config.ewelink_host:
            ewelink_request(
                config.ewelink_host,
                config.ewelink_port,
                config.ewelink_device_id,
                config.ewelink_device_key,
                config.ewelink_open_channel,
                "off",
            )
        if all(cloud):
            cloud_switch_request(
                config.ewelink_cloud_token,
                config.ewelink_cloud_app_id,
                config.ewelink_cloud_region,
                config.ewelink_device_id,
                config.ewelink_open_channel,
                "off",
            )

    def update(self, config: Config) -> None:
        self._validate(config)
        with self._guard:
            old = self._config
            self._config = config
            target_changed = self._target(old) != self._target(config)
            if target_changed:
                self._state = "unknown"
                self._state_source = "momentary_relay"
                self._last_command = None
                self.database.update_settings(
                    {"door_last_state": "unknown", "door_last_command": ""}
                )
        if target_changed and self._configured(config):
            self.refresh_state()

    @staticmethod
    def _target(config: Config) -> tuple:
        return (
            config.ewelink_host,
            config.ewelink_port,
            config.ewelink_device_id,
            config.ewelink_device_key,
            config.ewelink_cloud_token,
            config.ewelink_cloud_app_id,
            config.ewelink_cloud_region,
            config.ewelink_open_channel,
            config.ewelink_close_channel,
        )

    def start(self) -> None:
        with self._guard:
            if self._poll_thread and self._poll_thread.is_alive():
                return
            self._poll_stop.clear()
            self._poll_thread = threading.Thread(
                target=self._poll, daemon=True, name="door-state"
            )
            self._poll_thread.start()

    def _poll(self) -> None:
        while not self._poll_stop.is_set():
            self.refresh_state()
            self._poll_stop.wait(self.POLL_SECONDS)

    def stop(self) -> None:
        self._poll_stop.set()
        thread = self._poll_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._guard:
            self._poll_thread = None

    @property
    def configured(self) -> bool:
        with self._guard:
            return self._configured(self._config)

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    def status(self) -> dict:
        with self._guard:
            config = self._config
            state = self._state
            last_state_check = self._last_state_check
            state_check_error = self._state_check_error
            last_command = self._last_command
            state_source = self._state_source
        configured = self._configured(config)
        if self.busy:
            state = "changing"
        elif not configured or state_check_error:
            state = "unavailable"
        return {
            "configured": configured,
            "mode": "lan" if config.ewelink_host else "cloud",
            "busy": self.busy,
            "model": config.ewelink_model,
            "open_channel": config.ewelink_open_channel,
            "close_channel": config.ewelink_close_channel,
            "state": state,
            "state_source": state_source,
            "last_command": last_command,
            "last_state_check": last_state_check,
            "state_check_error": state_check_error,
            "last_event": self.last_event,
            "last_error": self.last_error,
        }

    def trigger(
        self,
        reason: str,
        camera: Camera | None = None,
        match: Match | None = None,
        action: str = "open",
    ) -> bool:
        if action not in {"open", "close"}:
            raise ValueError("Door action must be open or close")
        with self._guard:
            config = self._config
            now = time.monotonic()
            if not self._configured(config):
                message = "Door is not configured"
                self.last_error = message
                self.database.add_event("door_disabled", message, camera)
                return False
            if (
                action == "open" and now - self._last_trigger < config.cooldown
            ) or not self._busy.acquire(False):
                return False
            if action == "open":
                self._last_trigger = now
        threading.Thread(
            target=self._activate,
            args=(config, reason, camera, match, action),
            daemon=True,
            name=f"door-{action}",
        ).start()
        return True

    @staticmethod
    def _request_json(request) -> dict:
        with urlopen(request, timeout=5) as response:
            if response.status >= 300:
                raise RuntimeError(f"eWeLink relay returned HTTP {response.status}")
            try:
                result = json.loads(response.read() or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise RuntimeError("eWeLink relay returned an invalid response") from error
            if result.get("error", 0):
                raise RuntimeError(f'eWeLink relay returned error {result["error"]}')
            return result

    @classmethod
    def _send_request(cls, request) -> None:
        cls._request_json(request)

    @staticmethod
    def _switches(values) -> dict[int, str]:
        if not isinstance(values, list):
            raise RuntimeError("eWeLink returned invalid relay states")
        switches = {}
        for item in values:
            if not isinstance(item, dict) or item.get("switch") not in {"on", "off"}:
                raise RuntimeError("eWeLink returned invalid relay states")
            try:
                outlet = int(item["outlet"])
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError("eWeLink returned invalid relay states") from error
            switches[outlet] = item["switch"]
        return switches

    @classmethod
    def _query_switches(cls, config: Config) -> dict[int, str]:
        lan_error = None
        if config.ewelink_host:
            try:
                result = cls._request_json(
                    ewelink_info_request(
                        config.ewelink_host,
                        config.ewelink_port,
                        config.ewelink_device_id,
                        config.ewelink_device_key,
                    )
                )
                data = ewelink_response_data(result, config.ewelink_device_key)
                return cls._switches(data.get("switches"))
            except (HTTPError, URLError, OSError, RuntimeError, ValueError) as error:
                if isinstance(error, HTTPError):
                    error.close()
                lan_error = error
        if config.ewelink_cloud_token:
            try:
                result = cls._request_json(
                    cloud_status_request(
                        config.ewelink_cloud_token,
                        config.ewelink_cloud_app_id,
                        config.ewelink_cloud_region,
                        config.ewelink_device_id,
                    )
                )
                data = result.get("data") or {}
                params = data.get("params") if isinstance(data, dict) else None
                return cls._switches(params.get("switches") if isinstance(params, dict) else None)
            except (HTTPError, URLError, OSError, RuntimeError) as cloud_error:
                if isinstance(cloud_error, HTTPError):
                    cloud_error.close()
                if lan_error:
                    raise RuntimeError(
                        f"LAN failed ({lan_error}); cloud failed ({cloud_error})"
                    ) from cloud_error
                raise
        if lan_error:
            raise lan_error
        raise RuntimeError("No eWeLink LAN or cloud connection is configured")

    def _position_sensor_state(self, config: Config) -> tuple[str | None, bool]:
        device = self.database.ewelink_device(config.ewelink_device_id)
        if not device or not any(
            capability.get("type") == "binary_sensor"
            and capability.get("id") == "door"
            for capability in device.capabilities
        ):
            return None, False
        if not device.available or device.online is False:
            return None, True
        value = device.params.get("door")
        if type(value) is bool:
            return ("open" if value else "closed"), True
        if isinstance(value, str):
            normalized = value.casefold()
            if normalized in {"on", "open", "detected"}:
                return "open", True
            if normalized in {"off", "closed", "normal"}:
                return "closed", True
        return None, True

    def refresh_state(self) -> bool:
        with self._guard:
            config = self._config
        if not self._configured(config):
            return False
        source = "momentary_relay"
        try:
            sensor_state, has_sensor = self._position_sensor_state(config)
            if has_sensor:
                source = "binary_sensor:door"
            if has_sensor and sensor_state is None:
                raise RuntimeError("Door position sensor is unavailable")
            if has_sensor:
                state = sensor_state
            else:
                switches = self._query_switches(config)
                open_on = switches.get(config.ewelink_open_channel - 1) == "on"
                close_on = switches.get(config.ewelink_close_channel - 1) == "on"
                state = "changing" if open_on != close_on else "unknown"
            with self._guard:
                self._state = state
                self._state_source = source
                recovered = bool(self._state_check_error)
                self._state_check_error = ""
                self._last_state_check = time.time()
            if recovered:
                log.info("eWeLink door state check recovered")
            return True
        except (HTTPError, URLError, OSError, RuntimeError, ValueError) as error:
            if isinstance(error, HTTPError):
                error.close()
            message = str(error)
            with self._guard:
                changed = message != self._state_check_error
                self._state_check_error = message
                self._state = "unavailable"
                self._state_source = source
            if changed:
                log.warning("eWeLink door state check failed: %s", error)
            return False

    @classmethod
    def _send(cls, config: Config, channel: int, state: str) -> None:
        lan_error = None
        if config.ewelink_host:
            try:
                cls._send_request(
                    ewelink_request(
                        config.ewelink_host,
                        config.ewelink_port,
                        config.ewelink_device_id,
                        config.ewelink_device_key,
                        channel,
                        state,
                    )
                )
                return
            except (HTTPError, URLError, OSError, RuntimeError) as error:
                if isinstance(error, HTTPError):
                    error.close()
                lan_error = error
        if config.ewelink_cloud_token:
            try:
                cls._send_request(
                    cloud_switch_request(
                        config.ewelink_cloud_token,
                        config.ewelink_cloud_app_id,
                        config.ewelink_cloud_region,
                        config.ewelink_device_id,
                        channel,
                        state,
                    )
                )
                return
            except (HTTPError, URLError, OSError, RuntimeError) as cloud_error:
                if isinstance(cloud_error, HTTPError):
                    cloud_error.close()
                if lan_error:
                    raise RuntimeError(
                        f"LAN failed ({lan_error}); cloud failed ({cloud_error})"
                    ) from cloud_error
                raise
        if lan_error:
            raise lan_error
        raise RuntimeError("No eWeLink LAN or cloud connection is configured")

    def _record(
        self,
        kind: str,
        message: str,
        camera: Camera | None,
        match: Match | None,
    ) -> None:
        self.database.add_event(
            kind,
            message,
            camera,
            match.profile.id if match else None,
            match.profile.name if match else None,
            match.profile.label if match else None,
            match.similarity if match else None,
        )

    def _activate(
        self,
        config: Config,
        reason: str,
        camera: Camera | None,
        match: Match | None,
        action: str,
    ) -> None:
        channel = (
            config.ewelink_open_channel
            if action == "open"
            else config.ewelink_close_channel
        )
        failure = None
        try:
            with self._guard:
                self._state = "changing"
            self._send(config, channel, "on")
            with self._guard:
                self._last_command = action
            self.database.update_settings({"door_last_command": action})
            self.last_event = f"Door {action} command sent for {reason}"
            self.last_error = ""
            self._record(f"door_{action}", self.last_event, camera, match)
            log.info(self.last_event)
        except (HTTPError, URLError, OSError, RuntimeError) as error:
            if isinstance(error, HTTPError):
                error.close()
            failure = error
        finally:
            time.sleep(config.pulse_seconds)
            shutoff_succeeded = False
            for attempt in range(3):
                try:
                    self._send(config, channel, "off")
                    shutoff_succeeded = True
                    break
                except (HTTPError, URLError, OSError, RuntimeError) as error:
                    if isinstance(error, HTTPError):
                        error.close()
                    if attempt == 2:
                        message = f"Door safety shutoff failed: {error}"
                        self.last_error = message
                        self._record("door_error", message, camera, match)
                        log.error(message)
                    else:
                        time.sleep(0.5)
            with self._guard:
                self._state = "unknown" if shutoff_succeeded else "unavailable"
            if failure:
                message = f"Door control failed: {failure}"
                self.last_error = message
                self._record("door_error", message, camera, match)
                log.error(message)
            self._busy.release()


def vision_runtime(config: Config, device: str) -> tuple[str, int, int]:
    limited = config.performance_mode == "low_power" or (
        config.performance_mode == "auto" and device == "cpu"
    )
    return (
        "yolo11n.pt" if limited else config.yolo_model,
        min(config.yolo_imgsz, 512) if limited else config.yolo_imgsz,
        2 if limited else 1,
    )


def detection_caption(track: dict) -> str:
    return f'{track.get("match") or track["label"]} · {track["confidence"]:.0%}'


def authorized_presence_events(
    previous: dict[int, Match], current: dict[int, Match], camera: Camera
) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    def payload(track_id: int, match: Match) -> dict:
        return {
            "camera_id": camera.id,
            "camera_name": camera.name,
            "track_id": track_id,
            "profile_id": match.profile.id,
            "profile_name": match.profile.name,
            "label": match.profile.label,
            "similarity": match.similarity,
            "authorized": True,
        }

    for track_id in sorted(previous):
        old = previous[track_id]
        new = current.get(track_id)
        if new is None or new.profile.id != old.profile.id:
            events.append(
                ("trigger.camera.authorized_disappeared", payload(track_id, old))
            )
    for track_id in sorted(current):
        new = current[track_id]
        old = previous.get(track_id)
        if old is None or old.profile.id != new.profile.id:
            events.append(
                ("trigger.camera.authorized_appeared", payload(track_id, new))
            )
    if previous and not current:
        events.append(
            (
                "trigger.camera.no_authorized_present",
                {
                    "camera_id": camera.id,
                    "camera_name": camera.name,
                    "authorized_count": 0,
                },
            )
        )
    return events


def object_class_events(
    previous: set[str], current: set[str], camera: Camera
) -> list[tuple[str, dict]]:
    events = []
    for label in sorted(current - previous):
        events.append(
            (
                "trigger.camera.class_appeared",
                {"camera_id": camera.id, "camera_name": camera.name, "label": label},
            )
        )
    for label in sorted(previous - current):
        events.append(
            (
                "trigger.camera.class_disappeared",
                {"camera_id": camera.id, "camera_name": camera.name, "label": label},
            )
        )
    return events


def spatial_layout_descriptor(crop: np.ndarray) -> np.ndarray:
    """Capture color placement and edges after removing the global average color."""
    small = cv2.resize(crop, (8, 16), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32) / 255
    lab -= lab.mean(axis=(0, 1), keepdims=True)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255
    vector = np.concatenate(
        (
            lab.reshape(-1),
            cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3).reshape(-1),
            cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3).reshape(-1),
        )
    )
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


class MobileNetEmbedder:
    def __init__(self, device: str):
        import torch
        from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small

        network = mobilenet_v3_small(weights=MobileNet_V3_Small_Weights.DEFAULT)
        self.model = torch.nn.Sequential(network.features, network.avgpool, torch.nn.Flatten())
        self.model.eval().to(device)
        self.device = device
        self.torch = torch
        self.mean = torch.tensor([0.485, 0.456, 0.406], device=device)[None, :, None, None]
        self.std = torch.tensor([0.229, 0.224, 0.225], device=device)[None, :, None, None]

    def __call__(self, crops: list[np.ndarray], labels: list[str]) -> np.ndarray:
        regions = [
            region
            for crop, label in zip(crops, labels)
            for region in reid_regions(crop, label)
        ]
        images = [cv2.resize(region, (224, 224))[:, :, ::-1].copy() for region in regions]
        batch = self.torch.from_numpy(np.stack(images)).permute(0, 3, 1, 2)
        batch = batch.to(self.device, dtype=self.torch.float32).div_(255)
        batch = (batch - self.mean) / self.std
        with self.torch.inference_mode():
            vectors = self.model(batch)
            vectors = self.torch.nn.functional.normalize(vectors, dim=1)
            vectors = vectors.reshape(len(crops), 4, -1).flatten(1)
            vectors = self.torch.nn.functional.normalize(vectors, dim=1)
        deep = vectors.cpu().numpy().astype(np.float32)
        layout = np.stack([spatial_layout_descriptor(crop) for crop in crops])
        combined = np.concatenate((deep, layout * 0.25), axis=1)
        combined /= np.linalg.norm(combined, axis=1, keepdims=True)
        return combined.astype(np.float32)


class VisionSystem:
    labels = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle"}

    def __init__(
        self,
        camera: Camera,
        config: Config,
        database: Database,
        event_sink=None,
    ):
        self.camera = camera
        self.config = config
        self.database = database
        self.event_sink = event_sink or (lambda _kind, _payload: None)
        self.stop_event = threading.Event()
        self.state_lock = threading.Lock()
        self.capture_lock = threading.Lock()
        self.raw_frame: np.ndarray | None = None
        self.raw_sequence = 0
        self.processed_frame: np.ndarray | None = None
        self.processed_sequence = -1
        self.jpeg = self._message_frame(f"Starting {camera.name}...")
        self.tracks: list[dict] = []
        self.frame_size = (1280, 720)
        self.camera_state = "starting"
        self.vision_state = "starting"
        self.last_event = "Waiting for the camera"
        self.last_error = ""
        self.profiles = database.matching_profiles()
        self.authorized_count = 0
        self._authorized_tracks: dict[int, Match] = {}
        self._object_labels: set[str] = set()
        self.thread: threading.Thread | None = None

    @staticmethod
    def _message_frame(message: str) -> bytes:
        image = np.full((720, 1280, 3), (18, 22, 29), np.uint8)
        cv2.putText(image, message, (70, 370), cv2.FONT_HERSHEY_SIMPLEX, 1, (220, 230, 240), 2)
        return cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()

    def report(self, message: str, error: bool = False) -> None:
        with self.state_lock:
            self.last_event = message
            if error:
                self.last_error = message
        (log.error if error else log.info)("%s: %s", self.camera.name, message)

    def _emit(self, kind: str, payload: dict) -> None:
        try:
            self.event_sink(kind, payload)
        except Exception:
            log.exception("Camera event failed: %s", kind)

    def _set_camera_state(self, state: str) -> None:
        with self.state_lock:
            previous = self.camera_state
            self.camera_state = state
        if state == "connected" and previous != "connected":
            self._emit(
                "trigger.camera.online",
                {"camera_id": self.camera.id, "camera_name": self.camera.name},
            )
        elif state in {"unavailable", "reconnecting"} and previous not in {
            "unavailable",
            "reconnecting",
        }:
            self._emit(
                "trigger.camera.offline",
                {"camera_id": self.camera.id, "camera_name": self.camera.name},
            )

    def _publish_scene(
        self, authorized_tracks: dict[int, Match], labels: set[str]
    ) -> None:
        with self.state_lock:
            previous_tracks = dict(self._authorized_tracks)
            previous_labels = set(self._object_labels)
            self._authorized_tracks = dict(authorized_tracks)
            self._object_labels = set(labels)
            self.authorized_count = len(authorized_tracks)
        for kind, payload in authorized_presence_events(
            previous_tracks, authorized_tracks, self.camera
        ):
            self._emit(kind, payload)
        for kind, payload in object_class_events(previous_labels, labels, self.camera):
            self._emit(kind, payload)

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run, daemon=True, name=f"vision-{self.camera.id}"
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def reload_profiles(self) -> None:
        profiles = self.database.matching_profiles()
        with self.state_lock:
            self.profiles = profiles

    def selected_embedding(self, x: float, y: float) -> tuple[str, np.ndarray] | None:
        with self.state_lock:
            width, height = self.frame_size
            track = select_track(self.tracks, x, y, width, height)
            if track is None:
                return None
            return track["label"], track["embedding"].copy()

    def enrollment_snapshot(self):
        with self.state_lock:
            if self.processed_frame is None:
                return None
            tracks = [
                {
                    "id": track["id"],
                    "label": track["label"],
                    "box": tuple(track["box"]),
                    "confidence": track["confidence"],
                    "embedding": None
                    if track.get("embedding") is None
                    else track["embedding"].copy(),
                }
                for track in self.tracks
            ]
            return self.processed_sequence, self.processed_frame.copy(), tracks

    def snapshot(self) -> dict:
        with self.state_lock:
            return {
                "id": self.camera.id,
                "name": self.camera.name,
                "enabled": self.camera.enabled,
                "camera": self.camera_state,
                "vision": self.vision_state,
                "last_event": self.last_event,
                "last_error": self.last_error,
                "tracks": [
                    {
                        "id": item["id"],
                        "label": item["label"],
                        "confidence": round(item["confidence"], 3),
                        "match": item.get("match"),
                        "similarity": round(item.get("similarity", 0), 3),
                    }
                    for item in self.tracks
                ],
            }

    def latest_jpeg(self) -> bytes:
        with self.state_lock:
            return self.jpeg

    def _capture(self) -> None:
        url = camera_stream_url(
            self.camera.stream_url, self.camera.username, self.camera.password
        )
        failure_reported = False
        while not self.stop_event.is_set():
            capture = _video_capture(url)
            if not capture.isOpened():
                self._set_camera_state("unavailable")
                if not failure_reported:
                    self.report(
                        _camera_connection_error(),
                        True,
                    )
                    failure_reported = True
                capture.release()
                self.stop_event.wait(2)
                continue
            self._set_camera_state("connected")
            with self.state_lock:
                self.last_error = ""
            failure_reported = False
            while not self.stop_event.is_set():
                ok, frame = capture.read()
                if not ok:
                    break
                with self.capture_lock:
                    self.raw_frame = frame
                    self.raw_sequence += 1
            capture.release()
            self._set_camera_state("reconnecting")
            self.stop_event.wait(1)

    def _run(self) -> None:
        capture_thread = threading.Thread(
            target=self._capture, daemon=True, name=f"rtsp-{self.camera.id}"
        )
        capture_thread.start()
        try:
            import torch
            from ultralytics import YOLO

            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            if device == "cpu":
                torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
            model_name, image_size, frame_stride = vision_runtime(self.config, device)
            with self.state_lock:
                self.vision_state = f"loading models on {device}"
            detector = YOLO(model_name)
            embedder = MobileNetEmbedder(device)
            gate = AccessGate(self.config.confirmations, self.config.cooldown)
            embeddings: dict[int, np.ndarray] = {}
            matches: dict[int, tuple[Match, str, float]] = {}
            confirmed: dict[int, Match] = {}
            seen_sequence = -1
            processed = 0
            with self.state_lock:
                self.vision_state = f"running on {device}{' (optimized)' if frame_stride > 1 else ''}"
            while not self.stop_event.is_set():
                with self.capture_lock:
                    sequence, frame = self.raw_sequence, self.raw_frame
                if frame is None or sequence == seen_sequence:
                    self.stop_event.wait(0.01)
                    continue
                seen_sequence = sequence
                processed += 1
                if frame_stride > 1 and processed % frame_stride == 0:
                    continue
                result = detector.track(
                    frame,
                    persist=True,
                    tracker="bytetrack.yaml",
                    classes=list(self.labels),
                    conf=self.config.confidence,
                    imgsz=image_size,
                    device=device,
                    verbose=False,
                )[0]
                tracks: list[dict] = []
                if result.boxes is not None and result.boxes.id is not None:
                    boxes = result.boxes.xyxy.int().cpu().tolist()
                    ids = result.boxes.id.int().cpu().tolist()
                    classes = result.boxes.cls.int().cpu().tolist()
                    confidences = result.boxes.conf.cpu().tolist()
                    height, width = frame.shape[:2]
                    for track_id, class_id, box, confidence in zip(
                        ids, classes, boxes, confidences
                    ):
                        x1, y1, x2, y2 = box
                        x1, y1 = max(0, x1), max(0, y1)
                        x2, y2 = min(width, x2), min(height, y2)
                        if x2 - x1 < 12 or y2 - y1 < 12:
                            continue
                        tracks.append(
                            {
                                "id": track_id,
                                "label": self.labels[class_id],
                                "box": (x1, y1, x2, y2),
                                "confidence": float(confidence),
                                "embedding": embeddings.get(track_id),
                            }
                        )
                fresh = [
                    track
                    for track in tracks
                    if reid_eligible(track["label"], track["box"])
                    and (
                        track["id"] not in embeddings
                        or processed % self.config.embed_every == 0
                    )
                ]
                if fresh:
                    vectors = embedder(
                        [
                            frame[
                                item["box"][1] : item["box"][3],
                                item["box"][0] : item["box"][2],
                            ]
                            for item in fresh
                        ],
                        [item["label"] for item in fresh],
                    )
                    with self.state_lock:
                        profiles = list(self.profiles)
                    for track, vector in zip(fresh, vectors):
                        track_id = track["id"]
                        previous = embeddings.get(track_id)
                        if previous is not None:
                            vector = previous * 0.7 + vector * 0.3
                            vector /= np.linalg.norm(vector)
                        embeddings[track_id] = vector
                        track["embedding"] = vector
                        match = best_match(
                            profiles,
                            track["label"],
                            vector,
                            self.config.match_threshold,
                            self.config.match_margin,
                        )
                        if match:
                            matches[track_id] = (
                                match,
                                match.profile.name,
                                match.similarity,
                            )
                        else:
                            matches.pop(track_id, None)
                        if gate.observe(
                            track_id,
                            match.profile.id if match else None,
                            time.monotonic(),
                        ):
                            confirmed[track_id] = match
                            self.report(f"Access approved for {match.profile.name}")
                active_ids = {track["id"] for track in tracks}
                gate.retain(active_ids)
                for stale_id in set(embeddings) - active_ids:
                    embeddings.pop(stale_id, None)
                    matches.pop(stale_id, None)
                confirmed = {
                    track_id: approved
                    for track_id, approved in confirmed.items()
                    if track_id in active_ids
                    and (current := matches.get(track_id)) is not None
                    and current[0].profile.id == approved.profile.id
                }
                for track in tracks:
                    track["embedding"] = embeddings.get(track["id"])
                    if matched := matches.get(track["id"]):
                        track["match"], track["similarity"] = matched[1], matched[2]
                self._publish_scene(confirmed, {track["label"] for track in tracks})
                annotated = frame.copy()
                for track in tracks:
                    x1, y1, x2, y2 = track["box"]
                    matched = track.get("match")
                    color = (61, 214, 140) if matched else (234, 177, 66)
                    text = detection_caption(track)
                    cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
                    cv2.rectangle(
                        annotated,
                        (x1, max(0, y1 - 26)),
                        (x1 + 9 * len(text), y1),
                        color,
                        -1,
                    )
                    cv2.putText(
                        annotated,
                        text,
                        (x1 + 4, max(17, y1 - 7)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.52,
                        (12, 18, 24),
                        1,
                        cv2.LINE_AA,
                    )
                encoded = cv2.imencode(
                    ".jpg",
                    annotated,
                    [cv2.IMWRITE_JPEG_QUALITY, self.config.jpeg_quality],
                )[1].tobytes()
                with self.state_lock:
                    self.jpeg = encoded
                    self.tracks = tracks
                    self.frame_size = (frame.shape[1], frame.shape[0])
                    self.processed_frame = frame
                    self.processed_sequence = seen_sequence
        except Exception as error:
            with self.state_lock:
                self.vision_state = "failed"
                self.jpeg = self._message_frame(
                    f"{self.camera.name} failed - check configuration"
                )
            log.exception("Vision worker %s failed", self.camera.id)
            self.report(f"Vision failed: {error}", True)
        finally:
            self._publish_scene({}, set())
            self.stop_event.set()
            capture_thread.join(timeout=3)


class VisionManager:
    def __init__(self, database: Database, config: Config):
        self.database = database
        self.config = config
        self.door = DoorController(config, database)
        if not database.automations():
            database.create_automation(
                "Default smart door",
                default_door_graph(config.auto_close_seconds),
                enabled=True,
            )
        self.automation = AutomationEngine(
            database, self._automation_action, self._automation_state
        )
        self.devices = EWeLinkDeviceManager(
            database, EWELINK_CLOUD, event_sink=self.emit_event
        )
        self.workers: dict[int, VisionSystem] = {}
        self.lock = threading.RLock()
        self.started = False
        self.placeholder = VisionSystem._message_frame("Camera is disabled")
        self.enrollments = EnrollmentManager(
            database, database.path.parent / "enrollments", self.worker
        )

    def worker(self, camera_id: int) -> VisionSystem | None:
        with self.lock:
            return self.workers.get(camera_id)

    def start(self) -> None:
        self.door.start()
        self.automation.start()
        self.devices.start()
        self.enrollments.start_service()
        with self.lock:
            self.started = True
            if self.config.disable_vision:
                return
            for camera in self.database.cameras():
                if camera.enabled and camera.id not in self.workers:
                    worker = VisionSystem(
                        camera, self.config, self.database, self.emit_event
                    )
                    self.workers[camera.id] = worker
                    worker.start()

    def stop(self) -> None:
        self.enrollments.shutdown()
        with self.lock:
            workers = list(self.workers.values())
            self.workers.clear()
            self.started = False
        for worker in workers:
            worker.stop()

    def shutdown(self) -> None:
        self.stop()
        self.devices.stop()
        self.automation.stop()
        self.door.stop()

    def emit_event(self, kind: str, payload: dict) -> None:
        try:
            self.automation.emit(kind, payload)
        except Exception:
            log.exception("Automation event failed: %s", kind)

    def automation_resources(self) -> dict:
        devices = self.database.ewelink_devices()
        return {
            "camera_ids": {camera.id for camera in self.database.cameras()},
            "device_ids": {device.device_id for device in devices},
            "profile_ids": {profile.id for profile in self.database.all()},
            "device_capabilities": {
                device.device_id: device.capabilities for device in devices
            },
        }

    def _automation_state(self, field: str, config: dict, _context: dict):
        if field == "state.door":
            return self.door.status()["state"]
        if field in {"state.camera_online", "state.authorized_count"}:
            with self.lock:
                if config.get("camera_id") == "*":
                    workers = list(self.workers.values())
                    if field == "state.camera_online":
                        return any(worker.camera_state == "connected" for worker in workers)
                    return sum(worker.authorized_count for worker in workers)
                worker = self.workers.get(config.get("camera_id"))
            if field == "state.camera_online":
                return bool(worker and worker.camera_state == "connected")
            return worker.authorized_count if worker else 0
        if field == "state.ewelink_property":
            device = self.database.ewelink_device(config.get("device_id", ""))
            return self.devices._known_state(device)[0].get(config.get("property")) if device else None
        if field == "state.ewelink_online":
            device = self.database.ewelink_device(config.get("device_id", ""))
            return device.online if device else None
        return None

    def _automation_action(self, kind: str, config: dict, context: dict) -> dict:
        if kind == "action.log":
            message = config["message"]
            self.database.add_event("automation_log", message)
            log.info("Automation: %s", message)
            return {"logged": True}
        if kind.startswith("action.primary_door."):
            action = kind.rsplit(".", 1)[1]
            if action == "query":
                self.door.refresh_state()
                return self.door.status()
            if not self.door.configured:
                raise RuntimeError("Primary Door is not configured")
            target = "open" if action == "open" else "closed"
            if self.door.status()["state"] == target and not self.door.busy:
                return {"state": target, "unchanged": True}
            event = context.get("event", {})
            reason = str(event.get("profile_name") or event.get("kind") or "automation")
            if not self.door.trigger(reason, action=action):
                raise RuntimeError("Primary Door is busy or cooling down")
            deadline = time.monotonic() + 45
            while self.door.busy and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.door.busy:
                raise RuntimeError("Primary Door command timed out")
            if self.door.last_error:
                raise RuntimeError(self.door.last_error)
            return self.door.status()
        if kind.startswith("action.ewelink."):
            action = kind.rsplit(".", 1)[1]
            arguments = {key: value for key, value in config.items() if key != "device_id"}
            return self.devices.execute(config["device_id"], action, arguments)
        if kind in {"action.camera.enable", "action.camera.disable"}:
            camera = self.database.camera(config["camera_id"])
            if not camera:
                raise RuntimeError("Camera not found")
            enabled = kind.endswith("enable")
            updated = self.update_camera(
                camera.id,
                {
                    "name": camera.name,
                    "stream_url": camera.stream_url,
                    "username": camera.username,
                    "password": camera.password,
                    "enabled": enabled,
                },
            )
            return {"camera_id": updated.id, "enabled": updated.enabled}
        raise RuntimeError("Unsupported automation action")

    def _replace_worker(self, camera: Camera) -> None:
        with self.lock:
            previous = self.workers.pop(camera.id, None)
        if previous:
            previous.stop()
        if camera.enabled and self.started and not self.config.disable_vision:
            worker = VisionSystem(camera, self.config, self.database, self.emit_event)
            with self.lock:
                self.workers[camera.id] = worker
            worker.start()

    def add_camera(self, values: dict) -> Camera:
        camera = self.database.add_camera(**values)
        self._replace_worker(camera)
        return camera

    def update_camera(self, camera_id: int, values: dict) -> Camera | None:
        camera = self.database.update_camera(camera_id, **values)
        if camera:
            self._replace_worker(camera)
        return camera

    def delete_camera(self, camera_id: int) -> bool:
        camera = self.database.camera(camera_id)
        if not camera:
            return False
        with self.lock:
            worker = self.workers.pop(camera_id, None)
        if worker:
            worker.stop()
        return self.database.delete_camera(camera_id)

    def update_settings(self, values: dict) -> dict:
        old_config = self.config
        candidate = {**self.database.settings(), **values}
        new_config = _config(candidate)
        DoorController._validate(new_config)
        settings = self.database.update_settings(values)
        self.door.update(new_config)
        self.config = new_config
        vision_changed = (
            old_config.performance_mode,
            old_config.yolo_model,
            old_config.yolo_imgsz,
            old_config.confidence,
            old_config.embed_every,
            old_config.match_threshold,
            old_config.match_margin,
            old_config.confirmations,
            old_config.cooldown,
            old_config.jpeg_quality,
        ) != (
            new_config.performance_mode,
            new_config.yolo_model,
            new_config.yolo_imgsz,
            new_config.confidence,
            new_config.embed_every,
            new_config.match_threshold,
            new_config.match_margin,
            new_config.confirmations,
            new_config.cooldown,
            new_config.jpeg_quality,
        )
        if vision_changed:
            with self.lock:
                was_started = self.started
            self.stop()
            if was_started:
                self.start()
        return settings

    def reload_profiles(self) -> None:
        with self.lock:
            workers = list(self.workers.values())
        for worker in workers:
            worker.reload_profiles()

    def enroll(self, camera_id: int, x: float, y: float, name: str):
        with self.lock:
            worker = self.workers.get(camera_id)
        if not worker:
            return None
        selected = worker.selected_embedding(x, y)
        if not selected:
            return None
        label, embedding = selected
        profile = self.database.add(name, label, embedding)
        self.database.add_event(
            "profile_added",
            f"Whitelisted {profile.name} ({profile.label})",
            worker.camera,
            profile.id,
            profile.name,
            profile.label,
        )
        self.reload_profiles()
        return profile

    def remove_profile(self, profile_id: int) -> bool:
        profiles = {profile.id: profile for profile in self.database.all()}
        profile = profiles.get(profile_id)
        if not profile or not self.database.delete(profile_id):
            return False
        self.database.add_event(
            "profile_removed",
            f"Removed {profile.name} from whitelist",
            profile_id=profile.id,
            profile_name=profile.name,
            label=profile.label,
        )
        self.reload_profiles()
        return True

    def status(self) -> dict:
        cameras = []
        with self.lock:
            workers = dict(self.workers)
        for camera in self.database.cameras():
            if worker := workers.get(camera.id):
                cameras.append(worker.snapshot())
            else:
                cameras.append(
                    {
                        "id": camera.id,
                        "name": camera.name,
                        "enabled": camera.enabled,
                        "camera": "disabled" if not camera.enabled else "vision disabled",
                        "vision": "disabled",
                        "last_event": "Camera is not running",
                        "last_error": "",
                        "tracks": [],
                    }
                )
        sample_counts = self.database.profile_sample_counts()
        return {
            "cameras": cameras,
            "door": self.door.status(),
            "model": self.config.yolo_model,
            "threshold": self.config.match_threshold,
            "margin": self.config.match_margin,
            "profiles": [
                {
                    "id": profile.id,
                    "name": profile.name,
                    "label": profile.label,
                    "created_at": profile.created_at,
                    "descriptor": "spatial" if profile.embedding.size > 576 else "legacy",
                    "sample_count": sample_counts.get(profile.id, 0),
                }
                for profile in self.database.all()
            ],
        }

    def latest_jpeg(self, camera_id: int) -> bytes:
        with self.lock:
            worker = self.workers.get(camera_id)
        return worker.latest_jpeg() if worker else self.placeholder


MANAGER = VisionManager(DATABASE, CONFIG)


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class Enrollment(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    name: str = Field(min_length=1, max_length=60)


class EnrollmentCommit(BaseModel):
    sample_ids: list[str] = Field(min_length=1, max_length=64)
    profile_id: int | None = Field(default=None, ge=1)
    name: str = Field(default="", max_length=60)

    @model_validator(mode="after")
    def valid_target(self):
        self.name = self.name.strip()
        if self.profile_id is None and not self.name:
            raise ValueError("A name is required for a new identity")
        if any(
            len(sample_id) > 32
            or len(parts := sample_id.split("-")) != 2
            or not all(part.isdigit() for part in parts)
            for sample_id in self.sample_ids
        ):
            raise ValueError("Enrollment sample ID is invalid")
        return self


class DoorTest(BaseModel):
    confirm: bool
    action: str = Field(default="open", pattern=r"^(open|close)$")


class CameraPayload(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    stream_url: str = Field(min_length=8, max_length=1000)
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=200)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("camera name cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def valid_stream(self):
        clean_url, url_username, url_password = _camera_parts(self.stream_url)
        self.stream_url = clean_url
        self.username = self.username or url_username
        self.password = self.password or url_password
        return self


class CameraTestPayload(CameraPayload):
    camera_id: int | None = Field(default=None, ge=1)


class SettingsPayload(BaseModel):
    app_name: str = Field(min_length=1, max_length=40)
    brand_palette: str = Field(pattern=r"^(teal|blue|violet|orange)$")
    performance_mode: str = Field(pattern=r"^(auto|quality|low_power)$")
    yolo_model: str = Field(pattern=r"^yolo11[nsm]\.pt$")
    yolo_imgsz: int = Field(ge=320, le=1280)
    detection_confidence: float = Field(ge=0.05, le=0.95)
    embed_every: int = Field(ge=1, le=60)
    match_threshold: float = Field(ge=0.1, le=0.999)
    match_margin: float = Field(ge=0, le=0.25)
    match_confirmations: int = Field(ge=1, le=20)
    open_cooldown_seconds: float = Field(ge=0, le=3600)
    jpeg_quality: int = Field(ge=40, le=100)
    ewelink_model: str = Field(min_length=1, max_length=80)
    ewelink_host: str = Field(default="", max_length=45)
    ewelink_port: int = Field(ge=1, le=65535)
    ewelink_device_id: str = Field(
        default="", max_length=32, pattern=r"^$|^[A-Za-z0-9]{6,32}$"
    )
    ewelink_device_key: str = Field(default="", max_length=128)
    ewelink_open_channel: int = Field(ge=1, le=4)
    ewelink_close_channel: int = Field(ge=1, le=4)
    pulse_seconds: float = Field(ge=0.1, le=30)
    auto_close_seconds: float | None = Field(default=None, ge=0, le=3600)

    @model_validator(mode="after")
    def valid_door(self):
        self.app_name = self.app_name.strip()
        self.ewelink_model = self.ewelink_model.strip()
        self.ewelink_host = self.ewelink_host.strip()
        self.ewelink_device_id = self.ewelink_device_id.strip()
        self.ewelink_device_key = self.ewelink_device_key.strip()
        if not self.app_name:
            raise ValueError("app name cannot be blank")
        if not self.ewelink_model:
            raise ValueError("eWeLink model cannot be blank")
        if self.ewelink_open_channel == self.ewelink_close_channel:
            raise ValueError("open and close channels must be different")
        if self.ewelink_host:
            try:
                address = ipaddress.ip_address(self.ewelink_host)
            except ValueError as error:
                raise ValueError("eWeLink host must be a local IP address") from error
            if not (address.is_private or address.is_loopback):
                raise ValueError("eWeLink host must be on the local network")
        return self


class EWeLinkOAuthStart(BaseModel):
    app_id: str = Field(min_length=1, max_length=200)
    app_secret: str = Field(min_length=1, max_length=200)


class LogoPayload(BaseModel):
    image: str = Field(min_length=32, max_length=1_500_000)


class EWeLinkPasswordImport(BaseModel):
    account: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)
    country_code: str = Field(default="+351", pattern=r"^\+[1-9][0-9]{0,3}$")
    region: str = Field(default="eu", pattern=r"^(eu|us|as|cn)$")


class EWeLinkImportApply(BaseModel):
    session_id: str = Field(min_length=16, max_length=200)
    device_id: str = Field(pattern=r"^[A-Za-z0-9]{6,32}$")
    host: str = Field(default="", max_length=45)
    port: int = Field(default=8081, ge=1, le=65535)
    open_channel: int = Field(default=1, ge=1, le=4)
    close_channel: int = Field(default=2, ge=1, le=4)
    pulse_seconds: float = Field(default=1, ge=0.1, le=30)

    @model_validator(mode="after")
    def valid_relay(self):
        if self.open_channel == self.close_channel:
            raise ValueError("open and close channels must be different")
        self.host = self.host.strip()
        if self.host:
            try:
                address = ipaddress.ip_address(self.host)
            except ValueError as error:
                raise ValueError("relay host must be a local IP address") from error
            if not (address.is_private or address.is_loopback):
                raise ValueError("relay host must be on the local network")
            self.host = str(address)
        return self


class EWeLinkActionPayload(BaseModel):
    confirm: bool = False
    arguments: dict = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def valid_arguments(cls, value: dict) -> dict:
        if len(value) > 16 or len(json.dumps(value)) > 4096:
            raise ValueError("Device action arguments are too large")
        return value


class EWeLinkTestPayload(EWeLinkActionPayload):
    action: str = Field(pattern=r"^(switch|button|light|cover|number|enum|refresh)$")


class PrimaryDoorPayload(BaseModel):
    host: str = Field(default="", max_length=45)
    port: int = Field(default=8081, ge=1, le=65535)
    open_channel: int = Field(default=1, ge=1, le=4)
    close_channel: int = Field(default=2, ge=1, le=4)
    pulse_seconds: float = Field(default=1, ge=0.1, le=30)

    @model_validator(mode="after")
    def valid_primary_door(self):
        if self.open_channel == self.close_channel:
            raise ValueError("open and close channels must be different")
        self.host = self.host.strip()
        if self.host:
            try:
                address = ipaddress.ip_address(self.host)
            except ValueError as error:
                raise ValueError("relay host must be a local IP address") from error
            if not (address.is_private or address.is_loopback):
                raise ValueError("relay host must be on the local network")
            self.host = str(address)
        return self


class AutomationPayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = False
    graph: dict

    @model_validator(mode="after")
    def valid_document_size(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("automation name cannot be blank")
        if len(json.dumps(self.graph)) > 250_000:
            raise ValueError("automation graph is too large")
        return self


class AutomationValidationPayload(BaseModel):
    graph: dict


class ConfirmedAutomationRun(BaseModel):
    confirm: bool = False


@asynccontextmanager
async def lifespan(_app: FastAPI):
    MANAGER.start()
    yield
    MANAGER.shutdown()


app = FastAPI(title="VisionGate", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, error: RequestValidationError):
    return JSONResponse(
        {"detail": [{key: item[key] for key in ("type", "loc", "msg") if key in item} for item in error.errors()]},
        status_code=422,
    )


CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
PUBLIC_PATHS = {
    "/health",
    "/login",
    "/logo.png",
    "/login.js",
    "/visiongate.css",
    "/api/auth/login",
    "/api/brand",
    "/api/ewelink/oauth/callback",
}
allowed_hosts = {
    "localhost",
    "127.0.0.1",
    "::1",
    "testserver",
    "*.local",
    socket.gethostname(),
    socket.getfqdn(),
    *local_ipv4_addresses(),
    os.getenv("VISIONGATE_PUBLIC_HOST", "").strip(),
    *(host.strip() for host in os.getenv("VISIONGATE_ALLOWED_HOSTS", "").split(",")),
}
app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(filter(None, allowed_hosts)))


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    supplied = urlsplit(origin)
    expected = request.url
    supplied_port = supplied.port or (443 if supplied.scheme == "https" else 80)
    expected_port = expected.port or (443 if expected.scheme == "https" else 80)
    return (
        supplied.scheme,
        supplied.hostname,
        supplied_port,
    ) == (expected.scheme, expected.hostname, expected_port)


def _secure_cookie(request: Request) -> bool:
    if request.url.scheme == "https" or os.getenv("VISIONGATE_SECURE_COOKIES") != "1":
        return request.url.scheme == "https"
    try:
        address = ipaddress.ip_address(request.client.host)
        return not (address.is_private or address.is_loopback or address.is_link_local)
    except (AttributeError, ValueError):
        return True


@app.middleware("http")
async def secure_requests(request: Request, call_next):
    token = request.cookies.get(SESSION_COOKIE)
    session = AUTH.session(token)
    public = request.url.path in PUBLIC_PATHS
    if not public and not session:
        response = (
            RedirectResponse("/login", status_code=303)
            if request.url.path == "/"
            else JSONResponse({"detail": "Authentication required"}, status_code=401)
        )
    elif (
        session
        and request.method not in {"GET", "HEAD", "OPTIONS"}
        and not public
        and (not _same_origin(request) or not AUTH.valid_csrf(token, request.headers.get("x-csrf-token")))
    ):
        response = JSONResponse({"detail": "Invalid security token"}, status_code=403)
    else:
        request.state.session = session
        response = await call_next(request)
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }
    )
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


def _local_request(request: Request) -> bool:
    try:
        return bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
    except ValueError:
        return False


def _require_local_login(request: Request) -> None:
    if not _local_request(request):
        raise HTTPException(
            403,
            f"For account safety, open VisionGate at http://127.0.0.1:{APP_PORT} on its PC to import eWeLink devices",
        )


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if request.state.session:
        return RedirectResponse("/", status_code=303)
    return FileResponse(ROOT / "static" / "login.html")


@app.post("/api/auth/login")
def login(payload: LoginPayload, request: Request):
    if not AUTH.configured:
        raise HTTPException(503, "Login is not configured. Run Configure Login.bat on the VisionGate PC.")
    if not _same_origin(request):
        raise HTTPException(403, "Invalid request origin")
    client = request.client.host if request.client else "unknown"
    try:
        result = AUTH.login(payload.username, payload.password, client)
    except TooManyAttempts as error:
        raise HTTPException(
            429,
            f"Too many login attempts. Try again in {error.retry_after} seconds.",
            headers={"Retry-After": str(error.retry_after)},
        ) from error
    if not result:
        raise HTTPException(401, "Invalid username or password")
    token, csrf = result
    response = JSONResponse({"username": AUTH.username, "csrf_token": csrf})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=AUTH.session_lifetime_seconds,
        path="/",
        secure=_secure_cookie(request),
        httponly=True,
        samesite="strict",
    )
    return response


@app.get("/api/auth/session")
def auth_session(request: Request):
    return {
        "username": request.state.session.username,
        "csrf_token": request.state.session.csrf,
    }


@app.post("/api/auth/logout")
def logout(request: Request):
    AUTH.logout(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"logged_out": True})
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=_secure_cookie(request),
        httponly=True,
        samesite="strict",
    )
    return response


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def dashboard():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/automations", include_in_schema=False)
def automation_editor():
    return FileResponse(ROOT / "static" / "automations.html")


@app.get("/logo.png", include_in_schema=False)
def logo():
    custom = DATA_DIR / "logo.png"
    return FileResponse(custom if custom.exists() else ROOT / "media" / "logo.png", media_type="image/png")


@app.get("/api/brand")
def branding():
    settings = DATABASE.settings()
    logo_path = DATA_DIR / "logo.png"
    version = logo_path.stat().st_mtime_ns if logo_path.exists() else 0
    return {
        "name": settings.get("app_name", "VisionGate"),
        "palette": settings.get("brand_palette", "teal"),
        "logo": f"/logo.png?v={version}",
    }


@app.put("/api/branding/logo")
def update_logo(payload: LogoPayload):
    prefix = "data:image/png;base64,"
    if not payload.image.startswith(prefix):
        raise HTTPException(422, "Logo must be a PNG image")
    try:
        raw = base64.b64decode(payload.image[len(prefix) :], validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(422, "Logo must be a valid PNG image") from error
    if len(raw) > 1_000_000 or raw[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
        raise HTTPException(422, "Logo must be a valid PNG under 1 MB")
    width, height = int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    if not (32 <= width <= 2048 and 32 <= height <= 2048):
        raise HTTPException(422, "Logo dimensions must be between 32 and 2048 pixels")
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise HTTPException(422, "Logo must be a valid PNG image")
    encoded = cv2.imencode(".png", image)[1].tobytes()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = DATA_DIR / "logo.tmp.png"
    temporary.write_bytes(encoded)
    os.replace(temporary, DATA_DIR / "logo.png")
    return {"saved": True}


@app.get("/visiongate.css", include_in_schema=False)
def stylesheet():
    return FileResponse(ROOT / "static" / "visiongate.css", media_type="text/css")


@app.get("/login.js", include_in_schema=False)
def login_script():
    return FileResponse(ROOT / "static" / "login.js", media_type="text/javascript")


@app.get("/dashboard.js", include_in_schema=False)
def dashboard_script():
    return FileResponse(ROOT / "static" / "dashboard.js", media_type="text/javascript")


@app.get("/automations.js", include_in_schema=False)
def automations_script():
    return FileResponse(ROOT / "static" / "automations.js", media_type="text/javascript")


@app.get("/api/network")
def network_access():
    addresses = local_ipv4_addresses()
    return {
        "port": APP_PORT,
        "urls": [f"http://{address}:{APP_PORT}" for address in addresses],
    }


@app.get("/video/{camera_id}")
async def video(camera_id: int):
    if not DATABASE.camera(camera_id):
        raise HTTPException(404, "Camera not found")

    async def frames():
        while True:
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                + MANAGER.latest_jpeg(camera_id)
                + b"\r\n"
            )
            await asyncio.sleep(0.04)

    return StreamingResponse(
        frames(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/api/status")
def system_status():
    return MANAGER.status()


def _public_camera(camera: Camera) -> dict:
    return {
        "id": camera.id,
        "name": camera.name,
        "stream_url": camera.stream_url,
        "username": camera.username,
        "password_configured": bool(camera.password),
        "enabled": camera.enabled,
        "created_at": camera.created_at,
        "updated_at": camera.updated_at,
    }


def _public_settings(settings: dict) -> dict:
    public = dict(settings)
    public["ewelink_device_key_configured"] = bool(public.get("ewelink_device_key"))
    for key in (
        "ewelink_device_key",
        "ewelink_cloud_token",
        "ewelink_cloud_app_id",
        "ewelink_cloud_region",
        "ewelink_cloud_user_apikey",
        "door_last_state",
    ):
        public.pop(key, None)
    return public


@app.get("/api/config")
def configuration():
    return {
        "cameras": [_public_camera(camera) for camera in DATABASE.cameras()],
        "settings": _public_settings(DATABASE.settings()),
    }


@app.get("/api/devices")
def devices():
    camera_states = {
        camera["id"]: camera for camera in MANAGER.status().get("cameras", [])
    }
    return {
        "cameras": [
            {
                "id": camera.id,
                "name": camera.name,
                "enabled": camera.enabled,
                "camera": camera_states.get(camera.id, {}).get("camera", "unknown"),
                "vision": camera_states.get(camera.id, {}).get("vision", "unknown"),
            }
            for camera in MANAGER.database.cameras()
        ],
        "ewelink": MANAGER.devices.list_public(),
        "identities": [
            {"id": profile.id, "name": profile.name, "label": profile.label}
            for profile in MANAGER.database.all()
        ],
    }


@app.get("/api/ewelink/devices")
def ewelink_devices():
    return MANAGER.devices.list_public()


@app.post("/api/ewelink/devices/refresh")
def refresh_ewelink_devices():
    try:
        return MANAGER.devices.refresh()
    except (EWeLinkCloudError, RuntimeError, ValueError) as error:
        raise HTTPException(502, str(error)) from error


@app.get("/api/ewelink/devices/{device_id}")
def ewelink_device(device_id: str):
    device = MANAGER.database.ewelink_device(device_id)
    if not device:
        raise HTTPException(404, "eWeLink device not found")
    return MANAGER.devices.public(device)


@app.post("/api/ewelink/devices/{device_id}/primary-door")
def use_ewelink_device_as_primary_door(
    device_id: str, payload: PrimaryDoorPayload
):
    device = MANAGER.database.ewelink_device(device_id)
    if not device:
        raise HTTPException(404, "eWeLink device not found")
    channels = next(
        (
            capability.get("channels", [])
            for capability in device.capabilities
            if capability.get("type") == "channels"
        ),
        [],
    )
    if payload.open_channel not in channels or payload.close_channel not in channels:
        raise HTTPException(422, "Select open and close channels supported by this device")
    try:
        settings = MANAGER.update_settings(
            {
                "ewelink_model": device.model or device.name,
                "ewelink_host": payload.host or device.host,
                "ewelink_port": payload.port,
                "ewelink_device_id": device.device_id,
                "ewelink_device_key": device.device_key,
                "ewelink_open_channel": payload.open_channel,
                "ewelink_close_channel": payload.close_channel,
                "pulse_seconds": payload.pulse_seconds,
            }
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {
        "configured": True,
        "device": MANAGER.devices.public(device),
        "settings": _public_settings(settings),
    }


def _run_ewelink_action(
    device_id: str, action: str, payload: EWeLinkActionPayload
):
    if action != "refresh" and not payload.confirm:
        raise HTTPException(400, "Confirmation required")
    try:
        return MANAGER.devices.execute(device_id, action, payload.arguments)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(502, str(error)) from error


@app.post("/api/ewelink/devices/{device_id}/actions/{action}")
def run_ewelink_action(
    device_id: str,
    action: str,
    payload: EWeLinkActionPayload,
):
    if action not in {"switch", "button", "light", "cover", "number", "enum", "refresh"}:
        raise HTTPException(404, "Unsupported eWeLink action")
    return _run_ewelink_action(device_id, action, payload)


@app.post("/api/ewelink/devices/{device_id}/test")
def test_ewelink_device(device_id: str, payload: EWeLinkTestPayload):
    return _run_ewelink_action(device_id, payload.action, payload)


def _validated_automation(graph: dict) -> dict:
    try:
        return validate_graph(graph, MANAGER.automation_resources())
    except GraphValidationError as error:
        raise HTTPException(422, str(error)) from error


@app.get("/api/automations")
def automations():
    return [asdict(item) for item in MANAGER.database.automations()]


@app.post("/api/automations/validate")
def validate_automation(payload: AutomationValidationPayload):
    return {"valid": True, "graph": _validated_automation(payload.graph)}


@app.post("/api/automations", status_code=201)
def create_automation(payload: AutomationPayload):
    graph = _validated_automation(
        {**payload.graph, "name": payload.name, "enabled": payload.enabled}
    )
    try:
        item = MANAGER.database.create_automation(
            payload.name, graph, enabled=payload.enabled
        )
        MANAGER.automation.initialize_schedules()
        return asdict(item)
    except GraphValidationError as error:
        raise HTTPException(422, str(error)) from error


@app.get("/api/automations/{automation_id}")
def automation(automation_id: int):
    item = MANAGER.database.automation(automation_id)
    if not item:
        raise HTTPException(404, "Automation not found")
    return asdict(item)


@app.put("/api/automations/{automation_id}")
def update_automation(automation_id: int, payload: AutomationPayload):
    graph = _validated_automation(
        {**payload.graph, "name": payload.name, "enabled": payload.enabled}
    )
    try:
        item = MANAGER.database.update_automation(
            automation_id, payload.name, graph, payload.enabled
        )
    except GraphValidationError as error:
        raise HTTPException(422, str(error)) from error
    if not item:
        raise HTTPException(404, "Automation not found")
    MANAGER.automation.initialize_schedules()
    return asdict(item)


@app.delete("/api/automations/{automation_id}")
def delete_automation(automation_id: int):
    if not MANAGER.database.delete_automation(automation_id):
        raise HTTPException(404, "Automation not found")
    MANAGER.automation.initialize_schedules()
    return {"deleted": True}


@app.post("/api/automations/{automation_id}/validate")
def validate_saved_automation(automation_id: int):
    item = MANAGER.database.automation(automation_id)
    if not item:
        raise HTTPException(404, "Automation not found")
    return {"valid": True, "graph": _validated_automation(item.graph)}


@app.post("/api/automations/{automation_id}/dry-run")
def dry_run_automation(automation_id: int):
    try:
        run = MANAGER.automation.run_automation(
            automation_id, dry_run=True, wait=True
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return asdict(run)


@app.post("/api/automations/{automation_id}/run")
def run_automation(automation_id: int, payload: ConfirmedAutomationRun):
    if not payload.confirm:
        raise HTTPException(400, "Confirmation required")
    try:
        run = MANAGER.automation.run_automation(automation_id, wait=True)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return asdict(run)


@app.get("/api/automations/{automation_id}/runs")
def automation_runs(
    automation_id: int, limit: int = Query(default=100, ge=1, le=1000)
):
    if not MANAGER.database.automation(automation_id):
        raise HTTPException(404, "Automation not found")
    return [
        asdict(item)
        for item in MANAGER.database.automation_runs(automation_id, limit)
    ]


@app.post("/api/cameras", status_code=201)
def add_camera(payload: CameraPayload):
    return _public_camera(MANAGER.add_camera(payload.model_dump()))


@app.post("/api/cameras/test")
def test_camera_connection(payload: CameraTestPayload):
    password = payload.password
    if not password and payload.camera_id:
        existing = DATABASE.camera(payload.camera_id)
        if not existing:
            raise HTTPException(404, "Camera not found")
        password = existing.password
    url = camera_stream_url(payload.stream_url, payload.username, password)
    capture = _video_capture(url)
    try:
        if not capture.isOpened():
            raise HTTPException(422, _camera_connection_error())
        ok, frame = capture.read()
        if not ok or frame is None:
            raise HTTPException(422, "Camera connected but did not return a video frame")
    finally:
        capture.release()
    height, width = frame.shape[:2]
    return {"connected": True, "width": width, "height": height}


@app.put("/api/cameras/{camera_id}")
def update_camera(camera_id: int, payload: CameraPayload):
    existing = DATABASE.camera(camera_id)
    if not existing:
        raise HTTPException(404, "Camera not found")
    values = payload.model_dump()
    if not values["password"]:
        values["password"] = existing.password
    camera = MANAGER.update_camera(camera_id, values)
    if not camera:
        raise HTTPException(404, "Camera not found")
    return _public_camera(camera)


@app.delete("/api/cameras/{camera_id}")
def delete_camera(camera_id: int):
    if not MANAGER.delete_camera(camera_id):
        raise HTTPException(404, "Camera not found")
    return {"deleted": True}


@app.put("/api/settings")
def update_settings(payload: SettingsPayload):
    try:
        values = payload.model_dump(exclude_none=True)
        current = DATABASE.settings()
        if not values["ewelink_device_key"]:
            if values["ewelink_device_id"] == current.get("ewelink_device_id"):
                values.pop("ewelink_device_key")
            elif values["ewelink_device_id"]:
                raise ValueError("Enter the device key when changing the device ID")
        return _public_settings(MANAGER.update_settings(values))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@app.get("/api/ewelink/oauth/setup")
def ewelink_oauth_setup(request: Request):
    return {
        "callback_url": str(request.url_for("ewelink_oauth_callback")),
        "developer_url": "https://dev.ewelink.cc/",
        "local_login_allowed": _local_request(request),
    }


@app.post("/api/ewelink/oauth/start")
def ewelink_oauth_start(payload: EWeLinkOAuthStart, request: Request):
    _require_local_login(request)
    callback_url = str(request.url_for("ewelink_oauth_callback"))
    try:
        session_id, authorization_url = EWELINK_IMPORTS.begin_oauth(
            payload.app_id, payload.app_secret, callback_url
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {"session_id": session_id, "authorization_url": authorization_url}


@app.get("/api/ewelink/oauth/callback", response_class=HTMLResponse)
def ewelink_oauth_callback(
    state: str = Query(min_length=16, max_length=200),
    code: str | None = Query(default=None, max_length=500),
    region: str | None = Query(default=None, pattern=r"^(eu|us|as|cn)$"),
    error: str | None = Query(default=None, max_length=300),
):
    try:
        if error or not code or not region:
            EWELINK_IMPORTS.fail(state, error or "eWeLink authorization was cancelled")
        else:
            EWELINK_IMPORTS.complete_oauth(state, code, region, EWELINK_CLOUD)
    except KeyError as session_error:
        raise HTTPException(400, str(session_error)) from session_error
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>eWeLink connected</title>"
        "<h1>eWeLink authorization received</h1>"
        "<p>You can close this window and return to VisionGate.</p>"
    )


@app.get("/api/ewelink/import/{session_id}")
def ewelink_import_status(session_id: str):
    try:
        return EWELINK_IMPORTS.status(session_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@app.post("/api/ewelink/import/password")
def ewelink_password_import(payload: EWeLinkPasswordImport, request: Request):
    _require_local_login(request)
    try:
        devices = add_lan_addresses(
            EWELINK_CLOUD.account_devices(
                payload.account.strip(),
                payload.password,
                payload.country_code,
                payload.region,
            )
        )
    except (EWeLinkCloudError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
    if not devices:
        raise HTTPException(404, "No compatible eWeLink devices were returned")
    session_id = EWELINK_IMPORTS.ready(devices)
    return {"session_id": session_id, **EWELINK_IMPORTS.status(session_id)}


@app.post("/api/ewelink/import/apply")
def ewelink_import_apply(payload: EWeLinkImportApply):
    try:
        device, devices = EWELINK_IMPORTS.take_all(payload.session_id, payload.device_id)
        discovered_host = str(device.get("host") or "")
        selected_host = discovered_host or (
            "" if device.get("_cloud_token") else payload.host
        )
        selected_port = payload.port or int(device.get("port") or 8081)
        device["host"], device["port"] = selected_host, selected_port
        MANAGER.devices.import_devices(devices)
        MANAGER.update_settings(
            {
                "ewelink_model": device["model"],
                "ewelink_host": selected_host,
                "ewelink_port": selected_port,
                "ewelink_device_id": device["id"],
                "ewelink_device_key": device["device_key"],
                "ewelink_cloud_token": device.get("_cloud_token", ""),
                "ewelink_cloud_app_id": device.get("_cloud_app_id", ""),
                "ewelink_cloud_region": device.get("_cloud_region", ""),
                "ewelink_open_channel": payload.open_channel,
                "ewelink_close_channel": payload.close_channel,
                "pulse_seconds": payload.pulse_seconds,
            }
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {
        "configured": True,
        "device_id": device["id"],
        "name": device["name"],
        "mode": "lan" if selected_host else "cloud",
    }


@app.post("/api/cameras/{camera_id}/enrollment/start", status_code=201)
def start_enrollment(camera_id: int):
    if not DATABASE.camera(camera_id):
        raise HTTPException(404, "Camera not found")
    try:
        return MANAGER.enrollments.start(camera_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/enrollments/{session_id}/stop")
def stop_enrollment(session_id: str):
    try:
        return MANAGER.enrollments.stop(session_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@app.get("/api/enrollments/{session_id}")
def enrollment_review(session_id: str):
    try:
        return MANAGER.enrollments.metadata(session_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@app.get("/api/enrollments/{session_id}/frames/{frame_id}.jpg")
def enrollment_frame(session_id: str, frame_id: int):
    try:
        path = MANAGER.enrollments.frame_path(session_id, frame_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "Content-Disposition": "inline"},
    )


@app.get("/api/enrollments/{session_id}/samples/{sample_id}.jpg")
def enrollment_sample_thumbnail(session_id: str, sample_id: str):
    try:
        content = MANAGER.enrollments.sample_thumbnail(session_id, sample_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    return Response(
        content,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "Content-Disposition": "inline"},
    )


@app.post("/api/enrollments/{session_id}/commit", status_code=201)
def commit_enrollment(session_id: str, payload: EnrollmentCommit):
    try:
        result = MANAGER.enrollments.commit(
            session_id,
            sample_ids=payload.sample_ids,
            profile_id=payload.profile_id,
            name=payload.name,
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    profile = result["profile"]
    DATABASE.add_event(
        "profile_samples_added",
        f"Added {result['added']} visual sample(s) for {profile['name']}",
        profile_id=profile["id"],
        profile_name=profile["name"],
        label=profile["label"],
    )
    MANAGER.reload_profiles()
    return result


@app.delete("/api/enrollments/{session_id}")
def cancel_enrollment(session_id: str):
    try:
        MANAGER.enrollments.metadata(session_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    MANAGER.enrollments.cancel(session_id)
    return {"deleted": True}


@app.get("/api/profiles/{profile_id}/samples")
def profile_samples(profile_id: int):
    if not any(profile.id == profile_id for profile in DATABASE.all()):
        raise HTTPException(404, "Profile not found")
    return [
        {
            "id": sample.id,
            "profile_id": sample.profile_id,
            "label": sample.label,
            "created_at": sample.created_at,
            "thumbnail_url": f"/api/profiles/{profile_id}/samples/{sample.id}/thumbnail"
            if sample.thumbnail
            else None,
        }
        for sample in DATABASE.profile_samples(profile_id)
    ]


@app.get("/api/profiles/{profile_id}/samples/{sample_id}/thumbnail")
def profile_sample_thumbnail(profile_id: int, sample_id: int):
    sample = next(
        (
            item
            for item in DATABASE.profile_samples(profile_id)
            if item.id == sample_id and item.thumbnail
        ),
        None,
    )
    if not sample:
        raise HTTPException(404, "Sample thumbnail not found")
    return Response(
        sample.thumbnail,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=300", "Content-Disposition": "inline"},
    )


@app.delete("/api/profiles/{profile_id}/samples/{sample_id}")
def delete_profile_sample(profile_id: int, sample_id: int):
    samples = DATABASE.profile_samples(profile_id)
    if not any(sample.id == sample_id for sample in samples):
        raise HTTPException(404, "Sample not found")
    if len(samples) <= 1:
        raise HTTPException(409, "An identity must keep at least one sample")
    if not DATABASE.delete_sample(profile_id, sample_id):
        raise HTTPException(409, "Sample could not be removed")
    MANAGER.reload_profiles()
    return {"deleted": True}


@app.post("/api/cameras/{camera_id}/whitelist", status_code=201)
def whitelist(camera_id: int, enrollment: Enrollment):
    name = enrollment.name.strip()
    if not name:
        raise HTTPException(422, "Name cannot be blank")
    if not DATABASE.camera(camera_id):
        raise HTTPException(404, "Camera not found")
    profile = MANAGER.enroll(camera_id, enrollment.x, enrollment.y, name)
    if profile is None:
        raise HTTPException(409, "No tracked object with a visual embedding at that point")
    return {"id": profile.id, "name": profile.name, "label": profile.label}


@app.delete("/api/whitelist/{profile_id}")
def remove_from_whitelist(profile_id: int):
    if not MANAGER.remove_profile(profile_id):
        raise HTTPException(404, "Profile not found")
    return {"deleted": True}


@app.get("/api/events")
def events(limit: int = Query(100, ge=1, le=1000)):
    return [asdict(event) for event in DATABASE.events(limit)]


@app.delete("/api/events")
def clear_events():
    return {"deleted": DATABASE.clear_events()}


@app.post("/api/door/refresh")
def refresh_door_state():
    try:
        MANAGER.devices.refresh()
    except Exception:
        log.warning("eWeLink inventory refresh failed during door state check")
    MANAGER.door.refresh_state()
    return MANAGER.door.status()


@app.post("/api/door/test")
def test_door(request: DoorTest):
    if not request.confirm:
        raise HTTPException(400, "Confirmation required")
    if not MANAGER.door.configured:
        raise HTTPException(503, "Door integration is not configured")
    if not MANAGER.door.trigger(f"manual {request.action} test", action=request.action):
        raise HTTPException(409, "Door controller is busy or cooling down")
    deadline = time.monotonic() + 45
    while MANAGER.door.busy and time.monotonic() < deadline:
        time.sleep(0.05)
    if MANAGER.door.busy:
        raise HTTPException(504, "Door command timed out; check the relay state")
    if MANAGER.door.last_error:
        raise HTTPException(502, MANAGER.door.last_error)
    return {"completed": True, "action": request.action}
