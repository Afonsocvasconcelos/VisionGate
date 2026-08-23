from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import string
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_HOSTS = {
    "cn": "https://cn-apia.coolkit.cn",
    "as": "https://as-apia.coolkit.cc",
    "us": "https://us-apia.coolkit.cc",
    "eu": "https://eu-apia.coolkit.cc",
}
WS_DISPATCH = {
    "cn": "https://cn-dispa.coolkit.cn/dispatch/app",
    "as": "https://as-dispa.coolkit.cc/dispatch/app",
    "us": "https://us-dispa.coolkit.cc/dispatch/app",
    "eu": "https://eu-dispa.coolkit.cc/dispatch/app",
}
OAUTH_URL = "https://c2ccdn.coolkit.cc/oauth/index.html"
# SonoffLAN's maintained compatibility identity lets ordinary eWeLink users
# fetch their own device keys without creating a developer application.
# Source: https://github.com/AlexxIT/SonoffLAN (MIT)
SONOFFLAN_APP_ID = "R8Oq3y0eSZSYdKccHlrQzT1ACCOUT9Gv"
SONOFFLAN_APP_SECRET = "1ve5Qk9GXfUhKAn1svnKwpAlxXkMarru"


class EWeLinkCloudError(RuntimeError):
    pass


def websocket_handshake(
    access_token: str,
    app_id: str,
    user_apikey: str,
    *,
    timestamp: float | None = None,
) -> dict:
    timestamp = timestamp if timestamp is not None else time.time()
    return {
        "action": "userOnline",
        "at": access_token,
        "apikey": user_apikey,
        "appid": app_id,
        "nonce": str(int(timestamp / 100)),
        "ts": int(timestamp),
        "userAgent": "app",
        "sequence": str(int(timestamp * 1000)),
        "version": 8,
    }


