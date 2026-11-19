"""Мажорный лад — точка входа.

Запуск из исходников:  python main.py
Сборка в exe:          build_windows.bat (на компьютере с Windows)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout,
)

from gcapp import __version__, config, runlog, updater
from gcapp.ui import MainWindow


def _error_log_path() -> Path:
    return config.data_dir() / "error_log.txt"


def _install_error_handler() -> None:
    """Показывает ошибку и пишет её в error_log.txt.

    В собранной программе окна консоли нет: без этого обработчика любая
    ошибка внутри обработчика кнопки исчезает молча, и человек видит только,
    что кнопка «не работает».
    """
    def hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(_error_log_path(), "a", encoding="utf-8") as fh:
                fh.write(f"\n{datetime.now():%Y-%m-%d %H:%M:%S}\n{text}\n")
        except Exception:
            pass
        runlog.write_block(text)
        try:
            QMessageBox.critical(
                None, "Мажорный лад",
                "Что-то пошло не так.\n\n"
                f"{exc_type.__name__}: {exc}\n\n"
                "Программа продолжает работать. Подробности записаны "
                "в файл error_log.txt в папке Data рядом с программой.",
            )
        except Exception:
            pass

    sys.excepthook = hook


class RoleDialog(QDialog):
    """Первый запуск: кто перед нами — преподаватель или ученик."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.role = ""
        self.setWindowTitle("Мажорный лад")
        self.setAccessibleName("Выбор роли")
        self.setMinimumWidth(460)

        title = QLabel("Как вы будете пользоваться программой?")
        title.setWordWrap(True)
        title.setAccessibleName("Как вы будете пользоваться программой?")

        hint = QLabel(
            "Преподаватель открывает кабинет на своём компьютере и приглашает учеников. "
            "Ученик подключается к кабинету преподавателя."
        )
        hint.setWordWrap(True)
        hint.setAccessibleName("Пояснение")

        teacher = QPushButton("Я преподаватель")
        teacher.setAccessibleName("Я преподаватель")
        student = QPushButton("Я ученик")
        student.setAccessibleName("Я ученик")

        teacher.clicked.connect(lambda: self._choose("teacher"))
        student.clicked.connect(lambda: self._choose("student"))

        buttons = QHBoxLayout()
        buttons.addWidget(teacher)
        buttons.addWidget(student)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addLayout(buttons)

        teacher.setFocus()

    def _choose(self, role: str) -> None:
        self.role = role
        self.accept()


def _take_wait_pid() -> int | None:
    """Вынимает из аргументов номер процесса, которого нужно дождаться.

    Новая копия после обновления запускается сразу, ещё при работающей
    старой, и ждёт здесь её выхода: иначе две копии на секунду подерутся
    за порты и вторая может не открыть кабинет.
    """
    argv = list(sys.argv)
    try:
        index = argv.index("--wait-pid")
    except ValueError:
        return None
    pid = None
    if index + 1 < len(argv):
        try:
            pid = int(argv[index + 1])
        except ValueError:
            pid = None
    del argv[index:index + 2]
    sys.argv[:] = argv
    return pid


def _process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=flags,
        )
        return str(pid) in (out.stdout or "")
    except Exception:
        # Не смогли проверить — не держим человека: пусть открывается.
        return False


def _wait_previous(pid: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.25)


def main(forced_role: str | None = None) -> int:
    wait_pid = _take_wait_pid()
    if wait_pid:
        _wait_previous(wait_pid)
    # Прежние версии держали рабочие папки россыпью рядом с exe. Переносим
    # их в Data до первого чтения настроек: дальше весь код смотрит в Data.
    moved = config.migrate()
    app = QApplication(sys.argv)
    app.setApplicationName("Мажорный лад")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Мажорный лад")
    runlog.start(role=forced_role or "", version=__version__)
    if moved:
        runlog.write("Перенесено в Data: " + ", ".join(moved) + ".")
    _install_error_handler()

    # Файл прошлой версии после обновления больше не нужен.
    updater.cleanup_backup()

    # До загрузки: есть ли файл настроек. По нему отличаем первый запуск
    # после установки от обычного, где настройки уже выбирал человек.
    fresh_install = not config.config_path().exists()
    cfg = config.load()

    if forced_role in ("teacher", "student"):
        # Сборка «Преподаватель» и сборка «Ученик»: роль задана жёстко,
        # выбирать ничего не нужно.
        cfg["role"] = forced_role
        if forced_role == "teacher" and not cfg.get("name"):
            cfg["name"] = "Преподаватель"
        config.save(cfg)
    elif cfg.get("role") not in ("teacher", "student"):
        dialog = RoleDialog()
        if dialog.exec() != QDialog.Accepted or not dialog.role:
            return 0
        cfg["role"] = dialog.role
        if dialog.role == "teacher" and not cfg.get("name"):
            cfg["name"] = "Преподаватель"
        config.save(cfg)

    # Свежая установка у ученика: камера включена сразу, микрофон уже стоит
    # «системный по умолчанию». Человек запускает программу на любом
    # компьютере и получает звук и видео, не заходя в настройки.
    if (fresh_install and cfg.get("role") == "student"
            and not cfg["video"].get("enabled")):
        cfg["video"]["enabled"] = True
        config.save(cfg)

    runlog.write(f"Роль: {cfg.get('role')}.")
    window = MainWindow(cfg)
    window.show()

    if cfg.get("role") == "student" and not str(cfg.get("server_ip") or "").strip():
        # Ученику без адреса преподавателя подключаться некуда — сразу
        # открываем настройки, чтобы не искать их наугад.
        QTimer.singleShot(300, window.open_settings)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
