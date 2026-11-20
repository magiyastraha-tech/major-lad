"""Напоминания об уроках.

Программа тикает раз в полминуты и смотрит, не пора ли что-то сказать:
за день до урока, за час и за десять минут. Отдельно она спрашивает в
начале урока, идёт ли он, и через сорок минут — прошёл ли. Если ответа
нет, вопрос повторяется через час, пока не будет ответа.

Само напоминание здесь не показывается: служба только говорит «пора»,
а окно решает, как это озвучить. Так проще проверять логику отдельно от
интерфейса.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QTimer, Signal

from .store import LESSON_WAIT, Lesson

DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
TEN = timedelta(minutes=10)

# После урока напоминания не нужны: если программа запустилась позже,
# сообщения за прошедшие дни не вываливаются пачкой.
LATE_WINDOW = timedelta(hours=6)

ASK_EVERY = timedelta(hours=1)
LESSON_LENGTH = timedelta(minutes=40)

KINDS = (
    ("день", DAY),
    ("час", HOUR),
    ("десять", TEN),
)


class ReminderService(QObject):
    """Сторож уроков: говорит, когда напоминать и о чём спрашивать."""

    soon = Signal(object, str)      # урок, вид напоминания
    ask_start = Signal(object)      # урок: «идёт ли у вас урок?»
    ask_finish = Signal(object)     # урок: «прошёл ли урок?»

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self._fired: set[tuple[int, str]] = set()
        self._started: dict[int, datetime] = {}
        self._next_ask: dict[int, datetime] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self.tick)
        self._timer.start()
        self.tick()

    # --- ответы окна на вопросы -------------------------------------------

    def mark_started(self, lesson: Lesson) -> None:
        """Урок начался: через 40 минут спросим, прошёл ли он."""
        self._started[lesson.uid] = datetime.now()
        self._next_ask[lesson.uid] = datetime.now() + LESSON_LENGTH

    def mark_finished(self, lesson: Lesson) -> None:
        self._started.pop(lesson.uid, None)
        self._next_ask.pop(lesson.uid, None)
        self._fired.add((lesson.uid, "кончен"))

    def mark_cancelled(self, lesson: Lesson) -> None:
        self._started.pop(lesson.uid, None)
        self._next_ask.pop(lesson.uid, None)
        self._fired.add((lesson.uid, "кончен"))

    def snooze(self, lesson: Lesson, delay: timedelta = ASK_EVERY) -> None:
        """Ответа не было — вернёмся к этому уроку через час."""
        self._next_ask[lesson.uid] = datetime.now() + delay

    def started_at(self, lesson: Lesson) -> datetime | None:
        return self._started.get(lesson.uid)

    # --- проверка ---------------------------------------------------------

    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        for lesson in list(self.store.lessons):
            if lesson.status != LESSON_WAIT:
                continue
            if (lesson.uid, "кончен") in self._fired:
                continue
            stamp = lesson.stamp

            # Напоминания до урока: за день, за час, за десять минут.
            for kind, delta in KINDS:
                mark = (lesson.uid, kind)
                if mark in self._fired:
                    continue
                moment = stamp - delta
                if moment <= now < stamp + LATE_WINDOW:
                    self._fired.add(mark)
                    self.soon.emit(lesson, kind)

            # Начало урока.
            if lesson.uid in self._started:
                moment = self._next_ask.get(lesson.uid)
                if moment is not None and now >= moment:
                    self._next_ask[lesson.uid] = now + ASK_EVERY
                    self.ask_finish.emit(lesson)
                continue
            if now < stamp:
                continue
            moment = self._next_ask.get(lesson.uid)
            if moment is None:
                # Первый вопрос — с начала урока и до конца окна.
                if now < stamp + LATE_WINDOW:
                    self._next_ask[lesson.uid] = now + ASK_EVERY
                    self.ask_start.emit(lesson)
                continue
            if now >= moment:
                self._next_ask[lesson.uid] = now + ASK_EVERY
                self.ask_start.emit(lesson)


def soon_text(lesson: Lesson, kind: str) -> str:
    """Фраза напоминания до урока — одна на все три вида.

    Имя стоит отдельно, без «с»: склонять фамилии программа не умеет,
    а «урок с Андрей Булкин» диктор прочитает как ошибку.
    """
    who = f"{lesson.student}, в {lesson.time}."
    if kind == "день":
        return f"Завтра урок. {who}"
    if kind == "час":
        return f"Через час урок. {who}"
    return f"Через десять минут урок. {who}"