def device_capabilities(uiid: int | None, params: dict) -> list[dict]:
    """Map known eWeLink fields to safe typed controls; unknown fields stay read-only."""
    if not isinstance(params, dict):
        return []
    capabilities: list[dict] = []
    switches = params.get("switches")
    if isinstance(switches, list):
        channels = sorted(
            {
                int(item["outlet"]) + 1
                for item in switches
                if isinstance(item, dict)
                and str(item.get("switch")) in {"on", "off"}
                and isinstance(item.get("outlet"), int)
                and 0 <= item["outlet"] < 32
            }
        )
        if channels:
            capabilities.append(
                {
                    "id": "switches",
                    "type": "channels",
                    "channels": channels,
                    "writable": True,
                }
            )
    elif params.get("switch") in {"on", "off"}:
        capabilities.append(
            {"id": "switch", "type": "switch", "writable": True}
        )

    brightness_key = next(
        (key for key in ("bright", "brightness") if isinstance(params.get(key), (int, float))),
        None,
    )
    rgb_keys = (
        ["colorR", "colorG", "colorB"]
        if all(isinstance(params.get(key), (int, float)) for key in ("colorR", "colorG", "colorB"))
        else None
    )
    if brightness_key or rgb_keys:
        switch_key = next(
            (key for key in ("switch", "state") if params.get(key) in {"on", "off"}),
            None,
        )
        capabilities.append(
            {
                "id": "light",
                "type": "light",
                "brightness_key": brightness_key,
                "rgb_keys": rgb_keys,
                "switch_key": switch_key,
                "rgb_extras": {
                    **({"mode": 1} if "mode" in params else {}),
                    **({"light_type": 1} if "light_type" in params else {}),
                },
                "brightness_extras": (
                    {"mode": 0, "switch": "on"}
                    if uiid == 44 and brightness_key == "brightness"
                    else {}
                ),
                "minimum": 0,
                "maximum": 100,
                "writable": True,
            }
        )

    cover = None
    if "motorTurn" in params or "currLocation" in params:
        cover = {
            "action_key": "motorTurn",
            "action_values": {"stop": 0, "open": 1, "close": 2},
            "position_key": "currLocation",
            "position_command_key": "location",
            "position_reversed": False,
        }
    elif "curtainAction" in params or "curPercent" in params:
        cover = {
            "action_key": "curtainAction",
            "action_values": {"stop": "pause", "open": "open", "close": "close"},
            "position_key": "curPercent",
            "position_command_key": "openPercent",
            "position_reversed": True,
        }
    elif "setclose" in params:
        cover = {
            "action_key": "switch",
            "action_values": {"stop": "pause", "open": "on", "close": "off"},
            "position_key": "setclose",
            "position_command_key": "setclose",
            "position_reversed": True,
        }
    elif "electromotor" in params or "percentageControl" in params:
        cover = {
            "action_key": "electromotor",
            "action_values": {"stop": 1, "open": 0, "close": 2},
            "position_key": "percentageControl",
            "position_command_key": "percentageControl",
            "position_reversed": True,
        }
    elif "op" in params or "per" in params:
        cover = {
            "action_key": "op",
            "action_values": {"stop": 2, "open": 1, "close": 3},
            "position_key": "per",
            "position_command_key": None,
            "position_reversed": False,
        }
    if cover:
        raw_position = params.get(cover["position_key"])
        position = (
            float(raw_position)
            if isinstance(raw_position, (int, float)) and type(raw_position) is not bool
            else None
        )
        if position is not None and cover["position_reversed"]:
            position = 100 - position
        if position is not None and not 0 <= position <= 100:
            position = None
        if position is not None and position.is_integer():
            position = int(position)
        capabilities.append(
            {
                "id": "cover",
                "type": "cover",
                "actions": ["open", "close", "stop"],
                "position": position,
                **cover,
                "writable": True,
            }
        )

    sensor_keys = (
        "temperature",
        "humidity",
        "power",
        "voltage",
        "current",
        "dusty",
        "light",
        "noise",
    )
    for key in sensor_keys:
        if isinstance(params.get(key), (int, float)):
            capabilities.append(
                {"id": key, "type": "number_sensor", "writable": False}
            )

    for key in ("door", "motion", "water", "smoke", "tamper"):
        value = params.get(key)
        if type(value) is bool or (
            isinstance(value, str)
            and value in {"on", "off", "open", "closed", "detected", "normal"}
        ):
            capabilities.append(
                {"id": key, "type": "binary_sensor", "writable": False}
            )

    writable_numbers = {
        "temperatureCorrection": (-20, 20),
        "humidityCorrection": (-50, 50),
        "powerThreshold": (0, 50000),
    }
    for key, (minimum, maximum) in writable_numbers.items():
        if isinstance(params.get(key), (int, float)):
            capabilities.append(
                {
                    "id": key,
                    "type": "number",
                    "minimum": minimum,
                    "maximum": maximum,
                    "writable": True,
                }
            )

    enum_options = {
        "startup": ["on", "off", "stay"],
        "sledOnline": ["on", "off"],
        "workMode": ["manual", "auto"],
    }
    for key, options in enum_options.items():
        if key in params:
            capabilities.append(
                {
                    "id": key,
                    "type": "enum",
                    "options": options,
                    "writable": True,
                }
            )
    return capabilities


