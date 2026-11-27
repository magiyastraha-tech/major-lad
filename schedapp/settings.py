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

# Подсказки времени урока — своя громкость и своя строка «включено»:
#     cue_volume = 70
#     cues = off
CUE_VOLUME_KEY = "cue_volume"
DEFAULT_CUE_VOLUME = 70
CUES_KEY = "cues"
# Папка для копии базы в облаке (например, Диск O):
#     cloud = O:\Мажорный лад
CLOUD_KEY = "cloud"
# Порядок списков абонементов:
#     subs_order = date   — по дате добавления, новые сверху (по умолчанию)
#     subs_order = name   — по алфавиту
#     subs_order = bought — по дате покупки, новые сверху
#     subs_order = first  — по дню первого урока, новые сверху
SUBS_ORDER_KEY = "subs_order"
SUBS_ORDER_DATE = "date"
SUBS_ORDER_NAME = "name"
SUBS_ORDER_BOUGHT = "bought"
SUBS_ORDER_FIRST = "first"
# Значения «включено» во всех написаниях, какие можно набрать в блокноте.
CUES_ON = ("on", "вкл", "включено", "включены", "1", "да", "yes", "true")

# (код, название для диктора, клавиши по умолчанию)
ACTIONS = (
    ("card", "Карточка ученика", "F2"),
    ("today", "Уроки на сегодня", "F3"),
    ("tomorrow", "Уроки на завтра", "F4"),
    ("earnings", "Заработок за месяц", "F5"),
    ("export", "Выгрузить данные в текст", "F9"),
    ("subexport", "Выгрузить абонемент в текст", "F6"),
    ("past_lessons", "Прошедшие уроки", "F7"),
    ("planned_lessons", "Запланированные уроки", "F8"),
    ("settings", "Настройки", "Ctrl+P"),
)

# Кнопки на вкладках главного окна. Их тоже можно назначить на клавиши;
# по умолчанию клавиш нет — человек выбирает сам в настройках. Пока
# действия не назначены, список остаётся каким был.
BUTTON_ACTIONS = (
    # Урок стартует сам по расписанию; эта кнопка — чтобы начать его
    # раньше или позже, если ученик пришёл не вовремя.
    ("start", "Начать урок", ""),
    ("add_student", "Добавить ученика", ""),
    ("edit_student", "Редактировать ученика", ""),
    ("remove_student", "Удалить ученика", ""),
    # Код не «cues»: так называется строка включения подсказок в файле.
    ("toggle_cues", "Включить или выключить подсказки времени", ""),
    ("trial", "Добавить пробный урок", ""),
    ("single", "Добавить разовый урок", ""),
    ("sub_add", "Добавить абонемент", ""),
    ("sub_done", "Отметить урок проведённым", ""),
    ("sub_edit", "Редактировать абонемент", ""),
    ("sub_remove", "Удалить абонемент", ""),
    ("sub_order", "Порядок абонементов", ""),
    ("service_add", "Добавить услугу", ""),
    ("service_edit", "Редактировать услугу", ""),
    ("service_remove", "Удалить услугу", ""),
    ("block_add", "Добавить исключение уроков", ""),
)

# Вкладки главного окна. Клавиши по умолчанию — Ctrl вместе с цифрой
# на цифровом блоке: Num Lock при этом работает в любом положении.
TAB_ACTIONS = (
    ("tab_students", "Вкладка «Ученики»", "Ctrl+Num1"),
    ("tab_schedule", "Вкладка «Расписание»", "Ctrl+Num2"),
    ("tab_active", "Вкладка «Действующие абонементы»", "Ctrl+Num3"),
    ("tab_finished", "Вкладка «Законченные абонементы»", "Ctrl+Num4"),
    ("tab_one_off", "Вкладка «Разовые и пробные уроки»", "Ctrl+Num5"),
    ("tab_services", "Вкладка «Услуги»", "Ctrl+Num6"),
    ("tab_notifications", "Вкладка «Уведомления»", "Ctrl+Num7"),
    ("tab_blocks", "Вкладка «Исключения»", "Ctrl+Num8"),
    ("tab_future", "Вкладка «Будущие абонементы»", "Ctrl+Num9"),
)

ALL_ACTIONS = ACTIONS + BUTTON_ACTIONS + TAB_ACTIONS

# Раньше настройки открывались по F10. Если в файле так и осталось —
# подставляем новую клавишу, иначе Ctrl+P не заработает у тех, кто уже
# запускал программу. Если человек выбрал что-то своё — не трогаем.
OLD_SETTINGS_KEY = "F10"

CODES = tuple(code for code, _, _ in ALL_ACTIONS)
TITLES = {code: title for code, title, _ in ALL_ACTIONS}

HEADER = """# Настройки программы «Мажорный лад Расписание».
# Слева — действие, справа — клавиши. Пусто справа — без клавиши.
"""


