from __future__ import annotations

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
OAUTH_URL = "https://c2ccdn.coolkit.cc/oauth/index.html"
# SonoffLAN's maintained compatibility identity lets ordinary eWeLink users
# fetch their own device keys without creating a developer application.
# Source: https://github.com/AlexxIT/SonoffLAN (MIT)
SONOFFLAN_APP_ID = "R8Oq3y0eSZSYdKccHlrQzT1ACCOUT9Gv"
SONOFFLAN_APP_SECRET = "1ve5Qk9GXfUhKAn1svnKwpAlxXkMarru"


class EWeLinkCloudError(RuntimeError):
    pass


def cloud_switch_request(
    access_token: str,
    app_id: str,
    region: str,
    device_id: str,
    channel: int,
    state: str,
) -> Request:
    if region not in API_HOSTS:
        raise ValueError("Select a valid eWeLink region")
    if not access_token or not app_id:
        raise ValueError("eWeLink cloud authorization is required")
    if not re.fullmatch(r"[A-Za-z0-9]{6,32}", device_id):
        raise ValueError("eWeLink device ID must contain 6-32 letters or numbers")
    if channel not in {1, 2, 3, 4} or state not in {"on", "off"}:
        raise ValueError("Invalid eWeLink relay command")
    body = json.dumps(
        {
            "type": 1,
            "id": device_id,
            "params": {"switches": [{"switch": state, "outlet": channel - 1}]},
        },
        separators=(",", ":"),
    ).encode()
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
            message = str(payload.get("msg") or "request rejected").strip()
            raise EWeLinkCloudError(f"eWeLink rejected the request: {message} ({code})")
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
            devices.append(
                {
                    "id": str(item["deviceid"]),
                    "name": str(item.get("name") or item["deviceid"]),
                    "model": str(item.get("productModel") or "SONOFF device"),
                    "online": bool(item.get("online")),
                    "device_key": str(item["devicekey"]),
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
        return self._devices(app_id, str(token), region)

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
        for device in devices:
            device.update(
                {
                    "_cloud_token": str(token),
                    "_cloud_app_id": app_id,
                    "_cloud_region": actual_region,
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
                        for key in ("id", "name", "model", "online", "host", "port")
                    }
                    for device in session["devices"]
                ]
            return public

    def take(self, session_id: str, device_id: str) -> dict:
        with self._lock:
            session = self._get(session_id)
            if session["status"] != "ready":
                raise KeyError("eWeLink import is not ready")
            device = next(
                (item for item in session["devices"] if item["id"] == device_id), None
            )
            if not device:
                raise KeyError("eWeLink device not found in this import")
            self._sessions.pop(session_id, None)
            return device
