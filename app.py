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
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator

from auth import AuthManager, TooManyAttempts
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


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("visiongate")
AUTH = AuthManager.from_environment()
SESSION_COOKIE = "vg"


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
        self._last_authorized_seen = float("-inf")
        self._auto_close_armed = False
        self._auto_close_timer: threading.Timer | None = None
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._last_state_check: float | None = None
        self._state_check_error = ""
        self.last_event = "Waiting for an approved target"
        self.last_error = ""
        self._state = str(database.settings().get("door_last_state", ""))
        if self._state not in {"open", "closed", "unknown"}:
            recent_command = next(
                (
                    event.kind
                    for event in database.events(1000)
                    if event.kind in {"door_open", "door_close"}
                ),
                "",
            )
            self._state = {"door_open": "open", "door_close": "closed"}.get(
                recent_command, "unknown"
            )
            if self._state != "unknown":
                database.update_settings({"door_last_state": self._state})
        self._validate(config)

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
            if self._auto_close_armed:
                self._cancel_auto_close_locked()
                if config.auto_close_seconds:
                    self._auto_close_armed = True
                    self._schedule_auto_close_locked()
            target_changed = self._target(old) != self._target(config)
            if target_changed:
                self._state = "unknown"
                self.database.update_settings({"door_last_state": "unknown"})
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

    def _cancel_auto_close_locked(self) -> None:
        if self._auto_close_timer:
            self._auto_close_timer.cancel()
        self._auto_close_timer = None
        self._auto_close_armed = False

    def _schedule_auto_close_locked(self, delay: float | None = None) -> None:
        config = self._config
        if not self._auto_close_armed or not config.auto_close_seconds:
            return
        if delay is None:
            delay = max(
                0.01,
                self._last_authorized_seen + config.auto_close_seconds - time.monotonic(),
            )
        timer = threading.Timer(delay, self._auto_close_check)
        timer.daemon = True
        self._auto_close_timer = timer
        timer.start()

    def _arm_auto_close(self) -> None:
        with self._guard:
            if not self._config.auto_close_seconds:
                return
            if self._last_authorized_seen == float("-inf"):
                self._last_authorized_seen = time.monotonic()
            self._cancel_auto_close_locked()
            self._auto_close_armed = True
            self._schedule_auto_close_locked()

    def _auto_close_check(self) -> None:
        with self._guard:
            if not self._auto_close_armed:
                return
            remaining = (
                self._last_authorized_seen
                + self._config.auto_close_seconds
                - time.monotonic()
            )
            if remaining > 0:
                self._schedule_auto_close_locked(remaining)
                return
            self._auto_close_timer = None
        if not self.trigger("last authorized target left", action="close"):
            with self._guard:
                if self._configured(self._config):
                    self._schedule_auto_close_locked(0.25)
                else:
                    self._cancel_auto_close_locked()

    def authorized_seen(self) -> None:
        with self._guard:
            self._last_authorized_seen = time.monotonic()

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
            self._cancel_auto_close_locked()

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
            auto_close_armed = self._auto_close_armed
            auto_close_remaining = (
                max(
                    0.0,
                    self._last_authorized_seen
                    + config.auto_close_seconds
                    - time.monotonic(),
                )
                if auto_close_armed
                else None
            )
        return {
            "configured": self._configured(config),
            "mode": "lan" if config.ewelink_host else "cloud",
            "busy": self.busy,
            "model": config.ewelink_model,
            "open_channel": config.ewelink_open_channel,
            "close_channel": config.ewelink_close_channel,
            "auto_close_seconds": config.auto_close_seconds,
            "auto_close_armed": auto_close_armed,
            "auto_close_remaining": auto_close_remaining,
            "state": state,
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
            else:
                self._cancel_auto_close_locked()
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

    def refresh_state(self) -> bool:
        with self._guard:
            config = self._config
        if not self._configured(config):
            return False
        try:
            switches = self._query_switches(config)
            open_on = switches.get(config.ewelink_open_channel - 1) == "on"
            close_on = switches.get(config.ewelink_close_channel - 1) == "on"
            with self._guard:
                if open_on != close_on:
                    self._state = "open" if open_on else "closed"
                    self.database.update_settings({"door_last_state": self._state})
                recovered = bool(self._state_check_error)
                self._state_check_error = ""
                self._last_state_check = time.time()
            if recovered:
                log.info("eWeLink relay state check recovered")
            return True
        except (HTTPError, URLError, OSError, RuntimeError, ValueError) as error:
            if isinstance(error, HTTPError):
                error.close()
            message = str(error)
            with self._guard:
                changed = message != self._state_check_error
                self._state_check_error = message
            if changed:
                log.warning("eWeLink relay state check failed: %s", error)
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
            self._send(config, channel, "on")
            state = "open" if action == "open" else "closed"
            with self._guard:
                self._state = state
            self.database.update_settings({"door_last_state": state})
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
            for attempt in range(3):
                try:
                    self._send(config, channel, "off")
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
            if failure:
                message = f"Door control failed: {failure}"
                self.last_error = message
                self._record("door_error", message, camera, match)
                log.error(message)
            self._busy.release()
            if action == "open" and match is not None and failure is None:
                self._arm_auto_close()


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
        door: DoorController,
    ):
        self.camera = camera
        self.config = config
        self.database = database
        self.door = door
        self.stop_event = threading.Event()
        self.state_lock = threading.Lock()
        self.capture_lock = threading.Lock()
        self.raw_frame: np.ndarray | None = None
        self.raw_sequence = 0
        self.jpeg = self._message_frame(f"Starting {camera.name}...")
        self.tracks: list[dict] = []
        self.frame_size = (1280, 720)
        self.camera_state = "starting"
        self.vision_state = "starting"
        self.last_event = "Waiting for the camera"
        self.last_error = ""
        self.profiles = database.all()
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
        profiles = self.database.all()
        with self.state_lock:
            self.profiles = profiles

    def selected_embedding(self, x: float, y: float) -> tuple[str, np.ndarray] | None:
        with self.state_lock:
            width, height = self.frame_size
            track = select_track(self.tracks, x, y, width, height)
            if track is None:
                return None
            return track["label"], track["embedding"].copy()

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
                with self.state_lock:
                    self.camera_state = "unavailable"
                if not failure_reported:
                    self.report(
                        _camera_connection_error(),
                        True,
                    )
                    failure_reported = True
                capture.release()
                self.stop_event.wait(2)
                continue
            with self.state_lock:
                self.camera_state = "connected"
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
            with self.state_lock:
                self.camera_state = "reconnecting"
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
                            if self.door.trigger(match.profile.name, self.camera, match):
                                self.report(f"Access approved for {match.profile.name}")
                active_ids = {track["id"] for track in tracks}
                gate.retain(active_ids)
                for stale_id in set(embeddings) - active_ids:
                    embeddings.pop(stale_id, None)
                    matches.pop(stale_id, None)
                for track in tracks:
                    track["embedding"] = embeddings.get(track["id"])
                    if matched := matches.get(track["id"]):
                        track["match"], track["similarity"] = matched[1], matched[2]
                if any(track.get("match") for track in tracks):
                    self.door.authorized_seen()
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
        except Exception as error:
            with self.state_lock:
                self.vision_state = "failed"
                self.jpeg = self._message_frame(
                    f"{self.camera.name} failed - check configuration"
                )
            log.exception("Vision worker %s failed", self.camera.id)
            self.report(f"Vision failed: {error}", True)
        finally:
            self.stop_event.set()
            capture_thread.join(timeout=3)


