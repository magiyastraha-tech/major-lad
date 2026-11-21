"""Звуки программы.

Три звука: напоминание, начало урока, конец урока. При первом запуске
программа сама кладёт в папку `Data/Sound` простые WAV-файлы, чтобы было
что услышать сразу. Свой звук — это замена файла с тем же именем:
программа читает файл заново на каждое событие, перезапускать её не нужно.

Звуки раньше лежали в папке `Sound` рядом с exe. При запуске версии 0.3.0
файлы оттуда один раз переносятся в `Data/Sound`; уже существующие в новой
папке не перезаписываются, а старая папка удаляется, только если опустела.

Если файла нет или он не читается, вызывающий включает системный писк.
"""
from __future__ import annotations

import math
import shutil
import sys
import tempfile
import wave
from array import array
from pathlib import Path

from . import paths

RATE = 44100
VOLUME = 0.38

SOON_FILE = "Напоминание.wav"
START_FILE = "Начало урока.wav"
FINISH_FILE = "Конец урока.wav"

FILES = {
    "soon": SOON_FILE,
    "start": START_FILE,
    "finish": FINISH_FILE,
}

MEMO_FILE = "Как заменить звуки.txt"
MEMO = """Звуки программы «Мажорный лад Расписание».

Здесь лежат три звука:
Напоминание.wav — когда до урока осталось немного.
Начало урока.wav — вопрос «начинаем?» в начале урока.
Конец урока.wav — вопрос «урок прошёл?» в конце.

Чтобы поставить свой звук, замени файл с тем же именем своим.
Подходит формат WAV. Программа читает файл каждый раз заново, поэтому
перезапускать её не нужно.

Если файла нет или он не читается, программа подаёт системный звук.
"""

# Мелодии: (частота, секунды). Паузы — частотой 0.
_SOON = ((880, 0.14), (0, 0.07), (880, 0.14), (0, 0.07), (1175, 0.22))
_START = ((523, 0.14), (659, 0.14), (784, 0.26))
_FINISH = ((784, 0.14), (659, 0.14), (523, 0.30))

_MELODIES = {
    "soon": _SOON,
    "start": _START,
    "finish": _FINISH,
}


def _tone(freq: float, seconds: float) -> array:
    count = int(RATE * seconds)
    fade = max(1, int(RATE * 0.01))
    data = array("h")
    for index in range(count):
        if freq <= 0:
            data.append(0)
            continue
        level = 1.0
        if index < fade:
            level = index / fade
        elif index > count - fade:
            level = (count - index) / fade
        sample = math.sin(2 * math.pi * freq * index / RATE)
        data.append(int(VOLUME * level * sample * 32767))
    return data


def _wav_bytes(melody) -> bytes:
    data = array("h")
    for freq, seconds in melody:
        data.extend(_tone(freq, seconds))
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(data.tobytes())
    return buffer.getvalue()


def _move_old_sounds(target: Path) -> None:
    """Перенести звуки из старого `Sound` рядом с exe в `Data/Sound`.

    Свой звук пользователя важнее: если файл с таким именем в новой папке
    уже есть, старый остаётся на месте. Старую папку убираем, только когда
    она опустела, — чужие файлы там не трогаем.
    """
    old = paths.legacy_sound_dir()
    if old == target or not old.is_dir():
        return
    for item in old.iterdir():
        destination = target / item.name
        if destination.exists() or not item.is_file():
            continue
        try:
            shutil.move(str(item), str(destination))
        except OSError:
            continue
    try:
        if not any(old.iterdir()):
            old.rmdir()
    except OSError:
        pass


def ensure(folder: Path | None = None) -> None:
    """Создать папку звуков и недостающие файлы по умолчанию.

    Существующие файлы не трогаются: там может быть уже свой звук.
    """
    target = Path(folder) if folder is not None else paths.sound_dir()
    if folder is None:
        try:
            target.mkdir(parents=True, exist_ok=True)
            _move_old_sounds(target)
        except OSError:
            pass
    try:
        target.mkdir(parents=True, exist_ok=True)
        for kind, name in FILES.items():
            path = target / name
            if path.exists() and path.stat().st_size > 44:
                continue
            path.write_bytes(_wav_bytes(_MELODIES[kind]))
        memo = target / MEMO_FILE
        if not memo.exists():
            memo.write_text(MEMO, encoding="utf-8")
    except OSError:
        # Без звуков программа работает: системный писк остаётся.
        pass


def _volume(value) -> int:
    """Громкость 0–100; что попало — к ближайшей границе."""
    try:
        level = int(value)
    except (TypeError, ValueError):
        return 100
    return max(0, min(100, level))


def _quiet_copy(path: Path, level: int) -> Path | None:
    """Сделать приглушённую копию WAV: у winsound своей громкости нет.

    Масштабируем отсчёты 16-битного WAV во временный файл. Имя копии
    привязано к дате изменения исходника, поэтому заменённый звук не
    проиграется из старого кэша.
    """
    try:
        stamp = int(path.stat().st_mtime)
    except OSError:
        stamp = 0
    target = (Path(tempfile.gettempdir())
              / f"major-lad-{path.stem}-{level}-{stamp}.wav")
    if target.exists() and target.stat().st_size > 44:
        return target
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            frames = source.readframes(source.getnframes())
    except (OSError, wave.Error, EOFError):
        return None
    if width != 2:
        return None
    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    scale = level / 100.0
    for index, sample in enumerate(samples):
        samples[index] = int(sample * scale)
    if sys.byteorder == "big":
        samples.byteswap()
    try:
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(width)
            handle.setframerate(rate)
            handle.writeframes(samples.tobytes())
    except OSError:
        return None
    return target


def play(kind: str, folder: Path | None = None, volume: int = 100) -> bool:
    """Проиграть звук. False — не вышло, пусть вызывающий пищит.

    Громкость 0 значит «без звука»: возвращаем True, чтобы вызывающий не
    пищал вместо выключенного сигнала.
    """
    name = FILES.get(kind)
    if not name:
        return False
    target = Path(folder) if folder is not None else paths.sound_dir()
    path = target / name
    if not path.exists():
        return False
    level = _volume(volume)
    if level == 0:
        return True
    try:
        import winsound
    except ImportError:
        return False
    played = path
    if level < 100:
        quiet = _quiet_copy(path, level)
        if quiet is None:
            return False
        played = quiet
    try:
        winsound.PlaySound(
            str(played),
            winsound.SND_FILENAME | winsound.SND_ASYNC
            | winsound.SND_NODEFAULT)
        return True
    except RuntimeError:
        return False


def length(kind: str, folder: Path | None = None) -> float:
    """Сколько секунд звучит сигнал; 0 — файла нет или он не читается.

    Нужна, чтобы диктор начинал говорить после сигнала, а не вместе с ним.
    """
    name = FILES.get(kind)
    if not name:
        return 0.0
    target = Path(folder) if folder is not None else paths.sound_dir()
    path = target / name
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate() or RATE
            return handle.getnframes() / float(rate)
    except (OSError, wave.Error, EOFError):
        return 0.0
