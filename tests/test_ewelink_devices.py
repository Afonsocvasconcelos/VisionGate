import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

from core import Database
from ewelink_devices import EWeLinkDeviceManager


class FakeResponse:
    status = 200

    def __init__(self, payload=None):
        self.payload = json.dumps(payload or {"error": 0}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def four_channel_device(**values):
    return {
        "id": "1000abcd12",
        "name": "Gate",
        "model": "4CHPROR2",
        "device_key": "secret-device-key",
        "uiid": 126,
        "online": True,
        "params": {
            "switches": [
                {"outlet": 0, "switch": "off"},
                {"outlet": 1, "switch": "off"},
                {"outlet": 2, "switch": "off"},
                {"outlet": 3, "switch": "off"},
            ]
        },
        "_cloud_token": "cloud-token",
        "_cloud_app_id": "cloud-app",
        "_cloud_region": "eu",
        "_cloud_user_apikey": "account-api-key",
        **values,
    }


class EWeLinkDeviceManagerTests(unittest.TestCase):
    def test_live_reconnect_uses_bounded_exponential_backoff(self):
        class DatabaseSettings:
            @staticmethod
            def settings():
                return {
                    "ewelink_cloud_token": "token",
                    "ewelink_cloud_app_id": "app",
                    "ewelink_cloud_region": "eu",
                    "ewelink_cloud_user_apikey": "user",
                }

        class FailingCloud:
            @staticmethod
            def listen_updates(*_args):
                raise OSError("offline")

        class StopAfterWaits:
            def __init__(self):
                self.waits = []
                self.stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, seconds):
                self.waits.append(seconds)
                self.stopped = len(self.waits) == 8
                return self.stopped

        manager = EWeLinkDeviceManager(DatabaseSettings(), FailingCloud())
        manager._stop = StopAfterWaits()

        manager._live()

        self.assertEqual(manager._stop.waits, [15, 30, 60, 120, 240, 480, 900, 900])

    def test_inventory_poll_reconciles_every_sixty_seconds(self):
        class StopAfterOneWait:
            stopped = False
            seconds = None

            def is_set(self):
                return self.stopped

            def wait(self, seconds):
                self.seconds = seconds
                self.stopped = True
                return True

        manager = EWeLinkDeviceManager(object(), cloud=None)
        manager._stop = StopAfterOneWait()
        calls = []
        manager.refresh = lambda: calls.append(True)

        manager._poll()

        self.assertEqual(calls, [True])
        self.assertEqual(manager._stop.seconds, 60)

    def test_live_connection_survives_a_temporary_settings_error(self):
        class BrokenDatabase:
            def settings(self):
                manager._stop.set()
                raise OSError("database temporarily unavailable")

        manager = EWeLinkDeviceManager(BrokenDatabase(), cloud=object())

        manager._live()

        self.assertTrue(manager._stop.is_set())

    def test_import_saves_every_device_and_public_inventory_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "devices.db")
            manager = EWeLinkDeviceManager(database, cloud=None)

            public = manager.import_devices(
                [
                    four_channel_device(),
                    {
                        "id": "1000ffff99",
                        "name": "Lamp",
                        "model": "BASICR2",
                        "device_key": "lamp-key",
                        "uiid": 1,
                        "online": False,
                        "params": {"switch": "off", "unknown": "diagnostic"},
                        "_cloud_token": "cloud-token",
                        "_cloud_app_id": "cloud-app",
                        "_cloud_region": "eu",
                    },
                ]
            )

            self.assertEqual(len(public), 2)
            serialized = json.dumps(public)
            self.assertNotIn("secret-device-key", serialized)
            self.assertNotIn("lamp-key", serialized)
            self.assertNotIn("cloud-token", serialized)
            self.assertEqual(database.settings()["ewelink_cloud_region"], "eu")
            self.assertEqual(
                database.settings()["ewelink_cloud_user_apikey"], "account-api-key"
            )
            self.assertEqual(len(Database(database.path).ewelink_devices()), 2)
            self.assertEqual(public[1]["diagnostics"], {"unknown": "diagnostic"})
            self.assertEqual(public[0]["connections"], {"lan": False, "cloud": True})
            self.assertEqual(public[0]["state"]["channel_1"], "off")
            self.assertEqual(public[0]["state"]["channel_4"], "off")

    def test_cloud_action_is_validated_sent_and_reconciled_locally(self):
        opened = []

        def opener(request, timeout=0):
            opened.append((request, timeout))
            return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "cloud.db")
            manager = EWeLinkDeviceManager(database, cloud=None, opener=opener)
            manager.import_devices([four_channel_device()])

            result = manager.execute(
                "1000abcd12", "switch", {"channel": 2, "state": "on"}
            )

            request = opened[0][0]
            self.assertEqual(request.full_url, "https://eu-apia.coolkit.cc/v2/device/thing/status")
            self.assertEqual(
                json.loads(request.data)["params"],
                {"switches": [{"outlet": 1, "switch": "on"}]},
            )
            self.assertEqual(result["state"]["switches"][1]["switch"], "on")

    def test_lan_failure_falls_back_to_cloud(self):
        calls = []

        def opener(request, timeout=0):
            calls.append(request.full_url)
            if request.full_url.startswith("http://192.168"):
                raise URLError("offline")
            return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "fallback.db")
            manager = EWeLinkDeviceManager(database, cloud=None, opener=opener)
            manager.import_devices(
                [four_channel_device(host="192.168.1.44", port=8081)]
            )

            manager.execute("1000abcd12", "switch", {"channel": 1, "state": "on"})

            self.assertTrue(calls[0].startswith("http://192.168.1.44:8081/"))
            self.assertTrue(calls[1].startswith("https://eu-apia.coolkit.cc/"))

    def test_refresh_upserts_account_and_emits_property_and_online_changes(self):
        class Cloud:
            def token_devices(self, _app_id, _token, _region):
                device = four_channel_device(online=False)
                device["params"]["switches"][0]["switch"] = "on"
                return [device]

        events = []
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "refresh.db")
            manager = EWeLinkDeviceManager(
                database, cloud=Cloud(), event_sink=lambda kind, payload: events.append((kind, payload))
            )
            manager.import_devices([four_channel_device(online=True)])

            manager.refresh()

            kinds = [kind for kind, _payload in events]
            self.assertIn("trigger.ewelink.offline", kinds)
            self.assertIn("trigger.ewelink.property_changed", kinds)
            self.assertIn(
                (
                    "trigger.ewelink.property_changed",
                    {"device_id": "1000abcd12", "property": "channel_1", "value": "on"},
                ),
                events,
            )
            self.assertFalse(database.ewelink_device("1000abcd12").online)

    def test_websocket_update_merges_state_and_emits_typed_events(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "websocket.db")
            manager = EWeLinkDeviceManager(
                database,
                cloud=None,
                event_sink=lambda kind, payload: events.append((kind, payload)),
            )
            manager.import_devices([four_channel_device()])

            manager.apply_cloud_update(
                {
                    "action": "update",
                    "deviceid": "1000abcd12",
                    "params": {
                        "switches": [{"outlet": 0, "switch": "on"}]
                    },
                }
            )
            manager.apply_cloud_update(
                {
                    "action": "sysmsg",
                    "deviceid": "1000abcd12",
                    "params": {"online": False},
                }
            )

            device = database.ewelink_device("1000abcd12")
            self.assertEqual(device.params["switches"][0]["switch"], "on")
            self.assertFalse(device.online)
            self.assertEqual(
                [kind for kind, _payload in events],
                ["trigger.ewelink.property_changed", "trigger.ewelink.offline"],
            )
            self.assertEqual(events[0][1]["property"], "channel_1")
            self.assertEqual(events[0][1]["value"], "on")

    def test_unknown_secret_properties_are_neither_public_nor_emitted(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "secret-events.db")
            manager = EWeLinkDeviceManager(
                database,
                cloud=None,
                event_sink=lambda kind, payload: events.append((kind, payload)),
            )
            manager.import_devices([four_channel_device()])
            manager.apply_cloud_update(
                {
                    "action": "update",
                    "deviceid": "1000abcd12",
                    "params": {"accessToken": "do-not-expose"},
                }
            )

            public = manager.list_public()

        self.assertNotIn("do-not-expose", json.dumps(public))
        self.assertEqual(events, [])

    def test_light_and_cover_protocol_fields_are_public_state_not_unknown_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "typed-state.db")
            manager = EWeLinkDeviceManager(database, cloud=None)
            manager.import_devices(
                [
                    {
                        "id": "1000light1",
                        "name": "Light",
                        "model": "L1",
                        "device_key": "light-key",
                        "uiid": 59,
                        "online": True,
                        "params": {
                            "switch": "on",
                            "bright": 60,
                            "colorR": 10,
                            "colorG": 20,
                            "colorB": 30,
                            "mode": 1,
                            "unknown": "visible diagnostic",
                        },
                    },
                    {
                        "id": "1000cover1",
                        "name": "Cover",
                        "model": "DualR3",
                        "device_key": "cover-key",
                        "uiid": 126,
                        "online": True,
                        "params": {"motorTurn": 0, "currLocation": 35},
                    },
                ]
            )

            devices = {item["id"]: item for item in manager.list_public()}

        self.assertEqual(devices["1000light1"]["state"]["colorG"], 20)
        self.assertEqual(
            devices["1000light1"]["diagnostics"],
            {"unknown": "visible diagnostic"},
        )
        self.assertEqual(devices["1000cover1"]["state"]["currLocation"], 35)
        self.assertEqual(devices["1000cover1"]["diagnostics"], {})


if __name__ == "__main__":
    unittest.main()