class VisionManager:
    def __init__(self, database: Database, config: Config):
        self.database = database
        self.config = config
        self.door = DoorController(config, database)
        self.workers: dict[int, VisionSystem] = {}
        self.lock = threading.RLock()
        self.started = False
        self.placeholder = VisionSystem._message_frame("Camera is disabled")

    def start(self) -> None:
        self.door.start()
        with self.lock:
            self.started = True
            if self.config.disable_vision:
                return
            for camera in self.database.cameras():
                if camera.enabled and camera.id not in self.workers:
                    worker = VisionSystem(camera, self.config, self.database, self.door)
                    self.workers[camera.id] = worker
                    worker.start()

    def stop(self) -> None:
        with self.lock:
            workers = list(self.workers.values())
            self.workers.clear()
            self.started = False
        for worker in workers:
            worker.stop()

    def shutdown(self) -> None:
        self.stop()
        self.door.stop()

    def _replace_worker(self, camera: Camera) -> None:
        with self.lock:
            previous = self.workers.pop(camera.id, None)
        if previous:
            previous.stop()
        if camera.enabled and self.started and not self.config.disable_vision:
            worker = VisionSystem(camera, self.config, self.database, self.door)
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
                }
                for profile in self.database.all()
            ],
        }

    def latest_jpeg(self, camera_id: int) -> bytes:
        with self.lock:
            worker = self.workers.get(camera_id)
        return worker.latest_jpeg() if worker else self.placeholder


