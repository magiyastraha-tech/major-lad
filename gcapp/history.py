"""История переписки преподавателя — по файлу на ученика.

В папке Data рядом с программой преподавателя лежит папка `History`, в ней
по файлу на каждого ученика: имя ученика и есть имя файла. В файле вся переписка с ним —
и его сообщения, и ответы преподавателя. Между занятиями ничего не пропадает.

У ученика своих файлов нет: при подключении он получает историю от
преподавателя по частям — сначала последние сообщения, а более ранние
подгружаются, когда он листает чат вверх.

Формат строки — время, номер, отправитель и текст через табуляцию. Табуляции
и переводы строк внутри текста экранируются, чтобы одна строка файла всегда
оставалась одним сообщением.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from . import config, runlog

# Дальше этого история не растёт: старые сообщения отрезаются с начала.
# Пяти тысяч строк хватает на годы занятий, а файл остаётся небольшим.
MAX_MESSAGES = 5000

# Имена, которые Windows не даёт использовать для файлов.
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)}
_BAD_CHARS = '<>:"/\\|?*'


def history_dir() -> Path:
    """Папка истории: Data/History рядом с программой."""
    return config.data_dir() / "History"


def safe_name(student: str) -> str:
    """Имя ученика, пригодное для файла на Windows."""
    name = "".join("-" if (ch in _BAD_CHARS or ord(ch) < 32) else ch
                   for ch in str(student or ""))
    name = name.strip(" .")
    if not name:
        return "Ученик"
    if name.upper() in _RESERVED:
        name = "_" + name
    return name[:60].strip(" .") or "Ученик"


def file_for(student: str) -> Path:
    """Файл переписки с учеником: History/<имя>.txt."""
    return history_dir() / f"{safe_name(student)}.txt"


def _escape(text: str) -> str:
    return (str(text).replace("\\", "\\\\")
            .replace("\t", "\\t")
            .replace("\r", "\\r")
            .replace("\n", "\\n"))


def _unescape(text: str) -> str:
    out: list[str] = []
    it = iter(text)
    for ch in it:
        if ch != "\\":
            out.append(ch)
            continue
        try:
            nxt = next(it)
        except StopIteration:
            out.append("\\")
            break
        out.append({"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}.get(nxt, nxt))
    return "".join(out)


def load_file(path: Path) -> list[dict]:
    """Читает историю. Битые строки пропускаются — файл может быть повреждён."""
    messages: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                parts = line.split("\t", 3)
                if len(parts) != 4:
                    continue
                try:
                    when = datetime.fromisoformat(parts[0])
                except ValueError:
                    continue
                messages.append({
                    "id": parts[1],
                    "time": when.isoformat(timespec="seconds"),
                    "sender": parts[2],
                    "text": _unescape(parts[3]),
                })
    except OSError:
        return []
    return messages[-MAX_MESSAGES:]


def load(student: str) -> list[dict]:
    """Переписка с учеником из его файла. Нет файла — пустой список."""
    return load_file(file_for(student))


def save_file(messages: list[dict], path: Path) -> None:
    """Перезаписывает файл целиком. Ошибка не роняет программу."""
    lines = []
    for m in messages[-MAX_MESSAGES:]:
        lines.append("\t".join((
            str(m.get("time") or ""),
            str(m.get("id") or ""),
            str(m.get("sender") or ""),
            _escape(str(m.get("text") or "")),
        )))
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
            if lines:
                fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        runlog.write(f"История чата не сохранена: {exc}")
        try:
            tmp.unlink()
        except OSError:
            pass


def save(messages: list[dict], student: str) -> None:
    """Сохраняет переписку с учеником в его файл."""
    save_file(messages, file_for(student))


# Имя, под которым старый общий файл переезжает в папку History. Версия
# в имени — последняя, где такой файл писался: если папку откроют через
# месяц, сразу понятно, откуда это взялось.
_LEGACY_NAME = "Переписка до 0.2.5.txt"


def retire_legacy_file() -> Path | None:
    """Убирает старый общий History.txt, перенося его в папку History.

    До версии 0.2.5 вся переписка лежала одним файлом рядом с программой.
    Теперь она разложена по ученикам, и общий файл только мешает — его
    переносим целиком, ничего не теряя. Возвращает новый путь, если файл
    был, иначе None.
    """
    # Миграция переносит History.txt в Data, но если перенести не удалось
    # (файл был занят), старый экземпляр ищем и рядом с exe.
    legacy = None
    for base in (config.data_dir(), config.app_dir()):
        candidate = base / "History.txt"
        if candidate.is_file():
            legacy = candidate
            break
    if legacy is None:
        return None
    folder = history_dir()
    target = folder / _LEGACY_NAME
    try:
        folder.mkdir(parents=True, exist_ok=True)
        i = 1
        while target.exists():
            target = folder / f"{Path(_LEGACY_NAME).stem} ({i}).txt"
            i += 1
        os.replace(legacy, target)
    except OSError as exc:
        runlog.write(f"Старый History.txt не удалось перенести: {exc}")
        return None
    runlog.write(f"Старый History.txt перенесён в History/{target.name}")
    return target


def page(messages: list[dict], before_id: str = "",
         limit: int = 50) -> tuple[list[dict], bool]:
    """Возвращает порцию истории и признак «раньше есть ещё».

    Пустой before_id — самые новые сообщения. Иначе берутся сообщения,
    которые были до указанного: так ученик листает вверх по одной порции.
    """
    limit = max(1, min(int(limit or 50), 200))
    if before_id:
        index = next((i for i, m in enumerate(messages)
                      if str(m.get("id") or "") == before_id), None)
        if index is None:
            return [], False
        messages = messages[:index]
    chunk = messages[-limit:]
    return chunk, len(messages) > len(chunk)
