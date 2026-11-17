"""Проверка и установка обновлений.

Обновления лежат в релизах GitHub. Программа спрашивает последний релиз,
сравнивает версии и, если вышла новая, скачивает свой exe во временную папку.
Установка простая: запущенный exe нельзя перезаписать, но можно переименовать —
поэтому старый файл уходит в .old.exe, на его место встаёт новый, программа
запускается заново. Настройки лежат в json рядом и при обновлении не трогаются.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = "magiyastraha-tech/major-lad"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"

ASSETS = {
    "teacher": "MajorLad-Teacher.exe",
    "student": "MajorLad-Student.exe",
}

USER_AGENT = "MajorLad-Updater"


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for piece in str(text).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(remote: str, current: str) -> bool:
    return _version_tuple(remote) > _version_tuple(current)


def _get(url: str, timeout: float = 15.0) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def latest_release(role: str, current: str) -> dict:
    """Возвращает {"version", "url", "notes"} или {"none": True}.

    Исключения не ловит: вызывающий показывает их человеку словами.
    """
    asset_name = ASSETS.get(role, ASSETS["teacher"])
    data = json.loads(_get(API_LATEST).decode("utf-8"))
    version = str(data.get("tag_name") or data.get("name") or "").strip()
    url = ""
    for asset in data.get("assets") or []:
        if asset.get("name") == asset_name:
            url = asset.get("browser_download_url") or ""
            break
    if not version or not url:
        return {"none": True}
    if not is_newer(version, current):
        return {"none": True, "version": version}
    return {
        "version": version,
        "url": url,
        "notes": str(data.get("body") or "").strip(),
    }


def download(url: str, progress=None) -> Path:
    """Скачивает новый exe в папку обновления. Возвращает путь к файлу."""
    folder = Path(tempfile.gettempdir()) / "majorlad-update"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "MajorLad-new.exe"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with open(target, "wb") as fh:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(min(100, int(done * 100 / total)))
    if progress:
        progress(100)
    return target


def backup_path() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable)
    return exe.with_name(f"{exe.stem}.old.exe")


def cleanup_backup() -> None:
    """Убирает файл предыдущей версии, оставшийся после обновления."""
    backup = backup_path()
    if backup is None or not backup.exists():
        return
    try:
        backup.unlink()
    except Exception:
        pass


def _start_later(exe: Path) -> bool:
    """Запускает новый exe после того, как завершится текущий процесс.

    Если запустить сразу, две копии на секунду подерутся за порты, и новая
    может не открыть кабинет. Поэтому рядом с программой кладётся короткий
    bat-файл: он ждёт исчезновения процесса и только потом запускает
    программу. Имя exe латинское, поэтому кодировка bat ни на что не влияет.
    """
    bat = exe.with_name("restart_majorlad.bat")
    script = (
        "@echo off\r\n"
        ":wait\r\n"
        f'tasklist /FI "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul\r\n'
        "if errorlevel 1 goto run\r\n"
        "set /a tries+=1\r\n"
        "if %tries% geq 60 goto run\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        "goto wait\r\n"
        ":run\r\n"
        f'start "" "%~dp0{exe.name}"\r\n'
        'del /f /q "%~f0" >nul 2>&1\r\n'
    )
    try:
        bat.write_text(script, encoding="ascii")
    except Exception:
        return False
    try:
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(exe.parent),
                         close_fds=True, creationflags=flags)
    except Exception:
        return False
    return True


def install_and_restart(new_file: Path) -> tuple[bool, str]:
    """Ставит новый exe на место старого и запускает программу заново."""
    if not getattr(sys, "frozen", False):
        return False, "Обновление работает только в собранной программе."
    exe = Path(sys.executable)
    backup = exe.with_name(f"{exe.stem}.old.exe")
    try:
        if backup.exists():
            backup.unlink()
        exe.rename(backup)
        shutil.move(str(new_file), str(exe))
    except Exception as exc:
        if not exe.exists() and backup.exists():
            try:
                backup.rename(exe)
            except Exception:
                pass
        return False, f"Не удалось заменить файл программы: {exc}"
    if not _start_later(exe):
        try:
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "DETACHED_PROCESS", 0)
            subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True,
                             creationflags=flags)
        except Exception as exc:
            return False, f"Файл заменён, но запустить программу не удалось: {exc}"
    return True, ""
