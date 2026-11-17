"""Видео: камера -> JPEG -> UDP-фрагменты, и обратная сборка.

Заголовок фрагмента: 4s magic, I номер кадра, H номер фрагмента, H всего.
Кадр рвётся на куски по 1100 байт — это влезает в MTU любого домашнего
канала. Если кусок потерялся, кадр не собирается и выбрасывается: ждать
его бессмысленно, за 66 мс придёт следующий.
"""
from __future__ import annotations

import struct
import threading
import time

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

from PySide6.QtGui import QImage

MAGIC_VIDEO = b"GCV1"
MAX_PAYLOAD = 1100
HEADER = struct.Struct("!4sIHH")


class VideoSender:
    def __init__(self, camera: int, fps: int, quality: int, send_fn, log=None):
        self.camera = camera
        self.fps = max(1, min(30, int(fps)))
        self.quality = max(20, min(95, int(quality)))
        self.send_fn = send_fn
        self.log = log or (lambda *a: None)
        self._running = False
        self._thread = None
        self._frame_id = 0

    def start(self) -> tuple[bool, str]:
        if cv2 is None:
            return False, "Не установлен opencv-python — видео недоступно."
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True, "Камера запущена."

    def _loop(self) -> None:
        cap = cv2.VideoCapture(self.camera)
        if not cap.isOpened():
            self.log(f"Камера {self.camera} не открылась.")
            self._running = False
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        interval = 1.0 / self.fps
        try:
            while self._running:
                started = time.monotonic()
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                ok, buf = cv2.imencode(".jpg", frame,
                                       [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
                if ok:
                    self._send(buf.tobytes())
                rest = interval - (time.monotonic() - started)
                if rest > 0:
                    time.sleep(rest)
        finally:
            cap.release()

    def _send(self, data: bytes) -> None:
        total = (len(data) + MAX_PAYLOAD - 1) // MAX_PAYLOAD
        if total > 65535:
            return
        self._frame_id = (self._frame_id + 1) & 0xFFFFFFFF
        for index in range(total):
            chunk = data[index * MAX_PAYLOAD:(index + 1) * MAX_PAYLOAD]
            self.send_fn(HEADER.pack(MAGIC_VIDEO, self._frame_id, index, total) + chunk)

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


class FrameReassembler:
    """Собирает кадры из UDP и отдаёт готовый QImage."""

    def __init__(self, on_frame=None, log=None):
        self.on_frame = on_frame or (lambda *a: None)
        self.log = log or (lambda *a: None)
        self._parts: dict[int, dict] = {}
        self._last_frame = -1
        self._lock = threading.Lock()

    def feed(self, packet: bytes) -> None:
        if len(packet) < HEADER.size or packet[:4] != MAGIC_VIDEO:
            return
        _, frame_id, index, total = HEADER.unpack(packet[:HEADER.size])
        payload = packet[HEADER.size:]
        with self._lock:
            # Кадр старше последнего собранного уже не нужен.
            if frame_id <= self._last_frame and frame_id != 0:
                if frame_id < self._last_frame:
                    return
            entry = self._parts.get(frame_id)
            if entry is None:
                entry = {"total": total, "parts": {}, "at": time.monotonic()}
                self._parts[frame_id] = entry
            entry["parts"][index] = payload

            # Держим не больше четырёх кадров в сборке.
            if len(self._parts) > 4:
                oldest = min(self._parts, key=lambda k: self._parts[k]["at"])
                self._parts.pop(oldest, None)

            if len(entry["parts"]) < entry["total"]:
                return
            data = b"".join(entry["parts"][i] for i in range(entry["total"]))
            self._parts.pop(frame_id, None)
            self._last_frame = frame_id

        image = QImage.fromData(data, "JPG")
        if not image.isNull():
            self.on_frame(image)
