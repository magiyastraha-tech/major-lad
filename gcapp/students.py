"""Настройки громкости на каждого ученика.

В папке Data/Student settings лежит по файлу на ученика — так же, как
история переписки. Преподаватель настраивает громкости под конкретного
ученика: свой звук, громкость, которую слышит ученик, сторону минуса.
При следующем подключении этого ученика значения возвращаются сами,
даже если с другим учеником всё менялось.

Формат файла — JSON с понятными именами полей: его можно открыть
блокнотом и поправить руками, если в программе что-то не сходится.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import config, runlog
from .history import safe_name

_DIR = "Student settings"


def settings_dir() -> Path:
    """Папка с настройками учеников в Data."""
    path = config.data_dir() / _DIR
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        runlog.write(f"Папка Student settings не создана: {exc}")
    return path


def file_for(student: str) -> Path | None:
    """Файл ученика. Без имени файла нет: настройки не к чему привязать."""
    name = safe_name(str(student or "").strip())
    if not name:
        return None
    return settings_dir() / f"{name}.json"


def load(student: str) -> dict | None:
    """Сохранённые настройки ученика или None, если его ещё не было."""
    path = file_for(student)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        runlog.write(f"Настройки ученика {student} не прочитаны: {exc}")
        return None
    return data if isinstance(data, dict) else None


def save(student: str, volumes: dict, volumes_peer: dict | None = None,
         volume_mode: str | None = None) -> None:
    """Записывает настройки ученика. Ошибка записи не ломает урок."""
    path = file_for(student)
    if path is None:
        return
    data = {
        "student": str(student).strip(),
        "volumes": dict(volumes or {}),
    }
    if volumes_peer is not None:
        data["volumes_peer"] = dict(volumes_peer)
    if volume_mode:
        data["volume_mode"] = str(volume_mode)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        runlog.write(f"Настройки ученика {student} не сохранены: {exc}")
