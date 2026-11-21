"""Автозапуск программы вместе с Windows — сразу в трее, в фоне.

Регистрируемся в личном разделе реестра текущего пользователя
(`HKEY_CURRENT_USER`), поэтому права администратора не нужны и запись
касается только этого человека. Ключ `Run` Windows запускает при входе в
систему.

Программа стартует с ключом `--tray`: окно не открывается, приложение
живёт в трее и продолжает напоминать об уроках. Открыть его можно
щелчком по значку в трее.

Всё здесь работает только на Windows. На другой системе функции молча
ничего не делают — программа остаётся обычной.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ветка реестра с программами, которые Windows запускает при входе.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# Имя записи в реестре — его же видит человек в «Диспетчере задач».
VALUE_NAME = "Мажорный лад Расписание"

# Ключ командной строки: запуститься в трее, без окна.
TRAY_FLAG = "--tray"


def supported() -> bool:
    """Умеет ли эта система автозапуск программы."""
    return sys.platform == "win32"


def command() -> str:
    """Команда запуска: путь к exe в кавычках и ключ «в трее».

    Путь в кавычках — папка может содержать пробелы. При запуске из
    исходников запускаем pythonw.exe, чтобы не открывалось чёрное окно.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {TRAY_FLAG}'
    exe = sys.executable
    quiet = Path(exe).with_name("pythonw.exe")
    if quiet.exists():
        exe = str(quiet)
    script = Path(__file__).resolve().parent.parent / "schedule_main.py"
    return f'"{exe}" "{script}" {TRAY_FLAG}'


def is_enabled() -> bool:
    """Записана ли программа в автозапуск Windows."""
    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    return True


def set_enabled(enabled: bool) -> None:
    """Включить или выключить автозапуск. Ошибку реестра отдаём вызывающему."""
    if not supported():
        return
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
            return
        try:
            winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            # Записи и так нет — выключать нечего.
            pass


def refresh() -> None:
    """Поправить путь в автозапуске, если включён.

    Программу могли перенести в другую папку или обновить: старая запись
    указывала бы на прежнее место, а Windows запускала бы старую копию.
    При каждом запуске прописываем актуальный путь. Ошибку не показываем:
    автозапуск — удобство, а не повод не открыться.
    """
    if not supported() or not is_enabled():
        return
    try:
        set_enabled(True)
    except OSError:
        pass
