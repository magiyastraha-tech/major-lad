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

# THREAD_PRIORITY_BELOW_NORMAL
_BELOW_NORMAL = -1


def _quiet_thread() -> None:
    """Уводит видео-поток ниже обычного приоритета.

    Звук живёт по жёсткому расписанию: обратный вызов обязан уложиться в
    свои 5 мс. Кодирование кадра дедлайна не имеет, и если оно захватит
    процессор в неудачный момент, звук получит провал — на слух это треск.
    Поэтому видео уступает звуку, а не наоборот.
    """
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadPriority(
            ctypes.windll.kernel32.GetCurrentThread(), _BELOW_NORMAL)
    except Exception:
        pass


class VideoSender:
    QUALITY_FLOOR = 10

    def __init__(self, camera: int, fps: int, quality: int, send_fn, log=None,
                 width: int = 640, height: int = 480, bitrate: int = 0):
        self.camera = camera
        self.fps = max(1, min(30, int(fps)))
        self.quality = max(self.QUALITY_FLOOR, min(95, int(quality)))
        self.width = max(160, min(1920, int(width)))
        self.height = max(120, min(1080, int(height)))
        # Предел потока в килобитах в секунду; 0 — без предела.
        self.bitrate = max(0, int(bitrate))
        self.send_fn = send_fn
        self.log = log or (lambda *a: None)
        self._running = False
        self._thread = None
        self._frame_id = 0
        # Множитель размера кадра: когда и качество, и предел уже на
        # нижней границе, кадр уменьшается — иначе он не влезает в
        # бюджет никогда и видео замирает совсем.
        self._scale = 1.0

    def start(self) -> tuple[bool, str]:
        if cv2 is None:
            return False, "Не установлен opencv-python — видео недоступно."
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True, "Камера запущена."

    def _loop(self) -> None:
        _quiet_thread()
        cap = cv2.VideoCapture(self.camera)
        if not cap.isOpened():
            self.log(f"Камера {self.camera} не открылась.")
            self._running = False
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if self.bitrate:
            self.log(f"Видео: {self.width}×{self.height}, {self.fps} кадров/с, "
                     f"до {self.bitrate} кбит/с.")
        else:
            self.log(f"Видео: {self.width}×{self.height}, {self.fps} кадров/с, "
                     f"без предела потока.")
        interval = 1.0 / self.fps
        # Предел потока: байт в секунду. Бюджет одного кадра — его
        # fps-доля. Раньше качество подгонялось под бюджет, а кадр
        # отправлялся всегда: картинка «не влезала» в 600 кбит/с и
        # уходила втрое тяжелее, забивая канал, по которому рядом идёт
        # звук. Теперь бюджет — жёсткий: что не влезло, не отправляется.
        rate = (self.bitrate * 1000 / 8) if self.bitrate else 0.0
        budget = (rate / self.fps) if rate else 0.0
        # Копилка на три кадра: короткий всплеск движения не рвёт видео,
        # средний поток остаётся в пределе.
        capacity = budget * 3 if budget else 0.0
        tokens = capacity
        last = time.monotonic()
        # Статистика за окно — чтобы в журнале было видно, что реально
        # уходит в канал, а не что задумано настройками.
        stat_at = last
        stat_frames = 0
        stat_dropped = 0
        stat_bytes = 0
        try:
            while self._running:
                started = time.monotonic()
                dt = started - last
                last = started
                if rate:
                    tokens = min(capacity, tokens + rate * max(0.0, min(dt, 1.0)))
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                if self._scale < 0.999:
                    frame = cv2.resize(
                        frame,
                        (max(160, int(self.width * self._scale)),
                         max(120, int(self.height * self._scale))),
                        interpolation=cv2.INTER_AREA)
                elif (frame.shape[1] != self.width
                        or frame.shape[0] != self.height):
                    frame = cv2.resize(frame, (self.width, self.height),
                                       interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", frame,
                                       [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
                if ok:
                    data = buf.tobytes()
                    size = len(data)
                    if budget:
                        if size > budget * 1.1:
                            # Кадр великоват: сначала режем качество, а
                            # когда оно уже на полу — размер кадра.
                            if self.quality > self.QUALITY_FLOOR:
                                self.quality = max(
                                    self.QUALITY_FLOOR, self.quality - 4)
                            elif self._scale > 0.4:
                                self._scale = max(0.35, self._scale - 0.1)
                        elif size < budget * 0.5:
                            if self.quality < 95:
                                self.quality += 1
                            elif self._scale < 0.999:
                                self._scale = min(1.0, self._scale + 0.05)
                        if size <= tokens:
                            tokens -= size
                            stat_bytes += size
                            stat_frames += 1
                            self._send(data, interval, started + interval)
                        else:
                            # Не влезает в предел — молчим этот кадр.
                            # Лучше реже, но целым, и без ущерба звуку.
                            stat_dropped += 1
                    else:
                        stat_bytes += size
                        stat_frames += 1
                        self._send(data, interval, started + interval)
                now = time.monotonic()
                if budget and now - stat_at >= 10.0:
                    elapsed = now - stat_at
                    kbps = stat_bytes * 8 / 1000 / elapsed
                    self.log(
                        f"Видео: {stat_frames / elapsed:.1f} кадров/с, "
                        f"{kbps:.0f} кбит/с "
                        f"(предел {self.bitrate}), "
                        f"пропущено {stat_dropped}.")
                    stat_at = now
                    stat_frames = stat_dropped = stat_bytes = 0
                rest = interval - (time.monotonic() - started)
                if rest > 0:
                    time.sleep(rest)
        finally:
            cap.release()

    def _send(self, data: bytes, interval: float = 0.0,
              deadline: float = 0.0) -> None:
        total = (len(data) + MAX_PAYLOAD - 1) // MAX_PAYLOAD
        if total > 65535:
            return
        self._frame_id = (self._frame_id + 1) & 0xFFFFFFFF
        # Фрагменты растягиваем на время кадра. Залп из десятков пакетов
        # подряд забивает канал, и звук рядом с ним захлёбывается — именно
        # от этого трещало при включённой камере. Пауза между фрагментами
        # делает видеопоток ровным, а звук проходит между ними.
        gap = (interval / total) if (interval > 0 and total > 1) else 0.0
        for index in range(total):
            chunk = data[index * MAX_PAYLOAD:(index + 1) * MAX_PAYLOAD]
            self.send_fn(HEADER.pack(MAGIC_VIDEO, self._frame_id, index, total) + chunk)
            if gap and index + 1 < total:
                # Не выходим за время кадра: лучше короткий залп в конце,
                # чем видео, которое отстаёт от звука.
                rest = deadline - time.monotonic()
                if rest > 0:
                    time.sleep(min(gap, rest))

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
