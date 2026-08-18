import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import cv2

os.environ["DISABLE_VISION"] = "1"
from auth import hash_password

os.environ["VISIONGATE_USERNAME"] = "owner"
os.environ["VISIONGATE_PASSWORD_HASH"] = hash_password(
    "correct horse battery staple", n=1024
)

from fastapi.testclient import TestClient as BaseTestClient
from unittest.mock import patch

import app as app_module
from app import (
    CONFIG,
    DEFAULT_SETTINGS,
    DoorController,
    VisionManager,
    _config,
    detection_caption,
    app,
    spatial_layout_descriptor,
    vision_runtime,
)
from core import Database, Match, Profile


class TestClient(BaseTestClient):
    def __enter__(self):
        client = super().__enter__()
        origin = str(self.base_url).rstrip("/")
        login = self.post(
            "/api/auth/login",
            json={"username": "owner", "password": "correct horse battery staple"},
            headers={"Origin": origin},
        )
        if login.status_code != 200:
            raise AssertionError(f"Test login failed: {login.text}")
        self.headers.update(
            {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
        )
        return client


class FakeEWeLink(BaseHTTPRequestHandler):
    requests = []
    fail_first = False
    invalid_json = False

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.__class__.requests.append((self.path, json.loads(body)))
        should_fail = self.__class__.fail_first and len(self.__class__.requests) == 1
        self.send_response(500 if should_fail else 200)
        self.end_headers()
        if not should_fail:
            self.wfile.write(b"not-json" if self.__class__.invalid_json else b'{"error":0}')

    def log_message(self, _format, *args):
        pass


class DoorControllerTests(unittest.TestCase):
    @staticmethod
    def command(payload):
        from Crypto.Cipher import AES

        encrypted = base64.b64decode(payload["data"])
        decrypted = AES.new(
            hashlib.md5(b"1234567890abcdef").digest(),
            AES.MODE_CBC,
            base64.b64decode(payload["iv"]),
        ).decrypt(encrypted)
        return json.loads(decrypted[: -decrypted[-1]])["switches"][0]

    def test_open_and_close_use_independently_configured_channels(self):
        FakeEWeLink.requests = []
        FakeEWeLink.fail_first = False
        FakeEWeLink.invalid_json = False
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEWeLink)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = replace(
            CONFIG,
            ewelink_host="127.0.0.1",
            ewelink_port=server.server_port,
            ewelink_device_id="1000abcd12",
            ewelink_device_key="1234567890abcdef",
            ewelink_open_channel=1,
            ewelink_close_channel=2,
            ewelink_cloud_token="",
            ewelink_cloud_app_id="",
            ewelink_cloud_region="",
            pulse_seconds=0.01,
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "door.db")
            try:
                controller = DoorController(config, database)
                self.assertTrue(controller.trigger("test open", action="open"))
                deadline = time.time() + 3
                while controller.busy and time.time() < deadline:
                    time.sleep(0.01)
                self.assertTrue(controller.trigger("test close", action="close"))
                deadline = time.time() + 3
                while controller.busy and time.time() < deadline:
                    time.sleep(0.01)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

            self.assertEqual(
                [self.command(payload) for _, payload in FakeEWeLink.requests],
                [
                    {"switch": "on", "outlet": 0},
                    {"switch": "off", "outlet": 0},
                    {"switch": "on", "outlet": 1},
                    {"switch": "off", "outlet": 1},
                ],
            )
            self.assertEqual(
                [event.kind for event in database.events()],
                ["door_close", "door_open"],
            )
            self.assertEqual(controller.status()["state"], "closed")
            self.assertEqual(DoorController(config, database).status()["state"], "closed")

    def test_existing_door_events_seed_last_known_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "door-state.db")
            database.add_event("door_close", "Door close command sent")
            database.add_event("door_open", "Door open command sent")

            controller = DoorController(CONFIG, database)
            self.assertEqual(controller.status()["state"], "open")
            self.assertEqual(database.settings()["door_last_state"], "open")

    def test_relay_state_is_checked_on_start_and_every_minute(self):
        config = replace(
            CONFIG,
            ewelink_host="127.0.0.1",
            ewelink_device_id="1000abcd12",
            ewelink_device_key="1234567890abcdef",
            ewelink_cloud_token="",
            ewelink_cloud_app_id="",
            ewelink_cloud_region="",
        )
        with tempfile.TemporaryDirectory() as directory:
            controller = DoorController(config, Database(Path(directory) / "poll.db"))
            controller.POLL_SECONDS = 0.02
            with patch.object(
                controller,
                "_query_switches",
                return_value={0: "on", 1: "off", 2: "off", 3: "off"},
            ) as queried:
                try:
                    controller.start()
                    deadline = time.time() + 1
                    while queried.call_count < 2 and time.time() < deadline:
                        time.sleep(0.01)
                finally:
                    controller.stop()

            status = controller.status()
            self.assertGreaterEqual(queried.call_count, 2)
            self.assertEqual(status["state"], "open")
            self.assertIsNotNone(status["last_state_check"])

    def test_new_door_device_is_checked_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = DoorController(CONFIG, Database(Path(directory) / "new-door.db"))
            new_config = replace(
                CONFIG,
                ewelink_host="127.0.0.1",
                ewelink_device_id="1000abcd12",
                ewelink_device_key="1234567890abcdef",
                ewelink_cloud_token="",
                ewelink_cloud_app_id="",
                ewelink_cloud_region="",
            )
            with patch.object(controller, "refresh_state", return_value=True) as checked:
                controller.update(new_config)

            checked.assert_called_once_with()

    def test_uncertain_turn_on_still_sends_safety_turn_off(self):
        FakeEWeLink.requests = []
        FakeEWeLink.fail_first = True
        FakeEWeLink.invalid_json = False
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEWeLink)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = replace(
            CONFIG,
            ewelink_host="127.0.0.1",
            ewelink_port=server.server_port,
            ewelink_device_id="1000abcd12",
            ewelink_device_key="1234567890abcdef",
            ewelink_cloud_token="",
            ewelink_cloud_app_id="",
            ewelink_cloud_region="",
            pulse_seconds=0.01,
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "door.db")
            try:
                controller = DoorController(config, database)
                self.assertTrue(controller.trigger("test"))
                deadline = time.time() + 3
                while controller.busy and time.time() < deadline:
                    time.sleep(0.01)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

            self.assertEqual(len(FakeEWeLink.requests), 2)
            self.assertEqual(self.command(FakeEWeLink.requests[1][1])["switch"], "off")
            self.assertEqual(database.events()[0].kind, "door_error")

    def test_malformed_device_response_does_not_leave_controller_busy(self):
        FakeEWeLink.requests = []
        FakeEWeLink.fail_first = False
        FakeEWeLink.invalid_json = True
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEWeLink)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = replace(
            CONFIG,
            ewelink_host="127.0.0.1",
            ewelink_port=server.server_port,
            ewelink_device_id="1000abcd12",
            ewelink_device_key="1234567890abcdef",
            ewelink_cloud_token="",
            ewelink_cloud_app_id="",
            ewelink_cloud_region="",
            pulse_seconds=0.01,
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "door.db")
            try:
                controller = DoorController(config, database)
                self.assertTrue(controller.trigger("test"))
                deadline = time.time() + 3
                while controller.busy and time.time() < deadline:
                    time.sleep(0.01)
                self.assertFalse(controller.busy)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

    def test_cloud_control_opens_when_the_relay_is_not_reachable_over_lan(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"error":0}'

        config = replace(
            CONFIG,
            ewelink_host="",
            ewelink_device_id="1000abcd12",
            ewelink_device_key="device-key",
            ewelink_cloud_token="access-token",
            ewelink_cloud_app_id="app-id",
            ewelink_cloud_region="eu",
            pulse_seconds=0.01,
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "app.urlopen", side_effect=[Response(), Response()]
        ) as opened:
            database = Database(Path(directory) / "door.db")
            controller = DoorController(config, database)
            self.assertTrue(controller.trigger("cloud test", action="open"))
            deadline = time.time() + 3
            while controller.busy and time.time() < deadline:
                time.sleep(0.01)
            event_kind = database.events()[0].kind

        requests = [call.args[0] for call in opened.call_args_list]
        self.assertEqual(
            [json.loads(request.data)["params"]["switches"][0] for request in requests],
            [
                {"switch": "on", "outlet": 0},
                {"switch": "off", "outlet": 0},
            ],
        )
        self.assertEqual(event_kind, "door_open")

    def test_last_authorized_sighting_resets_the_automatic_close_timer(self):
        FakeEWeLink.requests = []
        FakeEWeLink.fail_first = False
        FakeEWeLink.invalid_json = False
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEWeLink)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = replace(
            CONFIG,
            ewelink_host="127.0.0.1",
            ewelink_port=server.server_port,
            ewelink_device_id="1000abcd12",
            ewelink_device_key="1234567890abcdef",
            ewelink_cloud_token="",
            ewelink_cloud_app_id="",
            ewelink_cloud_region="",
            pulse_seconds=0.01,
            auto_close_seconds=0.12,
        )
        profile = Profile(7, "Alice", "person", np.ones(2, np.float32), "now")
        match = Match(profile, 0.96)
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "auto-close.db")
            controller = DoorController(config, database)
            try:
                controller.authorized_seen()
                self.assertTrue(controller.trigger("Alice", match=match))
                deadline = time.time() + 2
                while controller.busy and time.time() < deadline:
                    time.sleep(0.005)
                time.sleep(0.06)
                controller.authorized_seen()
                time.sleep(0.075)
                self.assertEqual(len(FakeEWeLink.requests), 2)
                deadline = time.time() + 2
                while len(FakeEWeLink.requests) < 4 and time.time() < deadline:
                    time.sleep(0.01)
            finally:
                controller.stop()
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

            self.assertEqual(
                [self.command(payload) for _, payload in FakeEWeLink.requests],
                [
                    {"switch": "on", "outlet": 0},
                    {"switch": "off", "outlet": 0},
                    {"switch": "on", "outlet": 1},
                    {"switch": "off", "outlet": 1},
                ],
            )
            self.assertEqual(
                [event.kind for event in database.events()],
                ["door_close", "door_open"],
            )

    def test_manual_open_does_not_arm_automatic_close(self):
        FakeEWeLink.requests = []
        FakeEWeLink.fail_first = False
        FakeEWeLink.invalid_json = False
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEWeLink)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = replace(
            CONFIG,
            ewelink_host="127.0.0.1",
            ewelink_port=server.server_port,
            ewelink_device_id="1000abcd12",
            ewelink_device_key="1234567890abcdef",
            ewelink_cloud_token="",
            ewelink_cloud_app_id="",
            ewelink_cloud_region="",
            pulse_seconds=0.01,
            auto_close_seconds=0.03,
        )
        with tempfile.TemporaryDirectory() as directory:
            controller = DoorController(config, Database(Path(directory) / "manual.db"))
            try:
                self.assertTrue(controller.trigger("manual open", action="open"))
                deadline = time.time() + 2
                while controller.busy and time.time() < deadline:
                    time.sleep(0.005)
                time.sleep(0.06)
                self.assertEqual(len(FakeEWeLink.requests), 2)
                self.assertFalse(controller.status()["auto_close_armed"])
            finally:
                controller.stop()
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)