def typed_device_action(capabilities: list[dict], action: str, arguments: dict) -> dict:
    """Translate one typed UI action into a bounded eWeLink parameter update."""
    if not isinstance(arguments, dict):
        raise ValueError("Device action arguments must be an object")
    capabilities = [item for item in capabilities if isinstance(item, dict)]

    def capability(capability_id: str, capability_type: str | None = None) -> dict:
        found = next(
            (
                item
                for item in capabilities
                if item.get("id") == capability_id
                and (capability_type is None or item.get("type") == capability_type)
                and item.get("writable") is True
            ),
            None,
        )
        if not found:
            raise ValueError("This action is not supported by the selected device")
        return found

    if action in {"switch", "button"}:
        state = arguments.get("state", "on")
        if state not in {"on", "off"}:
            raise ValueError("Switch state must be on or off")
        channels = next(
            (item for item in capabilities if item.get("type") == "channels"), None
        )
        if channels:
            channel = arguments.get("channel")
            if type(channel) is not int or channel not in channels.get("channels", []):
                raise ValueError("Select a channel supported by this device")
            result = {
                "params": {"switches": [{"outlet": channel - 1, "switch": state}]},
                "lan": {"command": "switches", "channel": channel, "state": state},
            }
        else:
            capability("switch", "switch")
            result = {
                "params": {"switch": state},
                "lan": {"command": "switch", "state": state},
            }
        if action == "button":
            pulse = arguments.get("pulse_seconds", 1)
            if not isinstance(pulse, (int, float)) or type(pulse) is bool or not 0.1 <= pulse <= 30:
                raise ValueError("Button pulse must be between 0.1 and 30 seconds")
            result["pulse_seconds"] = float(pulse)
        return result

    if action == "light":
        light = capability("light", "light")
        mode = arguments.get("mode")
        if mode is None:
            mode = "color" if "color" in arguments else "brightness"
        if mode in {"on", "off"}:
            if not light.get("switch_key"):
                raise ValueError("This light does not report an on/off control")
            params = {light["switch_key"]: mode}
        elif mode == "brightness":
            brightness = arguments.get("brightness")
            if not light.get("brightness_key"):
                raise ValueError("This light does not report brightness control")
            if type(brightness) is not int or not 0 <= brightness <= 100:
                raise ValueError("Brightness must be between 0 and 100")
            params = {
                light["brightness_key"]: brightness,
                **light.get("brightness_extras", {}),
            }
        elif mode == "color":
            color = arguments.get("color")
            if not light.get("rgb_keys"):
                raise ValueError("This light does not report RGB color control")
            if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
                raise ValueError("Color must use #RRGGBB format")
            channels = [int(color[index : index + 2], 16) for index in (1, 3, 5)]
            params = {
                **dict(zip(light["rgb_keys"], channels)),
                **light.get("rgb_extras", {}),
            }
        else:
            raise ValueError("Light mode must be on, off, brightness, or color")
        return {"params": params, "lan": None}

    if action == "cover":
        cover = capability("cover", "cover")
        movement = arguments.get("action")
        if movement == "position":
            position = arguments.get("position")
            if not cover.get("position_command_key"):
                raise ValueError("This cover does not report position control")
            if type(position) is not int or not 0 <= position <= 100:
                raise ValueError("Cover position must be between 0 and 100")
            value = 100 - position if cover.get("position_reversed") else position
            params = {cover["position_command_key"]: value}
        else:
            values = cover.get("action_values", {})
            if movement not in values:
                raise ValueError("Cover action must be open, close, stop, or position")
            params = {cover["action_key"]: values[movement]}
        return {"params": params, "lan": None}

    if action == "number":
        property_name, value = arguments.get("property"), arguments.get("value")
        item = capability(str(property_name), "number")
        if not isinstance(value, (int, float)) or type(value) is bool:
            raise ValueError("Numeric setting requires a number")
        if not item["minimum"] <= value <= item["maximum"]:
            raise ValueError(
                f"{property_name} must be between {item['minimum']} and {item['maximum']}"
            )
        return {"params": {property_name: value}, "lan": None}

    if action == "enum":
        property_name, value = arguments.get("property"), arguments.get("value")
        item = capability(str(property_name), "enum")
        if value not in item.get("options", []):
            raise ValueError("Select a supported setting value")
        return {"params": {property_name: value}, "lan": None}

    if action == "refresh":
        return {"params": None, "lan": None}
    raise ValueError("Unsupported device action")


