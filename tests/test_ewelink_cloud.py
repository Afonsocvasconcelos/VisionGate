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
    EWeLinkCloudError,
    ImportSessions,
    cloud_device_request,
    cloud_status_request,
    cloud_switch_request,
    device_capabilities,
    service_device_id,
    typed_device_action,
    websocket_handshake,
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
    def test_remote_errors_never_echo_account_secrets(self):
        response = FakeResponse(
            {"error": 401, "msg": "password=do-not-return", "data": {}}
        )
        with patch("ewelink_cloud.urlopen", return_value=response):
            with self.assertRaises(EWeLinkCloudError) as raised:
                EWeLinkCloud._open(object())

        self.assertNotIn("do-not-return", str(raised.exception))

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
            "http://127.0.0.1:83/api/ewelink/oauth/callback",
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
            ["http://127.0.0.1:83/api/ewelink/oauth/callback"],
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
                                    "uiid": 126,
                                    "params": {
                                        "switches": [
                                            {"outlet": 0, "switch": "off"},
                                            {"outlet": 1, "switch": "on"},
                                        ]
                                    },
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
                "http://127.0.0.1:83/api/ewelink/oauth/callback",
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
                    "uiid": 126,
                    "params": {
                        "switches": [
                            {"outlet": 0, "switch": "off"},
                            {"outlet": 1, "switch": "on"},
                        ]
                    },
                    "capabilities": [
                        {
                            "id": "switches",
                            "type": "channels",
                            "channels": [1, 2],
                            "writable": True,
                        }
                    ],
                    "_cloud_token": "access-token",
                    "_cloud_app_id": "client-id",
                    "_cloud_region": "eu",
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
            FakeResponse(
                {
                    "error": 0,
                    "data": {
                        "at": "access-token",
                        "user": {"apikey": "account-api-key"},
                    },
                    "msg": "",
                }
            ),
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
            FakeResponse(
                {
                    "error": 0,
                    "data": {
                        "at": "access-token",
                        "user": {"apikey": "account-api-key"},
                    },
                    "msg": "",
                }
            ),
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
        self.assertEqual(devices[0]["_cloud_user_apikey"], "account-api-key")

    def test_websocket_handshake_uses_saved_account_authorization(self):
        payload = websocket_handshake(
            "access-token",
            "app-id",
            "account-api-key",
            timestamp=1_700_000_000.25,
        )

        self.assertEqual(
            payload,
            {
                "action": "userOnline",
                "at": "access-token",
                "apikey": "account-api-key",
                "appid": "app-id",
                "nonce": "17000000",
                "ts": 1700000000,
                "userAgent": "app",
                "sequence": "1700000000250",
                "version": 8,
            },
        )

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

    def test_typed_actions_only_allow_declared_device_capabilities(self):
        capabilities = [
            {
                "id": "switches",
                "type": "channels",
                "channels": [1, 2],
                "writable": True,
            },
            {
                "id": "temperatureCorrection",
                "type": "number",
                "minimum": -20,
                "maximum": 20,
                "writable": True,
            },
            {
                "id": "startup",
                "type": "enum",
                "options": ["on", "off", "stay"],
                "writable": True,
            },
        ]

        self.assertEqual(
            typed_device_action(
                capabilities, "switch", {"channel": 2, "state": "on"}
            )["params"],
            {"switches": [{"outlet": 1, "switch": "on"}]},
        )
        self.assertEqual(
            typed_device_action(
                capabilities,
                "number",
                {"property": "temperatureCorrection", "value": -2.5},
            )["params"],
            {"temperatureCorrection": -2.5},
        )
        self.assertEqual(
            typed_device_action(
                capabilities,
                "enum",
                {"property": "startup", "value": "stay"},
            )["params"],
            {"startup": "stay"},
        )
        with self.assertRaises(ValueError):
            typed_device_action(
                capabilities, "switch", {"channel": 4, "state": "on"}
            )
        with self.assertRaises(ValueError):
            typed_device_action(
                capabilities,
                "number",
                {"property": "temperatureCorrection", "value": 100},
            )
        with self.assertRaises(ValueError):
            typed_device_action(capabilities, "raw", {"params": {"switch": "on"}})

    def test_light_actions_only_send_fields_reported_by_the_device(self):
        capabilities = device_capabilities(
            59,
            {
                "switch": "off",
                "bright": 40,
                "colorR": 1,
                "colorG": 2,
                "colorB": 3,
                "mode": 1,
                "light_type": 1,
            },
        )

        self.assertEqual(
            typed_device_action(capabilities, "light", {"mode": "on"})["params"],
            {"switch": "on"},
        )
        self.assertEqual(
            typed_device_action(
                capabilities, "light", {"mode": "color", "color": "#12aBef"}
            )["params"],
            {
                "colorR": 18,
                "colorG": 171,
                "colorB": 239,
                "mode": 1,
                "light_type": 1,
            },
        )
        with self.assertRaises(ValueError):
            typed_device_action(
                capabilities, "light", {"mode": "color", "color": "red"}
            )

    def test_cover_actions_follow_each_reported_protocol_and_position_scale(self):
        cases = (
            (
                {"motorTurn": 0, "currLocation": 25},
                {"motorTurn": 1},
                {"location": 70},
                25,
            ),
            (
                {"curtainAction": "pause", "curPercent": 25},
                {"curtainAction": "open"},
                {"openPercent": 30},
                75,
            ),
            (
                {"switch": "pause", "setclose": 25},
                {"switch": "on"},
                {"setclose": 30},
                75,
            ),
            (
                {"electromotor": 1, "percentageControl": 25},
                {"electromotor": 0},
                {"percentageControl": 30},
                75,
            ),
        )

        for params, open_command, position_command, current_position in cases:
            with self.subTest(params=params):
                capabilities = device_capabilities(None, params)
                cover = next(item for item in capabilities if item["type"] == "cover")
                self.assertEqual(cover["position"], current_position)
                self.assertEqual(
                    typed_device_action(
                        capabilities, "cover", {"action": "open"}
                    )["params"],
                    open_command,
                )
                self.assertEqual(
                    typed_device_action(
                        capabilities,
                        "cover",
                        {"action": "position", "position": 70},
                    )["params"],
                    position_command,
                )

        read_only_position = device_capabilities(None, {"op": 2, "per": 40})
        with self.assertRaises(ValueError):
            typed_device_action(
                read_only_position,
                "cover",
                {"action": "position", "position": 70},
            )

    def test_generic_cloud_command_contains_only_validated_parameters(self):
        request = cloud_device_request(
            "access-token",
            "app-id",
            "eu",
            "1000abcd12",
            {"startup": "stay"},
        )

        self.assertEqual(request.method, "POST")
        self.assertEqual(
            json.loads(request.data),
            {"type": 1, "id": "1000abcd12", "params": {"startup": "stay"}},
        )

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

    def test_import_status_exposes_capabilities_but_never_raw_parameters(self):
        sessions = ImportSessions()
        session_id = sessions.ready(
            [
                {
                    "id": "1000abcd12",
                    "name": "Garage",
                    "model": "4CHPROR2",
                    "online": True,
                    "device_key": "secret-device-key",
                    "uiid": 126,
                    "params": {"secretUnknownValue": "do-not-return"},
                    "capabilities": device_capabilities(
                        126, {"switches": [{"outlet": 0, "switch": "off"}]}
                    ),
                }
            ]
        )

        status = sessions.status(session_id)

        self.assertEqual(status["devices"][0]["uiid"], 126)
        self.assertEqual(status["devices"][0]["capabilities"][0]["id"], "switches")
        self.assertNotIn("secretUnknownValue", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
