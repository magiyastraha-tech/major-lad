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

import hashlib
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
LOG_NAME = "Обновление.txt"
LOG_LIMIT = 200_000


class UpdateError(Exception):
    """Понятная человеку причина, почему обновление не вышло."""


def log(text: str) -> None:
    """Строка в журнал обновления `Data/Обновление.txt`.

    Журнал нужен, когда обновление не дошло до конца: по нему видно, на
    каком шаге всё встало. Пишем молча — сбой журнала не должен мешать
    самому обновлению.
    """
    try:
        from . import paths

        path = paths.data_dir() / LOG_NAME
        try:
            if path.exists() and path.stat().st_size > LOG_LIMIT:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        stamp = time.strftime("%d.%m.%Y %H:%M:%S")
        with path.open("a", encoding="utf-8") as out:
            out.write(f"{stamp} {text}\n")
    except Exception:  # noqa: BLE001 — журнал не мешает обновлению
        pass


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
            # GitHub отдаёт sha256 файла — по нему видно, что скачалось
            # ровно то, что выложено, а не огрызок или подмена.
            "digest": str(asset.get("digest") or ""),
            "notes": str(release.get("body") or "").strip(),
        }
        if not candidate["url"]:
            continue
        if best is None or version_tuple(version) > version_tuple(best["version"]):
            best = candidate
    return best


# --- скачивание -----------------------------------------------------------


def check_file(path: Path, size_hint: int = 0, digest: str = "") -> str | None:
    """Что не так со скачанным файлом; None — файл целый.

    Три проверки, от простой к строгой: размер, начало exe-файла («MZ») и
    sha256 из выпуска. Без них битая закачка доходила до запуска и падала
    с непонятной ошибкой вроде «incorrect header check».
    """
    try:
        size = path.stat().st_size
    except OSError as error:
        return f"файл не читается: {error}"
    if size_hint and size != size_hint:
        return f"размер {size} вместо {size_hint} байт"
    try:
        with path.open("rb") as file:
            head = file.read(2)
    except OSError as error:
        return f"файл не читается: {error}"
    if head != b"MZ":
        return "это не программа Windows"
    want = digest[len("sha256:"):].lower() if digest.startswith("sha256:") else ""
    if want:
        hasher = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != want:
            return "контрольная сумма не совпала"
    return None


def download(url: str, on_progress=None, size_hint: int = 0,
             digest: str = "") -> Path:
    """Скачать exe с докачкой и проверить его. on_progress(получено, всего).

    Битый файл не отдаём дальше: удаляем и качаем заново. Если после
    восьми попыток файл так и не сошёлся с выпуском, обновление
    отменяется — старая версия остаётся целой, а человек получает
    понятную причину вместо ошибки распаковки.
    """
    folder = Path(tempfile.gettempdir()) / TEMP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ASSET_NAME
    part = folder / (ASSET_NAME + ".part")

    last_error: Exception | None = None
    corrupt = False
    for attempt in range(1, 9):
        corrupt = False
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
            problem = check_file(part, size_hint, digest)
            if problem is not None:
                # Докачка с битым началом уже не поможет: начинаем с нуля.
                part.unlink(missing_ok=True)
                corrupt = True
                raise OSError(problem)
            os.replace(part, target)
            return target
        except (HTTPError, URLError, OSError) as error:
            last_error = error
            log(f"Попытка {attempt}: {error}")
            if attempt == 8:
                break
            time.sleep(min(15, 3 * attempt))
    if corrupt:
        raise UpdateError(
            "Скачанный файл пришёл битым, поэтому обновление отменено — "
            "старая версия работает. Скачай новую версию вручную со "
            "страницы выпусков на GitHub.")
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


