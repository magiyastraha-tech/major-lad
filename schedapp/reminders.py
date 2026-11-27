"""Напоминания об уроках и сроках услуг.

Программа тикает раз в полминуты и смотрит, не пора ли что-то сказать:
за сутки до урока, за час и за пять минут. Каждое напоминание звучит на
пять минут раньше круглой отметки: урок во вторник в 11:00 — напоминания
в понедельник в 10:55, во вторник в 9:55 и во вторник в 10:55.
В начале урока ничего спрашивать не нужно: урок стартует сам по
расписанию, и через сорок пять минут служба спрашивает, прошёл ли он.
Если ответа нет, вопрос повторяется через час, пока не будет ответа.
Кнопка «Начать урок» сдвигает отсчёт: урок начался раньше или позже —
значит, и конец наступит через сорок пять минут после нажатия.

Ещё она следит за сроками услуг: за три дня и за день до конца аренды
гитары или гарантии на настройку.

Само напоминание здесь не показывается: служба только говорит «пора»,
а окно решает, как это озвучить. Так проще проверять логику отдельно от
интерфейса.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from PySide6.QtCore import QObject, QTimer, Signal

from . import dates, services
from .store import LESSON_WAIT, Lesson, Payment, plural

DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
# Пять минут — тот запас, на который напоминания опережают урок: до
# начала остаётся время услышать и собраться.
EARLY = timedelta(minutes=5)

# После урока напоминания не нужны: если программа запустилась позже,
# сообщения за прошедшие дни не вываливаются пачкой.
LATE_WINDOW = timedelta(hours=6)

ASK_EVERY = timedelta(hours=1)
LESSON_LENGTH = timedelta(minutes=45)

# За сутки, за час и за пять минут — каждое на пять минут раньше
# круглой отметки. Например, урок во вторник в 11:00: напоминание в
# понедельник в 10:55, во вторник в 9:55 и во вторник в 10:55.
KINDS = (
    ("день", DAY + EARLY),
    ("час", HOUR + EARLY),
    ("пять", EARLY),
)


class ReminderService(QObject):
    """Сторож уроков: говорит, когда напоминать и о чём спрашивать."""

    soon = Signal(object, str)              # урок, вид напоминания
    service_soon = Signal(object, object, int)  # услуга, конец срока, дней
    ask_finish = Signal(object)             # урок: «прошёл ли урок?»

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self._fired: set[tuple] = set()
        self._started: dict[int, datetime] = {}
        self._next_ask: dict[int, datetime] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self.tick)
        self._timer.start()
        # Первую проверку делаем, когда окно уже подключило сигналы, —
        # иначе напоминание, срок которого наступил при запуске, будет
        # помечено сработавшим и пропадёт.
        QTimer.singleShot(0, self.tick)

    # --- ответы окна на вопросы -------------------------------------------

    def mark_started(self, lesson: Lesson,
                     moment: datetime | None = None) -> None:
        """Урок начался: через 45 минут спросим, прошёл ли он.

        `moment` — когда урок начался на самом деле. Кнопка «Начать
        урок» передаёт время нажатия, поэтому подсказки времени и
        вопрос о конце отсчитываются от него, а не от расписания.
        """
        moment = moment or datetime.now()
        self._started[lesson.uid] = moment
        self._next_ask[lesson.uid] = moment + LESSON_LENGTH

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

    def first_start(self) -> datetime | None:
        """Когда начался самый ранний из идущих уроков.

        По этой отметке подсказки времени считают, сколько прошло от
        начала занятия. Нет идущих уроков — None.
        """
        if not self._started:
            return None
        return min(self._started.values())

    # --- проверка ---------------------------------------------------------

    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        for lesson in list(self.store.lessons):
            if lesson.status != LESSON_WAIT:
                continue
            if (lesson.uid, "кончен") in self._fired:
                continue
            stamp = lesson.stamp

            # Напоминания до урока: за сутки, за час и за пять минут.
            for kind, delta in KINDS:
                mark = (lesson.uid, kind)
                if mark in self._fired:
                    continue
                moment = stamp - delta
                if moment <= now < stamp + LATE_WINDOW:
                    self._fired.add(mark)
                    self.soon.emit(lesson, kind)

            # Начало урока: в назначенное время урок стартует сам.
            # Кнопка «Начать урок» ставит свою отметку через
            # mark_started — тогда отсчёт идёт от нажатия.
            if lesson.uid not in self._started:
                if now < stamp or now >= stamp + LATE_WINDOW:
                    continue
                self._started[lesson.uid] = stamp
                self._next_ask[lesson.uid] = stamp + LESSON_LENGTH
            moment = self._next_ask.get(lesson.uid)
            if moment is not None and now >= moment:
                self._next_ask[lesson.uid] = now + ASK_EVERY
                self.ask_finish.emit(lesson)

        self._check_services(now)

    def _check_services(self, now: datetime) -> None:
        """Сроки услуг: за три дня и за день до конца аренды или гарантии.

        Срок выводится из даты услуги, поэтому отметка срабатывания
        включает сам конец срока: поправил дату — напоминание взведено
        заново, удалил услугу — оно пропадает вместе с записью.

        Если программа была закрыта в нужный день, напоминание скажет,
        сколько дней осталось на самом деле, — но только пока срок не
        кончился.
        """
        today = now.date()
        window = max(services.DAYS_BEFORE)
        for payment in list(self.store.payments):
            end = services.service_end(payment)
            if end is None:
                continue
            days = (end - today).days
            if days < 0 or days > window:
                continue
            # Две ступени: «за три дня» и «за день». Первая — пока до
            # конца больше дня, вторая — последний день и день конца.
            stage = "три" if days > 1 else "день"
            mark = (payment.uid, "услуга", stage, end.isoformat())
            if mark in self._fired:
                continue
            self._fired.add(mark)
            self.service_soon.emit(payment, end, days)


def soon_text(lesson: Lesson, kind: str) -> str:
    """Фраза напоминания до урока — одна на все три вида.

    Виды приходят с запасом в пять минут, поэтому «через час» звучит
    как «примерно через час»: до урока остаётся час и пять минут.

    Имя стоит отдельно, без «с»: склонять фамилии программа не умеет,
    а «урок с Андрей Булкин» диктор прочитает как ошибку.
    """
    who = f"{lesson.student}, в {lesson.time}."
    if kind == "день":
        return f"Завтра урок. {who}"
    if kind == "час":
        return f"Примерно через час урок. {who}"
    return f"Через пять минут урок. {who}"


def _day_words(day: date) -> str:
    """«21 ноября» — без цифр, которые диктор читает по одной."""
    return f"{day.day} {dates.MONTHS[day.month - 1]}"


def service_text(payment: Payment, end: date, days: int) -> str:
    """Фраза о конце срока услуги — аренды гитары или гарантии.

    Имя ставим после слова «ученик», в именительном падеже: склонять
    фамилии программа не умеет, а «ученик Иван Петров» звучит верно.
    """
    if days <= 0:
        when = f"сегодня, {_day_words(end)}"
    elif days == 1:
        when = f"завтра, {_day_words(end)}"
    else:
        when = (f"{_day_words(end)}, осталось {days} "
                f"{plural(days, 'день', 'дня', 'дней')}")
    return (f"{services.service_title(payment)}, ученик {payment.student}, "
            f"заканчивается {when}.")
