"""Сжатие звука кодеком Opus.

Несжатый звук стоит дорого: 48 кГц, 16 бит, моно — 768 кбит/с, стерео —
1,5 Мбит/с. Столько не всякая сеть тянет вместе с видео. Opus сжимает тот
же звук до 64–96 кбит/с, а задержка у него — 6,5 мс на кадр 10 мс, то есть
на уровне одного пакета. Именно его использует TeamTalk, о котором
спрашивал Сергей.

Своя тонкая обёртка над библиотекой, а не opuslib: opuslib ищет библиотеку
через ``find_library``, а на Windows тот возвращает None, и пакет просто не
загружается. Здесь библиотека ищется рядом с программой (туда её кладёт
сборка), во временной папке PyInstaller, в системе и по переменной
MAJOR_LAD_OPUS. Если библиотеки нет или она не подошла — говорят, что кодек
недоступен, и приложение спокойно продолжает слать звук без сжатия.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

SAMPLE_RATE = 48000
FRAME = 480                  # 10 мс — тот же шаг, что у звуковых пакетов
CHANNELS = (1, 2)

# Режим «музыка»: Opus в нём не поджимает верха и не гоняется за речью.
# Тесты на двух тонах дали 43 дБ сигнал/шум против 22 дБ у речевого режима.
APPLICATION_AUDIO = 2049
_BANDWIDTH_FULLBAND = 1105
# 64 кбит/с хватало для речи, но гитара на громких переходах «плыла»:
# на них кодеру не хватало битов, и звук становился мутным. 96/128 кбит/с
# — тот же канал, что тянет видео, и заметно больше запаса для музыки.
_BITRATE_MONO = 96000
_BITRATE_STEREO = 128000
_COMPLEXITY = 10

_SET_BITRATE = 4002
_SET_MAX_BANDWIDTH = 4004
_SET_COMPLEXITY = 4010
_SET_INBAND_FEC = 4012
_SET_PACKET_LOSS = 4014
_SET_SIGNAL = 4024
_SIGNAL_MUSIC = 3002

_MAX_PACKET = 4000
# Самый длинный кадр Opus — 120 мс. Буфер под раскодированный звук берём
# с этим запасом: кадр задаёт сам пакет, а libopus на маленький буфер
# отвечает «неверный пакет» (-2). Телефон шлёт кадры по 20 мс, и с буфером
# на 10 мс весь его звук молча пропадал.
_MAX_FRAME = SAMPLE_RATE * 120 // 1000


class OpusUnavailable(RuntimeError):
    """Библиотеки Opus нет или она не умеет того, что нужно."""


def _names() -> list[str]:
    if sys.platform.startswith("win"):
        return ["opus.dll", "libopus-0.dll"]
    if sys.platform == "darwin":
        return ["libopus.dylib", "libopus.0.dylib", "libopus.1.dylib"]
    return ["libopus.so.0", "libopus.so.1", "libopus.so"]


def _candidates() -> list[str]:
    names = _names()
    out: list[str] = []
    env = os.environ.get("MAJOR_LAD_OPUS", "").strip()
    if env:
        out.append(env)
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle:
        dirs.append(Path(bundle))
    dirs.append(Path(__file__).resolve().parent.parent)
    for folder in dirs:
        for name in names:
            out.append(str(folder / name))
    out.extend(names)
    found = ctypes.util.find_library("opus")
    if found:
        out.append(found)
    return out


_lib = None
_load_failed = False


def _load():
    """Загружает библиотеку один раз. Возвращает объект или None."""
    global _lib, _load_failed
    if _lib is not None or _load_failed:
        return _lib
    seen = set()
    for candidate in _candidates():
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            lib = ctypes.CDLL(candidate)
        except OSError:
            continue
        try:
            _bind(lib)
        except (AttributeError, OSError):
            continue
        _lib = lib
        return _lib
    _load_failed = True
    return None


def _bind(lib) -> None:
    lib.opus_encoder_create.restype = ctypes.c_void_p
    lib.opus_encoder_create.argtypes = [
        ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_encode.restype = ctypes.c_int32
    lib.opus_encode.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_int32]
    lib.opus_encoder_destroy.restype = None
    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    # opus_encoder_ctl — функция с переменным числом аргументов: задаём тип
    # только первого, обязательного параметра. Без этого ctypes передал бы
    # указатель как 32-битное число и уронил программу.
    lib.opus_encoder_ctl.restype = ctypes.c_int
    lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p]
    lib.opus_decoder_create.restype = ctypes.c_void_p
    lib.opus_decoder_create.argtypes = [
        ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_decode.restype = ctypes.c_int
    lib.opus_decode.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.opus_decoder_destroy.restype = None
    lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
    lib.opus_get_version_string.restype = ctypes.c_char_p
    lib.opus_get_version_string.argtypes = []


def bitrate(stereo: bool) -> int:
    """Сколько кбит/с уходит на канал при таком числе каналов."""
    return (_BITRATE_STEREO if stereo else _BITRATE_MONO) // 1000


def available() -> bool:
    return _load() is not None


def version() -> str:
    lib = _load()
    if lib is None:
        return ""
    try:
        raw = lib.opus_get_version_string()
        return raw.decode("ascii", "replace") if raw else ""
    except Exception:
        return ""


def _ctl(handle, request: int, value: int) -> None:
    lib = _lib
    if lib is None:
        raise OpusUnavailable("Библиотека Opus не загружена")
    lib.opus_encoder_ctl(ctypes.c_void_p(handle) if isinstance(handle, int)
                         else handle,
                         ctypes.c_int(request), ctypes.c_int(value))


class Encoder:
    """Кодирует блоки int16 в пакеты Opus."""

    def __init__(self, channels: int, bitrate: int | None = None):
        lib = _load()
        if lib is None:
            raise OpusUnavailable("Библиотека Opus не найдена")
        self._lib = lib
        self.channels = 1 if channels == 1 else 2
        err = ctypes.c_int(0)
        handle = lib.opus_encoder_create(
            SAMPLE_RATE, self.channels, APPLICATION_AUDIO,
            ctypes.byref(err))
        if not handle or err.value != 0:
            raise OpusUnavailable(f"Opus не создал кодировщик (код {err.value})")
        self._handle = ctypes.c_void_p(handle)
        if bitrate is None:
            bitrate = _BITRATE_MONO if self.channels == 1 else _BITRATE_STEREO
        try:
            _ctl(handle, _SET_BITRATE, int(bitrate))
            _ctl(handle, _SET_MAX_BANDWIDTH, _BANDWIDTH_FULLBAND)
            _ctl(handle, _SET_COMPLEXITY, _COMPLEXITY)
            _ctl(handle, _SET_SIGNAL, _SIGNAL_MUSIC)
            # Небольшой запас на потери: кодер вкладывает в пакет
            # ослабленную копию предыдущего кадра, и при потере одного
            # пакета его можно восстановить вместо предсказания.
            _ctl(handle, _SET_INBAND_FEC, 1)
            _ctl(handle, _SET_PACKET_LOSS, 10)
        except Exception:
            # Настройки не критичны: без них кодек работает с умолчаниями.
            pass

    def encode(self, pcm: bytes) -> bytes:
        frame_size = len(pcm) // (2 * self.channels)
        if frame_size <= 0:
            return b""
        buf = ctypes.create_string_buffer(_MAX_PACKET)
        result = self._lib.opus_encode(
            self._handle, pcm, frame_size,
            ctypes.cast(buf, ctypes.c_void_p), _MAX_PACKET)
        if result < 0:
            raise OpusUnavailable(f"Opus не закодировал кадр (код {result})")
        return buf.raw[:result]

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle:
            try:
                self._lib.opus_encoder_destroy(handle)
            except Exception:
                pass
            self._handle = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class Decoder:
    """Декодирует пакеты Opus в блоки int16."""

    def __init__(self, channels: int):
        lib = _load()
        if lib is None:
            raise OpusUnavailable("Библиотека Opus не найдена")
        self._lib = lib
        self.channels = 1 if channels == 1 else 2
        err = ctypes.c_int(0)
        handle = lib.opus_decoder_create(
            SAMPLE_RATE, self.channels, ctypes.byref(err))
        if not handle or err.value != 0:
            raise OpusUnavailable(f"Opus не создал декодер (код {err.value})")
        self._handle = ctypes.c_void_p(handle)
        # Сколько отсчётов было в последнем настоящем пакете: по этой
        # длине предсказываем потерянные кадры.
        self._last_samples = 0

    def decode(self, packet: bytes, fec: bool = False,
               frame_size: int | None = None) -> bytes:
        """Пакет в int16. Пустой пакет — предсказание потерянного кадра."""
        if frame_size is None:
            # Настоящий пакет сам скажет, сколько в нём звука, — буферу
            # нужен только запас. Предсказание и восстановление потерянного
            # кадра длятся столько же, сколько шёл предыдущий пакет: иначе
            # каждая потеря на 20-мс потоке растянулась бы на 120 мс.
            if packet and not fec:
                frame_size = _MAX_FRAME
            else:
                frame_size = self._last_samples or FRAME
        buf = ctypes.create_string_buffer(frame_size * self.channels * 2)
        result = self._lib.opus_decode(
            self._handle, packet, len(packet),
            ctypes.cast(buf, ctypes.c_void_p), frame_size,
            1 if fec else 0)
        if result < 0:
            raise OpusUnavailable(f"Opus не раскодировал пакет (код {result})")
        if packet and not fec:
            self._last_samples = result
        return buf.raw[:result * self.channels * 2]

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle:
            try:
                self._lib.opus_decoder_destroy(handle)
            except Exception:
                pass
            self._handle = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
