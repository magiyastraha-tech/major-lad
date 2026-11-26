"""Видео кодеком H.264.

Кадр JPEG живёт сам по себе: каждый новый кадр сжимается с нуля, и всё,
что в картинке не изменилось, уходит в канал заново. H.264 сравнивает
кадр с предыдущим и передаёт только разницу — при том же потоке картинка
в разы чище. Особенно это видно на неподвижной картинке: гитара, руки,
стол — там уходит почти ничего.

Библиотеку не пишем и не прикручиваем вручную: пакет av (PyAV) приносит
с собой libx264 и декодер H.264, собранные под ту же систему. Если пакета
нет — программа живёт как раньше, на JPEG: available() скажет False, и
видео пойдёт старым путём.

Данные кадра — Annex-B: куски потока H.264 с разделителями 00 00 00 01.
Фрагаменты по сети кладёт слой video.py, здесь только кодирование и
раскодирование.
"""
from __future__ import annotations

import numpy as np

try:
    import av
except Exception:  # pragma: no cover
    av = None


def available() -> bool:
    """Есть ли кодировщик и декодер H.264."""
    if av is None:
        return False
    try:
        return ("libx264" in av.codecs_available
                and "h264" in av.codecs_available)
    except Exception:  # pragma: no cover
        return False


def _even(value: int) -> int:
    """H.264 работает с шагом 2: нечётная сторона кадра не закодируется."""
    return max(2, int(value) // 2 * 2)


def has_idr(data: bytes) -> bool:
    """Есть ли в кадре ключевой кадр (IDR).

    После потери куска потока декодер нельзя кормить разницей: он ждёт
    кадр, которого не было, и выдаёт мусор. Пока не придёт IDR — молчим.
    """
    index = data.find(b"\x00\x00\x00\x01")
    while index >= 0:
        pos = index + 4
        if pos < len(data) and (data[pos] & 0x1F) == 5:
            return True
        index = data.find(b"\x00\x00\x00\x01", pos)
    index = data.find(b"\x00\x00\x01")
    while index >= 0:
        pos = index + 3
        if pos < len(data) and (data[pos] & 0x1F) == 5:
            return True
        index = data.find(b"\x00\x00\x01", pos)
    return False


class H264Encoder:
    """Кодирует кадры BGR (как их отдаёт OpenCV) в поток H.264."""

    def __init__(self, width: int, height: int, fps: int, bitrate: int,
                 quality: int = 60, log=None):
        self.width = _even(width)
        self.height = _even(height)
        self.fps = max(1, min(30, int(fps)))
        self.bitrate = max(0, int(bitrate))
        self.log = log or (lambda *a: None)
        self._pts = 0
        self._ctx = av.CodecContext.create("libx264", "w")
        self._ctx.width = self.width
        self._ctx.height = self.height
        self._ctx.pix_fmt = "yuv420p"
        self._ctx.framerate = av.AVRational(self.fps, 1)
        self._ctx.time_base = av.AVRational(1, self.fps)
        self._ctx.max_b_frames = 0
        # Ключевой кадр раз в секунду: если по сети потерялся кусок,
        # собеседник ждёт не дольше секунды, а не до конца урока.
        self._ctx.gop_size = self.fps
        params = [f"keyint={self.fps}", f"min-keyint={self.fps}",
                  "scenecut=0", "bframes=0"]
        if self.bitrate:
            self._ctx.bit_rate = self.bitrate * 1000
        else:
            # Без предела потока — постоянное качество. Ползунок
            # «Качество» 20..95 переводим в CRF 34..18: больше качество —
            # меньше CRF, как и у JPEG больше — лучше.
            crf = 34 - int(round((max(20, min(95, quality)) - 20)
                                 * 16 / 75))
            params.append(f"crf={crf}")
        self._ctx.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "x264-params": ":".join(params),
        }
        self._ctx.open()

    def encode(self, frame: np.ndarray) -> bytes:
        """Один кадр BGR -> байты потока. Пусто, если кадр ещё копится."""
        image = av.VideoFrame.from_ndarray(np.ascontiguousarray(frame),
                                           format="bgr24")
        image = image.reformat(format="yuv420p")
        image.pts = self._pts
        self._pts += 1
        chunks = [bytes(packet) for packet in self._ctx.encode(image)]
        return b"".join(chunks)

    def close(self) -> None:
        try:
            self._ctx.close()
        except Exception:
            pass


class H264Decoder:
    """Собирает картинку BGR из кусков потока H.264."""

    def __init__(self, log=None):
        self.log = log or (lambda *a: None)
        self._ctx = self._make()
        # После потери куска потока кормить декодер нельзя: ждём IDR.
        self._need_keyframe = False

    @staticmethod
    def _make():
        ctx = av.CodecContext.create("h264", "r")
        ctx.open()
        return ctx

    def reset(self) -> None:
        """Начать заново: собеседник переподключился или потерялся кусок."""
        try:
            self._ctx.close()
        except Exception:
            pass
        self._ctx = self._make()
        self._need_keyframe = True

    def decode(self, data: bytes) -> np.ndarray | None:
        """Байты кадра -> последняя готовая картинка, если она есть."""
        if self._need_keyframe:
            if not has_idr(data):
                return None
            self._need_keyframe = False
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = self._make()
        try:
            frames = self._ctx.decode(av.Packet(data))
        except Exception:
            # Битой кусок: отбрасываем и ждём ключевой кадр.
            self.reset()
            return None
        image = None
        for frame in frames:
            image = frame.to_ndarray(format="bgr24")
        return image

    def close(self) -> None:
        try:
            self._ctx.close()
        except Exception:
            pass
