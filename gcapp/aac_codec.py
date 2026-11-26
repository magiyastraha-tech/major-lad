"""Сжатие звука кодеком AAC-LC — запасной путь, если Opus не пошёл.

Несжатый звук занимает 768 кбит/с в одну сторону: канал забит, звук
трещит, на видео ничего не остаётся. Поэтому основной формат — Opus
(`opus_codec`), а этот модуль нужен там, где Opus не работает: на части
телефонов вендорский декодер открывается и тут же умирает. AAC-LC есть в
Android с первых версий, всегда программный и предсказуемый, — телефон
без него практически не встречается.

Здесь своя обёртка над PyAV (FFmpeg): он уже нужен проекту для H.264 и
уже кладётся в сборку. Если модуля нет или кодек не завёлся — говорим,
что AAC недоступен, и обе стороны остаются на Opus.

Пакеты сырые, без ADTS-заголовков, поэтому декодеру нужен
AudioSpecificConfig — те же два байта, что и на Android: AAC-LC, 48 кГц,
моно.
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 48000
FRAME = 1024                 # кадр AAC всегда 1024 отсчёта
BITRATE = 128000             # моно: с запасом для голоса и гитары
# AAC-LC, 48 кГц, моно. Для стерео было бы b"\x11\x90".
ASC_MONO = b"\x11\x88"


class AacUnavailable(RuntimeError):
    """Модуля av нет или он не умеет AAC."""


_av_module = None
_av_checked = False
_probe_ok: bool | None = None


def _av():
    """Модуль av один раз; None — его нет в этой сборке."""
    global _av_module, _av_checked
    if not _av_checked:
        _av_checked = True
        try:
            import av as module
            _av_module = module
        except Exception:
            _av_module = None
    return _av_module


def _frame(samples: np.ndarray):
    """Массив float32 моно -> кадр PyAV."""
    av = _av()
    frame = av.AudioFrame.from_ndarray(
        samples.reshape(1, -1), format="fltp", layout="mono")
    frame.sample_rate = SAMPLE_RATE
    return frame


def _probe() -> bool:
    """Честная проверка: закодировать тишину и раскодировать обратно."""
    av = _av()
    if av is None:
        return False
    try:
        enc = av.CodecContext.create("aac", "w")
        enc.rate = SAMPLE_RATE
        enc.sample_rate = SAMPLE_RATE
        enc.layout = "mono"
        enc.format = "fltp"
        enc.bit_rate = BITRATE
        packets = []
        silence = np.zeros(FRAME, dtype=np.float32)
        # Первый кадр кодер придерживает — шлём два, чтобы вышел пакет.
        for _ in range(2):
            packets += enc.encode(_frame(silence))
        if not packets:
            return False
        dec = av.CodecContext.create("aac", "r")
        dec.extradata = ASC_MONO
        dec.rate = SAMPLE_RATE
        dec.sample_rate = SAMPLE_RATE
        dec.layout = "mono"
        return any(dec.decode(p) for p in packets)
    except Exception:
        return False


def available() -> bool:
    """Можно ли обещать собеседнику AAC прямо сейчас."""
    global _probe_ok
    if _probe_ok is None:
        _probe_ok = _probe()
    return _probe_ok


class Encoder:
    """Кодирует моно PCM int16 в пакеты AAC-LC."""

    def __init__(self, bitrate: int | None = None):
        av = _av()
        if av is None:
            raise AacUnavailable("Модуль av не найден")
        self._ctx = av.CodecContext.create("aac", "w")
        self._ctx.rate = SAMPLE_RATE
        self._ctx.sample_rate = SAMPLE_RATE
        self._ctx.layout = "mono"
        # Нативный кодер FFmpeg принимает только fltp: с s16 он не открывается.
        self._ctx.format = "fltp"
        self._ctx.bit_rate = int(bitrate or BITRATE)
        self._pending = np.empty(0, dtype=np.int16)
        self._queue: list[bytes] = []

    def encode(self, pcm: bytes) -> bytes:
        """PCM int16 моно -> один пакет AAC или b"", если кадр ещё не набран.

        Микрофон отдаёт блоки по 5 мс (480 отсчётов), а кадр AAC — 1024:
        неполный кадр копится здесь. Кодер ещё и придерживает первый кадр,
        поэтому первые пакеты появляются через 40–50 мс.
        """
        if not pcm:
            return b""
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size:
            self._pending = np.concatenate((self._pending, samples))
        while self._pending.size >= FRAME:
            block = self._pending[:FRAME]
            self._pending = self._pending[FRAME:]
            try:
                frame = _frame(block.astype(np.float32) / 32768.0)
                for packet in self._ctx.encode(frame):
                    self._queue.append(bytes(packet))
            except Exception as exc:
                raise AacUnavailable(f"AAC не закодировал кадр: {exc}")
        if self._queue:
            return self._queue.pop(0)
        return b""

    def close(self) -> None:
        self._ctx = None
        self._queue.clear()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class Decoder:
    """Декодирует пакеты AAC-LC в моно PCM int16."""

    def __init__(self):
        av = _av()
        if av is None:
            raise AacUnavailable("Модуль av не найден")
        dec = av.CodecContext.create("aac", "r")
        # Без описания потока сырые кадры AAC не раскодировать.
        dec.extradata = ASC_MONO
        dec.rate = SAMPLE_RATE
        dec.sample_rate = SAMPLE_RATE
        dec.layout = "mono"
        self._dec = dec

    def decode(self, packet: bytes) -> bytes:
        """Пакет -> PCM int16 моно. Обычно 1024 отсчёта за пакет."""
        if not packet or self._dec is None:
            return b""
        av = _av()
        if av is None:
            return b""
        out = []
        # PyAV ждёт объект Packet, а не голые байты.
        for frame in self._dec.decode(av.Packet(packet)):
            data = frame.to_ndarray()
            if data.ndim == 2 and data.shape[0] > 1:
                data = data.mean(axis=0)
            else:
                data = data.reshape(-1)
            data = np.clip(data, -1.0, 1.0) * 32767.0
            out.append(data.astype(np.int16).tobytes())
        return b"".join(out)

    def close(self) -> None:
        self._dec = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
