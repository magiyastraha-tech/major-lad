"""Видео: камера -> H.264 (запасной путь JPEG) -> UDP-фрагменты, и обратная сборка.

H.264 передаёт разницу между кадрами, поэтому на том же канале картинка
заметно чище. Магия GCV2 — поток H.264, GCV1 — отдельный JPEG-кадр старого
образца. Если собеседник H.264 не понимает, уходит JPEG: он работает везде.

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

from . import video_h264

MAGIC_VIDEO = b"GCV1"
# Кадр H.264. Отдельная метка: старая версия такой пакет не поймёт и
# просто пропустит, связь не порвётся. Включается только после обмена
# возможностями (бит CAPS_H264).
MAGIC_VIDEO_H264 = b"GCV2"
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
                 width: int = 640, height: int = 480, bitrate: int = 0,
                 codec: str = "auto", h264_ok=None):
        self.camera = camera
        # auto — H.264, если его умеет собеседник; h264 — всегда, когда
        # библиотека на месте; jpeg — всегда отдельными кадрами.
        self.codec = str(codec or "auto").lower()
        # Спрашивает у соединения, объявил ли собеседник H.264. Видео
        # может включиться и до знакомства сторон — тогда ответ False,
        # и вопрос повторяется на каждом кадре.
        self.h264_ok = h264_ok or (lambda: False)
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

    def _want_h264(self) -> bool:
        """Пора ли слать H.264 вместо JPEG.

        «Авто» включает его только когда оба конца умеют: собеседник
        сам об этом сообщил в обмене возможностями. Старая версия такого
        бита не пришлёт, и видео останется на JPEG.
        """
        if self.codec == "jpeg":
            return False
        if not video_h264.available():
            return False
        if self.codec == "h264":
            return True
        try:
            return bool(self.h264_ok())
        except Exception:
            return False

    def _fit_h264(self, frame):
        """Кадр под размер кодировщика. H.264 требует чётных сторон."""
        width = self.width // 2 * 2
        height = self.height // 2 * 2
        if frame.shape[1] != width or frame.shape[0] != height:
            frame = cv2.resize(frame, (width, height),
                               interpolation=cv2.INTER_AREA)
        return frame

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
        # С H.264 бюджет держит сам кодировщик, копилка нужна только JPEG.
        rate = (self.bitrate * 1000 / 8) if self.bitrate else 0.0
        budget = (rate / self.fps) if rate else 0.0
        # Копилка на три кадра: короткий всплеск движения не рвёт видео,
        # средний поток остаётся в пределе.
        capacity = budget * 3 if budget else 0.0
        tokens = capacity
        last = time.monotonic()
        last_sent = 0.0
        # Статистика за окно — чтобы в журнале было видно, что реально
        # уходит в канал, а не что задумано настройками.
        stat_at = last
        stat_frames = 0
        stat_dropped = 0
        stat_bytes = 0
        encoder = None
        h264 = False
        try:
            while self._running:
                started = time.monotonic()
                dt = started - last
                last = started
                want = self._want_h264()
                if want != h264:
                    if encoder is not None:
                        encoder.close()
                        encoder = None
                    h264 = want
                    tokens = capacity
                    if h264:
                        encoder = video_h264.H264Encoder(
                            self.width, self.height, self.fps,
                            self.bitrate, self.quality, self.log)
                        self.log(f"Видео: H.264, {encoder.width}×"
                                 f"{encoder.height}, ключевой кадр раз "
                                 f"в секунду.")
                    else:
                        self.log("Видео: JPEG, каждый кадр отдельно.")
                if rate:
                    tokens = min(capacity, tokens + rate * max(0.0, min(dt, 1.0)))
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                if h264:
                    data = b""
                    try:
                        data = encoder.encode(self._fit_h264(frame))
                    except Exception as exc:
                        # Кодировщик споткнулся — не теряем картинку
                        # совсем, откатываемся на JPEG до следующего раза.
                        self.log(f"H.264 не закодировал кадр: {exc}. "
                                 f"Видео идёт JPEG.")
                        if encoder is not None:
                            encoder.close()
                        encoder = None
                        h264 = False
                        continue
                    if data:
                        stat_bytes += len(data)
                        stat_frames += 1
                        self._send(data, interval, started + interval,
                                   MAGIC_VIDEO_H264)
                else:
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
                    ok, buf = cv2.imencode(
                        ".jpg", frame,
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
                                elif self._scale > 0.26:
                                    self._scale = max(0.25, self._scale - 0.1)
                            elif size < budget * 0.5:
                                if self.quality < 95:
                                    self.quality += 1
                                elif self._scale < 0.999:
                                    self._scale = min(1.0, self._scale + 0.05)
                            send_it = size <= tokens
                            floor = (self.quality <= self.QUALITY_FLOOR
                                     and self._scale <= 0.26)
                            if not send_it and floor:
                                # Кадр не влезает в бюджет ни при каком
                                # качестве и размере. Молчать навсегда — это
                                # чёрный экран у собеседника (0.0 кадров/с
                                # весь урок при пределе 128 кбит/с). Отдаём
                                # реже, но отдаём: ждём, пока копилка наберёт
                                # ровно этот кадр, — средний поток остаётся
                                # у предела.
                                if started - last_sent >= size / rate:
                                    send_it = True
                                    tokens = 0.0
                            if send_it:
                                tokens = max(0.0, tokens - size)
                                last_sent = started
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
                if (budget or h264) and now - stat_at >= 10.0:
                    elapsed = now - stat_at
                    kbps = stat_bytes * 8 / 1000 / elapsed
                    name = "H.264" if h264 else "JPEG"
                    self.log(
                        f"Видео ({name}): {stat_frames / elapsed:.1f} "
                        f"кадров/с, {kbps:.0f} кбит/с "
                        f"(предел {self.bitrate}), "
                        f"пропущено {stat_dropped}.")
                    stat_at = now
                    stat_frames = stat_dropped = stat_bytes = 0
                rest = interval - (time.monotonic() - started)
                if rest > 0:
                    time.sleep(rest)
        finally:
            if encoder is not None:
                encoder.close()
            cap.release()

    def _send(self, data: bytes, interval: float = 0.0,
              deadline: float = 0.0, magic: bytes = MAGIC_VIDEO) -> None:
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
            self.send_fn(HEADER.pack(magic, self._frame_id, index, total) + chunk)
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
        # Декодер H.264 заводим при первом таком кадре: у собеседника на
        # старой версии его не будет вовсе.
        self._decoder = None
        # Номер следующего ожидаемого кадра. Пропуск в нумерации значит,
        # что кадр H.264 потерялся целиком: кормить декодер разницей после
        # дыры нельзя — ждём ключевой кадр.
        self._next_frame = -1

    def feed(self, packet: bytes) -> None:
        if len(packet) < HEADER.size:
            return
        magic = packet[:4]
        if magic != MAGIC_VIDEO and magic != MAGIC_VIDEO_H264:
            return
        _, frame_id, index, total = HEADER.unpack(packet[:HEADER.size])
        payload = packet[HEADER.size:]
        if magic == MAGIC_VIDEO_H264 and self._decoder is None:
            if not video_h264.available():
                return
            self._decoder = video_h264.H264Decoder(self.log)
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
            gap = self._next_frame >= 0 and frame_id != self._next_frame
            self._next_frame = frame_id + 1
            self._parts.pop(frame_id, None)
            self._last_frame = frame_id

        if magic == MAGIC_VIDEO_H264:
            if gap and self._decoder is not None:
                self._decoder.reset()
            self._feed_h264(data)
            return
        image = QImage.fromData(data, "JPG")
        if not image.isNull():
            self.on_frame(image)

    def _feed_h264(self, data: bytes) -> None:
        """Байты потока H.264 -> картинка в окне."""
        decoder = self._decoder
        if decoder is None:
            return
        frame = decoder.decode(data)
        if frame is None:
            return
        # Декодер отдаёт BGR — и говорим это QImage прямо, без
        # пересчёта каналов: лишняя работа на каждый кадр, да и OpenCV
        # для приёма видео тогда не нужен.
        height, width = frame.shape[:2]
        image = QImage(frame.data, width, height, frame.strides[0],
                       QImage.Format_BGR888).copy()
        if not image.isNull():
            self.on_frame(image)

    def reset(self) -> None:
        """Новая связь: старый поток H.264 больше не продолжается."""
        with self._lock:
            self._parts.clear()
            self._last_frame = -1
            self._next_frame = -1
        if self._decoder is not None:
            self._decoder.reset()
