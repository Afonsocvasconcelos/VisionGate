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

TEST_DATA = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = TEST_DATA.name
os.environ["DISABLE_VISION"] = "1"
os.environ["EWELINK_HOST"] = ""
os.environ["EWELINK_DEVICE_ID"] = ""
os.environ["EWELINK_DEVICE_KEY"] = ""
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
    authorized_presence_events,
    detection_caption,
    object_class_events,
    app,
    spatial_layout_descriptor,
    vision_runtime,
)
from core import Database, Match, Profile
from ewelink_devices import EWeLinkDeviceManager
from enrollment import EnrollmentManager


class TestClient(BaseTestClient):
    def __enter__(self):
        self._device_manager = app_module.MANAGER.devices
        self._device_cloud = self._device_manager.cloud
        self._device_manager.cloud = None
        try:
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
        except Exception:
            self._device_manager.cloud = self._device_cloud
            raise

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self._device_manager.cloud = self._device_cloud


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
            self.assertEqual(controller.status()["state"], "unknown")
            self.assertEqual(controller.status()["last_command"], "close")
            restarted = DoorController(config, database).status()
            self.assertEqual(restarted["state"], "unknown")
            self.assertEqual(restarted["last_command"], "close")


    def test_dashboard_has_camera_delete_and_persistent_automation_picker_without_primary_door(self):
        with TestClient(app) as client:
            dashboard = client.get("/").text
            script = client.get("/dashboard.js").text
            editor = client.get("/automations").text
            editor_script = client.get("/automations.js").text

        combined = dashboard + script + editor + editor_script
        self.assertIn("cameraDelete", script)
        self.assertIn('id="dashboardAutomation"', dashboard)
        self.assertIn('id="dashboardModules"', dashboard)
        self.assertIn('id="customizeDashboard"', dashboard)
        self.assertIn('class="node-template trigger"', editor)
        self.assertIn("dataTransfer", editor_script)
        self.assertIn("runDashboardAutomation", script)
        self.assertIn("/api/dashboard/automation/modules", script)
        self.assertIn("draggable", script)
        self.assertNotIn("Primary Door", combined)
        self.assertNotIn("primary_door", combined)

    def test_dashboard_automation_selection_persists_in_the_database(self):
        graph = {
            "schema_version": 1,
            "name": "First",
            "enabled": False,
            "revision": 1,
            "max_concurrent_runs": 1,
            "nodes": [{"id": "manual", "kind": "trigger.manual", "config": {}}],
            "edges": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "dashboard-selection.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            first = database.create_automation("First", graph)
            second = database.create_automation("Second", {**graph, "name": "Second"})
            manager = VisionManager(database, _config(settings))
            with patch("app.DATABASE", database), patch("app.MANAGER", manager):
                with TestClient(app) as client:
                    initial = client.get("/api/dashboard/automation")
                    selected = client.put(
                        "/api/dashboard/automation", json={"automation_id": second.id}
                    )
                    reloaded = client.get("/api/dashboard/automation")

            self.assertEqual(initial.json()["selected_id"], first.id)
            self.assertEqual(selected.status_code, 200)
            self.assertEqual(reloaded.json()["selected_id"], second.id)
            self.assertEqual(Database(database.path).settings()["dashboard_automation_id"], second.id)

    def test_dashboard_selection_returns_and_persists_automation_specific_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "dashboard-modules.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            camera = database.add_camera(
                "Driveway", "rtsp://camera.local:554/live", enabled=False
            )
            database.sync_ewelink_devices(
                [
                    {
                        "id": "1000abcd12",
                        "name": "Gate relay",
                        "model": "4CHPROR2",
                        "device_key": "device-key",
                        "uiid": 126,
                        "online": True,
                        "params": {"switches": []},
                    }
                ]
            )
            graph = {
                "schema_version": 1,
                "name": "Manual gate",
                "enabled": True,
                "revision": 1,
                "max_concurrent_runs": 1,
                "nodes": [
                    {"id": "manual", "kind": "trigger.manual", "config": {}},
                    {
                        "id": "camera",
                        "kind": "action.camera.disable",
                        "config": {"camera_id": camera.id},
                    },
                    {
                        "id": "relay",
                        "kind": "action.ewelink.button",
                        "config": {
                            "device_id": "1000abcd12",
                            "channel": 1,
                            "pulse_seconds": 1,
                        },
                    },
                ],
                "edges": [
                    {
                        "id": "to-camera",
                        "from": "manual",
                        "to": "camera",
                        "outcome": "success",
                        "steps": [],
                    },
                    {
                        "id": "to-relay",
                        "from": "manual",
                        "to": "relay",
                        "outcome": "success",
                        "steps": [],
                    },
                ],
            }
            first = database.create_automation("Manual gate", graph, enabled=True)
            second = database.create_automation(
                "Manual only",
                {
                    **graph,
                    "name": "Manual only",
                    "nodes": [graph["nodes"][0]],
                    "edges": [],
                },
            )
            manager = VisionManager(database, _config(settings))
            with patch("app.DATABASE", database), patch("app.MANAGER", manager):
                with TestClient(app) as client:
                    initial = client.get("/api/dashboard/automation")
                    saved = client.put(
                        "/api/dashboard/automation/modules",
                        json={
                            "automation_id": first.id,
                            "modules": ["ewelink:1000abcd12", "manual"],
                        },
                    )
                    reloaded = client.get("/api/dashboard/automation")
                    selected = client.put(
                        "/api/dashboard/automation",
                        json={"automation_id": second.id},
                    )
                    rejected = client.put(
                        "/api/dashboard/automation/modules",
                        json={
                            "automation_id": second.id,
                            "modules": [f"camera:{camera.id}"],
                        },
                    )

            self.assertEqual(
                initial.json()["available_modules"],
                ["manual", f"camera:{camera.id}", "ewelink:1000abcd12"],
            )
            self.assertEqual(initial.json()["modules"], initial.json()["available_modules"])
            self.assertTrue(initial.json()["selected"]["has_manual_trigger"])
            self.assertEqual(saved.json()["modules"], ["ewelink:1000abcd12", "manual"])
            self.assertEqual(reloaded.json()["modules"], ["ewelink:1000abcd12", "manual"])
            self.assertEqual(selected.json()["selected"]["id"], second.id)
            self.assertEqual(selected.json()["modules"], ["manual"])
            self.assertEqual(rejected.status_code, 422)
            self.assertEqual(
                Database(database.path).settings()["dashboard_layouts"][str(first.id)],
                ["ewelink:1000abcd12", "manual"],
            )


    def test_existing_door_events_seed_last_known_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "door-state.db")
            database.add_event("door_close", "Door close command sent")
            database.add_event("door_open", "Door open command sent")

            controller = DoorController(CONFIG, database)
            self.assertEqual(controller.status()["state"], "unavailable")
            self.assertEqual(controller.status()["last_command"], "open")
            self.assertEqual(database.settings()["door_last_command"], "open")

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
            self.assertEqual(status["state"], "changing")
            self.assertIsNotNone(status["last_state_check"])

    def test_idle_momentary_relays_report_unknown_and_failed_checks_report_unavailable(self):
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
            controller = DoorController(config, Database(Path(directory) / "honest-state.db"))
            with patch.object(
                controller,
                "_query_switches",
                return_value={0: "off", 1: "off", 2: "off", 3: "off"},
            ):
                self.assertTrue(controller.refresh_state())
            self.assertEqual(controller.status()["state"], "unknown")
            self.assertEqual(controller.status()["state_source"], "momentary_relay")

            with patch.object(
                controller, "_query_switches", side_effect=RuntimeError("offline")
            ):
                self.assertFalse(controller.refresh_state())
            self.assertEqual(controller.status()["state"], "unavailable")

    def test_primary_device_door_sensor_reports_authoritative_open_and_closed_state(self):
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
            database = Database(Path(directory) / "door-sensor.db")
            database.sync_ewelink_devices(
                [
                    {
                        "id": "1000abcd12",
                        "name": "Gate",
                        "model": "Door sensor relay",
                        "device_key": "1234567890abcdef",
                        "uiid": 126,
                        "host": "127.0.0.1",
                        "online": True,
                        "params": {"door": "closed"},
                    }
                ]
            )
            controller = DoorController(config, database)
            with patch.object(controller, "_query_switches") as relay_query:
                database.update_ewelink_device_state(
                    "1000abcd12", {"door": "closed"}, False
                )
                self.assertFalse(controller.refresh_state())
                self.assertEqual(controller.status()["state"], "unavailable")
                self.assertEqual(controller.status()["state_source"], "binary_sensor:door")
                database.update_ewelink_device_state(
                    "1000abcd12", {"door": "closed"}, True
                )
                self.assertTrue(controller.refresh_state())
                self.assertEqual(controller.status()["state"], "closed")
                self.assertEqual(controller.status()["state_source"], "binary_sensor:door")
                database.update_ewelink_device_state(
                    "1000abcd12", {"door": "open"}, True
                )
                self.assertTrue(controller.refresh_state())
                self.assertEqual(controller.status()["state"], "open")
            relay_query.assert_not_called()

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

    def test_manual_open_does_not_send_a_close_command(self):
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

    def test_scene_transitions_emit_authorized_and_class_events_once(self):
        camera = type("Camera", (), {"id": 3, "name": "Front"})()
        profile = Profile(7, "Alice", "person", np.ones(2, np.float32), "now")
        match = Match(profile, 0.94)

        arrived = authorized_presence_events({}, {21: match}, camera)
        unchanged = authorized_presence_events({21: match}, {21: match}, camera)
        left = authorized_presence_events({21: match}, {}, camera)
        classes = object_class_events({"person"}, {"person", "car"}, camera)

        self.assertEqual([kind for kind, _payload in arrived], ["trigger.camera.authorized_presence"])
        self.assertTrue(arrived[0][1]["present"])
        self.assertEqual(unchanged, [])
        self.assertEqual(
            [kind for kind, _payload in left],
            ["trigger.camera.authorized_presence"],
        )
        self.assertFalse(left[0][1]["present"])
        self.assertEqual(classes[0][0], "trigger.camera.class_presence")
        self.assertTrue(classes[0][1]["present"])
        self.assertEqual(classes[0][1]["label"], "car")
        self.assertEqual(arrived[0][1]["profile_name"], "Alice")