def cloud_device_request(
    access_token: str,
    app_id: str,
    region: str,
    device_id: str,
    params: dict,
) -> Request:
    if region not in API_HOSTS:
        raise ValueError("Select a valid eWeLink region")
    if not access_token or not app_id:
        raise ValueError("eWeLink cloud authorization is required")
    if not re.fullmatch(r"[A-Za-z0-9]{6,32}", device_id):
        raise ValueError("eWeLink device ID must contain 6-32 letters or numbers")
    if (
        not isinstance(params, dict)
        or not params
        or len(params) > 8
        or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", key) for key in params)
    ):
        raise ValueError("eWeLink command parameters are invalid")
    body = json.dumps(
        {"type": 1, "id": device_id, "params": params}, separators=(",", ":")
    ).encode()
    if len(body) > 4096:
        raise ValueError("eWeLink command is too large")
    return Request(
        f"{API_HOSTS[region]}/v2/device/thing/status",
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-CK-Appid": app_id,
        },
        method="POST",
    )


def cloud_switch_request(
    access_token: str,
    app_id: str,
    region: str,
    device_id: str,
    channel: int,
    state: str,
) -> Request:
    if channel not in {1, 2, 3, 4} or state not in {"on", "off"}:
        raise ValueError("Invalid eWeLink relay command")
    return cloud_device_request(
        access_token,
        app_id,
        region,
        device_id,
        {"switches": [{"switch": state, "outlet": channel - 1}]},
    )


def cloud_status_request(
    access_token: str, app_id: str, region: str, device_id: str
) -> Request:
    if region not in API_HOSTS:
        raise ValueError("Select a valid eWeLink region")
    if not access_token or not app_id:
        raise ValueError("eWeLink cloud authorization is required")
    if not re.fullmatch(r"[A-Za-z0-9]{6,32}", device_id):
        raise ValueError("eWeLink device ID must contain 6-32 letters or numbers")
    query = urlencode({"type": 1, "id": device_id, "params": "switches"})
    return Request(
        f"{API_HOSTS[region]}/v2/device/thing/status?{query}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-CK-Appid": app_id,
        },
        method="GET",
    )


