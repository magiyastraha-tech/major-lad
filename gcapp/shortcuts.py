"""Сочетания клавиш на кнопки и регуляторы главного окна.

Здесь только описание действий: что можно назначить и как это называется
для диктора. Само назначение хранится в config.json в разделе shortcuts,
применяет его главное окно.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

# Цифровой блок Qt помечает отдельным флагом, но в строку сочетания его не
# пишет. Своя пометка делает клавишу нумпада отличимой от такой же клавиши
# сверху — иначе назначенное «1 на нумпаде» срабатывало бы на обычной «1».
NUMPAD_PREFIX = "Num+"
NUMLOCK_TEXT = "NumLock"

# Клавиши-модификаторы: сами по себе сочетанием не являются.
MODIFIER_KEYS = (
    Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta,
    Qt.Key.Key_AltGr, Qt.Key.Key_CapsLock, Qt.Key.Key_unknown,
)

# Шаг регулятора громкости на одно нажатие сочетания.
SLIDER_STEP = 5

# Кнопки главного окна: код действия и название для диктора.
BUTTON_ACTIONS = [
    ("volume_mode", "Переключить режим громкости"),
    ("metronome", "Метроном, включить или выключить"),
    ("add_track", "Добавить трек"),
    ("import_audio", "Импортировать аудио"),
    ("play", "Играть, остановить или продолжить трек"),
    ("pause", "Пауза"),
    ("prev_track", "Выше: предыдущий трек"),
    ("next_track", "Ниже: следующий трек"),
    ("back", "Перемотать назад"),
    ("forward", "Перемотать вперёд"),
    ("delete_track", "Удалить выбранный трек"),
    ("effects", "Скорость и тон: показать или скрыть"),
    ("slow", "Замедлить трек на десять процентов"),
    ("fast", "Убыстрить трек на десять процентов"),
    ("lower", "Опустить тон на полутон"),
    ("raise", "Поднять тон на полутон"),
    ("speed_reset", "Сбросить скорость трека"),
    ("pitch_reset", "Сбросить тон трека"),
    ("join", "Присоединиться к преподавателю"),
    ("chat_focus", "Перейти к полю сообщения"),
    ("send", "Отправить сообщение"),
    ("send_file", "Отправить файл"),
    ("settings", "Открыть настройки"),
    ("connect", "Открыть кабинет или подключиться"),
    ("mic", "Микрофон: включить или выключить"),
    ("camera", "Камера: включить или выключить"),
]

# Действия, которых нет у преподавателя: присоединиться может только
# ученик. Импорт трека есть у обеих сторон, поэтому в списке он тоже у
# обеих — раньше здесь стоял import_audio, и преподаватель не мог
# назначить на него сочетание.
TEACHER_ONLY = ("join",)

# Действия, которых нет у ученика: камера есть только у преподавателя,
# его видео уходит ученику. У ученика камеры нет — сочетание ему незачем.
STUDENT_ONLY_OFF = ("camera",)

# Регуляторы громкости. Для каждого два действия: громче и тише.
SLIDER_ACTIONS = [
    ("student", "Звук собеседника"),
    ("in1", "Аудиоустройство 1"),
    ("in2", "Аудиоустройство 2"),
    ("usb", "USB-устройство"),
    ("midi", "MIDI-устройство"),
    ("metronome", "Громкость метронома"),
    ("player", "Громкость трека"),
]

# Сочетания, которые работали до появления вкладки. Их видно сразу.
DEFAULT_KEYS = {
    "settings": "Ctrl+P",
    "mic": "Ctrl+A",
    "camera": "Ctrl+V",
}

# Метка «сочетание снято»: отличает осознанно очищенное действие от того,
# у которого настройки ещё не было и должно работать сочетание по умолчанию.
UNSET = "none"


def resolved(mapping: dict, action_id: str) -> str:
    """Действующее сочетание действия: своё, снятое или по умолчанию."""
    raw = (mapping or {}).get(action_id)
    if raw == UNSET:
        return ""
    if raw:
        return normalize(str(raw))
    return normalize(DEFAULT_KEYS.get(action_id, ""))


def slider_title(key: str, is_teacher: bool) -> str:
    if key == "student":
        return "Звук ученика" if is_teacher else "Звук преподавателя"
    for name, title in SLIDER_ACTIONS:
        if name == key:
            return title
    return key


def all_actions(is_teacher: bool) -> list[tuple[str, str]]:
    """Все действия по порядку: сначала кнопки, потом регуляторы."""
    actions: list[tuple[str, str]] = []
    for action_id, title in BUTTON_ACTIONS:
        if is_teacher and action_id in TEACHER_ONLY:
            continue
        if not is_teacher and action_id in STUDENT_ONLY_OFF:
            continue
        actions.append((action_id, title))
    for key, _title in SLIDER_ACTIONS:
        name = slider_title(key, is_teacher)
        actions.append((f"volume_up:{key}", f"{name}: громче на пять процентов"))
        actions.append((f"volume_down:{key}", f"{name}: тише на пять процентов"))
    return actions


def combo_from_event(event) -> str:
    """Сочетание из нажатия клавиши, с пометкой цифрового блока и NumLock.

    Собирать строку из самого события, а не через QKeySequenceEdit, нужно
    ради нумпада: Qt клавишу цифрового блока записывает так же, как
    обычную цифру, и различие теряется.
    """
    key = event.key()
    if key in MODIFIER_KEYS:
        return ""
    mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
    pad = bool(event.modifiers() & Qt.KeyboardModifier.KeypadModifier)
    # Строку собираем сами, без QKeyCombination: в некоторых сборках
    # PySide6 этого класса в QtGui нет, и программа падала при запуске.
    # Имя самой клавиши знает QKeySequence, модификаторы дописываем в том
    # же порядке, в каком их печатает Qt: Ctrl, Alt, Shift, Meta.
    name = QKeySequence(key).toString(QKeySequence.SequenceFormat.PortableText)
    if not name:
        return ""
    parts = []
    if mods & Qt.KeyboardModifier.ControlModifier:
        parts.append("Ctrl")
    if mods & Qt.KeyboardModifier.AltModifier:
        parts.append("Alt")
    if mods & Qt.KeyboardModifier.ShiftModifier:
        parts.append("Shift")
    if mods & Qt.KeyboardModifier.MetaModifier:
        parts.append("Meta")
    # Пометку ставим прямо перед клавишей: Ctrl+Num+1 читается диктором
    # как «Control, нумпад 1».
    parts.append(NUMPAD_PREFIX + name if pad else name)
    return "+".join(parts)


def pretty(text: str) -> str:
    """Сочетание для диктора: нумпад и нумлок называем по-русски."""
    text = text or ""
    if not text:
        return ""
    if text == NUMLOCK_TEXT:
        return "Нумлок"
    text = text.replace(NUMPAD_PREFIX, "Нумпад ")
    if text.endswith("+" + NUMLOCK_TEXT):
        text = text[:-len(NUMLOCK_TEXT)] + "Нумлок"
    return text


def _is_numpad(text: str) -> bool:
    return NUMPAD_PREFIX in (text or "") or (text or "").endswith(NUMLOCK_TEXT)


def normalize(text: str) -> str:
    """Приводит сочетание к единому виду для хранения и сравнения."""
    text = (text or "").strip()
    if not text:
        return ""
    # Свои сочетания нумпада QKeySequence разобрал бы по-другому — храним
    # их как есть.
    if _is_numpad(text):
        return text
    seq = QKeySequence(text)
    if seq.isEmpty():
        return ""
    return seq.toString(QKeySequence.SequenceFormat.PortableText)


def text_is_safe(text: str) -> tuple[bool, str]:
    """Проверка сочетания, записанного строкой."""
    text = (text or "").strip()
    if not text:
        return False, "Сочетание не задано."
    if _is_numpad(text):
        return True, ""
    return combo_is_safe(QKeySequence(text))


def combo_is_safe(seq: QKeySequence) -> tuple[bool, str]:
    """Годится ли сочетание для глобального действия.

    Без Control, Alt или F-клавиши действие перехватывало бы обычный ввод
    текста в чате, поэтому такие сочетания не принимаем.
    """
    if seq.isEmpty():
        return False, "Сочетание не задано."
    if seq.count() != 1:
        return False, "Нужно одно сочетание, без цепочки из нескольких."
    combo = seq[0]
    key = combo.key()
    mods = combo.keyboardModifiers()
    strong = mods & (Qt.KeyboardModifier.ControlModifier
                     | Qt.KeyboardModifier.AltModifier
                     | Qt.KeyboardModifier.MetaModifier)
    is_fkey = Qt.Key.Key_F1 <= key <= Qt.Key.Key_F12
    if not (strong or is_fkey):
        return False, ("В сочетании должен быть Control, Alt или клавиша "
                       "F1–F12: иначе оно будет мешать вводу текста в чате.")
    return True, ""
