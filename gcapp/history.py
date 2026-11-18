"""История переписки преподавателя.

Файл `History.txt` лежит рядом с программой преподавателя и хранит весь чат
между занятиями. Ученик своего файла не держит: при подключении он получает
историю от преподавателя по частям — сначала последние сообщения, а более
ранние подгружаются, когда он листает чат вверх.

Формат строки — время, номер, отправитель и текст через табуляцию. Табуляции
и переводы строк внутри текста экранируются, чтобы одна строка файла всегда
оставалась одним сообщением.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from . import runlog

# Дальше этого история не растёт: старые сообщения отрезаются с начала.
# Пяти тысяч строк хватает на годы занятий, а файл остаётся небольшим.
MAX_MESSAGES = 5000


def history_path() -> Path:
    """Путь к файлу: рядом с exe, а при запуске из исходников — в проекте."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "History.txt"
    return Path(__file__).resolve().parent.parent / "History.txt"


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


def load() -> list[dict]:
    """Читает историю. Битые строки пропускаются — файл может быть повреждён."""
    path = history_path()
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


def save(messages: list[dict]) -> None:
    """Перезаписывает файл целиком. Ошибка не роняет программу."""
    path = history_path()
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
