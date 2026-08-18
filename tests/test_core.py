import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from core import (
    AccessGate,
    Database,
    Profile,
    best_match,
    camera_stream_url,
    local_ipv4_addresses,
    profile_similarity,
    reid_eligible,
    reid_regions,
    ewelink_info_request,
    ewelink_request,
    ewelink_response_data,
    rtsp_url_from_text,
    select_track,
)


class SecurityCoreTests(unittest.TestCase):
    def test_local_access_addresses_exclude_loopback_public_and_virtual_ranges(self):
        with patch(
            "core.socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("127.0.0.1", 0)),
                (2, 1, 6, "", ("192.168.2.197", 0)),
                (2, 1, 6, "", ("8.8.8.8", 0)),
                (2, 1, 6, "", ("26.104.86.239", 0)),
                (2, 1, 6, "", ("169.254.20.10", 0)),
            ],
        ), patch("core._route_ipv4", return_value="192.168.2.197"):
            self.assertEqual(local_ipv4_addresses(), ["192.168.2.197"])

    def test_match_is_class_aware_and_thresholded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Database(Path(directory) / "profiles.db")
            car = store.add("Blue car", "car", np.array([1.0, 0.0], np.float32))
            store.add("Rider", "person", np.array([1.0, 0.0], np.float32))

            match = best_match(
                store.all(), "car", np.array([0.99, 0.01], np.float32), 0.95
            )
            self.assertEqual(match.profile.id, car.id)
            self.assertGreater(match.similarity, 0.99)
            self.assertIsNone(
                best_match(
                    store.all(), "bicycle", np.array([1.0, 0.0], np.float32), 0.5
                )
            )

    def test_enhanced_descriptor_still_matches_a_legacy_profile(self):
        legacy = np.array([1.0, 0.0], np.float32)
        enhanced = np.array([1.0, 0.0, 1.0], np.float32)

        self.assertAlmostEqual(profile_similarity(legacy, enhanced), 1.0)

    def test_ambiguous_same_class_match_is_rejected(self):
        profiles = [
            Profile(1, "Alice", "person", np.array([1.0, 0.0]), "now"),
            Profile(2, "Bob", "person", np.array([0.999, 0.045]), "now"),
        ]

        self.assertIsNone(
            best_match(
                profiles,
                "person",
                np.array([1.0, 0.0]),
                threshold=0.8,
                ambiguity_margin=0.02,
            )
        )

    def test_reid_regions_preserve_spatial_appearance(self):
        crop = np.zeros((100, 200, 3), np.uint8)

        self.assertEqual(
            [region.shape[:2] for region in reid_regions(crop, "person")],
            [(100, 200), (62, 200), (62, 200), (70, 160)],
        )
        self.assertEqual(
            [region.shape[:2] for region in reid_regions(crop, "car")],
            [(100, 200), (100, 130), (100, 130), (80, 160)],
        )
        self.assertTrue(reid_eligible("person", (0, 0, 32, 64)))
        self.assertFalse(reid_eligible("person", (0, 0, 31, 64)))
        self.assertTrue(reid_eligible("car", (0, 0, 48, 32)))

    def test_profile_store_round_trip_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Database(Path(directory) / "profiles.db")
            profile = store.add(
                "Alice", "person", np.array([0.25, 0.75], np.float32)
            )

            loaded = store.all()[0]
            self.assertEqual((loaded.name, loaded.label), ("Alice", "person"))
            np.testing.assert_allclose(loaded.embedding, [0.25, 0.75])
            self.assertTrue(store.delete(profile.id))
            self.assertEqual(store.all(), [])

    def test_gate_requires_confirmation_and_does_not_reopen_for_same_track(self):
        gate = AccessGate(confirmations=3, cooldown_seconds=10)

        self.assertFalse(gate.observe(7, 2, now=0))
        self.assertFalse(gate.observe(7, 2, now=1))
        self.assertTrue(gate.observe(7, 2, now=2))
        self.assertFalse(gate.observe(7, 2, now=20))
        self.assertFalse(gate.observe(8, 2, now=5))
        self.assertFalse(gate.observe(8, 2, now=6))
        self.assertFalse(gate.observe(8, 2, now=7))
        self.assertFalse(gate.observe(9, 2, now=12))
        self.assertFalse(gate.observe(9, 2, now=13))
        self.assertTrue(gate.observe(9, 2, now=14))

    def test_mismatch_resets_confirmation(self):
        gate = AccessGate(confirmations=2, cooldown_seconds=0)

        self.assertFalse(gate.observe(3, 1, now=0))
        self.assertFalse(gate.observe(3, None, now=1))
        self.assertFalse(gate.observe(3, 1, now=2))
        self.assertTrue(gate.observe(3, 1, now=3))

    def test_gate_forgets_tracks_that_leave_the_scene(self):
        gate = AccessGate(confirmations=1, cooldown_seconds=0)

        self.assertTrue(gate.observe(4, 1, now=0))
        gate.retain(set())
        self.assertTrue(gate.observe(4, 1, now=1))

    def test_click_selects_smallest_box_under_pointer(self):
        tracks = [
            {"id": 1, "box": (0, 0, 100, 100), "embedding": np.ones(2)},
            {"id": 2, "box": (40, 40, 60, 60), "embedding": np.ones(2)},
        ]

        selected = select_track(tracks, x=0.5, y=0.5, width=100, height=100)
        self.assertEqual(selected["id"], 2)
        self.assertIsNone(select_track(tracks, x=1.5, y=0.5, width=100, height=100))

    def test_ewelink_request_encrypts_the_configured_relay_channel(self):
        request = ewelink_request(
            "192.168.2.50",
            8081,
            "1000abcd12",
            "1234567890abcdef",
            1,
            "on",
            sequence="123",
            iv=b"0123456789abcdef",
        )

        self.assertEqual(request.full_url, "http://192.168.2.50:8081/zeroconf/switches")
        payload = json.loads(request.data)
        self.assertEqual(payload["deviceid"], "1000abcd12")
        self.assertTrue(payload["encrypt"])
        from Crypto.Cipher import AES

        encrypted = base64.b64decode(payload["data"])
        decrypted = AES.new(
            hashlib.md5(b"1234567890abcdef").digest(),
            AES.MODE_CBC,
            base64.b64decode(payload["iv"]),
        ).decrypt(encrypted)
        plaintext = decrypted[: -decrypted[-1]]
        self.assertEqual(
            json.loads(plaintext),
            {"switches": [{"switch": "on", "outlet": 0}]},
        )

    def test_ewelink_request_rejects_unsafe_configuration(self):
        with self.assertRaises(ValueError):
            ewelink_request("8.8.8.8", 8081, "1000abcd12", "key", 1, "on")
        with self.assertRaises(ValueError):
            ewelink_request("192.168.2.50", 8081, "1000abcd12", "key", 5, "on")
        with self.assertRaises(ValueError):
            ewelink_request("192.168.2.50", 8081, "1000abcd12", "key", 1, "toggle")

    def test_ewelink_info_request_and_encrypted_response_round_trip(self):
        from Crypto.Cipher import AES

        key = "1234567890abcdef"
        iv = b"0123456789abcdef"
        request = ewelink_info_request(
            "192.168.2.50", 8081, "1000abcd12", key, sequence="123", iv=iv
        )
        plaintext = json.dumps(
            {"switches": [{"switch": "on", "outlet": 0}]}, separators=(",", ":")
        ).encode()
        padding = 16 - len(plaintext) % 16
        encrypted = AES.new(
            hashlib.md5(key.encode()).digest(), AES.MODE_CBC, iv
        ).encrypt(plaintext + bytes([padding]) * padding)

        self.assertEqual(request.full_url, "http://192.168.2.50:8081/zeroconf/info")
        self.assertEqual(
            ewelink_response_data(
                {
                    "error": 0,
                    "encrypt": True,
                    "iv": base64.b64encode(iv).decode(),
                    "data": base64.b64encode(encrypted).decode(),
                },
                key,
            ),
            {"switches": [{"switch": "on", "outlet": 0}]},
        )

    def test_explicit_camera_credentials_override_stream_url_credentials(self):
        text = '''
        stream link: "rtsp://old:wrong@camera.local:554/live"
        username: "camera user"
        password: "new@password"
        '''

        self.assertEqual(
            rtsp_url_from_text(text),
            "rtsp://camera%20user:new%40password@camera.local:554/live",
        )

    def test_camera_settings_and_events_persist_in_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "smart-door.db"
            database = Database(path)
            first = database.add_camera(
                "Front", "rtsp://front.local:554/live", "front-user", "front-pass"
            )
            second = database.add_camera(
                "Back", "rtsp://back.local:554/live", "back-user", "back-pass", False
            )
            database.update_camera(
                second.id,
                "Side",
                second.stream_url,
                second.username,
                second.password,
                True,
            )
            database.update_settings(
                {"match_threshold": 0.91, "ewelink_open_channel": 1}
            )
            database.add_event(
                "door_open",
                "Door opened for Alice",
                camera=first,
                profile_id=4,
                profile_name="Alice",
                label="person",
                similarity=0.94,
            )

            reopened = Database(path)
            self.assertEqual([camera.name for camera in reopened.cameras()], ["Front", "Side"])
            self.assertTrue(reopened.camera(second.id).enabled)
            self.assertEqual(reopened.settings()["match_threshold"], 0.91)
            self.assertEqual(reopened.events()[0].profile_name, "Alice")
            self.assertTrue(reopened.delete_camera(first.id))

    def test_camera_url_uses_separate_encoded_credentials(self):
        self.assertEqual(
            camera_stream_url(
                "rtsp://camera.local:554/live", "camera user", "p@ss/word"
            ),
            "rtsp://camera%20user:p%40ss%2Fword@camera.local:554/live",
        )


if __name__ == "__main__":
    unittest.main()