def _start_later(exe: Path) -> subprocess.Popen:
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NO_WINDOW)
    kwargs = {"cwd": str(exe.parent), "env": _clean_env(), "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = flags
    return subprocess.Popen(
        [str(exe), "--wait-pid", str(os.getpid())], **kwargs)


def _start_installer(staged: Path, target: Path) -> subprocess.Popen:
    """Запустить скачанную версию установщиком: она ждёт нас и меняет exe."""
    kwargs = {"cwd": str(target.parent), "env": _clean_env(),
              "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NO_WINDOW)
    args = [str(staged), INSTALL_FLAG, str(target),
            "--wait-pid", str(os.getpid())]
    return subprocess.Popen(args, **kwargs)


def _launch_checked(exe: Path, tries: int = 3) -> tuple[bool, str, int]:
    """Запустить exe и убедиться, что он не упал сразу же.

    Свежепоставленная версия иногда не стартует: её не пускает антивирус
    или скопировалась она не целиком. Две секунды достаточно, чтобы
    понять, что процесс живёт, а не закрылся на первом же шаге.
    """
    last = "процесс сразу закрылся"
    for attempt in range(1, tries + 1):
        try:
            proc = _start_later(exe)
        except OSError as error:
            last = f"запуск не удался: {error}"
        else:
            time.sleep(2.0)
            if _process_alive(proc.pid):
                log(f"Обновление: {exe.name} запущен, pid {proc.pid}.")
                return True, "", proc.pid
            last = "процесс сразу закрылся"
        log(f"Обновление: попытка запуска {attempt} не вышла — {last}.")
        time.sleep(1.0)
    return False, last, 0


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
    log(f"Обновление: ставлю {new_file.name} вместо {exe}.")
    try:
        if staged.exists():
            staged.unlink()
        shutil.copy2(str(new_file), str(staged))
        log(f"Обновление: новая версия скопирована в {staged.name}.")
    except OSError as error:
        log(f"Обновление: скопировать не вышло — {error}.")
        return False, (f"Не удалось положить новую версию в папку программы "
                       f"{exe.parent}: {error}. Похоже, в эту папку нельзя "
                       f"писать — перенеси программу вместе с папкой Дата "
                       f"в свою папку, например в Документы, и обновление "
                       f"пройдёт.")
    try:
        proc = _start_installer(staged, exe)
        log(f"Обновление: установщик запущен, pid {proc.pid}.")
    except OSError as error:
        log(f"Обновление: установщик не запустился — {error}.")
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
    log(f"Установщик: ставлю {me.name} на место {target}.")

    # Windows отпускает exe не мгновенно после выхода процесса: ещё
    # работает загрузчик PyInstaller, иногда файл держит антивирус.
    # Минуту ждём и пробуем снова — обычно хватает одной-двух попыток,
    # а минута выручает, когда рядом остался второй запуск программы.
    moved = False
    deadline = time.monotonic() + 60.0
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
        log(f"Установщик: старый файл не освободился — {last_error}.")
        return False, (f"Не удалось освободить файл программы "
                       f"{target.name}: {last_error}. Похоже, программа всё "
                       f"ещё запущена — закрой все её окна и значки в трее "
                       f"и запусти обновление снова.")
    log("Установщик: старый файл убран, копирую новый.")

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
        log(f"Установщик: скопировать новый файл не вышло — {last_error}.")
        try:
            target.unlink(missing_ok=True)
            if old.exists():
                old.rename(target)
        except OSError:
            pass
        return False, f"Не удалось поставить новую версию: {last_error}"

    ok, reason, _pid = _launch_checked(target)
    if ok:
        log("Установщик: новая версия установлена и запущена.")
        return True, "Новая версия установлена."

    # Новая версия не поднялась — возвращаем прошлую, чтобы человек не
    # остался без программы, и честно говорим, что случилось.
    log(f"Установщик: новая версия не запускается ({reason}), "
        f"возвращаю прошлую.")
    restored = False
    try:
        bad = target.with_name(_stem(target) + ".failed.exe")
        if target.exists():
            target.rename(bad)
        if old.exists():
            old.rename(target)
        restored = True
        try:
            bad.unlink(missing_ok=True)
        except OSError:
            pass
    except OSError as error:
        log(f"Установщик: вернуть прошлую версию не вышло — {error}.")
    if restored:
        try:
            _start_later(target)
        except OSError as error:
            log(f"Установщик: прошлую версию запустить не вышло — {error}.")
            return False, (f"Новая версия не запустилась ({reason}), прошлую "
                           f"вернуть на место удалось, но запустить её не "
                           f"вышло: {error}. Запусти программу вручную.")
        return False, (f"Новая версия не запустилась ({reason}). Программа "
                       f"вернулась к прошлой версии и уже запущена снова. "
                       f"Попробуй обновление ещё раз позже.")
    return False, (f"Новая версия не запустилась ({reason}). Запусти "
                   f"программу вручную и попробуй обновление ещё раз позже.")


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
