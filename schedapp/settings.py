"""Настройки программы — сочетания клавиш и громкость сигналов.

Файл `Data/Настройки.txt` устроен по-простому: слева действие, справа
клавиши. Такой файл можно прочитать диктором и поправить блокнотом, а
программа при запуске просто его читает.

    card = F2
    today = F3
    export =
    volume = 80

Пустая правая часть — действие без клавиши. Незнакомые строки
пропускаются: файл не должен ломать запуск.
"""
from __future__ import annotations

from pathlib import Path

FILE = "Настройки.txt"

# Громкость сигналов хранится в том же файле отдельной строкой:
#     volume = 80
VOLUME_KEY = "volume"
DEFAULT_VOLUME = 80

# (код, название для диктора, клавиши по умолчанию)
ACTIONS = (
    ("card", "Карточка ученика", "F2"),
    ("today", "Уроки на сегодня", "F3"),
    ("tomorrow", "Уроки на завтра", "F4"),
    ("earnings", "Заработок за месяц", "F5"),
    ("export", "Выгрузить данные в текст", "F9"),
    ("settings", "Настройки", "Ctrl+P"),
)

# Раньше настройки открывались по F10. Если в файле так и осталось —
# подставляем новую клавишу, иначе Ctrl+P не заработает у тех, кто уже
# запускал программу. Если человек выбрал что-то своё — не трогаем.
OLD_SETTINGS_KEY = "F10"

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
    if result["settings"] == OLD_SETTINGS_KEY:
        result["settings"] = defaults()["settings"]
    return result


def volume_value(text: str | int | None) -> int:
    """Громкость в процентах: 0–100, что попало — приводим к норме."""
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return DEFAULT_VOLUME
    return max(0, min(100, value))


def load_volume(folder: Path) -> int:
    """Громкость сигналов из файла настроек; нет строки — по умолчанию."""
    path = Path(folder) / FILE
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return DEFAULT_VOLUME
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        code, _, value = line.partition("=")
        if code.strip() == VOLUME_KEY:
            return volume_value(value.split("#", 1)[0])
    return DEFAULT_VOLUME


def save(folder: Path, bindings: dict[str, str],
         volume: int | None = None) -> Path:
    path = Path(folder) / FILE
    if volume is None:
        volume = load_volume(folder)
    lines = [HEADER]
    for code, title, _ in ACTIONS:
        lines.append(f"# {title}")
        lines.append(f"{code} = {bindings.get(code, '')}")
    lines.append("# Громкость сигналов, проценты (0 — без звука)")
    lines.append(f"{VOLUME_KEY} = {volume_value(volume)}")
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