MANAGER = VisionManager(DATABASE, CONFIG)
EWELINK_CLOUD = EWeLinkCloud()
EWELINK_IMPORTS = ImportSessions()


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class Enrollment(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    name: str = Field(min_length=1, max_length=60)


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
    auto_close_seconds: float = Field(ge=0, le=3600)

    @model_validator(mode="after")
    def valid_door(self):
        self.app_name = self.app_name.strip()
        self.ewelink_model = self.ewelink_model.strip()
        self.ewelink_host = self.ewelink_host.strip()
        self.ewelink_device_id = self.ewelink_device_id.strip()
        self.ewelink_device_key = self.ewelink_device_key.strip()
        identity = (self.ewelink_device_id, self.ewelink_device_key)
        if not self.app_name:
            raise ValueError("app name cannot be blank")
        if not self.ewelink_model:
            raise ValueError("eWeLink model cannot be blank")
        if any(identity) and not all(identity):
            raise ValueError("eWeLink device ID and device key are required together")
        if self.ewelink_host and not all(identity):
            raise ValueError("eWeLink IP requires a device ID and device key")
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    MANAGER.start()
    yield
    MANAGER.shutdown()


app = FastAPI(title="VisionGate", docs_url=None, redoc_url=None, lifespan=lifespan)


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
            "For account safety, open VisionGate at http://127.0.0.1:8000 on its PC to import eWeLink devices",
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


@app.get("/api/network")
def network_access():
    addresses = local_ipv4_addresses()
    return {
        "port": 8000,
        "urls": [f"http://{address}:8000" for address in addresses],
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


@app.get("/api/config")
def configuration():
    settings = DATABASE.settings()
    for key in (
        "ewelink_cloud_token",
        "ewelink_cloud_app_id",
        "ewelink_cloud_region",
        "door_last_state",
    ):
        settings.pop(key, None)
    return {
        "cameras": [asdict(camera) for camera in DATABASE.cameras()],
        "settings": settings,
    }


@app.post("/api/cameras", status_code=201)
def add_camera(payload: CameraPayload):
    return asdict(MANAGER.add_camera(payload.model_dump()))


@app.post("/api/cameras/test")
def test_camera_connection(payload: CameraPayload):
    url = camera_stream_url(payload.stream_url, payload.username, payload.password)
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
    camera = MANAGER.update_camera(camera_id, payload.model_dump())
    if not camera:
        raise HTTPException(404, "Camera not found")
    return asdict(camera)


@app.delete("/api/cameras/{camera_id}")
def delete_camera(camera_id: int):
    if not MANAGER.delete_camera(camera_id):
        raise HTTPException(404, "Camera not found")
    return {"deleted": True}


@app.put("/api/settings")
def update_settings(payload: SettingsPayload):
    try:
        settings = MANAGER.update_settings(payload.model_dump())
        for key in ("ewelink_cloud_token", "ewelink_cloud_app_id", "ewelink_cloud_region"):
            settings.pop(key, None)
        return settings
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
        device = EWELINK_IMPORTS.take(payload.session_id, payload.device_id)
        MANAGER.update_settings(
            {
                "ewelink_model": device["model"],
                "ewelink_host": payload.host,
                "ewelink_port": payload.port,
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
    }


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
