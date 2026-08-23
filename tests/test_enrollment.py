import json
import tempfile
import time
import unittest
from pathlib import Path

import cv2
import numpy as np

from core import Database


class FakeWorker:
    def __init__(self, label="person"):
        self.camera = type("Camera", (), {"id": 1, "name": "Gate"})()
        self.label = label
        self.sequence = 0

    def enrollment_snapshot(self):
        self.sequence += 1
        frame = np.zeros((600, 1200, 3), dtype=np.uint8)
        frame[:, :, 1] = self.sequence * 20
        vector = (
            np.array([1.0, 0.0, 0.0], dtype=np.float32)
            if self.sequence % 3
            else np.array([0.0, 1.0, 0.0], dtype=np.float32)
        )
        return self.sequence, frame, [
            {
                "id": 7,
                "label": self.label,
                "box": (100, 100, 500, 550),
                "confidence": 0.91,
                "embedding": vector,
            }
        ]


class EnrollmentTests(unittest.TestCase):
    def test_default_capture_limits_match_the_product_contract(self):
        from enrollment import EnrollmentManager

        self.assertEqual(EnrollmentManager.CAPTURE_INTERVAL, 0.25)
        self.assertEqual(EnrollmentManager.MAX_SECONDS, 120)
        self.assertEqual(EnrollmentManager.MAX_FRAMES, 480)
        self.assertEqual(EnrollmentManager.MAX_WIDTH, 960)
        self.assertEqual(EnrollmentManager.EXPIRY_SECONDS, 3600)

    def _manager(self, directory, worker):
        from enrollment import EnrollmentManager

        manager = EnrollmentManager(
            Database(Path(directory) / "visiongate.db"),
            Path(directory) / "enrollments",
            lambda camera_id: worker if camera_id == 1 else None,
        )
        manager.CAPTURE_INTERVAL = 0.01
        manager.MAX_SECONDS = 1
        return manager

    @staticmethod
    def _wait_for_frames(manager, session_id, count=3):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            metadata = manager.metadata(session_id)
            if len(metadata["frames"]) >= count:
                return metadata
            time.sleep(0.01)
        raise AssertionError("Enrollment did not capture frames")

    def test_capture_review_commit_deduplicates_and_removes_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / "enrollments" / "stale"
            stale.mkdir(parents=True)
            (stale / "frame.jpg").write_bytes(b"old")
            worker = FakeWorker()
            manager = self._manager(directory, worker)
            self.assertFalse(stale.exists())

            started = manager.start(1)
            metadata = self._wait_for_frames(manager, started["id"])
            metadata = manager.stop(started["id"])
            self.assertEqual(metadata["status"], "review")
            self.assertGreaterEqual(len(metadata["frames"]), 3)
            self.assertNotIn("embedding", json.dumps(metadata))
            self.assertEqual(metadata["frames"][0]["width"], 960)
            box = metadata["frames"][0]["detections"][0]["box"]
            self.assertTrue(all(0 <= value <= 1 for value in box))

            frame = cv2.imread(str(manager.frame_path(started["id"], 0)))
            self.assertEqual(frame.shape[1], 960)
            sample_ids = [frame["detections"][0]["id"] for frame in metadata["frames"][:3]]
            self.assertTrue(manager.sample_thumbnail(started["id"], sample_ids[0]).startswith(b"\xff\xd8"))

            result = manager.commit(started["id"], name="Alice", sample_ids=sample_ids)
            self.assertEqual(result["added"], 2)
            self.assertEqual(result["skipped_duplicates"], 1)
            samples = manager.database.profile_samples(result["profile"]["id"])
            self.assertEqual(len(samples), 2)
            self.assertTrue(all(sample.thumbnail for sample in samples))
            self.assertFalse((Path(directory) / "enrollments" / started["id"]).exists())
            with self.assertRaises(KeyError):
                manager.metadata(started["id"])

    def test_existing_identity_rejects_a_different_object_class(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = FakeWorker("bicycle")
            manager = self._manager(directory, worker)
            profile = manager.database.add(
                "Alice", "person", np.array([1.0, 0.0, 0.0], dtype=np.float32)
            )
            session = manager.start(1)
            metadata = self._wait_for_frames(manager, session["id"], 1)
            manager.stop(session["id"])
            sample_id = metadata["frames"][0]["detections"][0]["id"]
            with self.assertRaisesRegex(ValueError, "same object class"):
                manager.commit(session["id"], profile_id=profile.id, sample_ids=[sample_id])
            self.assertTrue((Path(directory) / "enrollments" / session["id"]).exists())
            manager.cancel(session["id"])
            self.assertFalse((Path(directory) / "enrollments" / session["id"]).exists())

    def test_idle_sessions_expire_and_stop_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory, FakeWorker())
            session = manager.start(1)
            manager._sessions[session["id"]].last_access = time.time() - 3601
            manager.cleanup_expired()
            with self.assertRaises(KeyError):
                manager.metadata(session["id"])
            self.assertFalse((Path(directory) / "enrollments" / session["id"]).exists())

    def test_capture_stops_at_the_configured_frame_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory, FakeWorker())
            manager.MAX_FRAMES = 2
            session = manager.start(1)
            metadata = self._wait_for_frames(manager, session["id"], 2)
            deadline = time.monotonic() + 1
            while metadata["status"] != "review" and time.monotonic() < deadline:
                time.sleep(0.01)
                metadata = manager.metadata(session["id"])
            self.assertEqual(metadata["status"], "review")
            self.assertEqual(len(metadata["frames"]), 2)
            manager.cancel(session["id"])


if __name__ == "__main__":
    unittest.main()
