"""Обновление программы прямо из GitHub.

Программа спрашивает у GitHub список выпусков, ищет среди них свежий
выпуск расписания (метка начинается с `schedule-v`) и, если он новее
установленного, скачивает exe с докачкой, подменяет старый файл и
перезапускается сама.

Искать «последний выпуск» по всему репозиторию нельзя: в том же
репозитории выходят версии основной программы «Мажорный лад», и её exe
расписанию не подходит. Поэтому фильтр по метке и по имени файла —
обязательный, а не украшение.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OWNER = "magiyastraha-tech"
REPO = "major-lad"
TAG_PREFIX = "schedule-v"
ASSET_NAME = "MajorLad-Schedule.exe"

API = f"https://api.github.com/repos/{OWNER}/{REPO}"
HEADERS = {
    "User-Agent": "MajorLad-Schedule-Updater",
    "Accept": "application/vnd.github+json",
}
TIMEOUT = 25
TEMP_DIR = "majorlad-schedule-update"


class UpdateError(Exception):
    """Понятная человеку причина, почему обновление не вышло."""


# --- версии ---------------------------------------------------------------


def version_tuple(text: str) -> tuple[int, ...]:
    clean = str(text or "").strip().lstrip("vV").split("-")[0]
    parts = []
    for piece in clean.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return version_tuple(candidate) > version_tuple(current)


# --- сеть -----------------------------------------------------------------


def _get(url: str, accept: str | None = None) -> bytes:
    headers = dict(HEADERS)
    if accept:
        headers["Accept"] = accept
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except HTTPError as error:
        raise UpdateError(f"GitHub ответил ошибкой {error.code}.") from error
    except URLError as error:
        raise UpdateError("Нет связи с GitHub. Проверь интернет.") from error
    except OSError as error:
        raise UpdateError(f"Сеть недоступна: {error}.") from error


def find_update(current: str) -> dict | None:
    """Свежий выпуск расписания новее current — или None."""
    import json

    raw = _get(f"{API}/releases?per_page=30")
    try:
        releases = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise UpdateError("GitHub прислал непонятный ответ.") from error
    best: dict | None = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = str(release.get("tag_name") or "")
        if not tag.startswith(TAG_PREFIX):
            continue
        asset = None
        for item in release.get("assets") or []:
            if item.get("name") == ASSET_NAME:
                asset = item
                break
        if asset is None:
            continue
        version = tag[len(TAG_PREFIX):]
        if not is_newer(version, current):
            continue
        candidate = {
            "tag": tag,
            "version": version,
            "url": asset.get("browser_download_url") or "",
            "size": int(asset.get("size") or 0),
            "notes": str(release.get("body") or "").strip(),
        }
        if not candidate["url"]:
            continue
        if best is None or version_tuple(version) > version_tuple(best["version"]):
            best = candidate
    return best


# --- скачивание -----------------------------------------------------------


def download(url: str, on_progress=None, size_hint: int = 0) -> Path:
    """Скачать exe с докачкой. on_progress(получено, всего)."""
    folder = Path(tempfile.gettempdir()) / TEMP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ASSET_NAME
    part = folder / (ASSET_NAME + ".part")

    last_error: Exception | None = None
    for attempt in range(1, 9):
        try:
            have = part.stat().st_size if part.exists() else 0
            if size_hint and have > size_hint:
                part.unlink(missing_ok=True)
                have = 0
            headers = {"User-Agent": HEADERS["User-Agent"],
                       "Accept": "application/octet-stream"}
            if have:
                headers["Range"] = f"bytes={have}-"
            request = Request(url, headers=headers)
            with urlopen(request, timeout=60) as response:
                status = getattr(response, "status", 200)
                if have and status != 206:
                    # Сервер не умеет докачку — качаем заново.
                    have = 0
                    part.unlink(missing_ok=True)
                total = int(response.headers.get("Content-Length") or 0) + have
                if not total:
                    total = size_hint
                mode = "ab" if have else "wb"
                with part.open(mode) as out:
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        have += len(chunk)
                        if on_progress is not None:
                            on_progress(have, total)
            if total and have < total:
                raise OSError("скачалось не всё")
            os.replace(part, target)
            return target
        except (HTTPError, URLError, OSError) as error:
            last_error = error
            if attempt == 8:
                break
            time.sleep(min(15, 3 * attempt))
    raise UpdateError(f"Скачать обновление не удалось: {last_error}.")


# --- установка ------------------------------------------------------------


def backup_path() -> Path:
    exe = Path(sys.executable).resolve()
    return exe.with_name(exe.stem.replace(".exe", "") + ".old.exe")


def cleanup_backup() -> None:
    """Убрать копию прошлой версии, оставшуюся после обновления."""
    try:
        path = backup_path()
        if path.exists():
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key == "_MEIPASS" or key.startswith("_PYI_"):
            env.pop(key, None)
    return env


def _start_later(exe: Path) -> None:
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NO_WINDOW)
    kwargs = {"cwd": str(exe.parent), "env": _clean_env(), "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = flags
    subprocess.Popen([str(exe), "--wait-pid", str(os.getpid())], **kwargs)


def install_and_restart(new_file: Path) -> tuple[bool, str]:
    """Подменить exe новой версией и запустить её."""
    if not getattr(sys, "frozen", False):
        return False, "Обновление работает только у собранной программы."
    exe = Path(sys.executable).resolve()
    old = backup_path()
    try:
        if old.exists():
            old.unlink()
        exe.rename(old)
    except OSError as error:
        return False, f"Не удалось освободить файл программы: {error}"
    try:
        shutil.move(str(new_file), str(exe))
    except OSError as error:
        try:
            old.rename(exe)
        except OSError:
            pass
        return False, f"Не удалось поставить новую версию: {error}"
    try:
        _start_later(exe)
    except OSError as error:
        return False, f"Новая версия на месте, но запустить её не вышло: {error}"
    return True, "Новая версия установлена."


# --- ожидание прошлого процесса ------------------------------------------


def take_wait_pid() -> int:
    """Вынуть из аргументов `--wait-pid N`: старый процесс ещё закрывается."""
    args = sys.argv
    pid = 0
    for index, value in enumerate(args):
        if value == "--wait-pid" and index + 1 < len(args):
            try:
                pid = int(args[index + 1])
            except ValueError:
                pid = 0
            del args[index:index + 2]
            break
        if value.startswith("--wait-pid="):
            try:
                pid = int(value.split("=", 1)[1])
            except ValueError:
                pid = 0
            del args[index]
            break
    return pid


def _process_alive(pid: int) -> bool:
    """Жив ли процесс.

    На Windows os.kill(pid, 0) не проверяет, а завершает процесс — для
    этого нужен отдельный путь через tasklist.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_previous(pid: int, timeout: float = 60.0) -> None:
    """Подождать, пока прошлая версия закроет свой exe."""
    if pid <= 0:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.25)