class EWeLinkCloud:
    @staticmethod
    def _signature(secret: str, message: bytes) -> str:
        return base64.b64encode(
            hmac.new(secret.encode(), message, hashlib.sha256).digest()
        ).decode()

    @classmethod
    def authorization_url(
        cls,
        app_id: str,
        app_secret: str,
        redirect_url: str,
        state: str,
        *,
        seq: str | None = None,
        nonce: str | None = None,
    ) -> str:
        app_id, app_secret = app_id.strip(), app_secret.strip()
        if not app_id or not app_secret:
            raise ValueError("eWeLink App ID and App Secret are required")
        seq = seq or str(time.time_ns() // 1_000_000)
        nonce = nonce or "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(8)
        )
        signature = cls._signature(app_secret, f"{app_id}_{seq}".encode())
        query = urlencode(
            {
                "clientId": app_id,
                "seq": seq,
                "authorization": signature,
                "redirectUrl": redirect_url,
                "grantType": "authorization_code",
                "state": state,
                "nonce": nonce,
                "showQRCode": "true",
            }
        )
        return f"{OAUTH_URL}?{query}"

    @staticmethod
    def _decode(response) -> dict:
        try:
            payload = json.loads(response.read())
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise EWeLinkCloudError("eWeLink returned an invalid response") from error
        if not isinstance(payload, dict):
            raise EWeLinkCloudError("eWeLink returned an invalid response")
        return payload

    @classmethod
    def _open(cls, request: Request) -> dict:
        try:
            with urlopen(request, timeout=10) as response:
                payload = cls._decode(response)
        except HTTPError as error:
            error.close()
            raise EWeLinkCloudError(f"eWeLink returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise EWeLinkCloudError("Could not reach eWeLink; check the internet connection") from error
        if payload.get("error", 0):
            code = payload.get("error")
            raise EWeLinkCloudError(f"eWeLink rejected the request ({code})")
        return payload.get("data") or {}

    @classmethod
    def _signed_post(
        cls, url: str, app_id: str, app_secret: str, payload: dict
    ) -> dict:
        body = json.dumps(payload, separators=(",", ":")).encode()
        return cls._open(
            Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Sign {cls._signature(app_secret, body)}",
                    "Content-Type": "application/json",
                    "X-CK-Appid": app_id,
                },
                method="POST",
            )
        )

    @classmethod
    def _devices(cls, app_id: str, access_token: str, region: str) -> list[dict]:
        if region not in API_HOSTS:
            raise EWeLinkCloudError("eWeLink returned an unknown account region")
        data = cls._open(
            Request(
                f"{API_HOSTS[region]}/v2/device/thing?num=0",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-CK-Appid": app_id,
                },
            )
        )
        devices = []
        things = data.get("thingList", [])
        if not isinstance(things, list):
            raise EWeLinkCloudError("eWeLink returned an invalid device list")
        for thing in things:
            item = thing.get("itemData", {}) if isinstance(thing, dict) else {}
            if not isinstance(item, dict):
                continue
            if not item.get("deviceid") or not item.get("devicekey"):
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            try:
                uiid = int(item["uiid"]) if item.get("uiid") is not None else None
            except (TypeError, ValueError):
                uiid = None
            devices.append(
                {
                    "id": str(item["deviceid"]),
                    "name": str(item.get("name") or item["deviceid"]),
                    "model": str(item.get("productModel") or "SONOFF device"),
                    "online": bool(item.get("online")),
                    "device_key": str(item["devicekey"]),
                    "uiid": uiid,
                    "params": params,
                    "capabilities": device_capabilities(uiid, params),
                }
            )
        return devices

    def oauth_devices(
        self,
        app_id: str,
        app_secret: str,
        redirect_url: str,
        code: str,
        region: str,
    ) -> list[dict]:
        if region not in API_HOSTS:
            raise EWeLinkCloudError("eWeLink returned an unknown account region")
        data = self._signed_post(
            f"{API_HOSTS[region]}/v2/user/oauth/token",
            app_id,
            app_secret,
            {
                "code": code,
                "redirectUrl": redirect_url,
                "grantType": "authorization_code",
            },
        )
        token = data.get("accessToken") or data.get("at")
        if not token:
            raise EWeLinkCloudError("eWeLink did not return an access token")
        devices = self._devices(app_id, str(token), region)
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        for device in devices:
            device.update(
                {
                    "_cloud_token": str(token),
                    "_cloud_app_id": app_id,
                    "_cloud_region": region,
                    **(
                        {"_cloud_user_apikey": str(user["apikey"])}
                        if user.get("apikey")
                        else {}
                    ),
                }
            )
        return devices

    def password_devices(
        self,
        app_id: str,
        app_secret: str,
        account: str,
        password: str,
        country_code: str,
        region: str,
    ) -> list[dict]:
        if region not in API_HOSTS:
            raise ValueError("Select a valid eWeLink region")
        payload = {"password": password, "countryCode": country_code}
        if "@" in account:
            payload["email"] = account.strip()
        else:
            phone = account.strip()
            payload["phoneNumber"] = phone if phone.startswith("+") else country_code + phone
        data = self._signed_post(
            f"{API_HOSTS[region]}/v2/user/login", app_id, app_secret, payload
        )
        token = data.get("at") or data.get("accessToken")
        actual_region = str(data.get("region") or region)
        if not token:
            raise EWeLinkCloudError("eWeLink did not return an access token")
        devices = self._devices(app_id, str(token), actual_region)
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        for device in devices:
            device.update(
                {
                    "_cloud_token": str(token),
                    "_cloud_app_id": app_id,
                    "_cloud_region": actual_region,
                    **(
                        {"_cloud_user_apikey": str(user["apikey"])}
                        if user.get("apikey")
                        else {}
                    ),
                }
            )
        return devices

    def account_devices(
        self,
        account: str,
        password: str,
        country_code: str,
        region: str,
    ) -> list[dict]:
        return self.password_devices(
            SONOFFLAN_APP_ID,
            SONOFFLAN_APP_SECRET,
            account,
            password,
            country_code,
            region,
        )

    def token_devices(self, app_id: str, access_token: str, region: str) -> list[dict]:
        devices = self._devices(app_id, access_token, region)
        for device in devices:
            device.update(
                {
                    "_cloud_token": access_token,
                    "_cloud_app_id": app_id,
                    "_cloud_region": region,
                }
            )
        return devices

    @staticmethod
    def _dispatch(access_token: str, region: str) -> tuple[str, int]:
        if region not in WS_DISPATCH:
            raise ValueError("Select a valid eWeLink region")
        try:
            with urlopen(
                Request(
                    WS_DISPATCH[region],
                    headers={"Authorization": f"Bearer {access_token}"},
                ),
                timeout=10,
            ) as response:
                payload = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            if isinstance(error, HTTPError):
                error.close()
            raise EWeLinkCloudError("Could not open the eWeLink live connection") from error
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        try:
            domain, port = str(payload["domain"]), int(payload["port"])
        except (KeyError, TypeError, ValueError) as error:
            raise EWeLinkCloudError("eWeLink returned an invalid live endpoint") from error
        if not domain or not 1 <= port <= 65535:
            raise EWeLinkCloudError("eWeLink returned an invalid live endpoint")
        return domain, port

    def listen_updates(
        self,
        access_token: str,
        app_id: str,
        region: str,
        user_apikey: str,
        stop_event: threading.Event,
        callback,
    ) -> None:
        domain, port = self._dispatch(access_token, region)
        asyncio.run(
            self._listen_updates(
                f"wss://{domain}:{port}/api/ws",
                websocket_handshake(access_token, app_id, user_apikey),
                stop_event,
                callback,
            )
        )

    @staticmethod
    async def _listen_updates(url: str, handshake: dict, stop_event, callback) -> None:
        from websockets.asyncio.client import connect

        async with connect(url, ping_interval=90, open_timeout=10, close_timeout=5) as socket:
            await socket.send(json.dumps(handshake, separators=(",", ":")))
            try:
                response = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            except (asyncio.TimeoutError, json.JSONDecodeError) as error:
                raise EWeLinkCloudError("eWeLink live authorization timed out") from error
            if not isinstance(response, dict) or response.get("error", 0):
                raise EWeLinkCloudError("eWeLink rejected the live connection")
            heartbeat = response.get("config", {}).get("hbInterval", 90)
            heartbeat = max(15, min(300, int(heartbeat)))
            last_ping = time.monotonic()
            while not stop_event.is_set():
                try:
                    message = await asyncio.wait_for(socket.recv(), timeout=1)
                except asyncio.TimeoutError:
                    if time.monotonic() - last_ping >= heartbeat:
                        await socket.send("ping")
                        last_ping = time.monotonic()
                    continue
                if message == "pong":
                    continue
                try:
                    payload = json.loads(message)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("action") in {"update", "sysmsg"} or "params" in payload:
                    callback(payload)


