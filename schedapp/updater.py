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
INSTALL_FLAG = "--install-update"
STAGED_SUFFIX = ".new.exe"


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


def _stem(exe: Path) -> str:
    stem = exe.stem
    if stem.lower().endswith(".exe"):
        stem = stem[:-4]
    return stem


def backup_path_for(exe: Path) -> Path:
    """Куда уходит старая версия при установке: рядом, с меткой .old."""
    return exe.with_name(_stem(exe) + ".old.exe")


def staged_path_for(exe: Path) -> Path:
    """Куда кладётся новая версия до установки: рядом, с меткой .new."""
    return exe.with_name(_stem(exe) + STAGED_SUFFIX)


def backup_path() -> Path:
    return backup_path_for(Path(sys.executable).resolve())


def _remove_file(path: Path) -> bool:
    """Удалить файл, подождав, если его ещё держит прошлый процесс."""
    for attempt in range(10):
        try:
            if not path.exists():
                return True
            path.unlink(missing_ok=True)
            return True
        except OSError:
            time.sleep(0.3)
    return False


def cleanup_backup() -> None:
    """Убрать копию прошлой версии, оставшуюся после обновления."""
    try:
        _remove_file(backup_path())
    except OSError:
        pass


def cleanup_staged() -> None:
    """Убрать временную копию новой версии, если она осталась."""
    try:
        _remove_file(staged_path_for(Path(sys.executable).resolve()))
    except OSError:
        pass


def cleanup_download() -> None:
    """Убрать скачанный exe из временной папки — он уже установлен."""
    try:
        folder = Path(tempfile.gettempdir()) / TEMP_DIR
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
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


def _start_installer(staged: Path, target: Path) -> None:
    """Запустить скачанную версию установщиком: она ждёт нас и меняет exe."""
    kwargs = {"cwd": str(target.parent), "env": _clean_env(),
              "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NO_WINDOW)
    args = [str(staged), INSTALL_FLAG, str(target),
            "--wait-pid", str(os.getpid())]
    subprocess.Popen(args, **kwargs)


def install_and_restart(new_file: Path) -> tuple[bool, str]:
    """Поставить новую версию рядом с программой и запустить установщик.

    Запущенный exe на Windows держит сам себя — подменить его на месте
    нельзя, Windows не отдаёт файл. Поэтому здесь ничего не подменяется:
    скачанная версия ложится в папку программы под именем
    «...new.exe», запускается установщиком и ждёт, пока мы закроемся.
    Установщик уже после нашего выхода убирает старый файл и встаёт на
    его место — когда файл никто не держит.
    """
    if not getattr(sys, "frozen", False):
        return False, "Обновление работает только у собранной программы."
    exe = Path(sys.executable).resolve()
    staged = staged_path_for(exe)
    try:
        if staged.exists():
            staged.unlink()
        shutil.copy2(str(new_file), str(staged))
    except OSError as error:
        return False, (f"Не удалось положить новую версию в папку программы "
                       f"{exe.parent}: {error}. Похоже, в эту папку нельзя "
                       f"писать — перенеси программу вместе с папкой Дата "
                       f"в свою папку, например в Документы, и обновление "
                       f"пройдёт.")
    try:
        _start_installer(staged, exe)
    except OSError as error:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"Не удалось запустить установщик обновления: {error}"
    return True, "Новая версия скачана. Программа перезапустится сама."


# --- установка (в процессе-установщике) -----------------------------------


def take_install_target() -> Path | None:
    """Вынуть из аргументов `--install-update <путь>`: мы — установщик."""
    args = sys.argv
    for index, value in enumerate(args):
        if value == INSTALL_FLAG and index + 1 < len(args):
            target = args[index + 1]
            del args[index:index + 2]
            return Path(target)
        if value.startswith(INSTALL_FLAG + "="):
            target = value.split("=", 1)[1]
            del args[index]
            return Path(target)
    return None


def finish_install(target: Path) -> tuple[bool, str]:
    """Встать новой версией на место старой и запустить её.

    Здесь уже нечему мешать: прошлая программа закрыта, exe свободен.
    Копируем себя (мы запущены из «...new.exe») на место программы, а не
    переименовываем: переименовывать запущенный файл Windows не даёт.
    """
    me = Path(sys.executable).resolve()
    target = Path(target)
    old = backup_path_for(target)
    last_error: OSError | None = None

    # Windows отпускает exe не мгновенно после выхода процесса: ещё
    # работает загрузчик PyInstaller, иногда файл держит антивирус.
    # Полминуты ждём и пробуем снова — обычно хватает одной-двух попыток.
    moved = False
    deadline = time.monotonic() + 30.0
    while not moved and time.monotonic() < deadline:
        try:
            if old.exists():
                old.unlink(missing_ok=True)
            if target.exists():
                target.rename(old)
            moved = True
        except OSError as error:
            last_error = error
            time.sleep(0.5)
    if not moved:
        return False, (f"Не удалось освободить файл программы "
                       f"{target.name}: {last_error}")

    copied = False
    deadline = time.monotonic() + 30.0
    while not copied and time.monotonic() < deadline:
        try:
            shutil.copy2(str(me), str(target))
            copied = True
        except OSError as error:
            last_error = error
            time.sleep(0.5)
    if not copied:
        try:
            target.unlink(missing_ok=True)
            if old.exists():
                old.rename(target)
        except OSError:
            pass
        return False, f"Не удалось поставить новую версию: {last_error}"

    try:
        _start_later(target)
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
