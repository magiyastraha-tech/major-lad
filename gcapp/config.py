"""Конфигурация программы «Мажорный лад».

Файл config.json лежит рядом с программой — это делает сборку портативной:
скопировал папку на другой компьютер, настройки переехали вместе с ней.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

DEFAULTS = {
    "role": "",
    "name": "",
    "server_ip": "",
    "server_port": 45800,
    "autoconnect": True,
    "audio": {
        "device1": "",
        "device2": "",
        "usb": "",
        "output": "",
        # Режим канала входа: «ch1» — первый вход устройства, «ch2» — второй,
        # «stereo» — оба сразу (первый слева, второй справа).
        "mode1": "ch1",
        "mode2": "ch1",
        "midi_enabled": False,
        "midi_port": "",
        "midi_fixed_velocity": False,
        # Показывать в списках все драйверы, а не только WASAPI.
        "show_all_drivers": False,
        "volumes": {
            "student": 80,
            "in1": 100,
            "in2": 100,
            "usb": 100,
            "midi": 80,
            "metronome": 60,
            "player": 80,
        },
        # Громкость, которую слышит собеседник. Отдельная от своей: регулятор
        # в режиме «Громкость ученика» меняет только его сторону.
        "volumes_peer": {
            "student": 80,
            "in1": 100,
            "in2": 100,
            "usb": 100,
            "midi": 80,
            "metronome": 60,
            "player": 80,
        },
        "volume_mode": "local",
    },
    "player": {
        "skip": 5,
        "last": "",
    },
    "metronome": {
        "enabled": False,
        "bpm": 90,
        "beats": 4,
    },
    "video": {
        "enabled": False,
        "camera": 0,
        "fps": 15,
        "quality": 60,
        # Размер кадра и предел потока в кбит/с (0 — без предела).
        "width": 640,
        "height": 480,
        "bitrate": 0,
        "show_remote": True,
    },
}


def app_dir() -> Path:
    """Папка программы. Для собранного exe — папка exe, для исходников — корень проекта."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    """Настройки лежат рядом с программой.

    У сборки exe файл называется по имени exe: у «Преподавателя» и «Ученика»
    настройки разные, и их можно держать в одной папке.
    """
    if getattr(sys, "frozen", False):
        return app_dir() / f"{Path(sys.executable).stem}.json"
    return app_dir() / "config.json"


def _merge(base: dict, data: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (data or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict:
    path = config_path()
    if path.exists():
        try:
            return _merge(DEFAULTS, json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return copy.deepcopy(DEFAULTS)


def save(cfg: dict) -> None:
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
