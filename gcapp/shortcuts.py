"""Сочетания клавиш на кнопки и регуляторы главного окна.

Здесь только описание действий: что можно назначить и как это называется
для диктора. Само назначение хранится в config.json в разделе shortcuts,
применяет его главное окно.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

# Шаг регулятора громкости на одно нажатие сочетания.
SLIDER_STEP = 5

# Кнопки главного окна: код действия и название для диктора.
BUTTON_ACTIONS = [
    ("volume_mode", "Переключить режим громкости"),
    ("metronome", "Метроном, включить или выключить"),
    ("add_track", "Добавить трек"),
    ("import_audio", "Импортировать аудио"),
    ("play", "Играть или остановить трек"),
    ("back", "Перемотать назад"),
    ("forward", "Перемотать вперёд"),
    ("delete_track", "Удалить трек"),
    ("effects", "Показать или скрыть эффекты"),
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
    ("mic", "Выключить или включить микрофон"),
    ("camera", "Включить или выключить камеру"),
]

# Действия, которых нет в одной из версий программы.
TEACHER_ONLY = ("import_audio", "join")

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
        actions.append((action_id, title))
    for key, _title in SLIDER_ACTIONS:
        name = slider_title(key, is_teacher)
        actions.append((f"volume_up:{key}", f"{name}: громче на пять процентов"))
        actions.append((f"volume_down:{key}", f"{name}: тише на пять процентов"))
    return actions


def normalize(text: str) -> str:
    """Приводит сочетание к единому виду для хранения и сравнения."""
    seq = QKeySequence(text or "")
    if seq.isEmpty():
        return ""
    return seq.toString(QKeySequence.SequenceFormat.PortableText)


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
