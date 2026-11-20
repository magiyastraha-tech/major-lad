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
import time
import urllib.request
from pathlib import Path

REPO = "magiyastraha-tech/major-lad"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"

ASSETS = {
    "teacher": "MajorLad-Teacher.exe",
    "student": "MajorLad-Student.exe",
}

USER_AGENT = "MajorLad-Updater"

# Сколько раз пробуем докачать файл после обрыва связи.
_DOWNLOAD_TRIES = 8


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
    """Скачивает новый exe в папку обновления. Возвращает путь к файлу.

    Файл пишется в `.part` и переименовывается только целиком скачанным.
    Если связь оборвалась, следующая попытка продолжает с того же места
    (запрос Range): из России GitHub отдаёт большие файлы медленно и рвёт
    соединение, а начинать 122 мегабайта заново каждый раз — мучение.
    `progress` получает число скачанных и общее число байт.
    """
    folder = Path(tempfile.gettempdir()) / "majorlad-update"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "MajorLad-new.exe"
    part = folder / "MajorLad-new.exe.part"
    try:
        if target.exists():
            target.unlink()
    except OSError:
        pass

    last_error: Exception | None = None
    for attempt in range(1, _DOWNLOAD_TRIES + 1):
        done = part.stat().st_size if part.exists() else 0
        try:
            total = _fetch(url, part, done, progress)
        except Exception as exc:
            last_error = exc
            if attempt < _DOWNLOAD_TRIES:
                time.sleep(min(15, 3 * attempt))
            continue
        size = part.stat().st_size if part.exists() else 0
        if total and size < total:
            last_error = OSError(
                f"скачано {size} байт из {total}")
            time.sleep(min(15, 3 * attempt))
            continue
        os.replace(part, target)
        if progress and total:
            progress(total, total)
        return target
    raise last_error or OSError("скачивание не удалось")


def _fetch(url: str, part: Path, done: int, progress=None) -> int:
    """Докачивает файл в `part` с позиции `done`. Возвращает полный размер.

    Сервер может не уметь докачку (ответ 200 вместо 206) — тогда файл
    начинается заново, иначе в него попали бы два куска подряд.
    """
    headers = {"User-Agent": USER_AGENT}
    if done:
        headers["Range"] = f"bytes={done}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        partial = getattr(response, "status", 200) == 206
        length = int(response.headers.get("Content-Length") or 0)
        if done and not partial:
            done = 0
            length = int(response.headers.get("Content-Length") or 0)
        total = done + length if length else 0
        mode = "ab" if done else "wb"
        with open(part, mode) as fh:
            if progress:
                progress(done, total)
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    return total


def backup_path() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable)
    return exe.with_name(f"{exe.stem}.old.exe")


def cleanup_backup() -> None:
    """Убирает всё, что осталось от прошлого обновления.

    После установки рядом с программой оставался .old.exe, а в Temp —
    скачанный файл и папка обновления. Теперь они удаляются при первом
    запуске новой версии.
    """
    backup = backup_path()
    if backup is None:
        return
    try:
        if backup.exists():
            backup.unlink()
    except Exception:
        pass
    # Bat-файл старого способа перезапуска: он больше не нужен.
    try:
        bat = backup.with_name("restart_majorlad.bat")
        if bat.exists():
            bat.unlink()
    except Exception:
        pass
    # Скачанный exe уже перенесён на место, папка обновления пуста.
    try:
        shutil.rmtree(Path(tempfile.gettempdir()) / "majorlad-update",
                      ignore_errors=True)
    except Exception:
        pass


def _clean_env() -> dict[str, str]:
    """Окружение для новой копии без путей распакованной старой.

    Собранная одним файлом программа живёт во временной папке _MEI… и
    передаёт её путь дочерним процессам через переменные окружения. Новая
    копия, запущенная из старой, получала этот путь и работала в чужой
    временной папке; когда старая закрывалась, папка удалялась и новая
    падала с «No such file or directory: …_MEI…\\base_library.zip».
    Убираем эти переменные — новая копия распакуется в свою папку.
    """
    env = dict(os.environ)
    for name in ("_MEIPASS", "_MEIPASS2", "_PYI_APPLICATION_HOME_DIR",
                 "_PYI_ARCHIVE_FILE", "PYINSTALLER_RESET_ENVIRONMENT"):
        env.pop(name, None)
    return env


def _start_later(exe: Path) -> bool:
    """Запускает новую копию, которая сама дождётся выхода старой.

    Раньше рядом клался bat-файл с ожиданием PID. На компьютерах с
    кириллицей и пробелами в пути он срабатывал через раз: программа
    закрывалась и не открывалась заново. Теперь новая копия стартует сразу
    с номером старого процесса и ждёт его в main() до открытия окна —
    так ей достаются свободные порты, и никакой bat не нужен.
    """
    try:
        args = [str(exe)]
        if os.name == "nt":
            args += ["--wait-pid", str(os.getpid())]
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        subprocess.Popen(args, cwd=str(exe.parent), close_fds=True,
                         creationflags=flags, env=_clean_env())
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
                             creationflags=flags, env=_clean_env())
        except Exception as exc:
            return False, f"Файл заменён, но запустить программу не удалось: {exc}"
    return True, ""
