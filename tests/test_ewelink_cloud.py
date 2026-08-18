import base64
import hashlib
import hmac
import json
import re
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


from ewelink_cloud import (
    EWeLinkCloud,
    ImportSessions,
    cloud_status_request,
    cloud_switch_request,
    service_device_id,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class EWeLinkCloudTests(unittest.TestCase):
    def test_service_name_maps_to_the_matching_device_id(self):
        self.assertEqual(
            service_device_id("eWeLink_1000abcd12._ewelink._tcp.local."),
            "1000abcd12",
        )
        self.assertIsNone(service_device_id("other-device._ewelink._tcp.local."))

    def test_qr_authorization_url_is_signed_and_uses_registered_callback(self):
        url = EWeLinkCloud.authorization_url(
            "client-id",
            "client-secret",
            "http://127.0.0.1:8000/api/ewelink/oauth/callback",
            "csrf-state",
            seq="123",
            nonce="a1B2c3D4",
        )

        query = parse_qs(urlsplit(url).query)
        expected = base64.b64encode(
            hmac.new(b"client-secret", b"client-id_123", hashlib.sha256).digest()
        ).decode()
        self.assertEqual(query["authorization"], [expected])
        self.assertEqual(query["showQRCode"], ["true"])
        self.assertEqual(query["state"], ["csrf-state"])
        self.assertEqual(
            query["redirectUrl"],
            ["http://127.0.0.1:8000/api/ewelink/oauth/callback"],
        )

    def test_qr_nonce_is_exactly_eight_alphanumeric_characters(self):
        with patch("ewelink_cloud.secrets.token_urlsafe", return_value="-_______"):
            url = EWeLinkCloud.authorization_url(
                "client-id", "client-secret", "http://127.0.0.1/callback", "csrf-state"
            )

        nonce = parse_qs(urlsplit(url).query)["nonce"][0]
        self.assertRegex(nonce, re.compile(r"^[A-Za-z0-9]{8}$"))

    def test_oauth_exchanges_code_and_returns_only_real_devices(self):
        responses = [
            FakeResponse(
                {"error": 0, "data": {"accessToken": "access-token"}, "msg": ""}
            ),
            FakeResponse(
                {
                    "error": 0,
                    "data": {
                        "thingList": [
                            {
                                "itemType": 1,
                                "itemData": {
                                    "name": "Garage",
                                    "deviceid": "1000abcd12",
                                    "devicekey": "secret-device-key",
                                    "productModel": "4CHPROR2",
                                    "online": True,
                                },
                            },
                            {"itemType": 3, "itemData": {"name": "A group"}},
                        ]
                    },
                    "msg": "",
                }
            ),
        ]

        with patch("ewelink_cloud.urlopen", side_effect=responses) as mocked:
            devices = EWeLinkCloud().oauth_devices(
                "client-id",
                "client-secret",
                "http://127.0.0.1:8000/api/ewelink/oauth/callback",
                "authorization-code",
                "eu",
            )

        self.assertEqual(
            devices,
            [
                {
                    "id": "1000abcd12",
                    "name": "Garage",
                    "model": "4CHPROR2",
                    "online": True,
                    "device_key": "secret-device-key",
                }
            ],
        )
        token_request, thing_request = [call.args[0] for call in mocked.call_args_list]
        self.assertEqual(
            token_request.full_url, "https://eu-apia.coolkit.cc/v2/user/oauth/token"
        )
        self.assertEqual(json.loads(token_request.data)["code"], "authorization-code")
        self.assertEqual(
            thing_request.full_url,
            "https://eu-apia.coolkit.cc/v2/device/thing?num=0",
        )
        self.assertEqual(thing_request.headers["Authorization"], "Bearer access-token")

    def test_phone_login_adds_the_selected_country_code(self):
        responses = [
            FakeResponse({"error": 0, "data": {"at": "access-token"}, "msg": ""}),
            FakeResponse({"error": 0, "data": {"thingList": []}, "msg": ""}),
        ]

        with patch("ewelink_cloud.urlopen", side_effect=responses) as mocked:
            EWeLinkCloud().password_devices(
                "client-id",
                "client-secret",
                "912345678",
                "password",
                "+351",
                "eu",
            )

        login_request = mocked.call_args_list[0].args[0]
        self.assertEqual(json.loads(login_request.data)["phoneNumber"], "+351912345678")

    def test_account_login_does_not_require_developer_credentials(self):
        responses = [
            FakeResponse({"error": 0, "data": {"at": "access-token"}, "msg": ""}),
            FakeResponse(
                {
                    "error": 0,
                    "data": {
                        "thingList": [
                            {
                                "itemData": {
                                    "deviceid": "1000abcd12",
                                    "devicekey": "device-key",
                                    "name": "Gate",
                                }
                            }
                        ]
                    },
                    "msg": "",
                }
            ),
        ]

        with patch("ewelink_cloud.urlopen", side_effect=responses) as mocked:
            devices = EWeLinkCloud().account_devices(
                "owner@example.com", "account-secret", "+351", "eu"
            )

        login_request = mocked.call_args_list[0].args[0]
        self.assertEqual(
            login_request.headers["X-ck-appid"],
            "R8Oq3y0eSZSYdKccHlrQzT1ACCOUT9Gv",
        )
        self.assertEqual(json.loads(login_request.data)["email"], "owner@example.com")
        self.assertEqual(devices[0]["_cloud_token"], "access-token")
        self.assertEqual(devices[0]["_cloud_region"], "eu")

    def test_cloud_switch_request_targets_the_selected_4ch_channel(self):
        request = cloud_switch_request(
            "access-token", "app-id", "eu", "1000abcd12", 2, "on"
        )

        self.assertEqual(
            request.full_url,
            "https://eu-apia.coolkit.cc/v2/device/thing/status",
        )
        self.assertEqual(request.headers["Authorization"], "Bearer access-token")
        self.assertEqual(request.headers["X-ck-appid"], "app-id")
        self.assertEqual(
            json.loads(request.data),
            {
                "type": 1,
                "id": "1000abcd12",
                "params": {"switches": [{"switch": "on", "outlet": 1}]},
            },
        )

    def test_cloud_status_request_reads_all_relay_channels(self):
        request = cloud_status_request(
            "access-token", "app-id", "eu", "1000abcd12"
        )

        self.assertEqual(request.method, "GET")
        self.assertEqual(
            request.full_url,
            "https://eu-apia.coolkit.cc/v2/device/thing/status?type=1&id=1000abcd12&params=switches",
        )
        self.assertEqual(request.headers["Authorization"], "Bearer access-token")

    def test_malformed_cloud_items_are_ignored(self):
        response = FakeResponse(
            {
                "error": 0,
                "data": {
                    "thingList": [
                        {"itemType": 1, "itemData": None},
                        {"itemType": 1, "itemData": "invalid"},
                        "invalid",
                    ]
                },
                "msg": "",
            }
        )

        with patch("ewelink_cloud.urlopen", return_value=response):
            devices = EWeLinkCloud._devices("client-id", "access-token", "eu")

        self.assertEqual(devices, [])

    def test_import_status_never_exposes_device_keys(self):
        sessions = ImportSessions()
        session_id = sessions.ready(
            [
                {
                    "id": "1000abcd12",
                    "name": "Garage",
                    "model": "4CHPROR2",
                    "online": True,
                    "device_key": "secret-device-key",
                    "_cloud_token": "cloud-access-token",
                    "_cloud_app_id": "cloud-app-id",
                    "_cloud_region": "eu",
                    "host": "192.168.1.44",
                    "port": 8081,
                }
            ]
        )

        status = sessions.status(session_id)
        self.assertEqual(status["status"], "ready")
        self.assertNotIn("device_key", json.dumps(status))
        self.assertNotIn("cloud-access-token", json.dumps(status))
        self.assertEqual(status["devices"][0]["host"], "192.168.1.44")
        self.assertEqual(
            sessions.take(session_id, "1000abcd12")["device_key"],
            "secret-device-key",
        )
        with self.assertRaises(KeyError):
            sessions.status(session_id)


if __name__ == "__main__":
    unittest.main()