def defaults() -> dict[str, str]:
    return {code: keys for code, _, keys in ALL_ACTIONS}


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


def volume_value(text: str | int | None, default: int = DEFAULT_VOLUME) -> int:
    """Громкость в процентах: 0–100, что попало — приводим к норме."""
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return default
    return max(0, min(100, value))


def cue_volume_value(text: str | int | None) -> int:
    """Громкость подсказок времени; нет значения — своя по умолчанию."""
    return volume_value(text, DEFAULT_CUE_VOLUME)


def _values(folder: Path) -> dict[str, str]:
    """Все строки файла настроек: слева имя, справа значение."""
    path = Path(folder) / FILE
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        code, _, value = line.partition("=")
        result[code.strip()] = value.split("#", 1)[0].strip()
    return result


def load_volume(folder: Path) -> int:
    """Громкость сигналов из файла настроек; нет строки — по умолчанию."""
    raw = _values(folder).get(VOLUME_KEY)
    return DEFAULT_VOLUME if raw is None else volume_value(raw)


def load_cue_volume(folder: Path) -> int:
    """Громкость подсказок времени; нет строки — по умолчанию."""
    raw = _values(folder).get(CUE_VOLUME_KEY)
    return DEFAULT_CUE_VOLUME if raw is None else cue_volume_value(raw)


def load_cues(folder: Path) -> bool:
    """Включены ли подсказки времени. По умолчанию выключены."""
    return _values(folder).get(CUES_KEY, "").casefold() in CUES_ON


def load_cloud(folder: Path) -> Path | None:
    """Папка для копий в облаке; пусто — копирование выключено."""
    raw = _values(folder).get(CLOUD_KEY, "").strip()
    return Path(raw) if raw else None


def subs_order_value(text: str | None) -> str:
    """Порядок списков абонементов: дата добавления, алфавит, покупка, урок."""
    raw = (text or "").strip().casefold()
    if raw == SUBS_ORDER_NAME:
        return SUBS_ORDER_NAME
    if raw == SUBS_ORDER_BOUGHT:
        return SUBS_ORDER_BOUGHT
    if raw == SUBS_ORDER_FIRST:
        return SUBS_ORDER_FIRST
    return SUBS_ORDER_DATE


def load_subs_order(folder: Path) -> str:
    """Как разложены абонементы. По умолчанию — по дате добавления."""
    return subs_order_value(_values(folder).get(SUBS_ORDER_KEY))


def save_subs_order(folder: Path, order: str) -> Path:
    """Записать только порядок абонементов, не трогая остальные настройки."""
    values = _values(folder)
    bindings = {code: values.get(code, keys)
                for code, _title, keys in ALL_ACTIONS}
    return save(folder, bindings, subs_order=subs_order_value(order))


def save_cloud(folder: Path, cloud: Path | str | None) -> Path:
    """Записать только папку для копий, не трогая остальные настройки."""
    values = _values(folder)
    bindings = {code: values.get(code, keys)
                for code, _title, keys in ALL_ACTIONS}
    return save(folder, bindings, load_volume(folder),
                load_cue_volume(folder), load_cues(folder),
                cloud="" if not cloud else str(cloud))


def save(folder: Path, bindings: dict[str, str],
         volume: int | None = None, cue_volume: int | None = None,
         cues: bool | None = None, cloud: Path | str | None = None,
         subs_order: str | None = None) -> Path:
    path = Path(folder) / FILE
    if volume is None:
        volume = load_volume(folder)
    if cue_volume is None:
        cue_volume = load_cue_volume(folder)
    if cues is None:
        cues = load_cues(folder)
    if cloud is None:
        cloud = load_cloud(folder) or ""
    if subs_order is None:
        subs_order = load_subs_order(folder)
    lines = [HEADER]
    for code, title, _ in ALL_ACTIONS:
        lines.append(f"# {title}")
        lines.append(f"{code} = {bindings.get(code, '')}")
    lines.append("# Громкость сигналов, проценты (0 — без звука)")
    lines.append(f"{VOLUME_KEY} = {volume_value(volume)}")
    lines.append("# Громкость подсказок времени урока, проценты (0 — без звука)")
    lines.append(f"{CUE_VOLUME_KEY} = {cue_volume_value(cue_volume)}")
    lines.append("# Подсказки времени урока: on — включены, off — выключены")
    lines.append(f"{CUES_KEY} = {'on' if cues else 'off'}")
    lines.append("# Папка для копии в облаке (пусто — копирование выключено)")
    lines.append(f"{CLOUD_KEY} = {cloud}")
    lines.append("# Порядок абонементов: date — по дате добавления, "
                 "name — по алфавиту, bought — по дате покупки, "
                 "first — по дню первого урока")
    lines.append(f"{SUBS_ORDER_KEY} = {subs_order_value(subs_order)}")
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