def service_device_id(name: str) -> str | None:
    match = re.match(r"(?i)^ewelink[_-]([a-z0-9]{6,32})(?:\.|$)", name)
    return match.group(1) if match else None


def add_lan_addresses(devices: list[dict], timeout: float = 2.0) -> list[dict]:
    """Add LAN host/port when the relay advertises _ewelink._tcp over mDNS."""
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        return devices

    found: dict[str, tuple[str, int]] = {}

    class Listener(ServiceListener):
        def add_service(self, zeroconf, service_type, name):
            device_id = service_device_id(name)
            info = zeroconf.get_service_info(service_type, name, timeout=1000)
            if not device_id or not info:
                return
            addresses = info.parsed_addresses()
            if addresses:
                found[device_id.lower()] = (addresses[0], info.port or 8081)

        def update_service(self, zeroconf, service_type, name):
            self.add_service(zeroconf, service_type, name)

        def remove_service(self, _zeroconf, _service_type, _name):
            pass

    zeroconf = Zeroconf()
    browser = ServiceBrowser(zeroconf, "_ewelink._tcp.local.", Listener())
    try:
        time.sleep(timeout)
    finally:
        browser.cancel()
        zeroconf.close()
    for device in devices:
        if address := found.get(device["id"].lower()):
            device["host"], device["port"] = address
    return devices