class AppearanceTests(unittest.TestCase):
    def test_same_average_colors_in_different_positions_remain_distinguishable(self):
        first = np.empty((224, 112, 3), np.uint8)
        second = np.empty_like(first)
        first[:112], first[112:] = (30, 30, 220), (220, 30, 30)
        second[:112], second[112:] = (220, 30, 30), (30, 30, 220)

        similarity = float(
            np.dot(spatial_layout_descriptor(first), spatial_layout_descriptor(second))
        )

        self.assertLess(similarity, 0)

    def test_detection_caption_includes_model_confidence(self):
        self.assertEqual(
            detection_caption({"label": "car", "confidence": 0.876}), "car · 88%"
        )
        self.assertEqual(
            detection_caption(
                {"label": "person", "confidence": 0.912, "match": "Alice"}
            ),
            "Alice · 91%",
        )

    def test_auto_performance_mode_reduces_cpu_work_only(self):
        config = replace(CONFIG, yolo_model="yolo11m.pt", yolo_imgsz=960)
        self.assertEqual(vision_runtime(config, "cpu"), ("yolo11n.pt", 512, 2))
        self.assertEqual(vision_runtime(config, "cuda:0"), ("yolo11m.pt", 960, 1))
        self.assertEqual(
            vision_runtime(replace(config, performance_mode="quality"), "cpu"),
            ("yolo11m.pt", 960, 1),
        )


