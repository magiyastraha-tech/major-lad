"""Где лежат данные программы.

Программа портативная: exe можно положить в любую папку, а рядом с ним
должны лежать папки Data и Sound. Отдельно искать их не нужно — базой
считается папка самого exe. При запуске из исходников базой считается
папка проекта.
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
    path = base_dir() / "Sound"
    path.mkdir(parents=True, exist_ok=True)
    return path
