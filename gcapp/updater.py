"""Проверка и установка обновлений.

Обновления лежат в релизах GitHub. Программа спрашивает последний релиз,
сравнивает версии и, если вышла новая, скачивает свой exe во временную папку.
Запущенный exe на Windows не отдаёт сам себя — ни перезаписать, ни даже
переименовать. Поэтому установка идёт в два шага: скачанная версия ложится
рядом с программой под именем «...new.exe» и запускается установщиком; она
дожидается нашего выхода, убирает старый файл в «...old.exe» и встаёт на его
место, когда файл уже никто не держит. Настройки лежат в json рядом и при
обновлении не трогаются.
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
# Не releases/latest: в этом же репозитории выходит «Расписание», и его
# свежий релиз не содержит exe звонка. latest указывал бы на расписание,
# нужного файла там нет — программа отвечала «это последняя версия»,
# хотя новая уже вышла. Поэтому берём список и ищем ближайший релиз,
# в котором есть наш exe.
API_RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=30"

ASSETS = {
    "teacher": "MajorLad-Teacher.exe",
    "student": "MajorLad-Student.exe",
}

USER_AGENT = "MajorLad-Updater"

# Сколько раз пробуем докачать файл после обрыва связи.
_DOWNLOAD_TRIES = 8

# Установка обновления: скачанная копия ждёт нас, потом встаёт на место.
INSTALL_FLAG = "--install-update"
STAGED_SUFFIX = ".new.exe"


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
    data = json.loads(_get(API_RELEASES).decode("utf-8"))
    releases = data if isinstance(data, list) else []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        version = str(
            release.get("tag_name") or release.get("name") or "").strip()
        # Релизы расписания живут в том же репозитории и к звонку отношения
        # не имеют — даже если тег случайно окажется «новее».
        if version.lower().startswith("schedule"):
            continue
        url = ""
        for asset in release.get("assets") or []:
            if asset.get("name") == asset_name:
                url = asset.get("browser_download_url") or ""
                break
        if not version or not url:
            continue
        if not is_newer(version, current):
            continue
        return {
            "version": version,
            "url": url,
            "notes": str(release.get("body") or "").strip(),
        }
    return {"none": True}


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


def backup_path_for(exe: Path) -> Path:
    """Куда уходит старая версия при установке: рядом, с меткой .old."""
    return exe.with_name(f"{exe.stem}.old.exe")


def staged_path_for(exe: Path) -> Path:
    """Куда ложится новая версия до установки: рядом, с меткой .new."""
    return exe.with_name(f"{exe.stem}{STAGED_SUFFIX}")


def backup_path() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    return backup_path_for(Path(sys.executable))


def cleanup_staged() -> None:
    """Убирает временную копию новой версии, если установка не довелась."""
    if not getattr(sys, "frozen", False):
        return
    staged = staged_path_for(Path(sys.executable))
    for _ in range(10):
        try:
            if not staged.exists():
                return
            staged.unlink(missing_ok=True)
            return
        except OSError:
            time.sleep(0.3)


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
    """Кладёт новую версию рядом с программой и запускает установщик.

    Запущенный exe на Windows держит сам себя — подменить его на месте
    нельзя. Поэтому здесь ничего не подменяется: скачанная версия ложится
    в папку программы под именем «...new.exe», запускается установщиком и
    ждёт, пока мы закроемся. Установщик уже после нашего выхода убирает
    старый файл и встаёт на его место — когда файл никто не держит.
    """
    if not getattr(sys, "frozen", False):
        return False, "Обновление работает только в собранной программе."
    exe = Path(sys.executable)
    staged = staged_path_for(exe)
    try:
        if staged.exists():
            staged.unlink()
        shutil.copy2(str(new_file), str(staged))
    except Exception as exc:
        return False, (
            f"Не удалось положить новую версию в папку программы "
            f"{exe.parent}: {exc}. Похоже, в эту папку нельзя писать — "
            f"перенеси программу вместе с папкой Data в свою папку, "
            f"например в Документы, и обновление пройдёт.")
    try:
        _start_installer(staged, exe)
    except Exception as exc:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"Не удалось запустить установщик обновления: {exc}"
    return True, ""


def _start_installer(staged: Path, target: Path) -> None:
    """Запускает скачанную версию установщиком: она ждёт нас и меняет exe."""
    args = [str(staged), INSTALL_FLAG, str(target),
            "--wait-pid", str(os.getpid())]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    subprocess.Popen(args, cwd=str(target.parent), close_fds=True,
                     creationflags=flags, env=_clean_env())


def take_install_target() -> Path | None:
    """Вынимает из аргументов `--install-update <путь>`: мы — установщик."""
    argv = list(sys.argv)
    for index, value in enumerate(argv):
        if value == INSTALL_FLAG and index + 1 < len(argv):
            target = Path(argv[index + 1])
            del argv[index:index + 2]
            sys.argv[:] = argv
            return target
        if value.startswith(INSTALL_FLAG + "="):
            target = Path(value.split("=", 1)[1])
            del argv[index]
            sys.argv[:] = argv
            return target
    return None


def finish_install(target: Path) -> tuple[bool, str]:
    """Встаёт новой версией на место старой и запускает её.

    Здесь уже нечему мешать: прошлая программа закрыта, exe свободен.
    Копируем себя (мы запущены из «...new.exe») на место программы, а не
    переименовываем: переименовывать запущенный файл Windows не даёт.
    """
    me = Path(sys.executable)
    target = Path(target)
    old = backup_path_for(target)
    last_error: Exception | None = None

    # Windows отпускает exe не мгновенно после выхода процесса: ещё
    # работает загрузчик PyInstaller, иногда файл держит антивирус.
    # Полминуты ждём и пробуем снова — обычно хватает одной-двух попыток.
    moved = False
    deadline = time.monotonic() + 30.0
    while not moved and time.monotonic() < deadline:
        try:
            if old.exists():
                old.unlink()
            if target.exists():
                target.rename(old)
            moved = True
        except Exception as exc:
            last_error = exc
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
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    if not copied:
        try:
            target.unlink(missing_ok=True)
            if old.exists():
                old.rename(target)
        except Exception:
            pass
        return False, f"Не удалось поставить новую версию: {last_error}"

    if not _start_later(target):
        return False, "Новая версия на месте, но запустить её не удалось."
    return True, ""
