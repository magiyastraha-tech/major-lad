"""Настройки программы — пока только сочетания клавиш.

Файл `Data/Настройки.txt` устроен по-простому: слева действие, справа
клавиши. Такой файл можно прочитать диктором и поправить блокнотом, а
программа при запуске просто его читает.

    card = F2
    today = F3
    export =

Пустая правая часть — действие без клавиши. Незнакомые строки
пропускаются: файл не должен ломать запуск.
"""
from __future__ import annotations

from pathlib import Path

FILE = "Настройки.txt"

# (код, название для диктора, клавиши по умолчанию)
ACTIONS = (
    ("card", "Карточка ученика", "F2"),
    ("today", "Уроки на сегодня", "F3"),
    ("tomorrow", "Уроки на завтра", "F4"),
    ("earnings", "Заработок за месяц", "F5"),
    ("export", "Выгрузить данные в текст", "F9"),
    ("settings", "Настройки", "F10"),
)

CODES = tuple(code for code, _, _ in ACTIONS)
TITLES = {code: title for code, title, _ in ACTIONS}

HEADER = """# Настройки программы «Мажорный лад Расписание».
# Слева — действие, справа — клавиши. Пусто справа — без клавиши.
"""


def defaults() -> dict[str, str]:
    return {code: keys for code, _, keys in ACTIONS}


def load(folder: Path) -> dict[str, str]:
    """Прочитать настройки; недостающее берётся по умолчанию."""
    result = defaults()
    path = Path(folder) / FILE
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        code, _, keys = line.partition("=")
        code = code.strip()
        if code in CODES:
            result[code] = keys.split("#", 1)[0].strip()
    return result


def save(folder: Path, bindings: dict[str, str]) -> Path:
    path = Path(folder) / FILE
    lines = [HEADER]
    for code, title, _ in ACTIONS:
        lines.append(f"# {title}")
        lines.append(f"{code} = {bindings.get(code, '')}")
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
