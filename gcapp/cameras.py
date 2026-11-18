"""Список камер для настроек видео.

OpenCV умеет открывать камеру только по номеру, а имена камер знает
Qt Multimedia. Здесь одно превращается в другое: список «имя — номер»,
из которого настройки строят выпадающий список.

Если Qt Multimedia в сборке нет (или камер не видно), возвращается пустой
список, и настройки показывают обычную нумерацию: 1, 2, 3. Программа при
этом продолжает работать как раньше.
"""
from __future__ import annotations


def list_cameras() -> list[tuple[str, int]]:
    """Камеры системы: (имя для диктора, номер для OpenCV)."""
    try:
        from PySide6.QtMultimedia import QMediaDevices
    except Exception:
        return []
    try:
        devices = QMediaDevices.videoInputs()
    except Exception:
        return []
    result: list[tuple[str, int]] = []
    for index, device in enumerate(devices):
        try:
            name = str(device.description()).strip()
        except Exception:
            name = ""
        result.append((name or f"Камера {index + 1}", index))
    return result
