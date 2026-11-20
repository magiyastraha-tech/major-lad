"""Даты и время словами — так, чтобы их было удобно слушать.

В файлах дата хранится коротко и однозначно: 25.11.2026. Диктору и тебе
программа называет её «25 ноября, среда» — без цифр, которые он читает
по одной.
"""
from __future__ import annotations

from datetime import date

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")
MONTHS_NOM = ("январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница",
            "суббота", "воскресенье")
# В расписании удобнее короткие названия дней недели.
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def parse_date(text: str) -> date | None:
    """«25.11.2026» → дата. Пустая строка или мусор — None."""
    parts = str(text or "").strip().split(".")
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[2]), int(parts[1]), int(parts[0]))
    except ValueError:
        return None


def fmt_date(day: date) -> str:
    return f"{day.day:02d}.{day.month:02d}.{day.year}"


def human_date(day: date) -> str:
    """«25 ноября, среда»."""
    return f"{day.day} {MONTHS[day.month - 1]}, {WEEKDAYS[day.weekday()]}"


def parse_time(text: str) -> tuple[int, int] | None:
    """«18:00» → (18, 0). Пустая строка или мусор — None."""
    parts = str(text or "").strip().replace(".", ":").split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour, minute


def fmt_time(hour: int, minute: int) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


def speak_time(text: str) -> str:
    """«18:00» → «18:00» (диктор читает как время сам)."""
    return str(text or "")