class WebAccessTests(unittest.TestCase):
    def test_public_branding_and_private_brand_settings_are_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "branding.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            manager = VisionManager(database, _config(settings))
            manager.update_settings({"app_name": "Gate House", "brand_palette": "blue"})
            with patch("app.DATABASE", database):
                with BaseTestClient(app) as client:
                    brand = client.get("/api/brand")
            saved = database.settings()

        self.assertEqual(brand.status_code, 200)
        self.assertEqual(brand.json()["name"], "Gate House")
        self.assertEqual(brand.json()["palette"], "blue")
        self.assertEqual(saved["app_name"], "Gate House")

    def test_custom_png_logo_can_be_uploaded_and_served(self):
        image = np.zeros((48, 48, 4), np.uint8)
        image[:, :] = (20, 120, 220, 255)
        encoded = base64.b64encode(cv2.imencode(".png", image)[1]).decode()
        with tempfile.TemporaryDirectory() as directory, patch(
            "app.DATA_DIR", Path(directory)
        ):
            with TestClient(app) as client:
                uploaded = client.put(
                    "/api/branding/logo",
                    json={"image": f"data:image/png;base64,{encoded}"},
                )
                logo = client.get("/logo.png")

        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(logo.status_code, 200)
        self.assertEqual(logo.headers["content-type"], "image/png")
        self.assertTrue(logo.content.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_dashboard_and_apis_require_a_login(self):
        with BaseTestClient(app, follow_redirects=False) as client:
            dashboard = client.get("/")
            status = client.get("/api/status")
            login = client.get("/login")

        self.assertEqual(dashboard.status_code, 303)
        self.assertEqual(dashboard.headers["location"], "/login")
        self.assertEqual(status.status_code, 401)
        self.assertEqual(login.status_code, 200)
        self.assertIn('id="title">Sign in', login.text)

    def test_login_uses_a_secure_server_side_session_and_csrf_token(self):
        with BaseTestClient(app, base_url="https://testserver") as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "owner", "password": "correct horse battery staple"},
                headers={"Origin": "https://testserver"},
            )
            csrf = login.json()["csrf_token"]
            status = client.get("/api/status")
            rejected = client.delete("/api/events")
            logout = client.post(
                "/api/auth/logout",
                headers={"Origin": "https://testserver", "X-CSRF-Token": csrf},
            )
            after_logout = client.get("/api/status")

        cookie = login.headers["set-cookie"].lower()
        self.assertEqual(login.status_code, 200)
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(after_logout.status_code, 401)

    def test_public_mode_keeps_direct_private_lan_login_usable(self):
        with patch.dict(os.environ, {"VISIONGATE_SECURE_COOKIES": "1"}):
            with BaseTestClient(
                app, base_url="http://testserver", client=("192.168.1.50", 50000)
            ) as client:
                login = client.post(
                    "/api/auth/login",
                    json={
                        "username": "owner",
                        "password": "correct horse battery staple",
                    },
                    headers={"Origin": "http://testserver"},
                )
            with BaseTestClient(
                app, base_url="http://testserver", client=("8.8.8.8", 50000)
            ) as public_client:
                public_login = public_client.post(
                    "/api/auth/login",
                    json={
                        "username": "owner",
                        "password": "correct horse battery staple",
                    },
                    headers={"Origin": "http://testserver"},
                )

        self.assertEqual(login.status_code, 200)
        self.assertNotIn("secure", login.headers["set-cookie"].lower())
        self.assertIn("secure", public_login.headers["set-cookie"].lower())

    def test_login_rejects_bad_credentials_with_a_generic_message(self):
        with BaseTestClient(app) as client:
            response = client.post(
                "/api/auth/login",
                json={"username": "owner", "password": "wrong password"},
                headers={"Origin": "http://testserver"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid username or password")
        self.assertNotIn("owner", response.text)

    def test_pages_include_browser_security_headers(self):
        with BaseTestClient(app) as client:
            response = client.get("/login")

        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertNotIn("sha256-", response.headers["content-security-policy"])
        self.assertNotIn("unsafe-inline", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_login_and_dashboard_share_a_public_responsive_design_system(self):
        with BaseTestClient(app) as public_client:
            login = public_client.get("/login")
            stylesheet = public_client.get("/visiongate.css")
            login_script = public_client.get("/login.js")
        with TestClient(app) as private_client:
            dashboard = private_client.get("/")
            dashboard_script = private_client.get("/dashboard.js")

        self.assertEqual(stylesheet.status_code, 200)
        self.assertEqual(login_script.status_code, 200)
        self.assertEqual(dashboard_script.status_code, 200)
        self.assertIn('href="/visiongate.css"', login.text)
        self.assertIn('href="/visiongate.css"', dashboard.text)
        self.assertIn('src="/login.js"', login.text)
        self.assertIn('src="/dashboard.js"', dashboard.text)
        self.assertNotIn("<style>", login.text)
        self.assertNotIn("<style>", dashboard.text)
        self.assertIn("--tap: 44px", stylesheet.text)
        self.assertIn("@media (max-width: 760px)", stylesheet.text)
        self.assertIn("prefers-reduced-motion", stylesheet.text)
        self.assertIn('class="topbar"', dashboard.text)
        self.assertIn('id="liveCamera"', dashboard.text)

    def test_everyday_ui_contains_only_operational_controls(self):
        with BaseTestClient(app) as public_client:
            login = public_client.get("/login").text
            stylesheet = public_client.get("/visiongate.css").text
        with TestClient(app) as private_client:
            dashboard = private_client.get("/").text

        self.assertIn('id="doorState"', dashboard)
        self.assertNotIn('id="tracks"', dashboard)
        self.assertNotIn('id="activity"', dashboard)
        self.assertNotIn('id="threshold"', dashboard)
        self.assertNotIn(".door-button:first-child", stylesheet)
        self.assertNotIn("auth-story", login)

    def test_visiongate_logo_and_lan_access_information_are_available(self):
        with patch("app.local_ipv4_addresses", return_value=["192.168.2.197"]):
            with TestClient(app) as client:
                logo = client.get("/logo.png")
                network = client.get("/api/network")

        self.assertEqual(logo.status_code, 200)
        self.assertEqual(logo.headers["content-type"], "image/png")
        self.assertEqual(
            network.json()["urls"], ["http://192.168.2.197:83"]
        )

    def test_invalid_door_settings_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "invalid-settings.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            manager = VisionManager(database, _config(settings))

            with self.assertRaises(ValueError):
                manager.update_settings(
                    {
                        "ewelink_device_id": "1000abcd12",
                        "ewelink_device_key": "device-key",
                        "ewelink_host": "",
                    }
                )

            saved = database.settings()
            self.assertEqual(saved["ewelink_device_id"], "")
            self.assertEqual(saved["ewelink_device_key"], "")

    def test_recognition_model_and_lookalike_margin_are_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "recognition-settings.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            manager = VisionManager(database, _config(settings))

            manager.update_settings(
                {
                    "yolo_model": "yolo11s.pt",
                    "match_margin": 0.07,
                    "auto_close_seconds": 17,
                }
            )

            self.assertEqual(manager.config.yolo_model, "yolo11s.pt")
            self.assertEqual(manager.config.match_margin, 0.07)
            self.assertEqual(database.settings()["match_margin"], 0.07)
            self.assertEqual(manager.config.auto_close_seconds, 17)
            self.assertEqual(database.settings()["auto_close_seconds"], 17)

    def test_door_test_reports_the_completed_relay_failure(self):
        class Door:
            configured = True
            busy = False
            last_error = "eWeLink relay rejected the command"

            @staticmethod
            def trigger(*_args, **_kwargs):
                return True

        manager = type("Manager", (), {"door": Door()})()
        with patch("app.MANAGER", manager):
            with self.assertRaises(app_module.HTTPException) as raised:
                app_module.test_door(app_module.DoorTest(confirm=True, action="open"))

        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("rejected", raised.exception.detail)

    def test_dashboard_and_status_are_available_after_login(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/api/status").status_code, 200)

    def test_camera_can_be_created_edited_and_deleted_in_app(self):
        with TestClient(app) as client:
            created = client.post(
                "/api/cameras",
                json={
                    "name": "Test camera",
                    "stream_url": "rtsp://test-camera.local:554/live",
                    "username": "viewer",
                    "password": "secret",
                    "enabled": False,
                },
            )
            self.assertEqual(created.status_code, 201)
            camera_id = created.json()["id"]
            updated = client.put(
                f"/api/cameras/{camera_id}",
                json={
                    "name": "Edited camera",
                    "stream_url": "rtsp://test-camera.local:554/stream2",
                    "username": "viewer2",
                    "password": "secret2",
                    "enabled": False,
                },
            )
            self.assertEqual(updated.status_code, 200)
            cameras = client.get("/api/config").json()["cameras"]
            self.assertEqual(
                next(item for item in cameras if item["id"] == camera_id)["name"],
                "Edited camera",
            )
            self.assertEqual(client.delete(f"/api/cameras/{camera_id}").status_code, 200)

    def test_camera_connection_can_be_checked_before_saving(self):
        class Capture:
            def isOpened(self):
                return True

            def read(self):
                return True, np.zeros((720, 1280, 3), np.uint8)

            def set(self, *_args):
                return True

            def release(self):
                pass

        with patch("app.cv2.VideoCapture", return_value=Capture()) as opened:
            with TestClient(app) as client:
                response = client.post(
                    "/api/cameras/test",
                    json={
                        "name": "Front gate",
                        "stream_url": "rtsp://camera.local:554/live",
                        "username": "viewer",
                        "password": "secret",
                        "enabled": True,
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"connected": True, "width": 1280, "height": 720})
        self.assertIn("viewer:secret@camera.local", opened.call_args.args[0])

    def test_camera_connection_failure_reports_the_pc_lan_address(self):
        class ClosedCapture:
            def isOpened(self):
                return False

            def set(self, *_args):
                return True

            def release(self):
                pass

        with patch("app.cv2.VideoCapture", return_value=ClosedCapture()), patch(
            "app.local_ipv4_addresses", return_value=["192.168.2.197"]
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/api/cameras/test",
                    json={
                        "name": "Front gate",
                        "stream_url": "rtsp://192.168.1.99:554/live",
                        "username": "viewer",
                        "password": "secret",
                        "enabled": True,
                    },
                )

        self.assertEqual(response.status_code, 422)
        self.assertIn("192.168.2.197", response.json()["detail"])

    def test_enabled_cameras_get_independent_workers(self):
        class Worker:
            def __init__(self, camera, *_args):
                self.camera = camera
                self.started = False

            def start(self):
                self.started = True

            def stop(self):
                self.started = False

            @staticmethod
            def _message_frame(_message):
                return b"placeholder"

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "independent-cameras.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            first = database.add_camera("Front", "rtsp://front.local:554/live")
            second = database.add_camera("Side", "rtsp://side.local:554/live")
            vision_config = replace(_config(settings), disable_vision=False)

            with patch("app.VisionSystem", Worker):
                manager = VisionManager(database, vision_config)
                manager.start()
                try:
                    self.assertEqual(set(manager.workers), {first.id, second.id})
                    self.assertIsNot(manager.workers[first.id], manager.workers[second.id])
                    self.assertTrue(all(worker.started for worker in manager.workers.values()))
                finally:
                    manager.stop()

    def test_editable_settings_and_event_history_are_exposed(self):
        with TestClient(app) as client:
            config = client.get("/api/config")
            self.assertEqual(config.status_code, 200)
            self.assertIn("match_threshold", config.json()["settings"])
            self.assertIn("match_margin", config.json()["settings"])
            self.assertIn("yolo_model", config.json()["settings"])
            self.assertIn("auto_close_seconds", config.json()["settings"])
            self.assertIn("ewelink_open_channel", config.json()["settings"])
            events = client.get("/api/events")
            self.assertEqual(events.status_code, 200)
            self.assertIsInstance(events.json(), list)

    def test_cloud_authorization_is_not_exposed_by_the_config_api(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "private-config.db")
            database.update_settings(
                {**DEFAULT_SETTINGS, "ewelink_cloud_token": "private-access-token"}
            )
            with patch("app.DATABASE", database):
                with TestClient(app) as client:
                    response = client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private-access-token", response.text)
        self.assertNotIn("ewelink_cloud_token", response.json()["settings"])

    def test_ewelink_qr_setup_uses_the_local_oauth_callback(self):
        with TestClient(app) as client:
            setup = client.get("/api/ewelink/oauth/setup")

        self.assertEqual(setup.status_code, 200)
        self.assertEqual(
            setup.json()["callback_url"],
            "http://testserver/api/ewelink/oauth/callback",
        )

    def test_dashboard_offers_account_import_without_developer_credentials(self):
        with TestClient(app) as client:
            dashboard = client.get("/").text

        self.assertIn("VisionGate", dashboard)
        self.assertIn('src="/logo.png"', dashboard)
        self.assertIn('id="yoloModel"', dashboard)
        self.assertIn('id="matchMargin"', dashboard)
        self.assertIn('id="enrollmentDialog"', dashboard)
        self.assertIn('id="testCameraConnection"', dashboard)
        self.assertIn('id="autoCloseSeconds"', dashboard)
        self.assertIn('id="performanceMode"', dashboard)
        self.assertIn('id="appName"', dashboard)
        self.assertIn('id="brandPalette"', dashboard)
        self.assertIn('id="appLogo"', dashboard)
        self.assertIn('id="ewelinkQrLogin"', dashboard)
        self.assertIn('id="ewelinkPasswordLogin"', dashboard)
        self.assertIn('id="ewelinkImportDevice"', dashboard)
        self.assertIn("No developer account required", dashboard)
        self.assertIn("Device IP (optional)", dashboard)
        self.assertIn("The account password is never saved", dashboard)
        self.assertIn('id="logout"', dashboard)
        self.assertIn("Configure Login.bat", dashboard)

    def test_ewelink_password_import_does_not_store_or_return_credentials(self):
        cloud_devices = [
            {
                "id": "1000abcd12",
                "name": "Garage",
                "model": "4CHPROR2",
                "online": True,
                "device_key": "device-secret",
            }
        ]
        with patch.object(
            app_module.EWELINK_CLOUD,
            "account_devices",
            return_value=cloud_devices,
        ) as account_devices, patch(
            "app.add_lan_addresses", side_effect=lambda devices: devices
        ):
            with TestClient(app, client=("127.0.0.1", 50000)) as client:
                response = client.post(
                    "/api/ewelink/import/password",
                    json={
                        "account": "owner@example.com",
                        "password": "account-secret",
                        "country_code": "+351",
                        "region": "eu",
                    },
                )

        self.assertEqual(response.status_code, 200)
        account_devices.assert_called_once_with(
            "owner@example.com", "account-secret", "+351", "eu"
        )
        rendered = json.dumps(response.json())
        self.assertNotIn("account-secret", rendered)
        self.assertNotIn("device-secret", rendered)
        self.assertEqual(response.json()["devices"][0]["id"], "1000abcd12")
        saved = app_module.DATABASE.settings()
        self.assertNotIn("ewelink_account", saved)
        self.assertNotIn("ewelink_password", saved)
        self.assertNotIn("ewelink_app_secret", saved)

    def test_ewelink_login_is_rejected_over_an_unencrypted_lan_request(self):
        with TestClient(app, client=("192.168.1.90", 50000)) as client:
            qr = client.post(
                "/api/ewelink/oauth/start",
                json={"app_id": "developer-app", "app_secret": "developer-secret"},
            )
            password = client.post(
                "/api/ewelink/import/password",
                json={
                    "account": "owner@example.com",
                    "password": "account-secret",
                    "country_code": "+351",
                    "region": "eu",
                },
            )

        self.assertEqual(qr.status_code, 403)
        self.assertEqual(password.status_code, 403)

    def test_imported_device_is_saved_with_user_selected_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "import.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            manager = VisionManager(database, _config(settings))
            session_id = app_module.EWELINK_IMPORTS.ready(
                [
                    {
                        "id": "1000abcd12",
                        "name": "Garage door",
                        "model": "4CHPROR2",
                        "online": True,
                        "device_key": "device-secret",
                    }
                ]
            )
            original_manager = app_module.MANAGER
            app_module.MANAGER = manager
            try:
                with TestClient(app) as client:
                    response = client.post(
                        "/api/ewelink/import/apply",
                        json={
                            "session_id": session_id,
                            "device_id": "1000abcd12",
                            "host": "192.168.1.44",
                            "port": 8081,
                            "open_channel": 1,
                            "close_channel": 2,
                            "pulse_seconds": 1.0,
                        },
                    )
            finally:
                app_module.MANAGER = original_manager

            self.assertEqual(response.status_code, 200)
            saved = database.settings()
            self.assertEqual(saved["ewelink_device_id"], "1000abcd12")
            self.assertEqual(saved["ewelink_device_key"], "device-secret")
            self.assertEqual(saved["ewelink_host"], "192.168.1.44")
            self.assertEqual(saved["ewelink_open_channel"], 1)
            self.assertEqual(saved["ewelink_close_channel"], 2)

    def test_cloud_device_can_be_applied_without_a_lan_ip(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "cloud-import.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            manager = VisionManager(database, _config(settings))
            session_id = app_module.EWELINK_IMPORTS.ready(
                [
                    {
                        "id": "1000abcd12",
                        "name": "Garage door",
                        "model": "4CHPROR2",
                        "online": True,
                        "device_key": "device-secret",
                        "_cloud_token": "access-token",
                        "_cloud_app_id": "app-id",
                        "_cloud_region": "eu",
                    }
                ]
            )
            original_manager = app_module.MANAGER
            app_module.MANAGER = manager
            try:
                with TestClient(app) as client:
                    response = client.post(
                        "/api/ewelink/import/apply",
                        json={
                            "session_id": session_id,
                            "device_id": "1000abcd12",
                            "host": "",
                            "port": 8081,
                            "open_channel": 1,
                            "close_channel": 2,
                            "pulse_seconds": 1.0,
                        },
                    )
            finally:
                app_module.MANAGER = original_manager

            self.assertEqual(response.status_code, 200)
            saved = database.settings()
            self.assertEqual(saved["ewelink_host"], "")
            self.assertEqual(saved["ewelink_cloud_token"], "access-token")


if __name__ == "__main__":
    unittest.main()
