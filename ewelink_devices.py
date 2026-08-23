from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from core import ewelink_request
from ewelink_cloud import cloud_device_request, typed_device_action


log = logging.getLogger("visiongate.ewelink")


class EWeLinkDeviceManager:
    POLL_SECONDS = 60

    def __init__(self, database, cloud, *, opener=urlopen, event_sink=None):
        self.database = database
        self.cloud = cloud
        self.opener = opener
        self.event_sink = event_sink or (lambda _kind, _payload: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._live_thread: threading.Thread | None = None
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @staticmethod
    def _known_state(device) -> tuple[dict, dict]:
        known_keys: set[str] = set()
        for capability in device.capabilities:
            capability_id = capability.get("id")
            capability_type = capability.get("type")
            if capability_type == "channels":
                known_keys.add("switches")
            elif capability_type == "light":
                known_keys.update(
                    key
                    for key in (
                        capability.get("brightness_key"),
                        capability.get("switch_key"),
                        *(capability.get("rgb_keys") or []),
                        *capability.get("rgb_extras", {}).keys(),
                        *capability.get("brightness_extras", {}).keys(),
                    )
                    if isinstance(key, str)
                )
            elif capability_type == "cover":
                known_keys.update(
                    key
                    for key in (
                        capability.get("action_key"),
                        capability.get("position_key"),
                        capability.get("position_command_key"),
                    )
                    if isinstance(key, str)
                )
            elif isinstance(capability_id, str):
                known_keys.add(capability_id)
        state = {key: value for key, value in device.params.items() if key in known_keys}
        for item in state.get("switches", []):
            if (
                isinstance(item, dict)
                and isinstance(item.get("outlet"), int)
                and item.get("switch") in {"on", "off"}
            ):
                state[f"channel_{item['outlet'] + 1}"] = item["switch"]
        diagnostics = {
            key: value
            for key, value in device.params.items()
            if key not in known_keys
            and not any(secret in key.lower() for secret in ("password", "token", "key", "secret"))
        }
        return state, diagnostics

    def public(self, device, cloud_available: bool | None = None) -> dict:
        state, diagnostics = self._known_state(device)
        if cloud_available is None:
            settings = self.database.settings()
            cloud_available = all(
                settings.get(key)
                for key in (
                    "ewelink_cloud_token",
                    "ewelink_cloud_app_id",
                    "ewelink_cloud_region",
                )
            )
        return {
            "id": device.device_id,
            "name": device.name,
            "model": device.model,
            "uiid": device.uiid,
            "host": device.host,
            "port": device.port,
            "online": device.online,
            "available": device.available,
            "connections": {
                "lan": bool(device.host),
                "cloud": cloud_available,
            },
            "capabilities": device.capabilities,
            "state": state,
            "diagnostics": diagnostics,
            "last_seen": device.last_seen,
            "last_sync": device.last_sync,
        }

    def list_public(self) -> list[dict]:
        settings = self.database.settings()
        cloud_available = all(
            settings.get(key)
            for key in (
                "ewelink_cloud_token",
                "ewelink_cloud_app_id",
                "ewelink_cloud_region",
            )
        )
        return [
            self.public(device, cloud_available)
            for device in self.database.ewelink_devices()
        ]

    def import_devices(self, devices: list[dict]) -> list[dict]:
        if not devices:
            raise ValueError("No eWeLink devices were returned")
        credentials = next(
            (
                device
                for device in devices
                if all(
                    device.get(key)
                    for key in ("_cloud_token", "_cloud_app_id", "_cloud_region")
                )
            ),
            None,
        )
        if credentials:
            self.database.update_settings(
                {
                    "ewelink_cloud_token": credentials["_cloud_token"],
                    "ewelink_cloud_app_id": credentials["_cloud_app_id"],
                    "ewelink_cloud_region": credentials["_cloud_region"],
                    **(
                        {
                            "ewelink_cloud_user_apikey": credentials[
                                "_cloud_user_apikey"
                            ]
                        }
                        if credentials.get("_cloud_user_apikey")
                        else {}
                    ),
                }
            )
        self.database.sync_ewelink_devices(devices)
        return self.list_public()

    @staticmethod
    def _decode_response(response) -> dict:
        if getattr(response, "status", 200) >= 300:
            raise RuntimeError(f"eWeLink returned HTTP {response.status}")
        try:
            payload = json.loads(response.read() or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RuntimeError("eWeLink returned an invalid response") from error
        if not isinstance(payload, dict) or payload.get("error", 0):
            raise RuntimeError(
                f"eWeLink rejected the command ({payload.get('error', 'invalid response') if isinstance(payload, dict) else 'invalid response'})"
            )
        return payload

    def _open(self, request) -> dict:
        try:
            with self.opener(request, timeout=5) as response:
                return self._decode_response(response)
        except HTTPError as error:
            error.close()
            raise RuntimeError(f"eWeLink returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise RuntimeError(str(error)) from error

    def _send(self, device, specification: dict) -> str:
        lan_error = None
        lan = specification.get("lan")
        if device.host and lan and lan.get("command") == "switches":
            try:
                self._open(
                    ewelink_request(
                        device.host,
                        device.port,
                        device.device_id,
                        device.device_key,
                        lan["channel"],
                        lan["state"],
                    )
                )
                return "lan"
            except (RuntimeError, ValueError) as error:
                lan_error = error
        settings = self.database.settings()
        cloud = (
            settings.get("ewelink_cloud_token"),
            settings.get("ewelink_cloud_app_id"),
            settings.get("ewelink_cloud_region"),
        )
        if all(cloud):
            try:
                self._open(
                    cloud_device_request(
                        cloud[0], cloud[1], cloud[2], device.device_id, specification["params"]
                    )
                )
                return "cloud"
            except (RuntimeError, ValueError) as cloud_error:
                if lan_error:
                    raise RuntimeError(
                        f"LAN failed ({lan_error}); cloud failed ({cloud_error})"
                    ) from cloud_error
                raise
        if lan_error:
            raise lan_error
        raise RuntimeError("No supported eWeLink LAN or cloud connection is available")

    @staticmethod
    def _merge_params(current: dict, updates: dict) -> dict:
        merged = json.loads(json.dumps(current))
        if "switches" in updates:
            switches = {
                int(item["outlet"]): dict(item)
                for item in merged.get("switches", [])
                if isinstance(item, dict) and isinstance(item.get("outlet"), int)
            }
            for item in updates["switches"]:
                switches[int(item["outlet"])] = dict(item)
            merged["switches"] = [switches[key] for key in sorted(switches)]
        merged.update({key: value for key, value in updates.items() if key != "switches"})
        return merged

    def _emit_changes(self, before, after) -> None:
        if before.online != after.online and after.online is not None:
            self.event_sink(
                "trigger.ewelink.connection",
                {"device_id": after.device_id, "online": after.online},
            )
        before_state, _before_diagnostics = self._known_state(before)
        after_state, _after_diagnostics = self._known_state(after)
        for key, value in after_state.items():
            if key != "switches" and before_state.get(key) != value:
                self.event_sink(
                    "trigger.ewelink.property_changed",
                    {"device_id": after.device_id, "property": key, "value": value},
                )

    def execute(self, device_id: str, action: str, arguments: dict) -> dict:
        device = self.database.ewelink_device(device_id)
        if not device:
            raise KeyError("eWeLink device not found")
        if action == "refresh":
            self.refresh()
            refreshed = self.database.ewelink_device(device_id)
            return self.public(refreshed)
        specification = typed_device_action(device.capabilities, action, arguments)
        with self._guard:
            lock = self._locks.setdefault(device_id, threading.Lock())
        with lock:
            updates = specification["params"]
            if action == "button":
                off_arguments = {**arguments, "state": "off"}
                off = typed_device_action(device.capabilities, "switch", off_arguments)
                try:
                    mode = self._send(device, specification)
                except Exception:
                    try:
                        self._send(device, off)
                    except Exception:
                        log.exception("eWeLink button safety shutoff failed")
                    raise
                time.sleep(specification["pulse_seconds"])
                self._send(device, off)
                updates = off["params"]
            else:
                mode = self._send(device, specification)
            merged = self._merge_params(device.params, updates)
            updated = self.database.update_ewelink_device_state(
                device_id, merged, device.online
            )
        self._emit_changes(device, updated)
        result = self.public(updated)
        result["control_mode"] = mode
        return result

    def refresh(self) -> list[dict]:
        settings = self.database.settings()
        credentials = (
            settings.get("ewelink_cloud_app_id"),
            settings.get("ewelink_cloud_token"),
            settings.get("ewelink_cloud_region"),
        )
        if not self.cloud or not all(credentials):
            return self.list_public()
        previous = {item.device_id: item for item in self.database.ewelink_devices()}
        devices = self.cloud.token_devices(*credentials)
        self.database.sync_ewelink_devices(devices)
        for current in self.database.ewelink_devices():
            if before := previous.get(current.device_id):
                self._emit_changes(before, current)
        return self.list_public()

    def apply_cloud_update(self, payload: dict) -> None:
        device_id = payload.get("deviceid")
        params = payload.get("params")
        if not isinstance(device_id, str) or not isinstance(params, dict):
            return
        before = self.database.ewelink_device(device_id)
        if not before:
            return
        online = before.online
        updates = params
        if payload.get("action") == "sysmsg" and type(params.get("online")) is bool:
            online = params["online"]
            updates = {key: value for key, value in params.items() if key != "online"}
        merged = self._merge_params(before.params, updates)
        after = self.database.update_ewelink_device_state(device_id, merged, online)
        self._emit_changes(before, after)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll, daemon=True, name="ewelink-inventory"
        )
        self._thread.start()
        if self.cloud:
            self._live_thread = threading.Thread(
                target=self._live, daemon=True, name="ewelink-live"
            )
            self._live_thread.start()

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as error:
                log.warning("eWeLink inventory refresh failed: %s", error)
            self._stop.wait(self.POLL_SECONDS)

    def _live(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                settings = self.database.settings()
                credentials = (
                    settings.get("ewelink_cloud_token"),
                    settings.get("ewelink_cloud_app_id"),
                    settings.get("ewelink_cloud_region"),
                    settings.get("ewelink_cloud_user_apikey"),
                )
                if not all(credentials):
                    failures = 0
                    self._stop.wait(5)
                    continue
                self.cloud.listen_updates(
                    *credentials, self._stop, self.apply_cloud_update
                )
                failures = 0
            except Exception as error:
                failures += 1
                log.warning("eWeLink live connection failed: %s", error)
            if not self._stop.is_set():
                self._stop.wait(min(15 * 2 ** max(0, failures - 1), 15 * 60))

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        live_thread = self._live_thread
        if live_thread and live_thread is not threading.current_thread():
            live_thread.join(timeout=2)
        self._thread = None
        self._live_thread = None
