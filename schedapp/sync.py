"""Копия базы и настроек в облачную папку — для работы на двух устройствах.

Облачная папка (например, «Диск O» от Mail.ru) — это обычная папка на
диске. Программа кладёт туда снимок базы и файл-опись с номером
сохранения, а на другом устройстве по номеру видно, чья копия свежее.

Почему не работаем с базой прямо в облаке: SQLite через сетевой диск
может испортиться при обрыве связи. Поэтому копия — снимок, сделанный
штатным механизмом SQLite (`backup`), а запись — через временный файл и
переименование: недописанной копии в облаке не бывает.

Между устройствами действует правило «последнее сохранение побеждает».
Если на другом устройстве есть сохранения новее, программа не затирает
их молча: при запуске скажет об этом, а взять данные можно кнопкой
«Взять данные из копии».
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from . import settings

COPY_NAME = "Мажорный лад Расписание.db"
NOTE_NAME = "Мажорный лад Расписание.облако.json"
TABLES = ("students", "subscriptions", "lessons", "payments")


class SyncError(Exception):
    """Копия не получилась; текст исключения можно показать человеку."""


def default_folder() -> Path | None:
    """Куда класть копию по умолчанию: «Диск O», если он есть."""
    if os.name != "nt":
        return None
    for letter in ("O", "o"):
        root = Path(f"{letter}:\\")
        try:
            if root.exists():
                return root / "Мажорный лад"
        except OSError:
            continue
    return None


def device_name() -> str:
    return os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or ""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _human(day: str) -> str:
    """«2026-11-21 18:40» → «21.11.2026 в 18:40», как читает диктор."""
    try:
        moment = datetime.strptime(day, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return day or "время неизвестно"
    return moment.strftime("%d.%m.%Y в %H:%M")


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _snapshot(db_path: Path, target: Path) -> None:
    """Снимок базы штатным механизмом SQLite — без полузаписанных данных."""
    source = sqlite3.connect(str(db_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(path))
    try:
        return {table: int(conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in TABLES}
    finally:
        conn.close()


def verify(path: Path) -> dict[str, int]:
    """Проверить базу и вернуть число записей; битую — не пускаем дальше."""
    if not path.exists():
        raise SyncError("Файла копии нет.")
    try:
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise SyncError("Копия повреждена: проверка базы не прошла.")
            return {table: int(conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in TABLES}
        finally:
            conn.close()
    except sqlite3.Error as error:
        raise SyncError(f"Копия не читается: {error}") from error


def _counts_text(counts: dict[str, int]) -> str:
    from .store import plural
    students = counts.get("students", 0)
    lessons = counts.get("lessons", 0)
    subs = counts.get("subscriptions", 0)
    payments = counts.get("payments", 0)
    return (f"учеников {students}, "
            f"{plural(lessons, 'урок', 'урока', 'уроков')}, "
            f"{plural(subs, 'абонемент', 'абонемента', 'абонементов')}, "
            f"{plural(payments, 'оплата', 'оплаты', 'оплат')}")


def read_note(folder: Path) -> dict:
    """Опись облачной копии; пустой словарь, если её нет или она битая."""
    try:
        text = (folder / NOTE_NAME).read_text(encoding="utf-8")
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write(store, folder: Path) -> None:
    """Положить в облако снимок базы и настроек вместе с описью."""
    folder = Path(folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SyncError(f"Папка для копий недоступна: {error}") from error
    existing = read_note(folder)
    try:
        cloud_saves = int(existing.get("saves") or 0)
    except (TypeError, ValueError):
        cloud_saves = 0
    if cloud_saves > store.save_no:
        # Чужое сохранение свежее — не затираем его своей старой базой.
        raise SyncError("В облаке есть более новые сохранения, поэтому "
                        "копию не записываю, чтобы их не затереть. Открой "
                        "настройки и нажми «Взять данные из копии».")
    target = folder / COPY_NAME
    temp = folder / (COPY_NAME + ".new")
    try:
        if temp.exists():
            temp.unlink()
        _snapshot(store._db_path, temp)
        verify(temp)
        os.replace(temp, target)

        # Настройки рядом, чтобы клавиши и громкость были одинаковыми.
        source_settings = store.folder / settings.FILE
        if source_settings.exists():
            settings_temp = folder / (settings.FILE + ".new")
            settings_temp.write_bytes(source_settings.read_bytes())
            os.replace(settings_temp, folder / settings.FILE)

        note = {
            "saves": int(store.save_no),
            "saved_at": _now(),
            "device": device_name(),
            "sha256": _sha256(target),
        }
        note_temp = folder / (NOTE_NAME + ".new")
        note_temp.write_text(json.dumps(note, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        os.replace(note_temp, folder / NOTE_NAME)
    except (OSError, sqlite3.Error) as error:
        for leftover in (temp,):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        raise SyncError(f"Копия в облако не записалась: {error}") from error


def cloud_state(store, folder: Path) -> dict:
    """Что сейчас в облаке и свежее ли оно местных данных."""
    folder = Path(folder)
    note = read_note(folder)
    copy = folder / COPY_NAME
    state = {"note": note, "exists": copy.exists(), "newer": False}
    try:
        state["saves"] = int(note.get("saves") or 0)
    except (TypeError, ValueError):
        state["saves"] = 0
    state["newer"] = bool(state["exists"] and state["saves"] > store.save_no)
    return state


def check(store, folder: Path) -> str:
    """Проверить копию и рассказать словами, что с ней."""
    folder = Path(folder)
    if not folder.exists():
        raise SyncError(f"Папки для копий нет: {folder}.")
    copy = folder / COPY_NAME
    if not copy.exists():
        raise SyncError("Облачной копии пока нет: сохрани её кнопкой "
                        "«Сохранить копию сейчас».")
    counts = verify(copy)
    note = read_note(folder)
    parts = ["Облачная копия целая."]
    if note:
        parts.append(f"Сохранение {note.get('saves', 0)} "
                     f"от {_human(note.get('saved_at', ''))}.")
        if note.get("device"):
            parts.append(f"Устройство: {note['device']}.")
    parts.append(_counts_text(counts) + ".")
    try:
        digest = _sha256(copy)
    except OSError as error:
        raise SyncError(f"Копию не удалось прочитать: {error}") from error
    if note.get("sha256") and note["sha256"] != digest:
        raise SyncError("Копия не совпала с описью: файл в облаке "
                        "изменился или недописался. Не берём его.")
    state = cloud_state(store, folder)
    if state["newer"]:
        parts.append("В облаке есть сохранения новее местных — можно "
                     "взять их кнопкой «Взять данные из копии».")
    else:
        parts.append("В облаке не новее местных: брать оттуда нечего.")
    return " ".join(parts)


def restore(store, folder: Path) -> str:
    """Взять данные из облачной копии, сохранив текущую базу в резерв.

    Текущая база не удаляется и не перезаписывается «на живую»: сначала
    снимок в папку Резерв, потом снимок из облака переносится в открытую
    базу. Если облако окажется битым, проверка не пустит его дальше.
    """
    folder = Path(folder)
    copy = folder / COPY_NAME
    counts = verify(copy)
    note = read_note(folder)
    if note.get("sha256"):
        digest = _sha256(copy)
        if note["sha256"] != digest:
            raise SyncError("Копия не совпала с описью: файл в облаке "
                            "изменился или недописался. Не берём его.")

    # Страховка: что было до взятия копии, всегда можно вернуть.
    backup_folder = store.folder / "Резерв"
    backup_folder.mkdir(parents=True, exist_ok=True)
    backup = backup_folder / (
        "перед восстановлением " + datetime.now().strftime("%Y-%m-%d %H-%M")
        + ".db")
    try:
        _snapshot(store._db_path, backup)
    except (OSError, sqlite3.Error) as error:
        raise SyncError("Не удалось сделать резервную копию текущих "
                        f"данных, поэтому не беру облачную: {error}"
                        ) from error

    source = sqlite3.connect(str(copy))
    try:
        source.backup(store._conn)
    except sqlite3.Error as error:
        raise SyncError(f"Данные из облака не перенеслись: {error}") from error
    finally:
        source.close()

    # Копия могла быть сделана прежней версией, без новых таблиц.
    # Дочитываем схему до `load()`: он читает все таблицы разом.
    try:
        store.ensure_schema()
    except sqlite3.Error as error:
        raise SyncError(f"Копия от прежней версии не подошла: {error}") from error

    store.load()
    store.recount_used()
    store._refresh_files()

    # Номер сохранения теперь от облака: следующая местная запись
    # станет на единицу больше, и другое устройство увидит её как новую.
    saves = 0
    try:
        saves = int(note.get("saves") or 0)
    except (TypeError, ValueError):
        saves = 0
    store.set_save_no(saves)

    # Настройки (клавиши, громкость) — вместе с данными.
    cloud_settings = folder / settings.FILE
    if cloud_settings.exists():
        try:
            temp = store.folder / (settings.FILE + ".new")
            temp.write_bytes(cloud_settings.read_bytes())
            os.replace(temp, store.folder / settings.FILE)
        except OSError:
            pass

    when = _human(note.get("saved_at", "")) if note else "время неизвестно"
    return (f"Данные взяты из облачной копии: сохранение {saves} "
            f"от {when}. {_counts_text(counts).capitalize()}. "
            f"Прежняя база сохранена в папке Резерв.")


def newer_message(store, folder: Path) -> str:
    """Фраза для запуска, если в облаке есть более свежие данные."""
    state = cloud_state(store, folder)
    if not state["newer"]:
        return ""
    note = state["note"]
    text = ("В облаке есть более новые данные: сохранение "
            f"{state['saves']} от {_human(note.get('saved_at', ''))}")
    if note.get("device"):
        text += f", устройство {note['device']}"
    return (text + ". Чтобы взять их, открой настройки и нажми "
            "«Взять данные из копии».")
