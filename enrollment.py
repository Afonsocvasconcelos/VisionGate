from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np


@dataclass(slots=True)
class _Session:
    id: str
    camera_id: int
    camera_name: str
    directory: Path
    created_at: float
    last_access: float
    status: str = "recording"
    frames: list[dict] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class EnrollmentManager:
    CAPTURE_INTERVAL = 0.25
    MAX_SECONDS = 120
    MAX_FRAMES = 480
    MAX_WIDTH = 960
    EXPIRY_SECONDS = 3600

    def __init__(self, database, root: Path, worker_lookup):
        self.database = database
        self.root = Path(root).resolve()
        self.worker_lookup = worker_lookup
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        self._janitor_stop = threading.Event()
        self._janitor: threading.Thread | None = None
        self._clear_startup_files()

    def _clear_startup_files(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for child in self.root.iterdir():
            if child.resolve().parent != self.root:
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink(missing_ok=True)

    def start_service(self) -> None:
        if self._janitor and self._janitor.is_alive():
            return
        self._janitor_stop.clear()

        def janitor():
            while not self._janitor_stop.wait(60):
                self.cleanup_expired()

        self._janitor = threading.Thread(
            target=janitor, daemon=True, name="enrollment-cleanup"
        )
        self._janitor.start()

    def shutdown(self) -> None:
        self._janitor_stop.set()
        if self._janitor:
            self._janitor.join(timeout=2)
        with self._lock:
            session_ids = list(self._sessions)
        for session_id in session_ids:
            self.cancel(session_id)

    def start(self, camera_id: int) -> dict:
        worker = self.worker_lookup(camera_id)
        if worker is None:
            raise ValueError("Camera is not running")
        with self._lock:
            if any(
                session.camera_id == camera_id and session.status == "recording"
                for session in self._sessions.values()
            ):
                raise ValueError("This camera is already recording samples")
            session_id = uuid.uuid4().hex
            directory = self.root / session_id
            directory.mkdir()
            now = time.time()
            session = _Session(
                session_id,
                camera_id,
                worker.camera.name,
                directory,
                now,
                now,
            )
            self._sessions[session_id] = session
            session.thread = threading.Thread(
                target=self._capture,
                args=(session, worker),
                daemon=True,
                name=f"enrollment-{camera_id}",
            )
            session.thread.start()
        return self.metadata(session_id)

    def _capture(self, session: _Session, worker) -> None:
        deadline = time.monotonic() + self.MAX_SECONDS
        last_sequence = -1
        try:
            while (
                not session.stop_event.is_set()
                and time.monotonic() < deadline
                and len(session.frames) < self.MAX_FRAMES
            ):
                started = time.monotonic()
                snapshot = worker.enrollment_snapshot()
                if snapshot and snapshot[0] != last_sequence:
                    last_sequence = snapshot[0]
                    self._store_frame(session, snapshot[1], snapshot[2])
                remaining = self.CAPTURE_INTERVAL - (time.monotonic() - started)
                if remaining > 0:
                    session.stop_event.wait(remaining)
        finally:
            with self._lock:
                if self._sessions.get(session.id) is session:
                    session.status = "review"

    @staticmethod
    def _thumbnail(frame: np.ndarray, box: tuple[int, int, int, int]) -> bytes | None:
        x1, y1, x2, y2 = box
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        height, width = crop.shape[:2]
        if width > 180:
            crop = cv2.resize(crop, (180, max(1, round(height * 180 / width))), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 84])
        return encoded.tobytes() if ok else None

    def _store_frame(self, session: _Session, frame: np.ndarray, tracks: list[dict]) -> None:
        if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
            return
        original_height, original_width = frame.shape[:2]
        scale = min(1.0, self.MAX_WIDTH / original_width)
        width, height = round(original_width * scale), round(original_height * scale)
        annotated = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA) if scale < 1 else frame.copy()
        frame_id = len(session.frames)
        detections = []
        for index, track in enumerate(tracks):
            x1, y1, x2, y2 = (int(value) for value in track.get("box", (0, 0, 0, 0)))
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(original_width, x2), min(original_height, y2)
            if x2 <= x1 or y2 <= y1:
                continue
            embedding = track.get("embedding")
            sample_id = f"{frame_id}-{index}"
            detection = {
                "id": sample_id,
                "track_id": int(track.get("id", -1)),
                "label": str(track.get("label", "")),
                "confidence": round(float(track.get("confidence", 0)), 3),
                "box": [x1 / original_width, y1 / original_height, x2 / original_width, y2 / original_height],
                "selectable": embedding is not None,
                "thumbnail_url": f"/api/enrollments/{session.id}/samples/{sample_id}.jpg" if embedding is not None else None,
                "_embedding": None if embedding is None else np.asarray(embedding, dtype=np.float32).copy(),
                "_thumbnail": self._thumbnail(frame, (x1, y1, x2, y2)) if embedding is not None else None,
            }
            detections.append(detection)
            color = (61, 214, 140) if embedding is not None else (160, 168, 176)
            sx1, sy1, sx2, sy2 = (round(value * scale) for value in (x1, y1, x2, y2))
            cv2.rectangle(annotated, (sx1, sy1), (sx2, sy2), color, 2)
            cv2.putText(
                annotated,
                f"{detection['label']} #{detection['track_id']}",
                (sx1 + 3, max(16, sy1 - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                color,
                1,
                cv2.LINE_AA,
            )
        ok, encoded = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            return
        filename = f"frame-{frame_id:04d}.jpg"
        temporary = session.directory / f"{filename}.tmp"
        temporary.write_bytes(encoded.tobytes())
        os.replace(temporary, session.directory / filename)
        with self._lock:
            if self._sessions.get(session.id) is session:
                session.frames.append(
                    {
                        "id": frame_id,
                        "captured_at": time.time(),
                        "width": width,
                        "height": height,
                        "detections": detections,
                        "_file": filename,
                    }
                )

    @staticmethod
    def _public_frame(frame: dict) -> dict:
        return {
            "id": frame["id"],
            "captured_at": frame["captured_at"],
            "width": frame["width"],
            "height": frame["height"],
            "url": "",
            "detections": [
                {key: value for key, value in detection.items() if not key.startswith("_")}
                for detection in frame["detections"]
            ],
        }

    def _get(self, session_id: str, touch: bool = True) -> _Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("Enrollment session not found")
            if touch:
                session.last_access = time.time()
            return session

    def metadata(self, session_id: str) -> dict:
        session = self._get(session_id)
        with self._lock:
            frames = [self._public_frame(frame) for frame in session.frames]
            for frame in frames:
                frame["url"] = f"/api/enrollments/{session.id}/frames/{frame['id']}.jpg"
            return {
                "id": session.id,
                "camera_id": session.camera_id,
                "camera_name": session.camera_name,
                "status": session.status,
                "created_at": session.created_at,
                "duration_seconds": round((session.frames[-1]["captured_at"] if session.frames else time.time()) - session.created_at, 1),
                "expires_in_seconds": self.EXPIRY_SECONDS,
                "frames": frames,
            }

    def stop(self, session_id: str) -> dict:
        session = self._get(session_id)
        session.stop_event.set()
        if session.thread and session.thread is not threading.current_thread():
            session.thread.join(timeout=5)
        with self._lock:
            session.status = "review"
        return self.metadata(session_id)

    def frame_path(self, session_id: str, frame_id: int) -> Path:
        session = self._get(session_id)
        with self._lock:
            frame = next((item for item in session.frames if item["id"] == frame_id), None)
        if not frame:
            raise KeyError("Enrollment frame not found")
        path = (session.directory / frame["_file"]).resolve()
        if path.parent != session.directory.resolve() or not path.is_file():
            raise KeyError("Enrollment frame not found")
        return path

    def _detection(self, session: _Session, sample_id: str) -> dict:
        with self._lock:
            for frame in session.frames:
                for detection in frame["detections"]:
                    if detection["id"] == sample_id:
                        return detection
        raise KeyError("Enrollment sample not found")

    def sample_thumbnail(self, session_id: str, sample_id: str) -> bytes:
        session = self._get(session_id)
        thumbnail = self._detection(session, sample_id).get("_thumbnail")
        if not thumbnail:
            raise KeyError("Enrollment sample thumbnail not found")
        return thumbnail

    def commit(
        self,
        session_id: str,
        *,
        sample_ids: list[str],
        profile_id: int | None = None,
        name: str = "",
    ) -> dict:
        session = self._get(session_id)
        if session.status == "recording":
            self.stop(session_id)
        unique_ids = list(dict.fromkeys(sample_ids))
        if not unique_ids:
            raise ValueError("Select at least one sample")
        detections = [self._detection(session, sample_id) for sample_id in unique_ids]
        if any(detection.get("_embedding") is None for detection in detections):
            raise ValueError("A selected object does not have a visual descriptor")
        labels = {detection["label"] for detection in detections}
        if len(labels) != 1:
            raise ValueError("All selected samples must use the same object class")
        profile, added, skipped = self.database.add_enrollment_samples(
            profile_id=profile_id,
            name=name,
            label=next(iter(labels)),
            samples=[
                (detection["_embedding"], detection.get("_thumbnail"))
                for detection in detections
            ],
        )
        result = {
            "profile": {"id": profile.id, "name": profile.name, "label": profile.label},
            "added": len(added),
            "skipped_duplicates": skipped,
            "sample_count": len(self.database.profile_samples(profile.id)),
        }
        self.cancel(session_id)
        return result

    def cancel(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if not session:
            return
        session.stop_event.set()
        if session.thread and session.thread is not threading.current_thread():
            session.thread.join(timeout=5)
        if session.directory.resolve().parent == self.root:
            shutil.rmtree(session.directory, ignore_errors=True)

    def cleanup_expired(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            expired = [
                session.id
                for session in self._sessions.values()
                if now - session.last_access >= self.EXPIRY_SECONDS
            ]
        for session_id in expired:
            self.cancel(session_id)
