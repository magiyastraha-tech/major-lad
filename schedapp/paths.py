"""Где лежат данные программы.

Программа портативная: exe можно положить в любую папку, а рядом с ним
появляется папка Data. Звуки теперь лежат внутри неё — `Data/Sound`.
Отдельно искать их не нужно — базой считается папка самого exe. При
запуске из исходников базой считается папка проекта.
"""
from __future__ import annotations

import sys
from pathlib import Path


def base_dir() -> Path:
    """Папка программы: рядом с exe или папка проекта при запуске из кода."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    path = base_dir() / "Data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sound_dir() -> Path:
    path = data_dir() / "Sound"
    path.mkdir(parents=True, exist_ok=True)
    return path


def legacy_sound_dir() -> Path:
    """Старое место звуков — папка `Sound` рядом с exe.

    Существует только в версиях до 0.3.0. Папку не создаём: нужна лишь
    для того, чтобы один раз перенести оттуда файлы в `Data/Sound`.
    """
    return base_dir() / "Sound"
