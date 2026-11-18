"""Плеер и папка Media.

Треки лежат рядом с программой, в папке Media: положил туда файл — он
появился в списке, нажал «Играть» — его слышат и наушники преподавателя,
и ученик на другом конце. Отдельного канала для музыки нет: звук трека
подмешивается в тот же поток, что и микрофон с MIDI, поэтому задержка у
трека такая же, как у всего остального.
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

import numpy as np

from .config import app_dir

SAMPLE_RATE = 48000
# Длиннее этого трек не открываем: весь файл держится в памяти ради
# мгновенной перемотки, а часовая запись заняла бы сотни мегабайт.
MAX_SECONDS = 900
SUPPORTED = (".mp3", ".wav", ".flac", ".ogg")


def media_dir() -> Path:
    """Папка с треками рядом с программой."""
    path = app_dir() / "Media"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def list_tracks() -> list[Path]:
    try:
        files = [p for p in media_dir().iterdir()
                 if p.is_file() and p.suffix.lower() in SUPPORTED]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.name.lower())


def add_track(source: str | Path) -> Path:
    """Копирует файл в Media. Одноимённый файл не затирается."""
    source = Path(source)
    target = media_dir() / source.name
    if target.exists() and target.resolve() != source.resolve():
        stem, suffix = target.stem, target.suffix
        i = 1
        while target.exists():
            target = media_dir() / f"{stem} ({i}){suffix}"
            i += 1
    if target.resolve() != source.resolve():
        shutil.copy2(source, target)
    return target


def import_track(name: str, data: bytes) -> Path:
    """Сохраняет трек, пришедший от собеседника, в свою папку Media."""
    name = Path(str(name or "")).name
    if not name:
        name = "трек.mp3"
    if Path(name).suffix.lower() not in SUPPORTED:
        name += ".mp3"
    target = media_dir() / name
    if target.exists():
        stem, suffix = target.stem, target.suffix
        i = 1
        while target.exists():
            target = media_dir() / f"{stem} ({i}){suffix}"
            i += 1
    target.write_bytes(data)
    return target


def track_duration(path: str | Path) -> float:
    import soundfile as sf

    info = sf.info(str(path))
    if info.samplerate <= 0:
        return 0.0
    return float(info.frames) / float(info.samplerate)


def load_track(path: str | Path, target_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Читает mp3 или wav и отдаёт моно float32 нужной частоты."""
    import soundfile as sf

    path = str(path)
    info = sf.info(path)
    duration = float(info.frames) / float(info.samplerate or 1)
    if duration > MAX_SECONDS:
        raise ValueError(
            f"Трек длиннее {MAX_SECONDS // 60} минут — такой пока не открываю.")
    data, rate = sf.read(path, dtype="float32", always_2d=True)
    if data.size == 0:
        raise ValueError("Файл пустой.")
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0].copy()
    if int(rate) != int(target_rate):
        mono = resample(mono, int(rate), int(target_rate))
    return np.ascontiguousarray(np.clip(mono, -1.0, 1.0), dtype=np.float32)


def resample(data: np.ndarray, rate: int, target: int) -> np.ndarray:
    """Пересчёт частоты. soxr даёт ровный результат; если его нет —
    линейная интерполяция, она годится для близких частот (44.1 и 48)."""
    try:
        import soxr

        return np.asarray(soxr.resample(data, rate, target,
                                        quality="HQ"), dtype=np.float32)
    except Exception:
        n = max(1, int(round(data.size * target / rate)))
        old = np.arange(data.size, dtype=np.float64)
        new = np.linspace(0.0, data.size - 1.0, n)
        return np.interp(new, old, data).astype(np.float32)


class Player:
    """Воспроизведение одного трека.

    Звук забирает аудиопоток из render(), поэтому состояние защищено
    замком: кнопки работают в главном потоке, рендер — в потоке PortAudio.
    """

    def __init__(self, rate: int = SAMPLE_RATE):
        self.rate = int(rate)
        self._lock = threading.Lock()
        self._data = np.zeros(0, dtype=np.float32)
        self._pos = 0
        self._playing = False
        self._finished = False
        self.path = ""
        self.name = ""

    # -- состояние ----------------------------------------------------------

    @property
    def has_track(self) -> bool:
        with self._lock:
            return self._data.size > 0

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._playing

    def duration(self) -> float:
        with self._lock:
            return self._data.size / self.rate if self._data.size else 0.0

    def position(self) -> float:
        with self._lock:
            return self._pos / self.rate

    # -- загрузка -----------------------------------------------------------

    def set_track(self, path: str | Path, data: np.ndarray) -> None:
        with self._lock:
            self._data = np.ascontiguousarray(data, dtype=np.float32)
            self._pos = 0
            self._playing = False
            self._finished = False
            self.path = str(path)
            self.name = Path(path).name

    def clear(self) -> None:
        with self._lock:
            self._data = np.zeros(0, dtype=np.float32)
            self._pos = 0
            self._playing = False
            self._finished = False
            self.path = ""
            self.name = ""

    # -- управление ---------------------------------------------------------

    def play(self) -> bool:
        with self._lock:
            if self._data.size == 0:
                return False
            if self._pos >= self._data.size:
                self._pos = 0
            self._playing = True
            self._finished = False
            return True

    def stop(self) -> None:
        with self._lock:
            self._playing = False

    def seek(self, seconds: float) -> float:
        """Сдвиг относительно текущего места. Возвращает новую позицию."""
        with self._lock:
            if self._data.size == 0:
                return 0.0
            limit = self._data.size
            self._pos = int(min(max(0, self._pos + seconds * self.rate), limit))
            # Перемотка за конец трека останавливает его, как и доигранный
            # до конца: иначе кнопка показывала бы «Стоп» у тишины.
            if self._pos >= limit:
                self._playing = False
                self._finished = True
            return self._pos / self.rate

    def take_finished(self) -> bool:
        """Забрать флаг «трек закончился». Читает только интерфейс."""
        with self._lock:
            flag, self._finished = self._finished, False
            return flag

    # -- рендер -------------------------------------------------------------

    def render(self, frames: int) -> np.ndarray | None:
        """Кусок трека для микшера. Без звука возвращает None."""
        with self._lock:
            if not self._playing or self._data.size == 0:
                return None
            end = min(self._pos + frames, self._data.size)
            block = self._data[self._pos:end]
            self._pos = end
            if self._pos >= self._data.size:
                self._playing = False
                self._finished = True
            return block