class WebAccessTests(unittest.TestCase):
    def test_global_authorized_count_includes_every_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "presence.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            manager = VisionManager(database, _config(settings))
            manager.workers = {
                1: type("Worker", (), {"authorized_count": 0, "camera_state": "connected"})(),
                2: type("Worker", (), {"authorized_count": 1, "camera_state": "connected"})(),
            }

            self.assertEqual(
                manager._automation_state("state.authorized_count", {"camera_id": "*"}, {}),
                1,
            )

    def test_ewelink_online_automation_state_uses_saved_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "online-state.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            database.sync_ewelink_devices(
                [
                    {
                        "id": "1000abcd12",
                        "name": "Gate",
                        "online": True,
                        "params": {"switches": [{"outlet": 0, "switch": "on"}]},
                    }
                ]
            )
            manager = VisionManager(database, _config(settings))

            self.assertTrue(
                manager._automation_state(
                    "state.ewelink_online", {"device_id": "1000abcd12"}, {}
                )
            )
            self.assertEqual(
                manager._automation_state(
                    "state.ewelink_property",
                    {"device_id": "1000abcd12", "property": "channel_1"},
                    {},
                ),
                "on",
            )

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
        self.assertIn(".brand { height: var(--tap); min-height: var(--tap);", stylesheet.text)
        self.assertIn(".automation-name input { min-height: var(--tap);", stylesheet.text)
        self.assertIn(".automation-concurrency input { min-height: var(--tap);", stylesheet.text)
        self.assertIn(".toggle { display: flex !important; align-items: center; min-height: var(--tap);", stylesheet.text)
        self.assertIn(".brand strong { display: none; }", stylesheet.text)
        self.assertIn(".brand img { width: var(--tap); height: var(--tap); }", stylesheet.text)
        self.assertIn(".mobile-edge { min-height: var(--tap);", stylesheet.text)
        self.assertIn(".mini-button { min-width: var(--tap); min-height: var(--tap); }", stylesheet.text)
        self.assertIn("prefers-reduced-motion", stylesheet.text)
        self.assertIn('class="topbar"', dashboard.text)
        self.assertIn('id="automationDashboard"', dashboard.text)
        self.assertIn('id="dashboardModules"', dashboard.text)
        self.assertIn("loadDashboardAutomations", dashboard_script.text)

    def test_everyday_ui_contains_only_operational_controls(self):
        with BaseTestClient(app) as public_client:
            login = public_client.get("/login").text
            stylesheet = public_client.get("/visiongate.css").text
        with TestClient(app) as private_client:
            dashboard = private_client.get("/").text

        self.assertIn('id="dashboardAutomation"', dashboard)
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
                    disabled = manager._automation_action(
                        "action.camera.disable", {"camera_id": first.id}, {}
                    )
                    self.assertFalse(disabled["enabled"])
                    self.assertNotIn(first.id, manager.workers)
                    enabled = manager._automation_action(
                        "action.camera.enable", {"camera_id": first.id}, {}
                    )
                    self.assertTrue(enabled["enabled"])
                    self.assertTrue(manager.workers[first.id].started)
                finally:
                    manager.shutdown()

    def test_editable_settings_and_event_history_are_exposed(self):
        with TestClient(app) as client:
            config = client.get("/api/config")
            self.assertEqual(config.status_code, 200)
            self.assertIn("match_threshold", config.json()["settings"])
            self.assertIn("match_margin", config.json()["settings"])
            self.assertIn("yolo_model", config.json()["settings"])
            self.assertNotIn("auto_close_seconds", config.json()["settings"])
            self.assertNotIn("ewelink_open_channel", config.json()["settings"])
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

    def test_camera_password_and_device_key_are_write_only_and_blank_keeps_them(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "write-only.db")
            saved_settings = database.update_settings(
                {
                    **DEFAULT_SETTINGS,
                    "ewelink_device_id": "1000abcd12",
                    "ewelink_device_key": "relay-secret-value",
                    "ewelink_host": "127.0.0.1",
                }
            )
            camera = database.add_camera(
                "Gate", "rtsp://camera.local/live", "viewer", "camera-secret-value"
            )
            manager = VisionManager(database, _config(saved_settings))
            settings_payload = {
                key: saved_settings[key]
                for key in app_module.SettingsPayload.model_fields
            }
            with patch("app.DATABASE", database), patch("app.MANAGER", manager):
                with TestClient(app) as client:
                    config = client.get("/api/config")
                    updated_camera = client.put(
                        f"/api/cameras/{camera.id}",
                        json={
                            "name": "Gate renamed",
                            "stream_url": camera.stream_url,
                            "username": camera.username,
                            "password": "",
                            "enabled": True,
                        },
                    )
                    updated_settings = client.put(
                        "/api/settings", json=settings_payload
                    )
                    dashboard = client.get("/")

            exposed = config.text + updated_camera.text + updated_settings.text + dashboard.text
            self.assertNotIn("camera-secret-value", exposed)
            self.assertNotIn("relay-secret-value", exposed)
            self.assertNotIn("password", config.json()["cameras"][0])
            self.assertTrue(config.json()["cameras"][0]["password_configured"])
            self.assertNotIn("ewelink_device_key", config.json()["settings"])
            self.assertNotIn("ewelink_device_key_configured", config.json()["settings"])
            self.assertEqual(database.camera(camera.id).password, "camera-secret-value")
            self.assertEqual(database.settings()["ewelink_device_key"], "relay-secret-value")

    def test_validation_errors_never_echo_submitted_secrets(self):
        with TestClient(app) as client:
            camera = client.post(
                "/api/cameras",
                json={
                    "name": " ",
                    "stream_url": "rtsp://camera.local/live",
                    "username": "viewer",
                    "password": "camera-validation-secret",
                    "enabled": True,
                },
            )
            ewelink = client.post(
                "/api/ewelink/import/password",
                json={
                    "account": "owner@example.com",
                    "password": "account-validation-secret",
                    "country_code": "invalid",
                    "region": "eu",
                },
            )

        self.assertEqual(camera.status_code, 422)
        self.assertEqual(ewelink.status_code, 422)
        self.assertNotIn("camera-validation-secret", camera.text)
        self.assertNotIn("account-validation-secret", ewelink.text)
        self.assertNotIn('"input"', camera.text)

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
        self.assertNotIn('id="autoCloseSeconds"', dashboard)
        self.assertIn('href="/automations"', dashboard)
        self.assertIn('id="performanceMode"', dashboard)
        self.assertIn('id="appName"', dashboard)
        self.assertIn('id="brandPalette"', dashboard)
        self.assertIn('id="appLogo"', dashboard)
        self.assertIn('id="ewelinkQrLogin"', dashboard)
        self.assertIn('id="ewelinkPasswordLogin"', dashboard)
        self.assertNotIn('id="ewelinkImportDevice"', dashboard)
        self.assertIn("No developer account required", dashboard)
        self.assertNotIn("Device IP (optional)", dashboard)
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
        self.assertIsNotNone(app_module.DATABASE.ewelink_device("1000abcd12"))
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

    def test_imported_device_is_saved_without_assigning_a_special_role(self):
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
            self.assertEqual(response.json()["device_count"], 1)
            saved = database.ewelink_device("1000abcd12")
            self.assertEqual(saved.device_key, "device-secret")
            self.assertEqual(database.settings()["ewelink_device_id"], "")

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
                            "host": "127.0.0.1",
                            "port": 8081,
                            "open_channel": 1,
                            "close_channel": 2,
                            "pulse_seconds": 1.0,
                        },
                    )
            finally:
                app_module.MANAGER = original_manager

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["device_count"], 1)
            saved = database.settings()
            self.assertEqual(saved["ewelink_cloud_token"], "access-token")

    def test_device_inventory_api_redacts_secrets_and_requires_action_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "inventory-api.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            manager = VisionManager(database, _config(settings))
            database.add("Alice", "person", np.array([1.0], np.float32))
            manager.devices = EWeLinkDeviceManager(database, cloud=None)
            manager.devices.import_devices(
                [
                    {
                        "id": "1000abcd12",
                        "name": "Gate",
                        "model": "4CHPROR2",
                        "device_key": "never-render-this-key",
                        "uiid": 126,
                        "online": True,
                        "params": {
                            "switches": [{"outlet": 0, "switch": "off"}],
                            "unknown": "read-only",
                        },
                    }
                ]
            )
            original_manager = app_module.MANAGER
            app_module.MANAGER = manager
            try:
                with TestClient(app) as client:
                    inventory = client.get("/api/devices")
                    rejected = client.post(
                        "/api/ewelink/devices/1000abcd12/actions/switch",
                        json={"confirm": False, "arguments": {"channel": 1, "state": "on"}},
                    )
                    with patch.object(
                        manager.devices,
                        "execute",
                        return_value={"id": "1000abcd12", "state": {}},
                    ) as execute:
                        accepted = client.post(
                            "/api/ewelink/devices/1000abcd12/actions/switch",
                            json={"confirm": True, "arguments": {"channel": 1, "state": "on"}},
                        )
            finally:
                app_module.MANAGER = original_manager

        self.assertEqual(inventory.status_code, 200)
        rendered = json.dumps(inventory.json())
        self.assertNotIn("never-render-this-key", rendered)
        self.assertEqual(inventory.json()["identities"][0]["name"], "Alice")
        self.assertEqual(inventory.json()["ewelink"][0]["diagnostics"], {"unknown": "read-only"})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(accepted.status_code, 200)
        execute.assert_called_once_with(
            "1000abcd12", "switch", {"channel": 1, "state": "on"}
        )

    def test_applying_an_account_import_keeps_every_device(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "all-imported.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            manager = VisionManager(database, _config(settings))
            session_id = app_module.EWELINK_IMPORTS.ready(
                [
                    {
                        "id": "1000abcd12",
                        "name": "Gate",
                        "model": "4CHPROR2",
                        "online": True,
                        "device_key": "gate-key",
                        "params": {"switches": [{"outlet": 0, "switch": "off"}]},
                    },
                    {
                        "id": "1000ffff99",
                        "name": "Lamp",
                        "model": "BASICR2",
                        "online": False,
                        "device_key": "lamp-key",
                        "params": {"switch": "off"},
                    },
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
                            "host": "127.0.0.1",
                            "port": 8081,
                            "open_channel": 1,
                            "close_channel": 2,
                            "pulse_seconds": 1,
                        },
                    )
            finally:
                app_module.MANAGER = original_manager
            imported_ids = {device.device_id for device in database.ewelink_devices()}

        self.assertEqual(response.status_code, 200)
        self.assertEqual(imported_ids, {"1000abcd12", "1000ffff99"})

    def test_new_installation_does_not_assume_a_device_or_automation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "default-automation.db")
            settings = database.update_settings(DEFAULT_SETTINGS)

            VisionManager(database, _config(settings))

            self.assertEqual(database.automations(), [])

    def test_disabled_automation_allows_confirmed_manual_run_and_persists_history(self):
        graph = {
            "schema_version": 1,
            "name": "Manual log",
            "enabled": False,
            "revision": 1,
            "max_concurrent_runs": 2,
            "nodes": [
                {"id": "manual", "kind": "trigger.manual", "config": {}},
                {
                    "id": "log",
                    "kind": "action.log",
                    "config": {"message": "Test action"},
                },
            ],
            "edges": [
                {
                    "id": "manual-log",
                    "from": "manual",
                    "to": "log",
                    "outcome": "success",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "automation-api.db")
            settings = database.update_settings(DEFAULT_SETTINGS)
            manager = VisionManager(database, _config(settings))
            original_manager = app_module.MANAGER
            app_module.MANAGER = manager
            try:
                with TestClient(app) as client:
                    created = client.post(
                        "/api/automations",
                        json={"name": "Manual log", "enabled": False, "graph": graph},
                    )
                    automation_id = created.json()["id"]
                    listed = client.get("/api/automations")
                    dry_run = client.post(f"/api/automations/{automation_id}/dry-run")
                    rejected = client.post(
                        f"/api/automations/{automation_id}/run",
                        json={"confirm": False},
                    )
                    live = client.post(
                        f"/api/automations/{automation_id}/run",
                        json={"confirm": True},
                    )
                    history = client.get(f"/api/automations/{automation_id}/runs")
                    invalid_graph = {**graph, "edges": graph["edges"] + [
                        {
                            "id": "cycle",
                            "from": "log",
                            "to": "manual",
                            "outcome": "success",
                        }
                    ]}
                    invalid = client.post(
                        "/api/automations/validate", json={"graph": invalid_graph}
                    )
            finally:
                app_module.MANAGER = original_manager

        self.assertEqual(created.status_code, 201)
        self.assertTrue(any(item["id"] == automation_id for item in listed.json()))
        self.assertEqual(dry_run.json()["status"], "completed")
        self.assertTrue(dry_run.json()["result"]["dry_run"])
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(live.json()["status"], "completed")
        self.assertGreaterEqual(len(history.json()), 2)
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("cycle", invalid.text)

    def test_automation_editor_has_desktop_and_phone_controls(self):
        with TestClient(app) as client:
            page = client.get("/automations")
            script = client.get("/automations.js")
            stylesheet = client.get("/visiongate.css")

        self.assertEqual(page.status_code, 200)
        self.assertEqual(script.status_code, 200)
        self.assertIn('id="automationList"', page.text)
        self.assertIn('id="graphCanvas"', page.text)
        self.assertIn('id="graphConnections"', page.text)
        self.assertIn('id="mobileGraph"', page.text)
        self.assertIn('id="nodeInspector"', page.text)
        self.assertIn('id="dryRunAutomation"', page.text)
        self.assertIn('id="runAutomation"', page.text)
        self.assertIn("trigger.schedule", script.text)
        self.assertIn("scheduleTime", script.text)
        self.assertIn("set_variable", script.text)
        self.assertIn("edge-chip-bg", script.text)
        self.assertIn('["position", "Set position"]', script.text)
        self.assertIn('type: "color"', script.text)
        self.assertIn("pointerdown", script.text)
        self.assertIn("ctrlKey", script.text)
        self.assertIn("@media (max-width: 760px)", stylesheet.text)
        self.assertIn(".mobile-graph", stylesheet.text)
        self.assertIn(".graph-node.condition::before", stylesheet.text)
        self.assertIn("clip-path: polygon(50% 0, 100% 50%, 50% 100%, 0 50%)", stylesheet.text)
        self.assertIn(".edge-chip-bg", stylesheet.text)

        with TestClient(app) as client:
            dashboard = client.get("/").text
        self.assertIn('href="/automations"', dashboard)

    def test_enrollment_api_records_reviews_commits_and_manages_samples(self):
        class Worker:
            camera = type("Camera", (), {"id": 1, "name": "Gate"})()
            sequence = 0

            def enrollment_snapshot(self):
                self.sequence += 1
                frame = np.zeros((240, 480, 3), np.uint8)
                vector = np.array(
                    [1.0, 0.0] if self.sequence % 3 else [0.0, 1.0],
                    np.float32,
                )
                return self.sequence, frame, [
                    {
                        "id": 4,
                        "label": "person",
                        "box": (40, 30, 300, 220),
                        "confidence": .9,
                        "embedding": vector,
                    }
                ]

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "enrollment.db")
            camera = database.add_camera("Gate", "rtsp://camera/live", enabled=True)
            worker = Worker()
            enrollments = EnrollmentManager(
                database, Path(directory) / "enrollments", lambda _camera_id: worker
            )
            enrollments.CAPTURE_INTERVAL = .01
            with patch("app.DATABASE", database), patch.object(
                app_module.MANAGER, "enrollments", enrollments
            ):
                with TestClient(app) as client:
                    started = client.post(f"/api/cameras/{camera.id}/enrollment/start")
                    session_id = started.json()["id"]
                    deadline = time.monotonic() + 2
                    review = None
                    while time.monotonic() < deadline:
                        review = client.get(f"/api/enrollments/{session_id}")
                        if len(review.json()["frames"]) >= 3:
                            break
                        time.sleep(.01)
                    stopped = client.post(f"/api/enrollments/{session_id}/stop")
                    frames = stopped.json()["frames"]
                    sample_ids = [frame["detections"][0]["id"] for frame in frames[:3]]
                    frame_response = client.get(frames[0]["url"])
                    sample_response = client.get(
                        frames[0]["detections"][0]["thumbnail_url"]
                    )
                    committed = client.post(
                        f"/api/enrollments/{session_id}/commit",
                        json={"name": "Alice", "sample_ids": sample_ids},
                    )
                    profile_id = committed.json()["profile"]["id"]
                    samples = client.get(f"/api/profiles/{profile_id}/samples")
                    thumbnail = client.get(samples.json()[0]["thumbnail_url"])
                    deleted = client.delete(
                        f"/api/profiles/{profile_id}/samples/{samples.json()[0]['id']}"
                    )
                    last_rejected = client.delete(
                        f"/api/profiles/{profile_id}/samples/{samples.json()[1]['id']}"
                    )
                    gone = client.get(f"/api/enrollments/{session_id}")

        self.assertEqual(started.status_code, 201)
        self.assertEqual(review.status_code, 200)
        self.assertEqual(stopped.json()["status"], "review")
        self.assertEqual(frame_response.headers["content-type"], "image/jpeg")
        self.assertEqual(sample_response.headers["content-type"], "image/jpeg")
        self.assertEqual(committed.status_code, 201)
        self.assertEqual(committed.json()["added"], 2)
        self.assertEqual(len(samples.json()), 2)
        self.assertEqual(thumbnail.status_code, 200)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(last_rejected.status_code, 409)
        self.assertEqual(gone.status_code, 404)

    def test_dashboard_uses_record_review_multi_sample_enrollment(self):
        with TestClient(app) as client:
            dashboard = client.get("/").text
            script = client.get("/dashboard.js").text
            stylesheet = client.get("/visiongate.css").text

        for element_id in (
            "enrollmentFrame",
            "enrollmentBoxes",
            "enrollmentTimeline",
            "selectedSamples",
            "enrollmentTarget",
            "samplesDialog",
            "profileSamples",
        ):
            self.assertIn(f'id="{element_id}"', dashboard)
        self.assertIn("Record samples", script)
        self.assertIn("/enrollment/start", script)
        self.assertIn("/api/enrollments/", script)
        self.assertIn("/api/profiles/", script)
        self.assertNotIn("enrollmentX", script)
        self.assertIn(".enrollment-stage", stylesheet)
        self.assertIn(".selected-samples", stylesheet)

    def test_settings_show_searchable_capability_specific_ewelink_inventory(self):
        with TestClient(app) as client:
            dashboard = client.get("/").text
            script = client.get("/dashboard.js").text
            stylesheet = client.get("/visiongate.css").text

        for element_id in (
            "ewelinkConnection",
            "refreshEwelinkDevices",
            "ewelinkDeviceSearch",
            "ewelinkDeviceList",
        ):
            self.assertIn(f'id="{element_id}"', dashboard)
        self.assertNotIn("Primary Door", script)
        self.assertIn("addCapabilityControls", script)
        self.assertIn("runEwelinkDeviceAction", script)
        self.assertIn('brightness.type = "range"', script)
        self.assertIn('color.type = "color"', script)
        self.assertIn('position.type = "range"', script)
        self.assertIn("Read-only diagnostics", script)
        self.assertIn("confirm(`", script)
        self.assertIn(".device-card", stylesheet)
        self.assertIn(".capability-row", stylesheet)


if __name__ == "__main__":
    unittest.main()
