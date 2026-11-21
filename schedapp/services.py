"""Сроки услуг: аренда гитары — месяц, гарантия на настройку — год.

Срок не хранится отдельным полем: он всегда считается от даты услуги.
Так правка даты автоматически сдвигает и срок, и напоминания о нём.
"""
from __future__ import annotations

import calendar
from datetime import date

from .store import Payment

RENT = "Аренда гитары"
TUNING = "Настройка гитары"

# За сколько дней до конца срока напоминать. Первое напоминание — когда
# до конца остаётся три дня, второе — когда один.
DAYS_BEFORE = (3, 1)


def add_month(day: date) -> date:
    """Та же дата через месяц; 31 января → 28 или 29 февраля."""
    if day.month == 12:
        year, month = day.year + 1, 1
    else:
        year, month = day.year, day.month + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def add_year(day: date) -> date:
    """Та же дата через год; 29 февраля → 28 февраля."""
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return day.replace(year=day.year + 1, day=28)


def service_end(payment: Payment) -> date | None:
    """Когда кончается срок услуги; у услуг без срока — None."""
    if payment.what == RENT:
        return add_month(payment.day)
    if payment.what == TUNING:
        return add_year(payment.day)
    return None


def service_title(payment: Payment) -> str:
    """Как назвать срок услуги в напоминании и в списке."""
    if payment.what == RENT:
        return "Аренда гитары"
    if payment.what == TUNING:
        return "Гарантия на настройку гитары"
    return payment.what
