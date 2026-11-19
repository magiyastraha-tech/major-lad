"""Журнал работы программы в файл.

Собранная программа работает без консоли, и всё, что происходит внутри —
открытие звука, подключение ученика, ошибки драйверов, — видно только в
строке состояния и исчезает при закрытии окна. Файл `majorlad.log` рядом с
программой хранит эти строки с временем, чтобы его можно было прислать на
разбор: по нему видно, что именно случилось до ошибки.

Файл дописывается от запуска к запуску, разделитель отделяет сеансы.
Больше мегабайта не растёт: старый журнал переезжает в `majorlad.log.1`,
новый начинается с чистого листа.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path

from . import config

MAX_BYTES = 1_000_000

_lock = threading.Lock()
_state: dict = {"path": None}


def log_path() -> Path:
    """Путь к журналу: в папке Data рядом с программой."""
    return config.data_dir() / "majorlad.log"


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            old = path.with_name(path.name + ".1")
            if old.exists():
                old.unlink()
            path.rename(old)
    except Exception:
        pass


def start(role: str = "", version: str = "") -> Path:
    """Открывает новый сеанс в журнале и пишет шапку."""
    path = log_path()
    _state["path"] = path
    with _lock:
        _rotate(path)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}"
                         f" | версия {version or '?'}"
                         f" | роль: {role or 'не выбрана'} =====\n")
                fh.write(f"Python {sys.version.split()[0]}, "
                         f"{sys.platform}\n")
        except Exception:
            pass
    return path


def write(text: str) -> None:
    """Дописывает строку с временем. Молча, если журнал не открыт."""
    text = str(text).strip()
    if not text:
        return
    path = _state.get("path")
    if path is None:
        return
    with _lock:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                for line in text.splitlines():
                    fh.write(f"{datetime.now():%H:%M:%S} {line}\n")
        except Exception:
            pass


def write_block(text: str) -> None:
    """Дописывает многострочный текст (трассировку) без времени на строках."""
    text = str(text).rstrip()
    if not text:
        return
    path = _state.get("path")
    if path is None:
        return
    with _lock:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\n{text}\n")
        except Exception:
            pass
