"""Рабочие звуки программы.

Звуки лежат рядом с программой в папке Sound. При первом запуске туда
записываются четыре файла по умолчанию; их можно заменить в любой момент
своими WAV с теми же именами — файл читается заново на каждое событие,
перезапускать программу не нужно.

Звук — подсказка ушам, когда глаза и диктор заняты другим: ученик вошёл,
ушёл, пришло сообщение, кто-то ещё просится в кабинет.
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

from . import runlog

SOUND_RATE = 44100
_DEFAULT_DIR = "Sound"

# Событие → имя файла. Имена по-русски: в проводнике сразу видно, какой
# звук за что отвечает, и легче заменить нужный своим.
FILES = {
    "connect": "Ученик подключился.wav",
    "disconnect": "Ученик отключился.wav",
    "message": "Сообщение.wav",
    "busy": "Второй ученик.wav",
}


def sound_dir() -> Path:
    """Папка Sound рядом с программой."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    return base / _DEFAULT_DIR


def _tone(freq: float, seconds: float, volume: float = 0.3,
          fade: float = 0.015) -> np.ndarray:
    """Один мягкий тон. Без плавного края слышен щелчок."""
    count = max(1, int(SOUND_RATE * seconds))
    t = np.arange(count) / SOUND_RATE
    data = np.sin(2.0 * np.pi * freq * t) * volume
    edge = max(1, int(SOUND_RATE * fade))
    envelope = np.ones(count, dtype=np.float32)
    envelope[:edge] = np.linspace(0.0, 1.0, edge)
    envelope[-edge:] = np.linspace(1.0, 0.0, edge)
    return data * envelope


def _pause(seconds: float = 0.04) -> np.ndarray:
    return np.zeros(max(1, int(SOUND_RATE * seconds)), dtype=np.float32)


def _default_sound(kind: str) -> np.ndarray:
    """Свой звук на каждое событие. Коротко, тихо, без резких краёв."""
    if kind == "connect":
        # Восходящее трезвучие: «вошёл, начинаем».
        parts = [_tone(523.25, 0.10), _pause(0.02),
                 _tone(659.25, 0.10), _pause(0.02),
                 _tone(783.99, 0.18)]
    elif kind == "disconnect":
        # То же трезвучие вниз: урок закончился.
        parts = [_tone(783.99, 0.10), _pause(0.02),
                 _tone(659.25, 0.10), _pause(0.02),
                 _tone(523.25, 0.18)]
    elif kind == "message":
        # Два коротких высоких сигнала — «пришло сообщение».
        parts = [_tone(880.0, 0.06, volume=0.22), _pause(0.04),
                 _tone(1174.66, 0.09, volume=0.22)]
    elif kind == "busy":
        # Два низких стука: в кабинет просится второй ученик.
        parts = [_tone(220.0, 0.12, volume=0.35), _pause(0.06),
                 _tone(196.0, 0.16, volume=0.35)]
    else:
        parts = [_tone(660.0, 0.10)]
    return np.concatenate(parts)


def _write(path: Path, data: np.ndarray) -> None:
    """Пишет 16-битный моно-WAV, который PlaySound понимает без вопросов."""
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(SOUND_RATE)
        fh.writeframes(pcm.tobytes())


def ensure(folder: Path | None = None) -> Path:
    """Создаёт папку Sound и недостающие звуки по умолчанию.

    Существующие файлы не трогаются: заменил владелец — так и останется.
    """
    folder = Path(folder) if folder is not None else sound_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        runlog.write(f"Папка Sound не создана: {exc}")
        return folder
    for kind, name in FILES.items():
        path = folder / name
        if path.exists():
            continue
        try:
            _write(path, _default_sound(kind))
        except OSError as exc:
            runlog.write(f"Звук {name} не создан: {exc}")
    return folder


def play(kind: str) -> None:
    """Играет звук события, не задерживая интерфейс.

    PlaySound с SND_ASYNC возвращается сразу; на другой системе (во время
    разработки) звука нет — это не ошибка.
    """
    name = FILES.get(kind)
    if not name:
        return
    path = sound_dir() / name
    if not path.is_file():
        return
    try:
        import winsound
    except ImportError:
        return
    try:
        winsound.PlaySound(
            str(path),
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception as exc:
        runlog.write(f"Звук {name} не сыграл: {exc}")