class ImportSessions:
    def __init__(self, ttl_seconds: float = 600):
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _new_id(self) -> str:
        return secrets.token_urlsafe(24)

    def _get(self, session_id: str) -> dict:
        session = self._sessions.get(session_id)
        if not session or session["expires"] < time.monotonic():
            self._sessions.pop(session_id, None)
            raise KeyError("eWeLink import session expired")
        return session

    def begin_oauth(
        self, app_id: str, app_secret: str, redirect_url: str
    ) -> tuple[str, str]:
        session_id = self._new_id()
        with self._lock:
            self._sessions[session_id] = {
                "status": "pending",
                "app_id": app_id.strip(),
                "app_secret": app_secret.strip(),
                "redirect_url": redirect_url,
                "expires": time.monotonic() + self.ttl_seconds,
            }
        return session_id, EWeLinkCloud.authorization_url(
            app_id, app_secret, redirect_url, session_id
        )

    def complete_oauth(
        self, session_id: str, code: str, region: str, cloud: EWeLinkCloud
    ) -> None:
        with self._lock:
            session = dict(self._get(session_id))
        try:
            devices = add_lan_addresses(
                cloud.oauth_devices(
                    session["app_id"],
                    session["app_secret"],
                    session["redirect_url"],
                    code,
                    region,
                )
            )
            if not devices:
                raise EWeLinkCloudError("No compatible eWeLink devices were returned")
            result = {"status": "ready", "devices": devices}
        except Exception as error:
            result = {"status": "error", "error": str(error)}
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id] = {
                    **result,
                    "expires": time.monotonic() + self.ttl_seconds,
                }

    def ready(self, devices: list[dict]) -> str:
        session_id = self._new_id()
        with self._lock:
            self._sessions[session_id] = {
                "status": "ready",
                "devices": devices,
                "expires": time.monotonic() + self.ttl_seconds,
            }
        return session_id

    def fail(self, session_id: str, message: str) -> None:
        with self._lock:
            self._get(session_id)
            self._sessions[session_id] = {
                "status": "error",
                "error": message,
                "expires": time.monotonic() + self.ttl_seconds,
            }

    def status(self, session_id: str) -> dict:
        with self._lock:
            session = self._get(session_id)
            public = {"status": session["status"]}
            if session["status"] == "error":
                public["error"] = session["error"]
            elif session["status"] == "ready":
                public["devices"] = [
                    {
                        key: device.get(key)
                        for key in (
                            "id",
                            "name",
                            "model",
                            "online",
                            "host",
                            "port",
                            "uiid",
                            "capabilities",
                        )
                    }
                    for device in session["devices"]
                ]
            return public

    def take(self, session_id: str, device_id: str) -> dict:
        device, _devices = self.take_all(session_id, device_id)
        return device

    def take_all(self, session_id: str, device_id: str) -> tuple[dict, list[dict]]:
        with self._lock:
            session = self._get(session_id)
            if session["status"] != "ready":
                raise KeyError("eWeLink import is not ready")
            device = next(
                (item for item in session["devices"] if item["id"] == device_id), None
            )
            if not device:
                raise KeyError("eWeLink device not found in this import")
            devices = list(session["devices"])
            self._sessions.pop(session_id, None)
            return device, devices
