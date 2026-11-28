"""Главное окно и формы программы «Мажорный лад Расписание».

Правило одно и то же во всех окнах: после каждого действия — ровно одна
фраза диктору. Фокус при этом остаётся там, где ты стоял, поэтому можно
продолжать работать стрелками сразу после ответа.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import (QDate, QEvent, QThread, Qt, QTimer, QUrl,
                            Signal)
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QIcon,
                           QKeySequence, QPainter, QPixmap, QShortcut)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget, QSystemTrayIcon,
)

from . import (__version__, autostart, dates, services, settings, sounds,
               store as data, sync, updater)
from .reminders import (KINDS, LATE_WINDOW, LESSON_LENGTH, ReminderService,
                        service_text, soon_text)
from .store import (DIRECTIONS, FORMATS, LESSON_CANCELLED, LESSON_DONE,
                    LESSON_WAIT, SERVICES, Store, money, plural)
from .uikit import Announcer, announce, key_text

WEEK_LABELS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница",
               "Суббота", "Воскресенье")
MINUTES = [f"{m:02d}" for m in range(0, 60, 5)]

# Фраза для последнего урока в абонементе: звучит после весёлого сигнала,
# до вопроса «урок прошёл?». Это подсказка преподавателю, что сказать
# ученику, поэтому обращение на «вы».
LAST_LESSON_TEXT = (
    "Минуточку внимания! У вас сегодня последнее занятие в абонементе. "
    "Если новый абонемент приобретёте сегодня, одно занятие — в подарок.")

# Подсказки времени от начала урока: (минута, звук).
CUE_MARKS = ((10, "cue10"), (20, "cue20"), (30, "cue30"), (40, "cue40"))


def make_date_edit(name: str, initial: date | None = None) -> QDateEdit:
    """Поле даты: набирается с клавиатуры и открывается календарём."""
    edit = QDateEdit()
    day = initial or date.today()
    edit.setDate(QDate(day.year, day.month, day.day))
    edit.setDisplayFormat("dd.MM.yyyy")
    edit.setCalendarPopup(True)
    edit.setAccessibleName(name)
    return edit


def picked_date(edit: QDateEdit) -> date:
    value = edit.date()
    return date(value.year(), value.month(), value.day())


def rubles(amount: int) -> str:
    return f"{money(amount)} {plural(amount, 'рубль', 'рубля', 'рублей')}"


def split_name(name: str) -> tuple[str, str]:
    """Разложить «Имя Фамилия» на два поля: имя — первое слово."""
    parts = (name or "").split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def lesson_number(store: Store,
                  lesson: data.Lesson) -> tuple[int, int] | None:
    """Номер урока внутри абонемента и сколько в нём всего уроков.

    Отменённый урок и урок без абонемента номера не получают. Для
    проведённого номер — его место среди уроков абонемента, для
    ожидающего — сколько уже проведено плюс сколько ожидающих идёт
    раньше него: так номер в расписании не прыгает при переносе.
    """
    if lesson.status == LESSON_CANCELLED:
        return None
    sub = None
    if lesson.subscription:
        for item in store.subscriptions:
            if item.uid == lesson.subscription:
                sub = item
                break
    if sub is None or sub.one_off or not sub.total:
        sub = store.current_subscription(lesson.student)
    if sub is None or sub.one_off or not sub.total:
        return None
    if lesson.status == LESSON_DONE:
        for index, item in enumerate(store.subscription_lessons(sub)):
            if item.uid == lesson.uid:
                return index + 1, sub.total
        return None
    number = sub.used + 1
    for item in store.lessons:
        if (item.uid != lesson.uid and item.student == lesson.student
                and item.status == LESSON_WAIT
                and item.subscription in (0, sub.uid)
                and item.stamp < lesson.stamp):
            number += 1
    if number > sub.total:
        return None
    return number, sub.total


def lesson_line(store: Store, lesson: data.Lesson,
                with_status: bool = True) -> str:
    """Урок одной фразой: как его прочитает диктор.

    Если урок входит в абонемент, после имени звучит его номер: «урок 11».
    """
    parts = [dates.human_date(lesson.day), f"в {lesson.time}",
             lesson.student]
    number = lesson_number(store, lesson)
    if number is not None:
        parts.append(f"урок {number[0]}")
    parts += [lesson.direction.casefold(), lesson.lesson_format.casefold()]
    if with_status:
        parts.append(lesson.status)
    return ", ".join(parts)


def make_icon() -> QIcon:
    """Иконка в трее: тёмный квадрат с нотой, без внешних файлов."""
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#2b2b2b"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
    painter.setBrush(QColor("#f2c94c"))
    painter.drawEllipse(14, 34, 20, 16)
    painter.drawRect(30, 12, 6, 26)
    painter.end()
    return QIcon(pix)


# --- формы ---------------------------------------------------------------


class StudentDialog(QDialog):
    """Карточка ученика: имя, фамилия, телефон, почта.

    Больше ничего не спрашиваем: формат, направление и абонемент заводятся
    потом — при добавлении пробного, разового урока или абонемента.
    """

    def __init__(self, store: Store, parent=None, student: data.Student | None = None):
        super().__init__(parent)
        self.store = store
        self.student = student
        self.created: data.Student | None = None
        adding = student is None
        self.setWindowTitle("Добавить ученика" if adding else "Изменить ученика")
        self.setMinimumWidth(460)

        form = QFormLayout(self)
        form.setSpacing(10)

        # Имя обязательно, фамилия нет. В базе ученик по-прежнему хранится
        # одной строкой «Имя Фамилия» — так его видят все остальные списки.
        first, last = split_name(student.name if student else "")
        self.first_name = QLineEdit(first)
        self.first_name.setAccessibleName("Имя")
        form.addRow("Имя", self.first_name)

        self.last_name = QLineEdit(last)
        self.last_name.setAccessibleName("Фамилия")
        form.addRow("Фамилия", self.last_name)

        self.phone = QLineEdit(student.phone if student else "")
        self.phone.setAccessibleName("Телефон")
        form.addRow("Телефон", self.phone)

        self.email = QLineEdit(student.email if student else "")
        self.email.setAccessibleName("Почта")
        form.addRow("Почта", self.email)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Добавить ученика" if adding else "Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self.first_name.setFocus()

    def _accept(self) -> None:
        first = self.first_name.text().strip()
        if not first:
            announce(self, "Имя не заполнено. Напиши имя ученика.")
            self.first_name.setFocus()
            return
        name = " ".join(part for part in (first, self.last_name.text().strip())
                        if part)
        other = self.store.find_student(name)
        if other is not None and other is not self.student:
            announce(self, f"Ученик {name} уже есть в списке.")
            self.first_name.setFocus()
            return
        if self.student is None:
            student = self.store.add_student(
                name, phone=self.phone.text(), email=self.email.text())
            self.created = student
            self.accept()
            return
        student = self.student
        old_name = student.name
        student.name = name
        student.phone = self.phone.text().strip()
        student.email = self.email.text().strip()
        # Абонементы, уроки и оплаты хранят имя ученика. Переименование
        # должно пройти по ним тоже, иначе карточка останется на старом
        # имени и покажет пустоту.
        if old_name != student.name:
            self.store.rename_student(old_name, student.name)
        self.store.update_student(student)
        self.created = student
        self.accept()


class StudentPickDialog(QDialog):
    """Выбор ученика из списка: стрелками, Enter — дальше.

    Нужен кнопкам «Редактировать ученика» и «Удалить ученика»: сначала
    человек выбирает, кого именно, потом открывается действие.
    """

    def __init__(self, store: Store, title: str, hint: str, parent=None):
        super().__init__(parent)
        self.store = store
        self.picked: data.Student | None = None
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)

        label = QLabel(hint)
        label.setWordWrap(True)
        lay.addWidget(label)

        self.list = QListWidget()
        self.list.setAccessibleName("Список учеников")
        for student in sorted(store.students,
                              key=lambda s: s.name.casefold()):
            item = QListWidgetItem(student.name)
            item.setData(Qt.ItemDataRole.UserRole, student.uid)
            self.list.addItem(item)
        self.list.itemDoubleClicked.connect(lambda _item: self._accept())
        lay.addWidget(self.list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Дальше")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        if self.list.count():
            self.list.setCurrentRow(0)
        self.list.setFocus()

    def _accept(self) -> None:
        item = self.list.currentItem()
        if item is None:
            announce(self, "Сначала выбери ученика в списке.")
            self.list.setFocus()
            return
        uid = item.data(Qt.ItemDataRole.UserRole)
        for student in self.store.students:
            if student.uid == uid:
                self.picked = student
                self.accept()
                return


class LessonPayDialog(QDialog):
    """«Урок прошёл?» плюс сумма, если урок разовый."""

    def __init__(self, student: str, parent=None, amount: int = 0,
                 question: str = ""):
        super().__init__(parent)
        self.amount = 0
        self.setWindowTitle("Подтверждение урока")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        text = question or f"{student}. Подтверждаете, что урок прошёл?"
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setAccessibleName(text)
        lay.addWidget(self.label)

        form = QFormLayout()
        self.money = QSpinBox()
        self.money.setRange(0, 500_000)
        self.money.setSingleStep(500)
        self.money.setSuffix(" ₽")
        self.money.setValue(int(amount))
        self.money.setAccessibleName("Сумма урока")
        form.addRow("Сумма урока", self.money)
        lay.addLayout(form)

        buttons = QDialogButtonBox()
        yes = buttons.addButton("Да, урок был",
                                QDialogButtonBox.ButtonRole.AcceptRole)
        no = buttons.addButton("Нет", QDialogButtonBox.ButtonRole.RejectRole)
        yes.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.money.setFocus()
        self.money.selectAll()

    def accept(self) -> None:
        self.amount = self.money.value()
        super().accept()


class AddDateDialog(QDialog):
    """Урок на выбранный день: день, час, минуты.

    Галочка «Двойной урок» ставит второй урок подряд, через столько же
    времени, сколько длится один урок в программе.
    """

    def __init__(self, store: Store, student: data.Student, parent=None,
                 subscription: int = 0):
        super().__init__(parent)
        self.store = store
        self.student = student
        self.subscription = int(subscription or 0)
        self.day_value: date | None = None
        self.times: list[str] = []
        self.setWindowTitle("Добавить дату урока")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.day = QComboBox()
        self.day.setAccessibleName("День урока")
        today = date.today()
        for offset in range(0, 120):
            day = today + timedelta(days=offset)
            if offset == 0:
                label = f"Сегодня, {dates.human_date(day)}"
            elif offset == 1:
                label = f"Завтра, {dates.human_date(day)}"
            else:
                label = dates.human_date(day)
            self.day.addItem(label, day)
        form.addRow("День", self.day)

        row = QHBoxLayout()
        self.hour = QSpinBox()
        self.hour.setRange(0, 23)
        self.hour.setValue(18)
        self.hour.setAccessibleName("Час")
        self.minute = QComboBox()
        self.minute.addItems(MINUTES)
        self.minute.setAccessibleName("Минуты")
        row.addWidget(self.hour)
        row.addWidget(QLabel("часов"))
        row.addWidget(self.minute)
        row.addWidget(QLabel("минут"))
        row.addStretch(1)
        form.addRow("Время", row)
        lay.addLayout(form)

        self.double = QCheckBox("Двойной урок, два урока подряд")
        lay.addWidget(self.double)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Добавить дату")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._add)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _add(self) -> None:
        day = self.day.currentData()
        hour = self.hour.value()
        minute = int(self.minute.currentText())
        first = datetime(day.year, day.month, day.day, hour, minute)
        moments = [first]
        if self.double.isChecked():
            moments.append(first + LESSON_LENGTH)
        self.day_value = day
        self.times = [dates.fmt_time(m.hour, m.minute) for m in moments]
        for moment, time in zip(moments, self.times):
            self.store.add_lesson(
                self.student.name, moment.date(), time,
                lesson_format=self.student.lesson_format,
                direction=self.student.direction,
                subscription=self.subscription)
        if self.subscription:
            self.store.promote_waiting(self.student.name)
        self.accept()


class WeekGrid(QWidget):
    """Дни недели галочками и время для каждого дня.

    Один и тот же набор нужен и в отдельном окне расписания, и в форме
    нового абонемента — поэтому он живёт отдельным полем.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.checks: list[QCheckBox] = []
        self.hours: list[QSpinBox] = []
        self.minutes: list[QComboBox] = []
        for index, label in enumerate(WEEK_LABELS):
            check = QCheckBox(label)
            check.setAccessibleName(f"Уроки в {label.lower()}")
            hour = QSpinBox()
            hour.setRange(0, 23)
            hour.setValue(18)
            hour.setAccessibleName(f"Час урока, {label.lower()}")
            minute = QComboBox()
            minute.addItems(MINUTES)
            minute.setAccessibleName(f"Минуты урока, {label.lower()}")
            check.toggled.connect(hour.setEnabled)
            check.toggled.connect(minute.setEnabled)
            hour.setEnabled(False)
            minute.setEnabled(False)
            lay.addWidget(check, index, 0)
            lay.addWidget(hour, index, 1)
            lay.addWidget(QLabel("часов"), index, 2)
            lay.addWidget(minute, index, 3)
            self.checks.append(check)
            self.hours.append(hour)
            self.minutes.append(minute)

    def picked(self) -> list[tuple[int, int, int]]:
        """Отмеченные дни: номер дня недели, час и минуты."""
        return [(i, self.hours[i].value(), int(self.minutes[i].currentText()))
                for i, check in enumerate(self.checks) if check.isChecked()]

    def set_pattern(self, pairs: list[tuple[int, int, int]]) -> None:
        """Отметить уже выбранные дни и поставить каждому своё время."""
        for weekday, hour, minute in pairs:
            if not 0 <= weekday < len(self.checks):
                continue
            self.checks[weekday].setChecked(True)
            self.hours[weekday].setValue(int(hour))
            index = self.minutes[weekday].findText(f"{int(minute):02d}")
            if index < 0:
                # Минуты в списке идут через пять: ближайшие снизу.
                index = min(len(MINUTES) - 1, int(minute) // 5)
            self.minutes[weekday].setCurrentIndex(index)


def add_weekly_lessons(store: Store, student: data.Student,
                       picked: list[tuple[int, int, int]],
                       first: date | None = None,
                       subscription: int = 0) -> int:
    """Записать уроки по отмеченным дням недели на четыре недели вперёд.

    Начало — сегодня или первый день абонемента, если он позже. Уже
    прошедшее время и уроки, которые в это же время уже стоят, пропускаются.
    Если урок попадает на исключение, он переезжает на ту же неделю вперёд
    (тот же день недели и час), пока не найдётся свободная неделя.
    """
    now = datetime.now()
    start = first or date.today()
    uid = Store._next_uid(store.lessons)
    added = 0
    for offset in range(0, 28):
        day = start + timedelta(days=offset)
        for weekday, hour, minute in picked:
            if day.weekday() != weekday:
                continue
            time = dates.fmt_time(hour, minute)
            if datetime(day.year, day.month, day.day, hour, minute) <= now:
                continue
            # Исключение — урок уходит в конец: та же неделя, тот же день
            # и час. Полгода вперёд ищем свободную неделю, дальше некуда.
            lesson_day = day
            for _attempt in range(26):
                if store.is_blocked(lesson_day, time) is None:
                    break
                lesson_day += timedelta(days=7)
            else:
                continue
            same = any(l.student == student.name and l.day == lesson_day
                       and l.time == time and l.status == LESSON_WAIT
                       for l in store.lessons)
            if same:
                continue
            # Уроки копятся в памяти, а на диск пишутся один раз в конце:
            # иначе двадцать восемь уроков заново сохраняют всю таблицу
            # и текстовые копии — программа заметно подвисает.
            store.lessons.append(data.Lesson(
                uid=uid, day=lesson_day, time=time, student=student.name,
                lesson_format=student.lesson_format,
                direction=student.direction,
                subscription=int(subscription or 0)))
            uid += 1
            added += 1
    if added:
        store.save_lessons()
        store.promote_waiting(student.name)
    return added


class ScheduleDialog(QDialog):
    """Постоянное расписание: галочки по дням недели и время каждого дня."""

    def __init__(self, store: Store, student: data.Student, parent=None,
                 subscription: int = 0):
        super().__init__(parent)
        self.store = store
        self.student = student
        self.subscription = int(subscription or 0)
        self.added = 0
        self.setWindowTitle("Добавить расписание")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        hint = QLabel("Отметь галочками дни, в которые бывают уроки, "
                      "и поставь время для каждого дня.")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.grid = WeekGrid()
        lay.addWidget(self.grid)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Добавить расписание")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._add)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _add(self) -> None:
        picked = self.grid.picked()
        if not picked:
            announce(self, "Ни один день не отмечен. Поставь галочки на нужных днях.")
            return
        self.added = add_weekly_lessons(self.store, self.student, picked,
                                        subscription=self.subscription)
        self.accept()


def weekly_pattern(store: Store,
                   student_name: str) -> list[tuple[int, int, int]]:
    """По каким дням и во сколько у ученика стоят будущие уроки.

    Если на один день недели приходится несколько времён (урок переносили
    вручную), берётся то, что встречается чаще.
    """
    today = date.today()
    counts: dict[int, dict[tuple[int, int], int]] = {}
    for lesson in store.lessons:
        if lesson.student != student_name or lesson.status != LESSON_WAIT:
            continue
        if lesson.day <= today:
            continue
        try:
            hour, minute = (int(part) for part in lesson.time.split(":")[:2])
        except ValueError:
            continue
        by_time = counts.setdefault(lesson.day.weekday(), {})
        by_time[(hour, minute)] = by_time.get((hour, minute), 0) + 1
    pattern = []
    for weekday in sorted(counts):
        best = max(counts[weekday].items(), key=lambda pair: pair[1])[0]
        pattern.append((weekday, best[0], best[1]))
    return pattern


class ScheduleEditDialog(QDialog):
    """Правка постоянного расписания: дни и часы уже отмечены.

    Человек снимает галочку с ненужного дня, ставит другой или меняет
    время, а программа перестраивает будущие уроки заново.
    """

    def __init__(self, store: Store, student: data.Student, parent=None,
                 subscription: int = 0):
        super().__init__(parent)
        self.store = store
        self.student = student
        self.subscription = int(subscription or 0)
        self.removed = 0
        self.added = 0
        self.setWindowTitle("Редактировать расписание")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        pattern = weekly_pattern(store, student.name)
        if pattern:
            text = ("Вот дни и время, по которым сейчас стоят уроки "
                    f"{student.name}. Сними галочку с ненужного дня, "
                    "поставь другой день или поменяй время и сохрани.")
        else:
            text = (f"Постоянных уроков у {student.name} сейчас нет. "
                    "Отметь дни и время, и уроки встанут на четыре "
                    "недели вперёд.")
        hint = QLabel(text)
        hint.setWordWrap(True)
        hint.setAccessibleName(text)
        lay.addWidget(hint)

        self.grid = WeekGrid()
        self.grid.set_pattern(pattern)
        lay.addWidget(self.grid)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Сохранить расписание")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _save(self) -> None:
        picked = self.grid.picked()
        if not picked:
            announce(self, "Ни один день не отмечен. Поставь галочки на "
                           "нужных днях или нажми «Отмена».")
            return
        self.removed = self.store.delete_future_waiting_lessons(
            self.student.name, subscription=self.subscription)
        self.added = add_weekly_lessons(self.store, self.student, picked,
                                        subscription=self.subscription)
        self.accept()


class ServiceDialog(QDialog):
    """Услуга: аренда, настройка, студия — и сумма в журнал оплат.

    Из карточки ученика ученик уже известен и не выбирается. На вкладке
    услуг его выбираем здесь. Если передан payment, это не новая услуга,
    а правка записанной.
    """

    def __init__(self, store: Store, student: data.Student | None = None,
                 parent=None, payment: data.Payment | None = None):
        super().__init__(parent)
        self.store = store
        self.student = student
        self.payment = payment
        self.setWindowTitle("Изменить услугу" if payment is not None
                            else "Добавить услугу")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.who: QComboBox | None = None
        if student is not None:
            who = QLabel(student.name)
            who.setAccessibleName("Ученик")
            form.addRow("Ученик", who)
        else:
            self.who = QComboBox()
            names = [s.name for s in sorted(store.students,
                                            key=lambda s: s.name.casefold())]
            if payment is not None and payment.student not in names:
                names.insert(0, payment.student)
            self.who.addItems(names)
            self.who.setAccessibleName("Ученик, кому услуга")
            if payment is not None:
                self.who.setCurrentText(payment.student)
            form.addRow("Ученик", self.who)

        self.service = QComboBox()
        self.service.addItems(SERVICES)
        self.service.setAccessibleName("Услуга")
        if payment is not None:
            if payment.what not in SERVICES:
                self.service.insertItem(0, payment.what)
            self.service.setCurrentText(payment.what)
        form.addRow("Услуга", self.service)

        self.amount = QSpinBox()
        self.amount.setRange(0, 500_000)
        self.amount.setSingleStep(500)
        self.amount.setSuffix(" ₽")
        self.amount.setAccessibleName("Сумма услуги")
        if payment is not None:
            self.amount.setValue(payment.amount)
        form.addRow("Сумма", self.amount)

        self.day = make_date_edit("Дата услуги",
                                  payment.day if payment is not None else None)
        form.addRow("Дата", self.day)
        lay.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Сохранить" if payment is not None else "Готово")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._add)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _add(self) -> None:
        name = (self.student.name if self.student is not None
                else self.who.currentText())
        what = self.service.currentText()
        amount = self.amount.value()
        day = picked_date(self.day)
        if self.payment is not None:
            self.payment.student = name
            self.payment.what = what
            self.payment.amount = amount
            self.payment.day = day
            self.store.update_payment(self.payment)
        else:
            self.payment = self.store.add_payment(name, what, amount, day)
        self.accept()


class SubscriptionDialog(QDialog):
    """Абонемент: дата покупки, дата начала, уроки, цена.

    Дата начала важнее даты покупки: человек платит иногда заранее, иногда
    с опозданием, а отсчёт уроков идёт от начала. Старый абонемент
    заводится законченным и без платежа — деньги за него пришли раньше,
    в сегодняшнюю кассу они не попадают.
    """

    def __init__(self, store: Store, student: data.Student,
                 historical: bool = False, parent=None):
        super().__init__(parent)
        self.store = store
        self.student = student
        self.historical = historical
        self.subscription: data.Subscription | None = None
        self.setWindowTitle("Добавить законченный абонемент" if historical
                            else "Добавить новый абонемент")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        if historical:
            text = (f"{student.name}. Старый абонемент уже закончился. "
                    "После добавления отметь в нём проведённые уроки.")
        else:
            text = (f"{student.name}. Новый абонемент станет действующим. "
                    "Уроки считаются от даты начала, а не от даты покупки.")
        hint = QLabel(text)
        hint.setWordWrap(True)
        hint.setAccessibleName(text)
        lay.addWidget(hint)

        form = QFormLayout()
        self.bought = make_date_edit("Дата покупки абонемента")
        form.addRow("Дата покупки", self.bought)

        self.start = make_date_edit("С какого числа действует абонемент")
        form.addRow("Абонемент с числа", self.start)

        self.direction = QComboBox()
        self.direction.addItems(DIRECTIONS)
        self.direction.setCurrentText(student.direction)
        self.direction.setAccessibleName("Направление")
        form.addRow("Направление", self.direction)

        self.count = QSpinBox()
        self.count.setRange(1, 500)
        self.count.setValue(8)
        self.count.setAccessibleName("Сколько уроков в абонементе")
        form.addRow("Уроков", self.count)

        self.price = QSpinBox()
        self.price.setRange(0, 500_000)
        self.price.setSingleStep(500)
        self.price.setSuffix(" ₽")
        self.price.setAccessibleName("Стоимость абонемента")
        form.addRow("Стоимость", self.price)
        lay.addLayout(form)

        self.schedule_added = 0
        self.schedule_empty = False
        # Расписание нужно только новому абонементу: у законченного уроки
        # уже прошли, записывать их вперёд некуда.
        self.schedule_box: QCheckBox | None = None
        self.grid: WeekGrid | None = None
        if not historical:
            self.schedule_box = QCheckBox("Заодно записать уроки по расписанию")
            self.schedule_box.setAccessibleName(
                "Заодно записать уроки по расписанию")
            self.schedule_box.setChecked(True)
            lay.addWidget(self.schedule_box)

            self.grid = WeekGrid()
            self.grid.setAccessibleName("Расписание уроков")
            lay.addWidget(self.grid)
            self.schedule_box.toggled.connect(self.grid.setEnabled)
            self.grid.setEnabled(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Добавить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._add)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.count.setFocus()
        self.count.selectAll()

    def _add(self) -> None:
        count = self.count.value()
        price = self.price.value()
        self.subscription = self.store.add_subscription(
            self.student.name, count, price=price,
            direction=self.direction.currentText(),
            bought=picked_date(self.bought),
            started=picked_date(self.start),
            finished=self.historical)
        if not self.historical and price > 0:
            word = plural(count, "урок", "урока", "уроков")
            # Оплата — в день покупки абонемента, а не всегда сегодня:
            # иначе заработок месяца уезжает не туда.
            self.store.add_payment(
                self.student.name, f"Абонемент, {count} {word}", price,
                day=picked_date(self.bought))
        if self.schedule_box is not None and self.schedule_box.isChecked():
            picked = self.grid.picked()
            if picked:
                # Расписание начинается не раньше сегодняшнего дня: то, что
                # уже прошло, вперёд не записывается.
                first = max(date.today(), self.subscription.begins)
                self.schedule_added = add_weekly_lessons(
                    self.store, self.student, picked, first=first,
                    subscription=self.subscription.uid)
            else:
                self.schedule_empty = True
        self.accept()


class OneOffLessonDialog(QDialog):
    """Одно занятие: пробный или разовый урок.

    Пробный и разовый уроки — не абонементы: урок всегда один. Пробный
    бесплатный, у разового своя цена. В историю урок попадает сразу
    законченным, чтобы не занимать действующий абонемент ученика.
    """

    def __init__(self, store: Store, student: data.Student,
                 kind: str = "trial", parent=None):
        super().__init__(parent)
        self.store = store
        self.student = student
        self.kind = kind
        self.subscription: data.Subscription | None = None
        title = "Пробный" if kind == "trial" else "Разовый"
        self.setWindowTitle(f"Добавить {title.lower()} урок")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        if kind == "trial":
            text = (f"{student.name}. Пробный урок — одно занятие: без "
                    "счёта уроков и без оплаты.")
        else:
            text = (f"{student.name}. Разовый урок — одно занятие: уроков "
                    "считать не нужно.")
        hint = QLabel(text)
        hint.setWordWrap(True)
        hint.setAccessibleName(text)
        lay.addWidget(hint)

        form = QFormLayout()
        day_name = ("Дата пробного урока" if kind == "trial"
                    else "Дата разового урока")
        self.day = make_date_edit(day_name, date.today())
        form.addRow(day_name, self.day)

        self.lesson_format = QComboBox()
        self.lesson_format.addItems(FORMATS)
        self.lesson_format.setCurrentText(student.lesson_format)
        self.lesson_format.setAccessibleName("Формат: очно или онлайн")
        form.addRow("Формат", self.lesson_format)

        self.direction = QComboBox()
        self.direction.addItems(DIRECTIONS)
        self.direction.setCurrentText(student.direction)
        self.direction.setAccessibleName("Направление")
        form.addRow("Направление", self.direction)

        self.price = None
        if kind != "trial":
            self.price = QSpinBox()
            self.price.setRange(0, 500_000)
            self.price.setSingleStep(500)
            self.price.setSuffix(" ₽")
            self.price.setAccessibleName("Стоимость разового урока")
            form.addRow("Стоимость", self.price)
        lay.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Добавить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._add)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.day.setFocus()

    def _add(self) -> None:
        day = picked_date(self.day)
        self.subscription = self.store.add_subscription(
            self.student.name, 1,
            price=self.price.value() if self.price is not None else 0,
            direction=self.direction.currentText(),
            bought=day, started=day, finished=True,
            trial=self.kind == "trial", single=self.kind == "single",
            lesson_format=self.lesson_format.currentText())
        # Разовый урок оплачен: деньги идут в журнал оплат отдельной
        # строкой, как за абонемент. Пробный бесплатный.
        if self.kind == "single" and self.subscription.price > 0:
            self.store.add_payment(
                self.student.name, "Разовый урок",
                self.subscription.price, day)
        self.accept()


class SubscriptionEditDialog(QDialog):
    """Правка уже заведённого абонемента.

    Меняются даты, направление, число уроков, стоимость и пометка
    «закончен». Уроков меньше, чем уже проведено, поставить нельзя;
    ноль означает «без ограничения».
    """

    def __init__(self, store: Store, sub: data.Subscription, parent=None):
        super().__init__(parent)
        self.store = store
        self.sub = sub
        # True, если после правки уроки досчитались до конца и абонемент
        # сам перешёл в законченные.
        self.closed_itself = False
        if sub.trial:
            title = "Изменить пробный урок"
        elif sub.single:
            title = "Изменить разовый урок"
        else:
            title = "Изменить абонемент"
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        if sub.one_off:
            what = "Пробный урок" if sub.trial else "Разовый урок"
            text = (f"{sub.student}. {what} "
                    f"{dates.fmt_date(sub.bought)}, "
                    + ("проведён." if sub.used else "записан."))
        elif sub.total:
            text = (f"{sub.student}. Проведено {sub.used} из {sub.total}, "
                    f"осталось {sub.left}. Уроков меньше {sub.used} "
                    f"поставить нельзя.")
        else:
            text = (f"{sub.student}. Уроков без ограничения, проведено "
                    f"{sub.used}.")
        hint = QLabel(text)
        hint.setWordWrap(True)
        hint.setAccessibleName(text)
        lay.addWidget(hint)

        form = QFormLayout()
        if sub.one_off:
            day_name = ("Дата пробного урока" if sub.trial
                        else "Дата разового урока")
            self.bought = make_date_edit(day_name, sub.bought)
            form.addRow(day_name, self.bought)

            self.lesson_format = QComboBox()
            self.lesson_format.addItems(FORMATS)
            if sub.lesson_format:
                self.lesson_format.setCurrentText(sub.lesson_format)
            self.lesson_format.setAccessibleName("Формат: очно или онлайн")
            form.addRow("Формат", self.lesson_format)

            self.direction = QComboBox()
            self.direction.addItems(DIRECTIONS)
            self.direction.setCurrentText(sub.direction)
            self.direction.setAccessibleName("Направление")
            form.addRow("Направление", self.direction)
            self.start = self.count = None
            self.price = None
            if sub.single:
                self.price = QSpinBox()
                self.price.setRange(0, 500_000)
                self.price.setSingleStep(500)
                self.price.setSuffix(" ₽")
                self.price.setValue(sub.price)
                self.price.setAccessibleName("Стоимость разового урока")
                form.addRow("Стоимость", self.price)
            self.finished_box = None
        else:
            self.bought = make_date_edit("Дата покупки абонемента", sub.bought)
            form.addRow("Дата покупки", self.bought)

            self.start = make_date_edit("С какого числа действует абонемент",
                                        sub.begins)
            form.addRow("Абонемент с числа", self.start)

            self.direction = QComboBox()
            self.direction.addItems(DIRECTIONS)
            self.direction.setCurrentText(sub.direction)
            self.direction.setAccessibleName("Направление")
            form.addRow("Направление", self.direction)

            self.count = QSpinBox()
            self.count.setRange(0, 500)
            self.count.setSpecialValueText("без ограничения")
            self.count.setValue(max(0, sub.total))
            self.count.setAccessibleName(
                "Сколько уроков в абонементе; ноль — без ограничения")
            form.addRow("Уроков", self.count)

            self.price = QSpinBox()
            self.price.setRange(0, 500_000)
            self.price.setSingleStep(500)
            self.price.setSuffix(" ₽")
            self.price.setValue(sub.price)
            self.price.setAccessibleName("Стоимость абонемента")
            form.addRow("Стоимость", self.price)
        lay.addLayout(form)

        if not sub.one_off:
            self.finished_box = QCheckBox("Абонемент закончен")
            self.finished_box.setChecked(sub.finished)
            self.finished_box.setAccessibleName("Абонемент закончен")
            lay.addWidget(self.finished_box)

            # Расписание правится здесь же: дни и часы уже отмечены, их
            # можно поменять, и уроки встанут заново.
            self.schedule_button = QPushButton("Редактировать расписание")
            self.schedule_button.setAccessibleName(
                "Редактировать расписание: дни недели и время уроков")
            self.schedule_button.clicked.connect(
                lambda _checked=False: self.edit_schedule())
            lay.addWidget(self.schedule_button)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        if self.count is not None:
            self.count.setFocus()
            self.count.selectAll()
        else:
            self.bought.setFocus()

    def edit_schedule(self) -> None:
        """Дни и часы уроков: открыть расписание с уже отмеченными днями."""
        student = self.store.find_student(self.sub.student)
        if student is None:
            announce(self, "Ученик не найден в списке.", assertive=True)
            return
        dialog = ScheduleEditDialog(self.store, student, self,
                                    subscription=self.sub.uid)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        announce(
            self,
            "Расписание перестроено: убрано "
            f"{dialog.removed} {plural(dialog.removed, 'урок', 'урока', 'уроков')}, "
            f"записано {dialog.added}.", assertive=True)

    def _save(self) -> None:
        if self.sub.one_off:
            self._save_one_off()
            return
        count = self.count.value()
        if count and count < self.sub.used:
            announce(
                self,
                f"В абонементе уже проведено {self.sub.used} "
                f"{plural(self.sub.used, 'урок', 'урока', 'уроков')}. "
                f"Поставь уроков не меньше, или ноль — без ограничения.",
                assertive=True)
            self.count.setFocus()
            return
        sub = self.sub
        old = (sub.bought, sub.started, sub.direction, sub.total,
               sub.price, sub.finished)
        sub.bought = picked_date(self.bought)
        sub.started = picked_date(self.start)
        sub.direction = self.direction.currentText()
        sub.total = count
        sub.price = self.price.value()
        sub.finished = self.finished_box.isChecked()
        try:
            self.closed_itself = self.store.update_subscription(sub)
        except ValueError as error:
            # Сохранение не прошло — возвращаем прежние значения, чтобы
            # память программы не разошлась с файлом.
            (sub.bought, sub.started, sub.direction, sub.total,
             sub.price, sub.finished) = old
            announce(self, str(error), assertive=True)
            return
        # Журнал оплат повторяет сумму и дату абонемента: иначе заработок
        # считался бы по старой цифре.
        self.store.sync_subscription_payment(sub, old[0], old[3])
        self.accept()

    def _save_one_off(self) -> None:
        sub = self.sub
        old = (sub.bought, sub.started, sub.direction, sub.lesson_format,
               sub.price)
        day = picked_date(self.bought)
        sub.bought = day
        sub.started = day
        sub.direction = self.direction.currentText()
        sub.lesson_format = self.lesson_format.currentText()
        if self.price is not None:
            sub.price = self.price.value()
        try:
            self.store.update_subscription(sub)
        except ValueError as error:
            (sub.bought, sub.started, sub.direction, sub.lesson_format,
             sub.price) = old
            announce(self, str(error), assertive=True)
            return
        # Разовый урок: журнал оплат получает ту же сумму и дату, что
        # записаны в уроке.
        self.store.sync_subscription_payment(sub, old[0])
        self.accept()


class DoneLessonDialog(QDialog):
    """Дата уже проведённого урока и сколько уроков в этот день прошло."""

    def __init__(self, store: Store, sub: data.Subscription, parent=None):
        super().__init__(parent)
        self.store = store
        self.sub = sub
        self.lessons: list[data.Lesson] = []
        self.day_value: date | None = None
        self.count_value = 1
        self.setWindowTitle("Добавить проведённый урок")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        begin = dates.fmt_date(sub.begins)
        if sub.one_off:
            what = "Пробный урок" if sub.trial else "Разовый урок"
            text = (f"{sub.student}. {what} {begin}: "
                    + ("уже проведён." if sub.used else "ещё не проведён."))
        elif sub.total:
            text = (f"{sub.student}. Абонемент с {begin}, уроки "
                    f"{sub.used} из {sub.total}.")
        else:
            text = f"{sub.student}. Абонемент с {begin}, уроков вписано {sub.used}."
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAccessibleName(text)
        lay.addWidget(label)

        form = QFormLayout()
        # Следующий урок по порядку: день после последнего отмеченного,
        # а если в абонементе ещё ничего нет — его первый день.
        last = store.last_done_day(sub)
        default = last + timedelta(days=1) if last else sub.begins
        self.day = make_date_edit("Дата проведённого урока", default)
        form.addRow("Дата урока", self.day)
        # Сколько уроков прошло в этот день: иногда идут два или три подряд.
        # Больше остатка не предложим — программа всё равно откажет.
        top = max(1, sub.left) if sub.total else 99
        self.count = QSpinBox()
        self.count.setRange(1, top)
        self.count.setValue(1)
        self.count.setAccessibleName("Количество уроков за этот день")
        if not sub.one_off:
            form.addRow("Количество уроков", self.count)
        lay.addLayout(form)

        hint = QLabel("Программа сама поставит уроки на нужное место в списке "
                      "и прибавит счётчик.")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Добавить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._add)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _add(self) -> None:
        day = picked_date(self.day)
        count = int(self.count.value())
        try:
            self.lessons = self.store.add_done_lesson(self.sub, day, count)
        except ValueError as error:
            announce(self, str(error))
            return
        self.day_value = day
        self.count_value = count
        self.accept()


class LessonPickDialog(QDialog):
    """Выбрать проведённый урок абонемента — для правки или удаления."""

    def __init__(self, store: Store, sub: data.Subscription, title: str,
                 ok_text: str, parent=None):
        super().__init__(parent)
        self.lessons = store.done_lessons(sub)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        if sub.one_off:
            what = "пробный урок" if sub.trial else "разовый урок"
            text = (f"{sub.student}, {what} "
                    f"{dates.fmt_date(sub.bought)}. Выбери урок в списке.")
        else:
            if sub.total:
                tail = f"уроки {sub.used} из {sub.total}"
            else:
                tail = f"всего уроков {sub.used}"
            text = (f"{sub.student}, абонемент с {dates.fmt_date(sub.begins)}, "
                    f"{tail}. Выбери урок в списке.")
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAccessibleName(text)
        lay.addWidget(label)

        self.pick = QComboBox()
        self.pick.setAccessibleName("Урок")
        for lesson in self.lessons:
            when = dates.fmt_date(lesson.day)
            if lesson.time:
                when += f" в {lesson.time}"
            self.pick.addItem(when, lesson)
        lay.addWidget(self.pick)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def chosen(self) -> data.Lesson | None:
        return self.pick.currentData()


class LessonListDialog(QDialog):
    """Список уроков по датам — для клавиш F7 и F8.

    По списку ходят стрелками вверх и вниз, Enter открывает урок на
    правку. Строки читаются как «24 ноября вторник», время добавляется,
    если оно у урока есть.
    """

    def __init__(self, lessons: list[data.Lesson], title: str, parent=None):
        super().__init__(parent)
        self.lessons = sorted(lessons, key=lambda l: (l.day, l.time))
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)

        hint = QLabel("Выбери урок стрелками вверх и вниз и нажми Enter, "
                      "чтобы открыть его на правку.")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.list = QListWidget()
        self.list.setAccessibleName(title)
        for lesson in self.lessons:
            day = lesson.day
            when = (f"{day.day} {dates.MONTHS[day.month - 1]} "
                    f"{dates.WEEKDAYS[day.weekday()]}")
            if lesson.time:
                when += f", {lesson.time}"
            item = QListWidgetItem(when)
            item.setData(Qt.ItemDataRole.UserRole, lesson)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        lay.addWidget(self.list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Открыть")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.list.itemActivated.connect(lambda _item: self.accept())
        self.list.setFocus()

    def chosen(self) -> data.Lesson | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class LessonEditDialog(QDialog):
    """Правка урока: дата и, если нужно, время.

    В карточке ученика так правят проведённые уроки — дата в будущем там
    ошибка. Из расписания переносят предстоящий урок, поэтому там
    `allow_future=True`.
    """

    def __init__(self, lesson: data.Lesson, parent=None,
                 allow_future: bool = False,
                 title: str = "Редактировать урок"):
        super().__init__(parent)
        self.lesson = lesson
        self.allow_future = allow_future
        self.new_day: date | None = None
        self.new_time = ""
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        text = f"Урок {dates.fmt_date(lesson.day)}"
        if lesson.time:
            text += f" в {lesson.time}"
        text += f". Ученик {lesson.student}."
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAccessibleName(text)
        lay.addWidget(label)

        form = QFormLayout()
        self.day = make_date_edit("Дата урока", lesson.day)
        form.addRow("Дата урока", self.day)

        hour, minute = 18, 0
        if lesson.time:
            try:
                hour, minute = (int(part) for part in lesson.time.split(":", 1))
            except ValueError:
                hour, minute = 18, 0
        self.has_time = QCheckBox("Указать время урока")
        self.has_time.setAccessibleName("Указать время урока")
        self.has_time.setChecked(bool(lesson.time))
        form.addRow(self.has_time)

        row = QHBoxLayout()
        self.hour = QSpinBox()
        self.hour.setRange(0, 23)
        self.hour.setValue(hour)
        self.hour.setAccessibleName("Час урока")
        self.minute = QSpinBox()
        self.minute.setRange(0, 59)
        self.minute.setValue(minute)
        self.minute.setAccessibleName("Минуты урока")
        row.addWidget(self.hour)
        row.addWidget(QLabel("часов"))
        row.addWidget(self.minute)
        row.addWidget(QLabel("минут"))
        row.addStretch(1)
        form.addRow("Время", row)
        lay.addLayout(form)

        self.has_time.toggled.connect(self.hour.setEnabled)
        self.has_time.toggled.connect(self.minute.setEnabled)
        self.hour.setEnabled(self.has_time.isChecked())
        self.minute.setEnabled(self.has_time.isChecked())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _save(self) -> None:
        day = picked_date(self.day)
        if day > date.today() and not self.allow_future:
            announce(self, "Дата урока не может быть в будущем: урок уже прошёл.",
                     assertive=True)
            return
        self.new_day = day
        self.new_time = (dates.fmt_time(self.hour.value(), self.minute.value())
                         if self.has_time.isChecked() else "")
        self.accept()


class BlockDialog(QDialog):
    """Исключение: целый день без уроков или промежуток «с … до …».

    Причина необязательна и остаётся у преподавателя: ученику программа
    скажет только то, что это время занято.
    """

    def __init__(self, whole_day: bool, parent=None,
                 flexible: bool = False):
        super().__init__(parent)
        self.whole_day = whole_day
        self.flexible = flexible
        self.day: date = date.today()
        self.start = ""
        self.end = ""
        self.note = ""
        if flexible:
            self.setWindowTitle("Добавить исключение уроков")
        else:
            self.setWindowTitle("Добавить день" if whole_day
                                else "Добавить время")
        self.setMinimumWidth(460)

        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.day_edit = make_date_edit("Дата исключения")
        form.addRow("Дата", self.day_edit)
        # В общем окне исключения можно закрыть и весь день, и часы:
        # переключатель решает, что именно.
        self.whole_box = None
        if flexible:
            self.whole_box = QCheckBox("Весь день — уроков не будет")
            self.whole_box.setChecked(whole_day)
            self.whole_box.setAccessibleName(
                "Исключение на весь день, а не на часы")
            self.whole_box.toggled.connect(self.set_whole_day)
            lay.addWidget(self.whole_box)
        self.day_hint = QLabel("Весь этот день уроков не будет.")
        self.day_hint.setWordWrap(True)
        lay.addWidget(self.day_hint)
        self.time_box = QWidget()
        time_lay = QVBoxLayout(self.time_box)
        time_lay.setContentsMargins(0, 0, 0, 0)
        time_form = QFormLayout()
        self.from_hour, self.from_minute = self._time_row(
            time_form, "С какого времени", "Час начала", "Минуты начала",
            12, 0)
        self.to_hour, self.to_minute = self._time_row(
            time_form, "До какого времени", "Час окончания",
            "Минуты окончания", 16, 0)
        time_lay.addLayout(time_form)
        lay.addWidget(self.time_box)
        self.set_whole_day(whole_day)
        self.note_edit = QLineEdit()
        self.note_edit.setAccessibleName(
            "Причина для себя. Ученик её не увидит")
        form.addRow("Причина (необязательно)", self.note_edit)
        lay.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Добавить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _time_row(self, form: QFormLayout, title: str, hour_name: str,
                  minute_name: str, hour: int, minute: int
                  ) -> tuple[QSpinBox, QSpinBox]:
        """Часы и минуты одной строкой — как в форме урока."""
        row = QHBoxLayout()
        hour_edit = QSpinBox()
        hour_edit.setRange(0, 23)
        hour_edit.setValue(hour)
        hour_edit.setAccessibleName(hour_name)
        row.addWidget(hour_edit)
        row.addWidget(QLabel("часов"))
        minute_edit = QSpinBox()
        minute_edit.setRange(0, 59)
        minute_edit.setSingleStep(5)
        minute_edit.setValue(minute)
        minute_edit.setAccessibleName(minute_name)
        row.addWidget(minute_edit)
        row.addWidget(QLabel("минут"))
        form.addRow(title, row)
        return hour_edit, minute_edit

    def set_whole_day(self, whole: bool) -> None:
        """Весь день или часы: показываем только то, что нужно."""
        self.whole_day = bool(whole)
        self.day_hint.setVisible(self.whole_day)
        self.time_box.setVisible(not self.whole_day)

    def _save(self) -> None:
        self.day = picked_date(self.day_edit)
        self.note = self.note_edit.text().strip()
        if not self.whole_day:
            self.start = dates.fmt_time(self.from_hour.value(),
                                        self.from_minute.value())
            self.end = dates.fmt_time(self.to_hour.value(),
                                      self.to_minute.value())
            if data.minutes(self.end) <= data.minutes(self.start):
                announce(self, "Конец должен быть позже начала.",
                         assertive=True)
                return
        self.accept()


class StudentCard(QDialog):
    """Карточка ученика: действующий абонемент и законченные в двух вкладках."""

    def __init__(self, store: Store, student: data.Student, parent=None):
        super().__init__(parent)
        self.store = store
        self.student = student
        self.changed = False
        self.setWindowTitle(student.name)
        self.setMinimumWidth(540)

        lay = QVBoxLayout(self)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.info)

        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("Абонементы ученика")

        active = QWidget()
        active_lay = QVBoxLayout(active)
        self.active_info = QLabel()
        self.active_info.setWordWrap(True)
        self.active_info.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        active_lay.addWidget(self.active_info)
        self.active_done_button = QPushButton("Добавить проведённый урок")
        self.active_done_button.clicked.connect(self.add_done_lesson)
        active_lay.addWidget(self.active_done_button)
        self.active_edit_button = QPushButton("Редактировать урок")
        self.active_edit_button.setAccessibleName("Редактировать урок")
        self.active_edit_button.clicked.connect(self.edit_active_lesson)
        active_lay.addWidget(self.active_edit_button)
        self.active_delete_button = QPushButton("Удалить урок")
        self.active_delete_button.setAccessibleName("Удалить урок")
        self.active_delete_button.clicked.connect(self.delete_active_lesson)
        active_lay.addWidget(self.active_delete_button)
        self.new_sub_button = QPushButton("Добавить новый абонемент")
        self.new_sub_button.clicked.connect(self.add_new_subscription)
        active_lay.addWidget(self.new_sub_button)
        active_lay.addStretch(1)
        self.tabs.addTab(active, "Действующий абонемент")

        history = QWidget()
        history_lay = QVBoxLayout(history)
        self.history_info = QLabel()
        self.history_info.setWordWrap(True)
        self.history_info.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        history_lay.addWidget(self.history_info)
        self.history_pick = QComboBox()
        self.history_pick.setAccessibleName("Законченный абонемент")
        self.history_pick.currentIndexChanged.connect(self._sync_history)
        history_lay.addWidget(self.history_pick)
        self.old_done_button = QPushButton("Добавить проведённый урок")
        self.old_done_button.clicked.connect(self.add_old_done_lesson)
        history_lay.addWidget(self.old_done_button)
        self.old_edit_button = QPushButton("Редактировать урок")
        self.old_edit_button.setAccessibleName("Редактировать урок")
        self.old_edit_button.clicked.connect(self.edit_old_lesson)
        history_lay.addWidget(self.old_edit_button)
        self.old_delete_button = QPushButton("Удалить урок")
        self.old_delete_button.setAccessibleName("Удалить урок")
        self.old_delete_button.clicked.connect(self.delete_old_lesson)
        history_lay.addWidget(self.old_delete_button)
        self.old_sub_button = QPushButton("Добавить законченный абонемент")
        self.old_sub_button.clicked.connect(self.add_old_subscription)
        history_lay.addWidget(self.old_sub_button)
        history_lay.addStretch(1)
        self.tabs.addTab(history, "Законченные абонементы")

        lay.addWidget(self.tabs)
        self.refresh()

        for text, slot in (
                ("Подтвердить урок", self.confirm_lesson),
                ("Добавить дату", self.add_date),
                ("Добавить уроки по расписанию", self.add_schedule),
                ("Добавить пробный урок", self.add_trial_lesson),
                ("Добавить разовый урок", self.add_single_lesson),
                ("Добавить услугу", self.add_service)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            lay.addWidget(button)

        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        lay.addWidget(close)

    def card_text(self) -> str:
        student = self.student
        sub = self.store.current_subscription(student.name)
        parts = [student.name]
        if student.phone:
            parts.append(f"телефон {student.phone}")
        if student.email:
            parts.append(f"почта {student.email}")
        parts.append(f"{student.direction}, {student.lesson_format.casefold()}")
        if sub is None:
            parts.append("действующего абонемента нет")
        elif sub.total:
            parts.append(f"действующий абонемент с {dates.fmt_date(sub.begins)}, "
                         f"уроки {sub.used} из {sub.total}")
        else:
            parts.append(f"действующий абонемент с {dates.fmt_date(sub.begins)}, "
                         f"уроков вписано {sub.used}")
        history = self.store.finished_subscriptions(student.name)
        if history:
            parts.append(f"законченных абонементов в истории {len(history)}")
        near = self.store.nearest_lesson(student.name)
        if near is not None:
            parts.append(f"ближайший урок {dates.human_date(near.day)} в {near.time}")
        else:
            parts.append("уроков не записано")
        return ". ".join(parts) + "."

    def refresh(self) -> None:
        text = self.card_text()
        self.info.setText(text)
        self.info.setAccessibleName(text)

        sub = self.store.current_subscription(self.student.name)
        if sub is None:
            active = ("Действующего абонемента нет. Его можно завести кнопкой "
                      "«Добавить новый абонемент» или добавить одно занятие "
                      "кнопкой «Добавить разовый урок».")
        elif sub.total:
            active = (f"Абонемент с {dates.fmt_date(sub.begins)}, куплен "
                      f"{dates.fmt_date(sub.bought)}. Уроки {sub.used} из "
                      f"{sub.total}, осталось {sub.left}.")
        else:
            active = (f"Абонемент с {dates.fmt_date(sub.begins)}, куплен "
                      f"{dates.fmt_date(sub.bought)}. Уроки без счёта, "
                      f"проведено {sub.used}.")
        self.active_info.setText(active)
        self.active_info.setAccessibleName(active)
        self.active_done_button.setEnabled(
            sub is not None and (not sub.total or sub.left > 0))
        has_done = sub is not None and bool(self.store.done_lessons(sub))
        self.active_edit_button.setEnabled(has_done)
        self.active_delete_button.setEnabled(has_done)

        history = self.store.finished_subscriptions(self.student.name)
        if not history:
            text = ("Законченных абонементов нет. Старые абонементы вписываются "
                    "по одному кнопкой «Добавить законченный абонемент». "
                    "Пробный и разовый уроки добавляются кнопками "
                    "«Добавить пробный урок» и «Добавить разовый урок».")
        else:
            word = plural(len(history), "абонемент", "абонемента", "абонементов")
            text = (f"Законченных {word}: {len(history)}. Пробный и разовый "
                    "уроки добавляются кнопками «Добавить пробный урок» и "
                    "«Добавить разовый урок». Выбери запись в списке.")
        self.history_info.setText(text)
        self.history_info.setAccessibleName(text)

        chosen = self.history_pick.currentData()
        self.history_pick.blockSignals(True)
        self.history_pick.clear()
        for item in history:
            if item.one_off:
                title = "Пробный урок" if item.trial else "Разовый урок"
                label = (f"{title} {dates.fmt_date(item.bought)}: "
                         + ("проведён." if item.used else "записан."))
            elif item.total:
                label = (f"Абонемент с {dates.fmt_date(item.begins)}: отмечено "
                         f"{item.used} из {item.total}, осталось {item.left}.")
            else:
                label = (f"Абонемент с {dates.fmt_date(item.begins)}: "
                         f"уроков {item.used}.")
            self.history_pick.addItem(label, item)
        if chosen is not None:
            for index, item in enumerate(history):
                if item.uid == chosen.uid:
                    self.history_pick.setCurrentIndex(index)
                    break
        self.history_pick.blockSignals(False)
        self._sync_history()

    def _sync_history(self) -> None:
        sub = self.history_pick.currentData()
        self.old_done_button.setEnabled(
            sub is not None and (not sub.total or sub.left > 0))
        has_done = sub is not None and bool(self.store.done_lessons(sub))
        self.old_edit_button.setEnabled(has_done)
        self.old_delete_button.setEnabled(has_done)

    def _done(self, message: str) -> None:
        self.changed = True
        self.refresh()
        announce(self, message)

    def add_new_subscription(self) -> None:
        dialog = SubscriptionDialog(self.store, self.student, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Абонемент не добавлен.")
            return
        sub = dialog.subscription
        self.tabs.setCurrentIndex(0)
        text = (f"{self.student.name}, абонемент с "
                f"{dates.fmt_date(sub.begins)} добавлен.")
        if sub.total:
            text += f" Уроки 0 из {sub.total}."
        if dialog.schedule_added:
            word = plural(dialog.schedule_added, "урок", "урока", "уроков")
            text += f" Расписание записано, {dialog.schedule_added} {word}."
        elif dialog.schedule_empty:
            text += (" Расписание не записано: ни один день не отмечен.")
        self._done(text)

    def add_old_subscription(self) -> None:
        dialog = SubscriptionDialog(self.store, self.student,
                                    historical=True, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Абонемент не добавлен.")
            return
        sub = dialog.subscription
        self.tabs.setCurrentIndex(1)
        self._done(f"Законченный абонемент с {dates.fmt_date(sub.begins)} "
                   f"добавлен. Отметь в нём проведённые уроки кнопкой "
                   f"«Добавить проведённый урок».")

    def add_one_off_lesson(self, kind: str) -> None:
        title = "Пробный" if kind == "trial" else "Разовый"
        dialog = OneOffLessonDialog(self.store, self.student, kind=kind,
                                    parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, f"{title} урок не добавлен.")
            return
        sub = dialog.subscription
        self.tabs.setCurrentIndex(1)
        text = f"{title} урок на {dates.fmt_date(sub.bought)} добавлен."
        if sub.price:
            text += f" Стоимость: {money(sub.price)} рублей."
        text += (" Отметь его проведённым кнопкой «Добавить проведённый "
                 "урок».")
        self._done(text)

    def add_trial_lesson(self) -> None:
        self.add_one_off_lesson("trial")

    def add_single_lesson(self) -> None:
        self.add_one_off_lesson("single")

    def add_done_lesson(self) -> None:
        sub = self.store.current_subscription(self.student.name)
        if sub is None:
            announce(self, "Действующего абонемента нет.")
            return
        self._add_done_lesson(sub)

    def add_old_done_lesson(self) -> None:
        sub = self.history_pick.currentData()
        if sub is None:
            announce(self, "Сначала выбери законченный абонемент в списке.")
            return
        self._add_done_lesson(sub)

    def _add_done_lesson(self, sub: data.Subscription) -> None:
        dialog = DoneLessonDialog(self.store, sub, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Урок не добавлен.")
            return
        day = dates.fmt_date(dialog.day_value)
        if dialog.count_value > 1:
            word = plural(dialog.count_value, "урок", "урока", "уроков")
            head = f"Записано {word}: {dialog.count_value}, {day}."
        else:
            head = f"Урок {day} записан."
        if sub.total:
            text = f"{head} В абонементе уроки {sub.used} из {sub.total}."
        else:
            text = f"{head} Всего уроков {sub.used}."
        self._done(text)

    @staticmethod
    def _sub_tail(sub: data.Subscription) -> str:
        if sub.total:
            return f" В абонементе уроки {sub.used} из {sub.total}."
        return f" Всего уроков {sub.used}."

    def edit_active_lesson(self) -> None:
        sub = self.store.current_subscription(self.student.name)
        if sub is None:
            announce(self, "Действующего абонемента нет.")
            return
        self._edit_lesson(sub)

    def edit_old_lesson(self) -> None:
        sub = self.history_pick.currentData()
        if sub is None:
            announce(self, "Сначала выбери законченный абонемент в списке.")
            return
        self._edit_lesson(sub)

    def delete_active_lesson(self) -> None:
        sub = self.store.current_subscription(self.student.name)
        if sub is None:
            announce(self, "Действующего абонемента нет.")
            return
        self._delete_lesson(sub)

    def delete_old_lesson(self) -> None:
        sub = self.history_pick.currentData()
        if sub is None:
            announce(self, "Сначала выбери законченный абонемент в списке.")
            return
        self._delete_lesson(sub)

    def _pick_done_lesson(self, sub: data.Subscription, title: str,
                          ok_text: str) -> data.Lesson | None:
        if not self.store.done_lessons(sub):
            announce(self, "В этом абонементе нет проведённых уроков.")
            return None
        dialog = LessonPickDialog(self.store, sub, title, ok_text, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.chosen()

    def _edit_lesson(self, sub: data.Subscription) -> None:
        lesson = self._pick_done_lesson(sub, "Редактировать урок",
                                        "Редактировать")
        if lesson is None:
            announce(self, "Урок не изменён.")
            return
        dialog = LessonEditDialog(lesson, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Урок не изменён.")
            return
        new_day = dialog.new_day or lesson.day
        new_time = dialog.new_time
        if new_day == lesson.day and new_time == lesson.time:
            announce(self, "Урок не изменён.")
            return
        for other in self.store.done_lessons(sub):
            if other is not lesson and other.day == new_day:
                announce(self, f"На {dates.fmt_date(new_day)} в этом абонементе "
                               f"уже есть урок. Выбери другую дату.",
                         assertive=True)
                return
        old_text = dates.fmt_date(lesson.day)
        lesson.day = new_day
        lesson.time = new_time
        self.store.update_lesson(lesson)
        when = dates.fmt_date(new_day)
        if new_time:
            when += f" в {new_time}"
        self._done(f"Урок {old_text} изменён: теперь {when}."
                   f"{self._sub_tail(sub)}")

    def _delete_lesson(self, sub: data.Subscription) -> None:
        lesson = self._pick_done_lesson(sub, "Удалить урок", "Удалить")
        if lesson is None:
            announce(self, "Урок не удалён.")
            return
        when = dates.fmt_date(lesson.day)
        if lesson.time:
            when += f" в {lesson.time}"
        self.store.delete_lesson(lesson)
        self._done(f"Урок {when} удалён.{self._sub_tail(sub)}")

    def confirm_lesson(self) -> None:
        name = self.student.name
        sub = self.store.current_subscription(name)
        dialog = LessonPayDialog(name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Урок не отмечен.")
            return
        amount = dialog.amount
        now = datetime.now()
        lesson = None
        for item in self.store.lessons:
            if (item.student == name and item.day == now.date()
                    and item.status == LESSON_WAIT):
                lesson = item
                break
        if lesson is None:
            lesson = self.store.add_lesson(
                name, now.date(), now.strftime("%H:%M"),
                lesson_format=self.student.lesson_format,
                direction=self.student.direction, note="разовый урок")
        lesson.status = LESSON_DONE
        lesson.price = amount
        if sub is not None:
            # Урок приписывается к абонементу: в файле «Проведённые уроки»
            # он считается внутри своего абонемента, а не сквозным номером.
            lesson.subscription = sub.uid
        self.store.update_lesson(lesson)

        if sub is not None:
            self.store.bump_used(sub)
        used, total = self.store.balance(name)

        if amount > 0:
            self.store.add_payment(name, "Разовый урок", amount, now.date())

        text = f"{name}, урок проведён."
        if total:
            text += f" Уроки {used} из {total}."
        if amount > 0:
            text += f" Оплата {rubles(amount)}."
        self._done(text)

    def add_date(self) -> None:
        sub = self.store.target_subscription(self.student.name)
        dialog = AddDateDialog(self.store, self.student, self,
                               subscription=sub.uid if sub else 0)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Дата не добавлена.")
            return
        if len(dialog.times) > 1:
            when = dates.human_date(dialog.day_value)
            self._done(f"Двойной урок добавлен. {when}, "
                       f"{' и '.join(dialog.times)}.")
            return
        near = self.store.nearest_lesson(self.student.name)
        when = (f"{dates.human_date(near.day)} в {near.time}"
                if near else "")
        self._done(f"Дата добавлена. Ближайший урок {when}."
                   if when else "Дата добавлена.")

    def add_schedule(self) -> None:
        sub = self.store.target_subscription(self.student.name)
        dialog = ScheduleDialog(self.store, self.student, self,
                                subscription=sub.uid if sub else 0)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Расписание не добавлено.")
            return
        count = dialog.added
        word = plural(count, "урок", "урока", "уроков")
        self._done(f"{self.student.name}, на месяц вперёд записано "
                   f"{count} {word}.")

    def add_service(self) -> None:
        dialog = ServiceDialog(self.store, self.student, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Услуга не добавлена.")
            return
        service = dialog.service.currentText()
        amount = dialog.amount.value()
        self._done(f"Услуга {service.casefold()} записана, {rubles(amount)}.")


# --- настройки -----------------------------------------------------------


class UpdateWorker(QThread):
    """Проверка и установка обновления в фоне, чтобы окно не замирало."""

    status = Signal(str)
    percent = Signal(int)
    failed = Signal(str)
    installed = Signal(str)

    def run(self) -> None:  # noqa: D401 (поток)
        try:
            update = updater.find_update(__version__)
            if update is None:
                self.status.emit("У программы последняя версия.")
                return
            self.status.emit(
                f"Найдено обновление {update['version']}. Скачиваю.")
            last = 0

            def on_progress(done: int, total: int) -> None:
                nonlocal last
                if not total:
                    return
                percent = int(done * 100 / total)
                # Диктору довольно каждого десятого процента: чаще — очередь
                # announcements растягивается и отстаёт от скачивания.
                if percent - last >= 10 or percent == 100:
                    last = percent
                    self.percent.emit(percent)

            path = updater.download(
                update["url"], on_progress, update["size"],
                update.get("digest", ""))
            self.status.emit("Скачано. Устанавливаю новую версию.")
            ok, message = updater.install_and_restart(path)
            if ok:
                self.installed.emit(message)
            else:
                self.failed.emit(message)
        except updater.UpdateError as error:
            self.failed.emit(str(error))
        except Exception as error:  # noqa: BLE001 — поток не должен падать молча
            # Непонятную ошибку пишем в журнал: по одной фразе в окне
            # её потом не разобрать.
            updater.log(f"Обновление не удалось: {error!r}")
            self.failed.emit(f"Обновление не удалось: {error}")


# Цифры цифрового блока при выключенном Num Lock: Qt присылает вместо
# цифры клавишу перемещения, но с пометкой цифрового блока.
NUMPAD_DIGITS = {
    int(Qt.Key.Key_0): 0,
    int(Qt.Key.Key_End): 1,
    int(Qt.Key.Key_Down): 2,
    int(Qt.Key.Key_PageDown): 3,
    int(Qt.Key.Key_Left): 4,
    int(Qt.Key.Key_Clear): 5,
    int(Qt.Key.Key_Right): 6,
    int(Qt.Key.Key_Home): 7,
    int(Qt.Key.Key_Up): 8,
    int(Qt.Key.Key_PageUp): 9,
}

# «Ctrl+Num1» и «Ctrl+Num+1» — одна и та же запись: Qt пишет с плюсом.
NUMPAD_TEXT = re.compile(r"(?i)(?<![A-Za-z])num(?=\s*[0-9])")
NUMPAD_KEYS = re.compile(r"(?i)^(?:[A-Za-z]+\+)*Num\+[0-9]$")


# Скан-коды цифрового блока в Windows: 0x52 — 0, 0x4F–0x51 — 1–3,
# 0x4B–0x4D — 4–6, 0x47–0x49 — 7–9. Они одинаковы при любом положении
# Num Lock, поэтому это самая надёжная примета. Стрелки и End отдельной
# части клавиатуры приходят со старшим битом (0x100) и сюда не попадают.
NUMPAD_SCAN = {
    0x52: 0, 0x4F: 1, 0x50: 2, 0x51: 3, 0x4B: 4,
    0x4C: 5, 0x4D: 6, 0x47: 7, 0x48: 8, 0x49: 9,
}

def mods_value(flags) -> int:
    """Модификаторы числом.

    В новых сборках PySide6 перечисление KeyboardModifier к int не
    приводится: int() на нём падает ещё при запуске программы. Число
    берём из .value — оно есть и у одной клавиши, и у сочетания.
    """
    try:
        return int(flags)
    except (TypeError, ValueError):
        pass
    value = getattr(flags, "value", None)
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


CTRL_MOD = mods_value(Qt.KeyboardModifier.ControlModifier)
SHIFT_MOD = mods_value(Qt.KeyboardModifier.ShiftModifier)
ALT_MOD = mods_value(Qt.KeyboardModifier.AltModifier)
META_MOD = mods_value(Qt.KeyboardModifier.MetaModifier)
KEYPAD_MOD = mods_value(Qt.KeyboardModifier.KeypadModifier)

# Действие без Ctrl, Alt или Meta не срабатывает: голая цифра блока —
# это набор числа в поле, куда человек печатает.
COMMAND_MODS = CTRL_MOD | ALT_MOD | META_MOD
ALL_MODS = CTRL_MOD | SHIFT_MOD | ALT_MOD | META_MOD


def numpad_digit(key: int) -> int | None:
    """Какая цифра блока нажата — при любом положении Num Lock."""
    return NUMPAD_DIGITS.get(key)


def numpad_index(event) -> tuple[int | None, bool]:
    """Цифра цифрового блока и уверенность, что нажата именно она.

    Приметы по надёжности: скан-код Windows (Num Lock не важен), затем
    пометка цифрового блока Qt, затем обычная цифра верхнего ряда. Раньше
    всё держалось на одной пометке Qt — и на Windows она для цифр блока
    с включённым Num Lock приходит не всегда, из-за чего сочетания с
    Ctrl молчали. Обычная цифра считается догадкой: её может нажать и
    верхний ряд, поэтому совпадение с назначением цифрового блока для
    неё — запасной путь, а не первый.
    """
    if sys.platform == "win32":
        try:
            scan = int(event.nativeScanCode())
        except (AttributeError, TypeError, ValueError):
            scan = -1
        if 0 <= scan < 0x100:
            digit = NUMPAD_SCAN.get(scan)
            if digit is not None:
                return digit, True
    if mods_value(event.modifiers()) & KEYPAD_MOD:
        digit = numpad_digit(int(event.key()))
        if digit is not None:
            return digit, True
    key = int(event.key())
    if int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
        return key - int(Qt.Key.Key_0), False
    return None, False


def digit_binding(text: str) -> tuple[int, int, bool] | None:
    """Назначение на цифру: (модификаторы, цифра, цифровой ли это блок).

    «Ctrl+Num1» и «Ctrl+Num+1» — цифровой блок, «Ctrl+1» — обычная
    цифра. Строку разбираем сами, без Qt: на разборе сочетаний с цифровым
    блоком Qt ненадёжен, а сравнение выходит точным.
    """
    mods = 0
    digit = None
    keypad = False
    for token in (text or "").replace(" ", "").split("+"):
        low = token.casefold()
        if not low:
            continue
        if low == "ctrl":
            mods |= CTRL_MOD
        elif low == "alt":
            mods |= ALT_MOD
        elif low == "shift":
            mods |= SHIFT_MOD
        elif low in ("meta", "win"):
            mods |= META_MOD
        elif low == "num":
            keypad = True
        elif low.startswith("num") and low[3:].isdigit():
            keypad = True
            digit = int(low[3:])
        elif len(low) == 1 and low.isdigit() and digit is None:
            digit = int(low)
        else:
            return None
    if digit is None:
        return None
    return mods, digit, keypad


def numpad_text(text: str) -> str:
    """Отметить, что цифра нажата на цифровом блоке: «Ctrl» + «1» → «Ctrl+Num+1»."""
    if not text or text[-1] not in "0123456789":
        return text
    return text[:-1] + "Num+" + text[-1]


def norm_keys(text: str) -> str:
    """Клавиши единой записью: «ctrl+p», «Ctrl+P» и «CTRL+P» — одно и то же."""
    text = (text or "").strip()
    if not text:
        return ""
    # Человек пишет «Ctrl+Num1», Qt понимает «Ctrl+Num+1».
    text = NUMPAD_TEXT.sub("Num+", text)
    text = re.sub(r"\s*\+\s*", "+", text).strip()
    sequence = QKeySequence(text)
    if not sequence.isEmpty():
        return key_text(sequence)
    # Сочетание с цифровым блоком Qt может и не разобрать: его ловит
    # фильтр событий главного окна, поэтому запись храним как есть.
    return text if NUMPAD_KEYS.match(text) else ""


def combo_text(mods, key: int) -> str:
    """Сочетание из нажатых клавиш, записанное как «Ctrl+P».

    Собираем строку сами, без QKeyCombination: в новых сборках PySide6
    (6.12) этого класса в QtGui больше нет, и программа падала при
    запуске — ещё до первого окна. Имя самой клавиши знает QKeySequence,
    модификаторы дописываем в том же порядке, в каком их печатает Qt:
    Ctrl, Alt, Shift, Meta.
    """
    name = QKeySequence(key).toString(QKeySequence.SequenceFormat.PortableText)
    if not name:
        return ""
    value = mods_value(mods)
    parts = []
    if value & CTRL_MOD:
        parts.append("Ctrl")
    if value & ALT_MOD:
        parts.append("Alt")
    if value & SHIFT_MOD:
        parts.append("Shift")
    if value & META_MOD:
        parts.append("Meta")
    parts.append(name)
    return "+".join(parts)


def section_label(text: str) -> QLabel:
    """Заголовок раздела настроек — жирный и крупнее обычного текста."""
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    font.setPointSize(font.pointSize() + 2)
    label.setFont(font)
    label.setAccessibleName(f"Раздел {text}")
    return label


class KeyCaptureDialog(QDialog):
    """Ждёт одно сочетание клавиш: нажал — назначено.

    Окно открывается уже в режиме записи, поэтому не нужно попадать
    курсором в поле: диктор говорит, что делать, а первое нажатие с
    модификаторами становится новыми клавишами действия.
    """

    def __init__(self, title: str, current: str = "", parent=None):
        super().__init__(parent)
        self.title = title
        self.keys = current
        self.setWindowTitle("Назначить клавиши")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        text = (f"Нажми сочетание клавиш для действия «{title}». "
                "Escape — отмена, Backspace — оставить без клавиши.")
        hint = QLabel(text)
        hint.setWordWrap(True)
        hint.setAccessibleName(text)
        lay.addWidget(hint)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt)
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.reject()
            return
        if key in (0, Qt.Key.Key_Backspace):
            self.keys = ""
            self.accept()
            return
        if key in (Qt.Key.Key_Unknown, Qt.Key.Key_Shift, Qt.Key.Key_Control,
                   Qt.Key.Key_Alt, Qt.Key.Key_Meta, Qt.Key.Key_AltGr):
            return
        mods = mods_value(event.modifiers()) & ALL_MODS
        # Цифровой блок узнаём так же, как главное окно (скан-код,
        # пометка Qt или цифра), и записываем цифрой: при выключенном
        # Num Lock Qt присылает вместо неё стрелку или End, а человеку
        # нужна цифра.
        digit, keypad = numpad_index(event)
        if keypad and digit is not None:
            if not mods & COMMAND_MODS:
                announce(self, "Цифровое поле назначается вместе с "
                               "Ctrl: голая цифра блока — это набор "
                               "числа.", assertive=True)
                return
            self.keys = numpad_text(
                combo_text(mods, int(Qt.Key.Key_0) + digit))
            self.accept()
            return
        self.keys = combo_text(mods, key)
        self.accept()


class SettingsDialog(QDialog):
    """Настройки: «Общее», «Звуки» и «Сочетания клавиш» одним столбцом."""

    bindings_changed = Signal(dict)
    volume_changed = Signal(int)
    cue_volume_changed = Signal(int)
    restored = Signal()
    cloud_changed = Signal()

    def __init__(self, store: Store, bindings: dict[str, str],
                 volume: int = settings.DEFAULT_VOLUME,
                 cue_volume: int = settings.DEFAULT_CUE_VOLUME, parent=None):
        super().__init__(parent)
        self.store = store
        self.bindings = dict(bindings)
        self.volume = settings.volume_value(volume)
        self.cue_volume = settings.cue_volume_value(cue_volume)
        self.cloud = settings.load_cloud(store.folder)
        self._worker: UpdateWorker | None = None
        self._update_ready = False
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(560)
        self.resize(600, 700)

        outer = QVBoxLayout(self)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setAccessibleName("Настройки программы")
        page = QWidget()
        column = QVBoxLayout(page)
        area.setWidget(page)
        outer.addWidget(area, 1)

        # --- общее
        column.addWidget(section_label("Общее"))
        version = QLabel(f"Версия программы {__version__}.")
        version.setAccessibleName(f"Версия программы {__version__}")
        column.addWidget(version)

        self.check_button = QPushButton("Проверить обновления")
        self.check_button.setAccessibleName("Проверить обновления программы")
        self.check_button.clicked.connect(self.check_updates)
        column.addWidget(self.check_button)

        self.status = QLabel("Обновление идёт в фоне: программой можно "
                             "пользоваться дальше.")
        self.status.setWordWrap(True)
        column.addWidget(self.status)

        export = QPushButton("Выгрузить данные в текст")
        export.clicked.connect(self.export_text)
        column.addWidget(export)

        folder = QPushButton("Открыть папку Данные")
        folder.clicked.connect(self.open_data_folder)
        column.addWidget(folder)

        # --- копия в облако
        column.addSpacing(12)
        column.addWidget(section_label("Копия в облако"))
        cloud_hint = QLabel(
            "Программа кладёт копию данных и настроек в облачную папку — "
            "например, на Диск O. На другом устройстве с тем же облаком "
            "открой настройки и нажми «Взять данные из копии».")
        cloud_hint.setWordWrap(True)
        column.addWidget(cloud_hint)

        self.cloud_label = QLabel()
        self.cloud_label.setWordWrap(True)
        column.addWidget(self.cloud_label)

        self.cloud_choose = QPushButton("Выбрать папку для копий")
        self.cloud_choose.setAccessibleName("Выбрать папку для копий")
        self.cloud_choose.clicked.connect(self.choose_cloud_folder)
        column.addWidget(self.cloud_choose)

        self.cloud_save = QPushButton("Сохранить копию сейчас")
        self.cloud_save.setAccessibleName("Сохранить копию в облако сейчас")
        self.cloud_save.clicked.connect(self.save_cloud_now)
        column.addWidget(self.cloud_save)

        self.cloud_check = QPushButton("Проверить копию")
        self.cloud_check.setAccessibleName("Проверить облачную копию")
        self.cloud_check.clicked.connect(self.check_cloud)
        column.addWidget(self.cloud_check)

        self.cloud_take = QPushButton("Взять данные из копии")
        self.cloud_take.setAccessibleName("Взять данные из облачной копии")
        self.cloud_take.clicked.connect(self.take_cloud)
        column.addWidget(self.cloud_take)
        self.show_cloud()

        # --- автозапуск вместе с Windows (только на Windows)
        self.autostart_box: QCheckBox | None = None
        if autostart.supported():
            column.addSpacing(12)
            column.addWidget(section_label("Запуск с Windows"))
            self.autostart_box = QCheckBox(
                "Запускать при входе в Windows, сразу в трее")
            self.autostart_box.setChecked(autostart.is_enabled())
            self.autostart_box.setAccessibleName(
                "Запускать программу при входе в Windows, сразу в трее")
            column.addWidget(self.autostart_box)
            auto_hint = QLabel(
                "Программа запустится в фоне, в трее: окно не откроется, "
                "а расписание и напоминания начнут работать сами. Чтобы "
                "открыть окно, щёлкни по значку в трее.")
            auto_hint.setWordWrap(True)
            column.addWidget(auto_hint)

        # --- звуки
        column.addSpacing(12)
        column.addWidget(section_label("Звуки"))
        volume_hint = QLabel("Громкость сигналов напоминаний. "
                             "Ноль — совсем без звука.")
        volume_hint.setWordWrap(True)
        column.addWidget(volume_hint)

        self.volume_box = QSpinBox()
        self.volume_box.setRange(0, 100)
        self.volume_box.setSingleStep(5)
        self.volume_box.setValue(self.volume)
        self.volume_box.setAccessibleName("Громкость сигналов в процентах")
        column.addWidget(self.volume_box)

        test = QPushButton("Проверить звук")
        test.setAccessibleName("Проверить звук напоминания")
        test.clicked.connect(self.test_sound)
        column.addWidget(test)

        column.addSpacing(8)
        cue_hint = QLabel("Громкость подсказок времени урока: короткие "
                          "сигналы через десять, двадцать, тридцать и "
                          "сорок минут от начала занятия. Ноль — подсказки "
                          "без звука.")
        cue_hint.setWordWrap(True)
        column.addWidget(cue_hint)

        self.cue_volume_box = QSpinBox()
        self.cue_volume_box.setRange(0, 100)
        self.cue_volume_box.setSingleStep(5)
        self.cue_volume_box.setValue(self.cue_volume)
        self.cue_volume_box.setAccessibleName(
            "Громкость подсказок времени в процентах")
        column.addWidget(self.cue_volume_box)

        cue_test = QPushButton("Проверить подсказку времени")
        cue_test.setAccessibleName("Проверить звук подсказки времени")
        cue_test.clicked.connect(self.test_cue_sound)
        column.addWidget(cue_test)

        # --- сочетания клавиш
        column.addSpacing(12)
        column.addWidget(section_label("Сочетания клавиш"))
        hint = QLabel("Нажми на действие, затем нажми нужное сочетание "
                      "клавиш. Backspace оставляет действие без клавиши.")
        hint.setWordWrap(True)
        column.addWidget(hint)

        self.key_buttons: dict[str, QPushButton] = {}

        def add_key_buttons(actions) -> None:
            for code, title, _keys in actions:
                button = QPushButton()
                button.clicked.connect(
                    lambda _checked=False, c=code: self.assign_key(c))
                self.key_buttons[code] = button
                column.addWidget(button)

        add_key_buttons(settings.ACTIONS)
        column.addSpacing(8)
        column.addWidget(section_label("Кнопки на вкладках"))
        note = QLabel("Любую кнопку вкладки можно назначить на клавиши. "
                      "Они работают на той вкладке, которая открыта.")
        note.setWordWrap(True)
        column.addWidget(note)
        add_key_buttons(settings.BUTTON_ACTIONS)
        column.addSpacing(8)
        column.addWidget(section_label("Вкладки"))
        tabs_note = QLabel("Переключение на вкладки главного окна. "
                           "Ctrl вместе с цифрой на цифровом блоке работает "
                           "при любом положении Num Lock.")
        tabs_note.setWordWrap(True)
        column.addWidget(tabs_note)
        add_key_buttons(settings.TAB_ACTIONS)
        self._show_keys()
        column.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.save_bindings)
        buttons.rejected.connect(self.reject)
        reset = buttons.addButton(
            "Сбросить по умолчанию", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(self.reset_bindings)
        outer.addWidget(buttons)

    # --- подписка на обновления ------------------------------------------

    def check_updates(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            announce(self, "Обновление уже проверяется.")
            return
        self.check_button.setEnabled(False)
        self.status.setText("Проверяю обновления.")
        announce(self, "Проверяю обновления.", assertive=True)
        # Родитель — главное окно: поток переживёт закрытие этого диалога.
        worker = UpdateWorker(self.parentWidget() or self)
        self._worker = worker
        worker.status.connect(self.on_status)
        worker.percent.connect(self.on_percent)
        worker.failed.connect(self.on_update_failed)
        worker.installed.connect(self.on_installed)
        worker.finished.connect(self.on_worker_finished)
        worker.start()

    def on_status(self, text: str) -> None:
        self.status.setText(text)
        announce(self, text, assertive=True)

    def on_percent(self, percent: int) -> None:
        text = f"Скачано {percent} процентов."
        self.status.setText(text)
        announce(self, text)

    def on_update_failed(self, text: str) -> None:
        self.status.setText(text)
        announce(self, text, assertive=True)

    def on_installed(self, text: str) -> None:
        self.status.setText(text)
        announce(self, text + " Программа перезапускается.", assertive=True)
        # Закрываемся не здесь, а когда поток обновления действительно
        # закончил: закрыться раньше — значит убить поток на полуслове.
        self._update_ready = True

    def on_worker_finished(self) -> None:
        self.check_button.setEnabled(True)
        if self._update_ready:
            self._update_ready = False
            QTimer.singleShot(300, QApplication.quit)

    # --- остальные кнопки ------------------------------------------------

    def export_text(self) -> None:
        try:
            path = self.store.export_text()
        except OSError:
            announce(self, "Не получилось выгрузить данные в текст.",
                     assertive=True)
            return
        announce(self, f"Данные выгружены в файл {path.name}, "
                       f"он лежит в папке Дата.", assertive=True)

    def open_data_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.folder)))
        announce(self, "Открыта папка Дата.")

    # --- копия в облако ---------------------------------------------------

    def show_cloud(self) -> None:
        if self.cloud is None:
            text = ("Копия в облако выключена: папка не выбрана. Нажми "
                    "«Выбрать папку для копий».")
        else:
            text = f"Папка для копий: {self.cloud}."
        self.cloud_label.setText(text)
        self.cloud_label.setAccessibleName(text)

    def choose_cloud_folder(self) -> None:
        start = self.store.folder
        default = sync.default_folder()
        if default is not None and default.parent.exists():
            start = default
        chosen = QFileDialog.getExistingDirectory(
            self, "Выбери папку для копий", str(start))
        if not chosen:
            announce(self, "Папка не изменена.")
            return
        try:
            settings.save_cloud(self.store.folder, chosen)
        except OSError:
            announce(self, "Не получилось запомнить папку для копий.",
                     assertive=True)
            return
        self.cloud = Path(chosen)
        self.show_cloud()
        self.cloud_changed.emit()
        try:
            sync.write(self.store, self.cloud)
        except sync.SyncError as error:
            announce(self, f"Папка выбрана, но копия не записалась: {error}",
                     assertive=True)
            return
        announce(self, f"Копия будет лежать в папке {self.cloud}. "
                       "Сейчас в ней сохранена первая копия.",
                 assertive=True)

    def save_cloud_now(self) -> None:
        if self.cloud is None:
            announce(self, "Сначала выбери папку для копий.", assertive=True)
            return
        if self.store.save_no == 0:
            announce(self, "В базе пока нет данных для копии.",
                     assertive=True)
            return
        try:
            sync.write(self.store, self.cloud)
        except sync.SyncError as error:
            announce(self, str(error), assertive=True)
            return
        announce(self, f"Копия сохранена в папке {self.cloud}, "
                       f"сохранение номер {self.store.save_no}.",
                 assertive=True)

    def check_cloud(self) -> None:
        if self.cloud is None:
            announce(self, "Сначала выбери папку для копий.", assertive=True)
            return
        try:
            text = sync.check(self.store, self.cloud)
        except sync.SyncError as error:
            announce(self, str(error), assertive=True)
            return
        announce(self, text, assertive=True)

    def take_cloud(self) -> None:
        if self.cloud is None:
            announce(self, "Сначала выбери папку для копий.", assertive=True)
            return
        box = QMessageBox(self)
        box.setWindowTitle("Взять данные из копии")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("Текущие данные будут заменены копией из облака. "
                    "Прежняя база останется в папке Резерв. Взять?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            announce(self, "Данные не изменены.")
            return
        try:
            text = sync.restore(self.store, self.cloud)
        except sync.SyncError as error:
            announce(self, str(error), assertive=True)
            return
        # Клавиши и громкость могли приехать из копии — перечитываем их,
        # а окна списков пересоберутся после сигнала.
        self.bindings = settings.load(self.store.folder)
        self.volume = settings.load_volume(self.store.folder)
        self.cue_volume = settings.load_cue_volume(self.store.folder)
        self._show_keys()
        self.volume_box.setValue(self.volume)
        self.cue_volume_box.setValue(self.cue_volume)
        self.restored.emit()
        announce(self, text, assertive=True)

    # --- звук -------------------------------------------------------------

    def test_sound(self) -> None:
        """Проиграть сигнал с выбранной громкостью, не закрывая настройки."""
        level = settings.volume_value(self.volume_box.value())
        if level == 0:
            announce(self, "Громкость ноль, сигналы выключены.", assertive=True)
            return
        folder = self.store.folder / "Sound"
        if not sounds.play("soon", folder, level):
            QApplication.beep()
        wait = sounds.length("soon", folder) + 0.15
        QTimer.singleShot(
            int(wait * 1000),
            lambda: announce(
                self, f"Сигнал напоминания, громкость {level} процентов."))

    def test_cue_sound(self) -> None:
        """Проиграть подсказку «двадцать минут» с её громкостью."""
        level = settings.cue_volume_value(self.cue_volume_box.value())
        if level == 0:
            announce(self, "Громкость подсказок ноль, они выключены.",
                     assertive=True)
            return
        folder = self.store.folder / "Sound"
        if not sounds.play("cue20", folder, level):
            QApplication.beep()
        wait = sounds.length("cue20", folder) + 0.15
        QTimer.singleShot(
            int(wait * 1000),
            lambda: announce(
                self, f"Подсказка времени, громкость {level} процентов."))

    # --- клавиши ----------------------------------------------------------

    def _show_keys(self) -> None:
        """Каждая кнопка называет действие и его клавиши — как в дикторе."""
        for code, title, _keys in settings.ALL_ACTIONS:
            keys = norm_keys(self.bindings.get(code, ""))
            text = f"{title}: {keys}" if keys else f"{title}: без клавиши"
            button = self.key_buttons[code]
            button.setText(text)
            button.setAccessibleName(f"{text}. Нажми, чтобы назначить клавиши")

    def assign_key(self, code: str) -> None:
        """Поймать новое сочетание: нажал — оно и назначено."""
        title = settings.TITLES[code]
        dialog = KeyCaptureDialog(title, self.bindings.get(code, ""), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Клавиши не изменены.")
            return
        keys = norm_keys(dialog.keys)
        if keys:
            other = next((c for c, k in self.bindings.items()
                          if c != code and norm_keys(k) == keys), None)
            if other is not None:
                announce(self, f"Клавиша {keys} уже занята действием "
                               f"«{settings.TITLES[other]}». Сначала освободи её.",
                         assertive=True)
                return
            announce(self, f"{title}: {keys}. Нажми «Сохранить», чтобы применить.",
                     assertive=True)
        else:
            announce(self, f"{title}: без клавиши. Нажми «Сохранить», "
                           f"чтобы применить.", assertive=True)
        self.bindings[code] = keys
        self._show_keys()

    def reset_bindings(self) -> None:
        self.bindings = settings.defaults()
        self.volume_box.setValue(settings.DEFAULT_VOLUME)
        self.cue_volume_box.setValue(settings.DEFAULT_CUE_VOLUME)
        self._show_keys()
        announce(self, "Расставлены клавиши по умолчанию и средние "
                       "громкости. Нажми «Сохранить», чтобы применить.")

    def save_bindings(self) -> None:
        result: dict[str, str] = {}
        seen: dict[str, str] = {}
        for code, title, _keys in settings.ALL_ACTIONS:
            text = norm_keys(self.bindings.get(code, ""))
            if text:
                other = seen.get(text)
                if other is not None:
                    announce(self, f"Клавиша {text} назначена дважды: "
                                   f"{other} и {title}. Поменяй одну.",
                             assertive=True)
                    return
                seen[text] = title
            result[code] = text
        level = settings.volume_value(self.volume_box.value())
        cue_level = settings.cue_volume_value(self.cue_volume_box.value())
        if self.autostart_box is not None:
            try:
                autostart.set_enabled(self.autostart_box.isChecked())
            except OSError:
                announce(self, "Не получилось изменить запуск с Windows. "
                               "Проверь права пользователя и попробуй ещё "
                               "раз.", assertive=True)
                return
        try:
            settings.save(self.store.folder, result, level, cue_level)
        except OSError:
            announce(self, "Не получилось сохранить настройки.", assertive=True)
            return
        self.bindings = result
        self.volume = level
        self.cue_volume = cue_level
        self.bindings_changed.emit(result)
        self.volume_changed.emit(level)
        self.cue_volume_changed.emit(cue_level)
        if level:
            first = f"Громкость сигналов {level} процентов."
        else:
            first = "Сигналы напоминаний выключены."
        if cue_level:
            second = f"Громкость подсказок времени {cue_level} процентов."
        else:
            second = "Подсказки времени без звука."
        if self.autostart_box is not None:
            if self.autostart_box.isChecked():
                third = ("При входе в Windows программа запускается "
                         "в трее. ")
            else:
                third = "Запуск при входе в Windows выключен. "
        else:
            third = ""
        announce(self, f"Настройки сохранены. {first} {second} {third}".strip(),
                 assertive=True)
        self.accept()


# --- главное окно --------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self, store: Store, background: bool = False):
        super().__init__()
        self.store = store
        # Запуск при входе в Windows: окно не показываем, живём в трее.
        self.background = background
        self.bindings = settings.load(store.folder)
        self.volume = settings.load_volume(store.folder)
        self.cue_volume = settings.load_cue_volume(store.folder)
        self.cues = settings.load_cues(store.folder)
        # Порядок списков абонементов: по дате добавления или по алфавиту.
        # Одна настройка на обе вкладки — переключатель в каждой.
        self.subs_order = settings.load_subs_order(store.folder)

        # --- копия в облако
        # Папку помним в настройках. Если её ещё нет, а Диск O на месте —
        # включаем копии сами: человек уже сказал, что пользуется им.
        self.cloud = settings.load_cloud(store.folder)
        if self.cloud is None:
            default = sync.default_folder()
            if default is not None:
                self.cloud = default
        if self.cloud is not None:
            try:
                self.cloud.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.cloud = None
            else:
                try:
                    settings.save_cloud(store.folder, self.cloud)
                except OSError:
                    pass
        self._sync_problem = ""
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(1500)
        self._sync_timer.timeout.connect(self.write_cloud_copy)
        # После каждого сохранения данных — копия в облако (с небольшой
        # задержкой: за одну правку таблица может записаться дважды).
        store.sync = self.schedule_cloud_copy

        self.setWindowTitle(f"Мажорный лад Расписание {__version__}")
        self.resize(820, 620)
        self.announcer = Announcer(self)

        self.tabs = QTabWidget(self)
        # Стрелки прокрутки вкладок не нужны: вкладки переключаются с
        # клавиатуры, а лишние кнопки только мешают.
        self.tabs.tabBar().setUsesScrollButtons(False)
        self.setCentralWidget(self.tabs)

        # --- вкладка учеников
        page = QWidget()
        self.students_page = page
        lay = QVBoxLayout(page)
        self.students_hint = QLabel()
        self.students_hint.setWordWrap(True)
        lay.addWidget(self.students_hint)

        self.students_list = QListWidget()
        self.students_list.setAccessibleName("Список учеников")
        self.students_list.itemDoubleClicked.connect(lambda _item: self.open_card())
        lay.addWidget(self.students_list, 1)

        row = QHBoxLayout()
        self.add_button = QPushButton("Добавить ученика")
        self.add_button.setDefault(True)
        self.add_button.clicked.connect(self.add_student)
        row.addWidget(self.add_button)
        self.edit_button = QPushButton("Редактировать ученика")
        self.edit_button.clicked.connect(self.edit_student)
        row.addWidget(self.edit_button)
        self.remove_button = QPushButton("Удалить ученика")
        self.remove_button.clicked.connect(self.remove_student)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        lay.addLayout(row)

        row = QHBoxLayout()
        # Урок начинается сам по расписанию; эта кнопка — для случая,
        # когда ученик пришёл раньше или чуть позже. От нажатия
        # отсчитываются подсказки времени и вопрос о конце урока.
        self.start_button = QPushButton("Начать урок")
        self.start_button.clicked.connect(
            lambda _checked=False: self.start_lesson())
        row.addWidget(self.start_button)
        self.card_button = QPushButton()
        self.card_button.clicked.connect(self.open_card)
        row.addWidget(self.card_button)
        self.settings_button = QPushButton()
        self.settings_button.clicked.connect(self.open_settings)
        row.addWidget(self.settings_button)
        self.cues_button = QPushButton()
        self.cues_button.clicked.connect(self.toggle_cues)
        row.addWidget(self.cues_button)
        self.block_button = QPushButton("Добавить исключение уроков")
        self.block_button.clicked.connect(
            lambda _checked=False: self.add_exception())
        row.addWidget(self.block_button)
        row.addStretch(1)
        lay.addLayout(row)
        self.tabs.addTab(page, "Ученики")

        # --- вкладка расписания
        page = QWidget()
        self.schedule_page = page
        lay = QVBoxLayout(page)
        self.schedule_hint = QLabel()
        self.schedule_hint.setWordWrap(True)
        lay.addWidget(self.schedule_hint)
        self.schedule_list = QListWidget()
        self.schedule_list.setAccessibleName("Расписание уроков")
        self.schedule_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.schedule_list.customContextMenuRequested.connect(
            self.schedule_menu)
        self.schedule_list.itemDoubleClicked.connect(
            lambda _item: self.lesson_card())
        lay.addWidget(self.schedule_list, 1)

        row = QHBoxLayout()
        trial_button = QPushButton("Добавить пробный урок")
        trial_button.clicked.connect(
            lambda _checked=False: self.add_trial_lesson())
        row.addWidget(trial_button)
        single_button = QPushButton("Добавить разовый урок")
        single_button.clicked.connect(
            lambda _checked=False: self.add_single_lesson())
        row.addWidget(single_button)
        row.addStretch(1)
        lay.addLayout(row)
        self.tabs.addTab(page, "Расписание")

        # --- вкладка исключений: когда уроков нет вовсе
        self._blocks_tab()

        # --- вкладка уведомлений: всё, что ещё не подтверждено
        self._notifications_tab()

        # --- вкладки абонементов
        (self.active_subs_page, self.active_subs_hint,
         self.active_subs_list, self.active_order_button) = self._subs_tab(
            "Действующие абонементы", "Список действующих абонементов",
            historical=False)
        (self.finished_subs_page, self.finished_subs_hint,
         self.finished_subs_list,
         self.finished_order_button) = self._subs_tab(
            "Законченные абонементы", "Список законченных абонементов",
            historical=True)
        (self.one_off_subs_page, self.one_off_subs_hint,
         self.one_off_subs_list,
         self.one_off_order_button) = self._subs_tab(
            "Разовые и пробные уроки", "Список разовых и пробных уроков",
            historical=True, one_off=True)

        # Будущие абонементы: вкладка видна, только когда есть что
        # показать. Купили абонемент поверх действующего — он здесь
        # ждёт; старый кончился — этот переехал в действующие, и вкладка
        # снова спряталась.
        (self.future_subs_page, self.future_subs_hint,
         self.future_subs_list,
         self.future_order_button) = self._subs_tab(
            "Будущие абонементы", "Список будущих абонементов",
            historical=False)
        self.tabs.setTabVisible(
            self.tabs.indexOf(self.future_subs_page), False)

        # --- вкладка услуг
        self._services_tab()
        self.update_hints()

        # --- горячие клавиши на всё приложение
        self._settings_open = False
        self._shortcuts: list[QShortcut] = []
        # Сочетания с цифровым блоком приходят в фильтр событий, а не в
        # QShortcut: фильтр понимает клавиши при любом положении Num Lock.
        QApplication.instance().installEventFilter(self)
        self.install_shortcuts()

        # --- напоминания
        self.reminders = ReminderService(self.store, self)
        self.reminders.soon.connect(self.on_soon)
        self.reminders.service_soon.connect(self.on_service_soon)
        self.reminders.ask_finish.connect(self.on_ask_finish)
        # Подтверждённые уведомления: их ключи помним между запусками,
        # иначе вкладка «Уведомления» заново показывала бы старое.
        self._dismissed = self.load_dismissed()

        # --- подсказки времени урока
        self._cue_anchor: datetime | None = None
        self._cue_done: set[int] = set()
        self._cue_timer = QTimer(self)
        self._cue_timer.setInterval(1000)
        self._cue_timer.timeout.connect(self.check_cues)
        self._cue_timer.start()

        self.refresh_all()
        self.setup_tray()
        if self.background:
            self.tray.showMessage(
                "Мажорный лад Расписание",
                "Программа запущена и работает в трее. Расписание и "
                "напоминания включены.",
                QSystemTrayIcon.MessageIcon.Information, 8000)
        else:
            QTimer.singleShot(600, self.hello)

    # --- общее ------------------------------------------------------------

    def hello(self) -> None:
        count = len(self.store.students)
        word = plural(count, "ученик", "ученика", "учеников")
        text = f"Мажорный лад Расписание. В списке {count} {word}."
        if self.store.notice:
            text += " " + self.store.notice
            self.store.notice = ""
        if self.cloud is not None:
            newer = sync.newer_message(self.store, self.cloud)
            if newer:
                text += " " + newer
        announce(self, text)
        self.students_list.setFocus()

    # --- копия в облако ---------------------------------------------------

    def schedule_cloud_copy(self, *_args) -> None:
        if self.cloud is None:
            return
        self._sync_timer.start()

    def write_cloud_copy(self) -> None:
        if self.cloud is None:
            return
        try:
            sync.write(self.store, self.cloud)
        except sync.SyncError as error:
            # Про сбой говорим один раз за запуск: повторять на каждое
            # сохранение — только мешать работе.
            if not self._sync_problem:
                self._sync_problem = str(error)
                announce(self, f"Копия в облако не записалась: {error}",
                         assertive=True)
            return
        self._sync_problem = ""

    def reload_cloud(self) -> None:
        self.cloud = settings.load_cloud(self.store.folder)
        self._sync_problem = ""

    def after_restore(self) -> None:
        """После взятия копии: перечитать настройки и пересобрать окна."""
        self.bindings = settings.load(self.store.folder)
        self.volume = settings.load_volume(self.store.folder)
        self.cue_volume = settings.load_cue_volume(self.store.folder)
        self.cues = settings.load_cues(self.store.folder)
        self.subs_order = settings.load_subs_order(self.store.folder)
        self.install_shortcuts()
        self.update_hints()
        self.update_cues_button()
        self.refresh_all()

    def student_line(self, student: data.Student) -> str:
        used, total = self.store.balance(student.name)
        balance = (f"уроки {used} из {total}" if total
                   else "абонемента нет")
        near = self.store.nearest_lesson(student.name)
        if near is not None:
            tail = f"ближайший урок {dates.human_date(near.day)} в {near.time}"
        else:
            tail = "уроков не записано"
        return f"{student.name}, {balance}, {tail}"

    def refresh_students(self) -> None:
        current = self.selected_uid()
        self.students_list.clear()
        for student in sorted(self.store.students, key=lambda s: s.name.casefold()):
            item = QListWidgetItem(self.student_line(student))
            item.setData(Qt.ItemDataRole.UserRole, student.uid)
            self.students_list.addItem(item)
        if current is not None:
            for row in range(self.students_list.count()):
                item = self.students_list.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == current:
                    self.students_list.setCurrentRow(row)
                    break

    def refresh_schedule(self) -> None:
        self.schedule_list.clear()
        start = date.today() - timedelta(days=1)
        for lesson in self.store.lessons_between(start, date.max):
            item = QListWidgetItem(lesson_line(self.store, lesson))
            item.setData(Qt.ItemDataRole.UserRole, lesson.uid)
            self.schedule_list.addItem(item)

    # --- меню урока в расписании ------------------------------------------

    def selected_lesson(self) -> data.Lesson | None:
        """Урок, выбранный в списке расписания."""
        item = self.schedule_list.currentItem()
        if item is None:
            return None
        uid = item.data(Qt.ItemDataRole.UserRole)
        for lesson in self.store.lessons:
            if lesson.uid == uid:
                return lesson
        return None

    def schedule_menu(self, pos) -> None:
        """Меню урока: правый щелчок или клавиша «Меню».

        Клавиатура присылает точку в центре списка, а не на строке, —
        тогда меню открывается у выбранного урока. Меню работает и без
        выбранного урока: из него можно добавить пробный или разовый
        урок, которого в расписании ещё не было. Меню родное, диктор
        читает пункты сам.
        """
        lst = self.schedule_list
        item = lst.itemAt(pos)
        if item is None:
            item = lst.currentItem()
            if item is not None:
                pos = lst.visualItemRect(item).center()
        if item is not None:
            lst.setCurrentItem(item)
        lesson = self.selected_lesson()
        menu = QMenu(lst)
        if lesson is None:
            menu.setAccessibleName("Расписание")
        else:
            when = dates.fmt_date(lesson.day)
            if lesson.time:
                when += f" в {lesson.time}"
            menu.setAccessibleName(f"Урок {lesson.student}, {when}")
        trial = menu.addAction("Добавить пробный урок")
        single = menu.addAction("Добавить разовый урок")
        edit = shift = done = card = remove = None
        if lesson is not None:
            menu.addSeparator()
            edit = menu.addAction("Редактировать урок")
            shift = menu.addAction("Перенести урок")
            done = menu.addAction("Снять отметку «проведён»"
                                  if lesson.status == LESSON_DONE
                                  else "Отметить урок проведённым")
            card = menu.addAction("Открыть карточку ученика")
            menu.addSeparator()
            remove = menu.addAction("Удалить урок")
        chosen = menu.exec(lst.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is trial:
            self.add_trial_lesson()
        elif chosen is single:
            self.add_single_lesson()
        elif chosen is edit:
            self.edit_lesson_here(lesson)
        elif chosen is shift:
            self.shift_lesson(lesson)
        elif chosen is done:
            self.toggle_lesson_done(lesson)
        elif chosen is card:
            self.lesson_card(lesson)
        elif chosen is remove:
            self.delete_lesson_here(lesson)

    def lesson_card(self, lesson: data.Lesson | None = None) -> None:
        """Открыть карточку ученика этого урока."""
        if lesson is None:
            lesson = self.selected_lesson()
        if lesson is None:
            announce(self, "Выбери урок в списке расписания.")
            return
        student = self.store.find_student(lesson.student)
        if student is None:
            announce(self, f"Ученик {lesson.student} не найден в списке.",
                     assertive=True)
            return
        self.select_uid(student.uid)
        self.open_card()

    def edit_lesson_here(self, lesson: data.Lesson,
                         allow_future: bool = True) -> None:
        """Перенести урок на другую дату или время из расписания."""
        dialog = LessonEditDialog(lesson, self, allow_future=allow_future)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Урок не изменён.")
            return
        new_day = dialog.new_day or lesson.day
        new_time = dialog.new_time
        if new_day == lesson.day and new_time == lesson.time:
            announce(self, "Урок не изменён.")
            return
        for other in self.store.lessons:
            if (other is not lesson and other.student == lesson.student
                    and other.day == new_day and other.time == new_time):
                announce(self, f"У {lesson.student} на "
                               f"{dates.fmt_date(new_day)} в это время уже "
                               f"есть урок. Выбери другое время.",
                         assertive=True)
                return
        old_text = dates.fmt_date(lesson.day)
        lesson.day = new_day
        lesson.time = new_time
        self.store.update_lesson(lesson)
        when = dates.fmt_date(new_day)
        if new_time:
            when += f" в {new_time}"
        announce(self, f"Урок {lesson.student} перенесён: было {old_text}, "
                       f"теперь {when}.")
        self.refresh_all()

    def shift_lesson(self, lesson: data.Lesson) -> None:
        """Увести урок в конец графика по расписанию ученика.

        Урок не сдвигается на день-два: он уезжает на ближайший день, в
        который ученик и так занимается (вторник, пятница — как записано
        в его графике), после последнего урока. Количество уроков остаётся
        прежним, меняется только дата.
        """
        target = data.plan_move_to_end(self.store.lessons, lesson)
        if target is None:
            announce(self, f"Не нашёл свободного дня по расписанию "
                           f"{lesson.student}: ближайшие дни заняты. "
                           f"Перенеси урок вручную — «Редактировать урок».",
                     assertive=True)
            return
        new_day, new_time = target
        old_when = dates.fmt_date(lesson.day)
        if lesson.time:
            old_when += f" в {lesson.time}"
        new_when = dates.fmt_date(new_day)
        if new_time:
            new_when += f" в {new_time}"
        box = QMessageBox(self)
        box.setWindowTitle("Перенести урок")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Урок {lesson.student} перенесётся с {old_when} на "
                    f"{new_when} — в конец расписания, на привычный день "
                    f"занятий ученика. Количество уроков не изменится. "
                    f"Перенести?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            announce(self, "Урок не перенесён.")
            return

        lesson.day = new_day
        lesson.time = new_time
        self.store.update_lesson(lesson)
        announce(self, f"Урок {lesson.student} перенесён с {old_when} на "
                       f"{new_when}.")
        self.refresh_all()

    def toggle_lesson_done(self, lesson: data.Lesson) -> None:
        """Отметить урок проведённым или снять отметку из расписания.

        Оплату это не трогает: записывается сам факт урока, а счётчик
        абонемента пересчитывается по урокам. Если ученик на абонементе,
        урок приписывается к действующему — как в карточке ученика.
        """
        when = dates.fmt_date(lesson.day)
        if lesson.time:
            when += f" в {lesson.time}"
        if lesson.status == LESSON_DONE:
            lesson.status = LESSON_WAIT
            self.store.update_lesson(lesson)
            self.store.recount_used()
            announce(self, f"Урок {lesson.student}, {when}, снова ожидается.")
        else:
            lesson.status = LESSON_DONE
            if not lesson.subscription:
                sub = self.store.current_subscription(lesson.student)
                if sub is not None:
                    lesson.subscription = sub.uid
            self.store.update_lesson(lesson)
            self.store.recount_used()
            text = f"Урок {lesson.student}, {when}, отмечен проведённым."
            used, total = self.store.balance(lesson.student)
            if total:
                text += f" Уроки {used} из {total}."
            announce(self, text)
        self.refresh_all()

    def delete_lesson_here(self, lesson: data.Lesson) -> None:
        """Удалить урок из расписания — с подтверждением."""
        when = dates.fmt_date(lesson.day)
        if lesson.time:
            when += f" в {lesson.time}"
        answer = QMessageBox.question(
            self, "Удалить урок",
            f"Удалить урок {lesson.student}, {when}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            announce(self, "Урок не удалён.")
            return
        self.store.delete_lesson(lesson)
        announce(self, f"Урок {lesson.student}, {when}, удалён.")
        self.refresh_all()

    def _subs_tab(self, title: str, name: str, historical: bool,
                  one_off: bool = False
                  ) -> tuple[QWidget, QLabel, QListWidget, QPushButton]:
        """Вкладка со списком абонементов: подсказка, список, кнопки.

        Двойной щелчок открывает карточку ученика, чей это абонемент.
        Правый щелчок или клавиша «Меню» открывает меню абонемента.
        Кнопка «Добавить проведённый урок» отмечает урок в выбранном
        абонементе — так же, как в карточке ученика; на вкладке разовых
        уроков она называется «Отметить урок проведённым».
        Кнопка «Порядок» переключает раскладку списка. На вкладке
        разовых и пробных уроков (`one_off`) вместо кнопки абонемента
        стоят кнопки «Добавить пробный урок» и «Добавить разовый урок».
        """
        page = QWidget()
        lay = QVBoxLayout(page)
        hint = QLabel()
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lst = QListWidget()
        lst.setAccessibleName(name)
        lst.itemDoubleClicked.connect(self.open_sub_card)
        lst.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lst.customContextMenuRequested.connect(
            lambda pos, box=lst: self.subs_menu(box, pos))
        lay.addWidget(lst, 1)

        row = QHBoxLayout()
        if one_off:
            trial_button = QPushButton("Добавить пробный урок")
            trial_button.clicked.connect(
                lambda _checked=False: self.add_trial_lesson())
            row.addWidget(trial_button)
            single_button = QPushButton("Добавить разовый урок")
            single_button.clicked.connect(
                lambda _checked=False: self.add_single_lesson())
            row.addWidget(single_button)
        else:
            add_button = QPushButton("Добавить абонемент")
            add_button.clicked.connect(
                lambda _checked=False, old=historical:
                self.add_subscription(old))
            row.addWidget(add_button)
        done_button = QPushButton(
            "Отметить урок проведённым" if one_off
            else "Добавить проведённый урок")
        done_button.clicked.connect(
            lambda _checked=False: self.add_done_lesson())
        row.addWidget(done_button)
        edit_button = QPushButton(
            "Редактировать урок" if one_off else "Редактировать абонемент")
        edit_button.clicked.connect(
            lambda _checked=False: self.edit_subscription())
        row.addWidget(edit_button)
        remove_button = QPushButton(
            "Удалить урок" if one_off else "Удалить абонемент")
        remove_button.clicked.connect(
            lambda _checked=False: self.delete_subscription())
        row.addWidget(remove_button)
        order_button = QPushButton()
        order_button.clicked.connect(
            lambda _checked=False: self.toggle_subs_order())
        row.addWidget(order_button)
        row.addStretch(1)
        lay.addLayout(row)

        self.tabs.addTab(page, title)
        return page, hint, lst, order_button

    def sub_line(self, sub: data.Subscription) -> str:
        """Абонемент одной фразой: как его прочитает диктор."""
        if sub.one_off:
            title = "пробный урок" if sub.trial else "разовый урок"
            parts = [sub.student, f"{title} {dates.fmt_date(sub.bought)}"]
            if sub.lesson_format:
                parts.append(sub.lesson_format.casefold())
            parts.append(sub.direction)
            if sub.single and sub.price:
                parts.append(f"{money(sub.price)} рублей")
            return ", ".join(parts)
        parts = [sub.student,
                 f"куплен {dates.fmt_date(sub.bought)}",
                 f"с {dates.fmt_date(sub.begins)}"]
        if sub.total:
            parts.append(f"уроки {sub.used} из {sub.total}")
            if not sub.finished:
                parts.append(f"осталось {sub.left}")
        else:
            parts.append(f"проведено {sub.used}")
        return ", ".join(parts)

    def subs_in_order(self, subs):
        """Абонементы в выбранном порядке: алфавит, два вида дат, урок.

        По дате добавления — это по номеру записи: он растёт при
        добавлении, поэтому новые оказываются сверху. По дате покупки —
        по дню, когда абонемент купили. По дню первого урока — по дате
        начала абонемента, а у пробного и разового урока это дата самого
        занятия; новые сверху.
        """
        if self.subs_order == settings.SUBS_ORDER_NAME:
            return sorted(subs, key=lambda s: (s.student.casefold(), s.uid))
        if self.subs_order == settings.SUBS_ORDER_BOUGHT:
            return sorted(subs, key=lambda s: (s.bought, s.uid),
                          reverse=True)
        if self.subs_order == settings.SUBS_ORDER_FIRST:
            return sorted(subs, key=lambda s: (s.begins, s.uid),
                          reverse=True)
        return sorted(subs, key=lambda s: s.uid, reverse=True)

    def _fill_subs(self, lst: QListWidget, subs) -> None:
        """Наполнить список абонементов в выбранном порядке."""
        current = None
        item = lst.currentItem()
        if item is not None:
            current = item.data(Qt.ItemDataRole.UserRole)
        lst.clear()
        for sub in self.subs_in_order(subs):
            item = QListWidgetItem(self.sub_line(sub))
            item.setData(Qt.ItemDataRole.UserRole, sub.uid)
            lst.addItem(item)
        if current is not None:
            for row in range(lst.count()):
                item = lst.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == current:
                    lst.setCurrentRow(row)
                    break

    def refresh_subscriptions(self) -> None:
        """Разложить абонементы по трём вкладкам.

        Пробные и разовые уроки — отдельные записи, а не абонементы:
        они живут только на вкладке «Разовые и пробные уроки» и не
        попадают ни в действующие, ни в законченные.
        """
        self._fill_subs(self.active_subs_list,
                        [s for s in self.store.subscriptions
                         if not s.finished and not s.one_off and not s.future])
        self._fill_subs(self.finished_subs_list,
                        [s for s in self.store.subscriptions
                         if s.finished and not s.one_off])
        self._fill_subs(self.one_off_subs_list,
                        [s for s in self.store.subscriptions if s.one_off])
        future = [s for s in self.store.subscriptions
                  if s.future and not s.finished and not s.one_off]
        self._fill_subs(self.future_subs_list, future)
        self.update_tab_visibility()

    # Порядок переключателя: каждое нажатие — следующий вид.
    SUBS_ORDERS = (settings.SUBS_ORDER_DATE, settings.SUBS_ORDER_NAME,
                   settings.SUBS_ORDER_BOUGHT, settings.SUBS_ORDER_FIRST)
    ORDER_SHORT = {
        settings.SUBS_ORDER_DATE: "по дате добавления",
        settings.SUBS_ORDER_NAME: "по алфавиту",
        settings.SUBS_ORDER_BOUGHT: "по дате покупки",
        settings.SUBS_ORDER_FIRST: "по дню первого урока",
    }

    def order_words(self) -> str:
        """Порядок списков словами — для подсказки и объявления."""
        words = {
            settings.SUBS_ORDER_DATE: "по дате добавления, новые сверху",
            settings.SUBS_ORDER_NAME: "по алфавиту",
            settings.SUBS_ORDER_BOUGHT: "по дате покупки, новые сверху",
            settings.SUBS_ORDER_FIRST: "по дню первого урока, новые сверху",
        }
        return words.get(self.subs_order, words[settings.SUBS_ORDER_DATE])

    def next_order(self) -> str:
        """Какой порядок включится при следующем нажатии."""
        try:
            index = self.SUBS_ORDERS.index(self.subs_order)
        except ValueError:
            return self.SUBS_ORDERS[0]
        return self.SUBS_ORDERS[(index + 1) % len(self.SUBS_ORDERS)]

    def toggle_subs_order(self) -> None:
        """Переключить порядок абонементов и переложить все три списка."""
        self.subs_order = self.next_order()
        try:
            settings.save_subs_order(self.store.folder, self.subs_order)
        except OSError:
            # Порядок — мелочь: не записался на диск, но в этой сессии
            # работает.
            pass
        self.update_hints()
        self.refresh_subscriptions()
        announce(self, f"Списки разложены {self.order_words()}.")

    def _update_order_buttons(self) -> None:
        """Кнопка называет текущий порядок списков."""
        text = "Порядок: " + self.ORDER_SHORT.get(
            self.subs_order, self.ORDER_SHORT[settings.SUBS_ORDER_DATE])
        for button in (self.active_order_button, self.finished_order_button,
                       self.one_off_order_button, self.future_order_button):
            button.setText(text)

    def open_sub_card(self, item: QListWidgetItem) -> None:
        """Из списка абонементов — в карточку его ученика."""
        sub = self.store.subscription_by_uid(
            item.data(Qt.ItemDataRole.UserRole))
        if sub is None:
            return
        student = self.store.find_student(sub.student)
        if student is None:
            announce(self, f"Ученик {sub.student} не найден в списке.",
                     assertive=True)
            return
        self.select_uid(student.uid)
        self.open_card()

    def selected_subscription(self) -> data.Subscription | None:
        """Абонемент или урок, выбранный на вкладке абонементов."""
        page = self.tabs.currentWidget()
        if page is self.active_subs_page:
            lst = self.active_subs_list
        elif page is self.finished_subs_page:
            lst = self.finished_subs_list
        elif page is self.one_off_subs_page:
            lst = self.one_off_subs_list
        elif page is self.future_subs_page:
            lst = self.future_subs_list
        else:
            return None
        item = lst.currentItem()
        if item is None:
            return None
        return self.store.subscription_by_uid(
            item.data(Qt.ItemDataRole.UserRole))

    def person_lessons(self) -> tuple[str | None, list]:
        """Уроки того, на кого стоит выбор: ученика или абонемента.

        В списке учеников — все уроки выбранного ученика, в абонементах —
        уроки выбранной записи. Имя None — выбор ни на ком не стоит.
        """
        page = self.tabs.currentWidget()
        if page is self.students_page:
            student = self.selected_student()
            if student is None:
                return None, []
            return (student.name,
                    [l for l in self.store.lessons
                     if l.student == student.name])
        sub = self.selected_subscription()
        if sub is None:
            return None, []
        return sub.student, self.store.subscription_lessons(sub)

    @staticmethod
    def _lesson_days(lessons) -> str:
        """«3 ноября вторник, 6 ноября пятница» — без лишней запятой."""
        days = sorted({lesson.day for lesson in lessons})
        return ", ".join(
            f"{d.day} {dates.MONTHS[d.month - 1]} {dates.WEEKDAYS[d.weekday()]}"
            for d in days)

    def open_lesson_list(self, lessons: list[data.Lesson], title: str,
                         allow_future: bool) -> None:
        """Список уроков стрелками, Enter — открыть урок на правку."""
        dialog = LessonListDialog(lessons, title, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        lesson = dialog.chosen()
        if lesson is None:
            return
        self.edit_lesson_here(lesson, allow_future=allow_future)

    def speak_past_lessons(self) -> None:
        """F7: прошедшие уроки выбранного ученика или абонемента."""
        name, lessons = self.person_lessons()
        if name is None:
            announce(self, "Ничего не выбрано.", assertive=True)
            return
        past = [l for l in lessons if l.status == LESSON_DONE]
        if not past:
            announce(self, "Прошедших уроков нет.", assertive=True)
            return
        word = plural(len(past), "урок", "урока", "уроков")
        self.open_lesson_list(past, f"Прошедшие уроки, {name}, {len(past)} {word}",
                              allow_future=False)

    def speak_planned_lessons(self) -> None:
        """F8: запланированные уроки выбранного ученика или абонемента."""
        name, lessons = self.person_lessons()
        if name is None:
            announce(self, "Ничего не выбрано.", assertive=True)
            return
        planned = [l for l in lessons if l.status == LESSON_WAIT]
        sub = self.selected_subscription()
        if not planned:
            if sub is not None and sub.finished:
                announce(self, "Все уроки проведены.", assertive=True)
            else:
                announce(self, "Запланированных уроков нет.", assertive=True)
            return
        word = plural(len(planned), "урок", "урока", "уроков")
        self.open_lesson_list(
            planned, f"Запланированные уроки, {name}, {len(planned)} {word}",
            allow_future=True)

    def select_subscription(self, sub: data.Subscription) -> None:
        """Поставить выбор на запись в том списке, где она лежит."""
        for lst in (self.active_subs_list, self.finished_subs_list,
                    self.one_off_subs_list, self.future_subs_list):
            for row in range(lst.count()):
                item = lst.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == sub.uid:
                    lst.setCurrentRow(row)
                    item.setSelected(True)
                    return

    def subs_menu(self, lst: QListWidget, pos) -> None:
        """Меню абонемента: правый щелчок или клавиша «Меню».

        Клавиатура присылает точку в центре списка, а не на строке, —
        тогда меню открывается у выбранного абонемента. «Добавить» есть
        всегда, остальные пункты — когда абонемент выбран. Выбранный
        абонемент можно отметить уроком, поправить, удалить или
        выгрузить. На вкладке разовых и пробных уроков вместо
        «Добавить абонемент» стоят «Добавить пробный урок» и «Добавить
        разовый урок». Меню родное, диктор читает пункты сам.
        """
        item = lst.itemAt(pos)
        if item is None:
            item = lst.currentItem()
            if item is not None:
                pos = lst.visualItemRect(item).center()
        sub = None
        if item is not None:
            sub = self.store.subscription_by_uid(
                item.data(Qt.ItemDataRole.UserRole))
            lst.setCurrentItem(item)
        on_one_off = lst is self.one_off_subs_list
        menu = QMenu(lst)
        if sub is not None:
            menu.setAccessibleName(
                f"Урок {sub.student}" if on_one_off else
                f"Абонемент {sub.student}")
        else:
            menu.setAccessibleName("Разовые и пробные уроки" if on_one_off
                                   else "Абонементы")
        add = trial = single = None
        if on_one_off:
            trial = menu.addAction("Добавить пробный урок")
            single = menu.addAction("Добавить разовый урок")
        else:
            add = menu.addAction("Добавить абонемент")
        done = edit = remove = export = None
        if sub is not None:
            menu.addSeparator()
            done = menu.addAction(
                "Отметить урок проведённым" if on_one_off
                else "Добавить проведённый урок")
            edit = menu.addAction("Редактировать урок" if on_one_off
                                  else "Редактировать")
            remove = menu.addAction("Удалить урок" if on_one_off
                                    else "Удалить")
            export = menu.addAction("Экспортировать")
        chosen = menu.exec(lst.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is add:
            self.add_subscription()
        elif chosen is trial:
            self.add_trial_lesson()
        elif chosen is single:
            self.add_single_lesson()
        elif chosen is done:
            self.add_done_lesson(sub)
        elif chosen is edit:
            self.edit_subscription(sub)
        elif chosen is remove:
            self.delete_subscription(sub)
        elif chosen is export:
            self.export_subscription(sub)

    def add_subscription(self, historical: bool | None = None) -> None:
        """Добавить абонемент со вкладки абонементов.

        На вкладке законченных заводится старый абонемент, на действующих —
        новый. Ученика выбираем отдельно: на этих вкладках их несколько.
        """
        if not self.store.students:
            announce(self, "Учеников нет. Сначала добавь ученика на вкладке "
                           "Ученики.", assertive=True)
            return
        if historical is None:
            historical = self.tabs.currentWidget() is self.finished_subs_page
        picker = StudentPickDialog(
            self.store,
            "Законченный абонемент" if historical else "Новый абонемент",
            "Чей абонемент добавить? Выбери ученика в списке и нажми "
            "«Дальше».",
            self)
        if picker.exec() != QDialog.DialogCode.Accepted \
                or picker.picked is None:
            announce(self, "Абонемент не добавлен.")
            return
        student = picker.picked
        dialog = SubscriptionDialog(self.store, student,
                                    historical=historical, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Абонемент не добавлен.")
            return
        sub = dialog.subscription
        self.refresh_all()
        if historical:
            self.tabs.setCurrentWidget(self.finished_subs_page)
            text = (f"Законченный абонемент {student.name} с "
                    f"{dates.fmt_date(sub.begins)} добавлен. Отметь в нём "
                    f"проведённые уроки кнопкой «Добавить проведённый "
                    f"урок».")
        else:
            self.tabs.setCurrentWidget(self.active_subs_page)
            text = (f"{student.name}, абонемент с "
                    f"{dates.fmt_date(sub.begins)} добавлен.")
            if sub.total:
                text += f" Уроки 0 из {sub.total}."
            if dialog.schedule_added:
                word = plural(dialog.schedule_added, "урок", "урока", "уроков")
                text += (f" Расписание записано, {dialog.schedule_added} "
                         f"{word}.")
            elif dialog.schedule_empty:
                text += " Расписание не записано: ни один день не отмечен."
        announce(self, text, assertive=True)

    def add_one_off_lesson(self, kind: str) -> None:
        """Одно занятие: пробный или разовый урок.

        Спрашиваем ученика, дату, формат, направление, а для разового —
        ещё и цену. Урока в расписании не появляется — это запись в
        истории на вкладке «Разовые и пробные уроки». Проведён он или
        ещё нет, видно по отметке в списке и в карточке.
        """
        title = "Пробный" if kind == "trial" else "Разовый"
        low = "пробный" if kind == "trial" else "разовый"
        if not self.store.students:
            announce(self, "Учеников нет. Сначала добавь ученика на вкладке "
                           "Ученики.", assertive=True)
            return
        picker = StudentPickDialog(
            self.store, f"{title} урок",
            f"Чей {low} урок добавить? Выбери ученика в списке и нажми "
            "«Дальше».", self)
        if picker.exec() != QDialog.DialogCode.Accepted \
                or picker.picked is None:
            announce(self, f"{title} урок не добавлен.")
            return
        student = picker.picked
        dialog = OneOffLessonDialog(self.store, student, kind=kind,
                                    parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, f"{title} урок не добавлен.")
            return
        sub = dialog.subscription
        self.refresh_all()
        self.tabs.setCurrentWidget(self.one_off_subs_page)
        self.select_subscription(sub)
        text = (f"{title} урок {student.name} на "
                f"{dates.fmt_date(sub.bought)} добавлен.")
        if sub.price:
            text += f" Стоимость: {money(sub.price)} рублей."
        text += (" Он уже выбран в списке — отметь его проведённым кнопкой "
                 "«Отметить урок проведённым».")
        announce(self, text, assertive=True)

    def add_trial_lesson(self) -> None:
        self.add_one_off_lesson("trial")

    def add_single_lesson(self) -> None:
        self.add_one_off_lesson("single")

    def add_done_lesson(
            self, sub: data.Subscription | None = None) -> None:
        """Проведённый урок в выбранном абонементе — как в карточке.

        Кнопка на вкладке действующих, законченных или разовых уроков
        и пункт меню работают с записью, выбранной в списке. Для
        пробного или разового урока это отметка «проведён». Урок с
        пустым временем: он уже прошёл, расписание его не касается.
        """
        if sub is None:
            sub = self.selected_subscription()
        if sub is None:
            announce(self, "Сначала выбери абонемент или урок в списке на "
                           "вкладке действующих, законченных или «Разовые "
                           "и пробные уроки».", assertive=True)
            return
        was_finished = sub.finished
        dialog = DoneLessonDialog(self.store, sub, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Урок не отмечен." if sub.one_off
                     else "Урок не добавлен.")
            return
        self.refresh_all()
        day = dates.fmt_date(dialog.day_value)
        if dialog.count_value > 1:
            word = plural(dialog.count_value, "урок", "урока", "уроков")
            text = f"{sub.student}: записано {word} {dialog.count_value}, {day}."
        elif sub.one_off:
            title = "Пробный" if sub.trial else "Разовый"
            text = (f"{title} урок {sub.student}, {day}: отмечен "
                    f"проведённым.")
        else:
            text = f"{sub.student}: урок {day} записан."
        if sub.one_off:
            pass
        elif sub.total:
            text += f" В абонементе уроки {sub.used} из {sub.total}."
        else:
            text += f" Всего уроков {sub.used}."
        if sub.finished and not was_finished and not sub.one_off:
            text += " Уроки кончились, абонемент перешёл в законченные."
        announce(self, text, assertive=True)

    def edit_subscription(
            self, sub: data.Subscription | None = None) -> None:
        """Правка абонемента: из меню или для выбранного в списке."""
        if sub is None:
            sub = self.selected_subscription()
        if sub is None:
            announce(self, "Сначала выбери абонемент или урок в списке на "
                           "вкладке действующих, законченных или «Разовые "
                           "и пробные уроки».", assertive=True)
            return
        dialog = SubscriptionEditDialog(self.store, sub, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Правка отменена.")
            return
        self.refresh_subscriptions()
        what = "Урок" if sub.one_off else "Абонемент"
        text = f"{what} {sub.student} сохранён: {self.sub_line(sub)}."
        if dialog.closed_itself and not sub.one_off:
            text += " Уроки кончились, поэтому он перешёл в законченные."
        announce(self, text, assertive=True)

    def delete_subscription(
            self, sub: data.Subscription | None = None) -> None:
        """Удалить абонемент совсем — только когда из него убраны уроки."""
        if sub is None:
            sub = self.selected_subscription()
        if sub is None:
            announce(self, "Сначала выбери абонемент или урок в списке на "
                           "вкладке действующих, законченных или «Разовые "
                           "и пробные уроки».", assertive=True)
            return
        what = "урок" if sub.one_off else "абонемент"
        count = len(self.store.subscription_lessons(sub))
        if count:
            word = plural(count, "урок", "урока", "уроков")
            announce(self,
                     f"{what.capitalize()} {sub.student} удалить нельзя: "
                     f"в нём записано {count} {word}. Сначала убери уроки "
                     f"в карточке ученика.", assertive=True)
            return
        if sub.one_off:
            text = (f"Удалить {what} {sub.student} от "
                    f"{dates.fmt_date(sub.bought)}? Оплата в журнале "
                    f"останется.")
        else:
            text = (f"Удалить {what} {sub.student} от "
                    f"{dates.fmt_date(sub.bought)}? Уроков в нём нет, "
                    f"оплаты в журнале останутся.")
        box = QMessageBox(self)
        box.setWindowTitle(f"Удалить {what}")
        box.setText(text)
        yes = box.addButton("Удалить", QMessageBox.ButtonRole.DestructiveRole)
        no = box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        announce(self, text, assertive=True)
        box.exec()
        if box.clickedButton() is not yes:
            announce(self, "Удаление отменено.")
            return
        try:
            self.store.delete_subscription(sub)
        except ValueError as error:
            announce(self, str(error), assertive=True)
            return
        self.refresh_subscriptions()
        announce(self, f"{what.capitalize()} {sub.student} от "
                       f"{dates.fmt_date(sub.bought)} удалён.",
                 assertive=True)

    def export_subscription(
            self, sub: data.Subscription | None = None) -> None:
        """F6 или меню — абонемент текстом в папку Данные, в «Абонементы»."""
        if sub is None:
            sub = self.selected_subscription()
        if sub is None:
            announce(self,
                     "Чтобы выгрузить абонемент или урок, открой вкладку "
                     "действующих, законченных абонементов или «Разовые и "
                     "пробные уроки», выбери запись в списке и нажми ещё "
                     "раз.", assertive=True)
            return
        what = "Урок" if sub.one_off else "Абонемент"
        try:
            path = self.store.export_subscription(sub)
        except OSError:
            announce(self, f"Не получилось выгрузить {what.casefold()}.",
                     assertive=True)
            return
        announce(self, f"{what} {sub.student} выгружен в файл "
                       f"{path.name}. Он лежит в папке Данные, внутри "
                       f"папки Абонементы.", assertive=True)

    # --- услуги -----------------------------------------------------------

    def _services_tab(self) -> QWidget:
        """Вкладка услуг: список оплаченных услуг и кнопки под ним.

        Правый щелчок или клавиша «Меню» открывают меню услуги. Двойной
        щелчок открывает карточку ученика, которому эта услуга.
        """
        page = QWidget()
        self.services_page = page
        lay = QVBoxLayout(page)
        self.services_hint = QLabel()
        self.services_hint.setWordWrap(True)
        lay.addWidget(self.services_hint)

        self.services_list = QListWidget()
        self.services_list.setAccessibleName("Список услуг")
        self.services_list.itemDoubleClicked.connect(self.open_service_card)
        self.services_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.services_list.customContextMenuRequested.connect(
            lambda pos: self.services_menu(self.services_list, pos))
        lay.addWidget(self.services_list, 1)

        row = QHBoxLayout()
        add_button = QPushButton("Добавить услугу")
        add_button.clicked.connect(lambda _checked=False: self.add_service())
        row.addWidget(add_button)
        edit_button = QPushButton("Редактировать услугу")
        edit_button.clicked.connect(lambda _checked=False: self.edit_service())
        row.addWidget(edit_button)
        remove_button = QPushButton("Удалить услугу")
        remove_button.clicked.connect(
            lambda _checked=False: self.delete_service())
        row.addWidget(remove_button)
        row.addStretch(1)
        lay.addLayout(row)

        self.tabs.addTab(page, "Услуги")
        return page

    def service_line(self, payment: data.Payment) -> str:
        """Услуга одной фразой: как её прочитает диктор."""
        line = (f"{dates.fmt_date(payment.day)}, {payment.student}, "
                f"{payment.what.casefold()}, {rubles(payment.amount)}")
        end = services.service_end(payment)
        if end is not None:
            line += f", до {dates.fmt_date(end)}"
        return line

    def refresh_services(self) -> None:
        """Показать оплаченные услуги; новые сверху."""
        current = None
        item = self.services_list.currentItem()
        if item is not None:
            current = item.data(Qt.ItemDataRole.UserRole)
        self.services_list.clear()
        services = (p for p in self.store.payments if p.what in SERVICES)
        for payment in sorted(services, key=lambda p: (p.day, p.uid),
                              reverse=True):
            item = QListWidgetItem(self.service_line(payment))
            item.setData(Qt.ItemDataRole.UserRole, payment.uid)
            self.services_list.addItem(item)
        if current is not None:
            for row in range(self.services_list.count()):
                item = self.services_list.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == current:
                    self.services_list.setCurrentRow(row)
                    break

    def selected_service(self) -> data.Payment | None:
        """Услуга, выбранная в списке услуг."""
        item = self.services_list.currentItem()
        if item is None:
            return None
        return self.store.payment_by_uid(
            item.data(Qt.ItemDataRole.UserRole))

    def open_service_card(self, item: QListWidgetItem) -> None:
        """Из списка услуг — в карточку ученика, которому услуга."""
        payment = self.store.payment_by_uid(
            item.data(Qt.ItemDataRole.UserRole))
        if payment is None:
            return
        student = self.store.find_student(payment.student)
        if student is None:
            announce(self, f"Ученик {payment.student} не найден в списке.",
                     assertive=True)
            return
        self.select_uid(student.uid)
        self.open_card()

    def services_menu(self, lst: QListWidget, pos) -> None:
        """Меню услуги: правый щелчок или клавиша «Меню».

        Клавиатура присылает точку в центре списка, а не на строке, —
        тогда меню открывается у выбранной услуги. «Добавить» есть
        всегда, остальные пункты — когда услуга выбрана.
        """
        item = lst.itemAt(pos)
        if item is None:
            item = lst.currentItem()
            if item is not None:
                pos = lst.visualItemRect(item).center()
        payment = None
        if item is not None:
            payment = self.store.payment_by_uid(
                item.data(Qt.ItemDataRole.UserRole))
            lst.setCurrentItem(item)
        menu = QMenu(lst)
        menu.setAccessibleName(
            f"Услуга {payment.what}" if payment is not None else "Услуги")
        add = menu.addAction("Добавить услугу")
        edit = remove = None
        if payment is not None:
            menu.addSeparator()
            edit = menu.addAction("Редактировать")
            remove = menu.addAction("Удалить")
        chosen = menu.exec(lst.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is add:
            self.add_service()
        elif chosen is edit:
            self.edit_service(payment)
        elif chosen is remove:
            self.delete_service(payment)

    def add_service(self) -> None:
        """Новая услуга со вкладки услуг: ученика выбираем в форме."""
        if not self.store.students:
            announce(self, "Учеников нет. Сначала добавь ученика на вкладке "
                           "Ученики.", assertive=True)
            return
        dialog = ServiceDialog(self.store, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted \
                or dialog.payment is None:
            announce(self, "Услуга не добавлена.")
            return
        payment = dialog.payment
        self.refresh_services()
        announce(self, f"Услуга {payment.what.casefold()} для "
                       f"{payment.student} записана, "
                       f"{rubles(payment.amount)}.", assertive=True)

    def edit_service(self, payment: data.Payment | None = None) -> None:
        """Правка услуги: из меню или для выбранной в списке."""
        if payment is None:
            payment = self.selected_service()
        if payment is None:
            announce(self, "Сначала выбери услугу в списке на вкладке "
                           "Услуги.", assertive=True)
            return
        dialog = ServiceDialog(self.store, parent=self, payment=payment)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Правка отменена.")
            return
        self.refresh_services()
        announce(self, f"Услуга сохранена: {self.service_line(payment)}.",
                 assertive=True)

    def delete_service(self, payment: data.Payment | None = None) -> None:
        """Удалить услугу из журнала оплат — с подтверждением."""
        if payment is None:
            payment = self.selected_service()
        if payment is None:
            announce(self, "Сначала выбери услугу в списке на вкладке "
                           "Услуги.", assertive=True)
            return
        text = (f"Удалить услугу {payment.what.casefold()} для "
                f"{payment.student} от {dates.fmt_date(payment.day)}, "
                f"{rubles(payment.amount)}? Запись пропадёт из журнала "
                f"оплат.")
        box = QMessageBox(self)
        box.setWindowTitle("Удалить услугу")
        box.setText(text)
        yes = box.addButton("Удалить", QMessageBox.ButtonRole.DestructiveRole)
        no = box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        announce(self, text, assertive=True)
        box.exec()
        if box.clickedButton() is not yes:
            announce(self, "Удаление отменено.")
            return
        self.store.delete_payment(payment)
        self.refresh_services()
        announce(self, f"Услуга {payment.what.casefold()} для "
                       f"{payment.student} удалена.", assertive=True)

    def refresh_all(self) -> None:
        self.refresh_students()
        self.refresh_schedule()
        self.refresh_blocks()
        self.refresh_subscriptions()
        self.refresh_services()
        self.refresh_notifications()
        self.update_tab_visibility()

    def update_tab_visibility(self) -> None:
        """Спрятать вкладки, в которых сейчас нечего показывать.

        Ученики, расписание, действующие абонементы и услуги остаются
        всегда: на них лежат кнопки, которыми заводят новые записи.
        Остальные пустые вкладки не мешают переключаться.
        """
        pairs = (
            ("finished_subs_page", "finished_subs_list"),
            ("one_off_subs_page", "one_off_subs_list"),
            ("future_subs_page", "future_subs_list"),
            ("notifications_page", "notifications_list"),
            ("blocks_page", "blocks_list"),
        )
        for page_name, list_name in pairs:
            page = getattr(self, page_name, None)
            item_list = getattr(self, list_name, None)
            if page is None or item_list is None:
                continue
            index = self.tabs.indexOf(page)
            if index < 0:
                continue
            self.tabs.setTabVisible(index, item_list.count() > 0)

    def selected_uid(self) -> int | None:
        item = self.students_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def selected_student(self) -> data.Student | None:
        uid = self.selected_uid()
        for student in self.store.students:
            if student.uid == uid:
                return student
        return None

    def select_uid(self, uid: int | None) -> None:
        """Поставить выбор на ученика с этим номером, если он в списке."""
        for row in range(self.students_list.count()):
            item = self.students_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == uid:
                self.students_list.setCurrentRow(row)
                item.setSelected(True)
                return

    # --- клавиши и настройки ---------------------------------------------

    def update_hints(self) -> None:
        """Кнопки называют свои клавиши; длинных подсказок внизу нет.

        Всё на экране и так подписано, диктор читает название элемента.
        Абзацы под списками он читал поверх этого и только путал.
        """
        for hint in (self.students_hint, self.schedule_hint,
                     self.active_subs_hint, self.finished_subs_hint,
                     self.future_subs_hint, self.one_off_subs_hint,
                     self.services_hint, self.blocks_hint):
            hint.setText("")
        start_keys = norm_keys(self.bindings.get("start", ""))
        self.start_button.setText(
            f"Начать урок ({start_keys})" if start_keys else "Начать урок")
        card_keys = norm_keys(self.bindings.get("card", ""))
        self.card_button.setText(
            f"Карточка ученика ({card_keys})" if card_keys
            else "Карточка ученика")
        settings_keys = norm_keys(self.bindings.get("settings", ""))
        self.settings_button.setText(
            f"Настройки ({settings_keys})" if settings_keys else "Настройки")
        self.update_cues_button()
        self._update_order_buttons()

    def update_cues_button(self) -> None:
        """Кнопка называет своё состояние словами — диктор прочитает его."""
        text = ("Подсказки времени: включены" if self.cues
                else "Подсказки времени: выключены")
        self.cues_button.setText(text)

    def install_shortcuts(self) -> None:
        """Собрать горячие клавиши заново по текущим настройкам.

        Родные ярлыки Qt (QShortcut) понимают раскладку клавиатуры,
        цифровую клавиатуру и всплывающие окна. Пока открыты настройки,
        они выключены: иначе назначение клавиши запускало бы команду.
        """
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self._shortcuts = []
        enabled = not self._settings_open
        # Какие цифры верхнего ряда уже заняты: запасной ярлык для цифро-
        # вого блока не должен перебивать собственное назначение человека.
        plain_used = set()
        for code, _title, _keys in settings.ALL_ACTIONS:
            parsed = digit_binding(self.bindings.get(code, ""))
            if parsed is not None and not parsed[2] and parsed[0] & COMMAND_MODS:
                plain_used.add((parsed[0], parsed[1]))
        for code, _title, _keys in settings.ALL_ACTIONS:
            raw = self.bindings.get(code, "")
            parsed = digit_binding(raw)
            if parsed is not None and parsed[2]:
                # Цифровой блок ловит фильтр событий главного окна: так
                # сочетание работает при любом положении Num Lock. Запасной
                # ярлык — та же цифра верхнего ряда: если пометка блока до
                # фильтра не дошла, Qt всё равно узнает сочетание.
                mods, digit, _keypad = parsed
                if (mods, digit) in plain_used:
                    continue
                sequence = QKeySequence(
                    combo_text(mods, int(Qt.Key.Key_0) + digit))
            else:
                text = norm_keys(raw)
                if not text:
                    continue
                sequence = QKeySequence(text)
            if sequence.isEmpty():
                continue
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(lambda c=code: self.hotkey(c))
            shortcut.setEnabled(enabled)
            self._shortcuts.append(shortcut)

    def apply_bindings(self, bindings: dict[str, str]) -> None:
        self.bindings = dict(bindings)
        self.install_shortcuts()
        self.update_hints()

    def apply_volume(self, volume: int) -> None:
        self.volume = settings.volume_value(volume)

    def apply_cue_volume(self, volume: int) -> None:
        self.cue_volume = settings.cue_volume_value(volume)

    # --- подсказки времени урока ------------------------------------------

    def toggle_cues(self) -> None:
        """Включить или выключить подсказки времени и запомнить выбор."""
        self.cues = not self.cues
        try:
            settings.save(self.store.folder, self.bindings, self.volume,
                          self.cue_volume, self.cues)
        except OSError:
            announce(self, "Не получилось сохранить настройку.",
                     assertive=True)
            return
        self.update_cues_button()
        # Отсчёт всегда от начала текущего урока, а не от момента щелчка:
        # включил посреди урока — прошлые минуты не догоняем пачкой.
        self._cue_anchor = None
        self._cue_done.clear()
        if not self.cues:
            announce(self, "Звуковые подсказки времени выключены.")
            return
        if self.cue_volume == 0:
            announce(self, "Подсказки времени включены, но их громкость "
                           "ноль. Поставь громкость в настройках.",
                     assertive=True)
            return
        announce(self, "Звуковые подсказки времени включены. Сигналы "
                       "прозвучат через десять, двадцать, тридцать и "
                       "сорок минут от начала урока.", assertive=True)

    def check_cues(self) -> None:
        """Раз в секунду: не пора ли подать подсказку времени."""
        if not self.cues:
            self._cue_anchor = None
            self._cue_done.clear()
            return
        anchor = self.reminders.first_start()
        if anchor is None:
            self._cue_anchor = None
            self._cue_done.clear()
            return
        elapsed = (datetime.now() - anchor).total_seconds()
        if self._cue_anchor != anchor:
            # Урок только начался или подсказки включили на ходу: то, что
            # уже прошло, считаем пройденным — сигнал нужен заранее,
            # а не пачкой задним числом.
            self._cue_anchor = anchor
            self._cue_done = {mark for mark, _kind in CUE_MARKS
                              if elapsed >= mark * 60}
            return
        for mark, kind in CUE_MARKS:
            if mark in self._cue_done or elapsed < mark * 60:
                continue
            self._cue_done.add(mark)
            self.play_cue(kind)

    def play_cue(self, kind: str) -> None:
        folder = self.store.folder / "Sound"
        if not sounds.play(kind, folder, self.cue_volume):
            QApplication.beep()

    def open_settings(self) -> None:
        # Окно настроек модальное, но ярлыки живут на всё приложение:
        # пока оно открыто, они выключены, чтобы новая клавиша не
        # сработала командой в момент назначения.
        if getattr(self, "_settings_open", False):
            return
        self._settings_open = True
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
        try:
            dialog = SettingsDialog(self.store, self.bindings, self.volume,
                                    self.cue_volume, self)
            dialog.bindings_changed.connect(self.apply_bindings)
            dialog.volume_changed.connect(self.apply_volume)
            dialog.cue_volume_changed.connect(self.apply_cue_volume)
            dialog.restored.connect(self.after_restore)
            dialog.cloud_changed.connect(self.reload_cloud)
            announce(dialog, f"Настройки. Версия программы {__version__}.")
            dialog.exec()
        finally:
            self._settings_open = False
            self.install_shortcuts()
        self.students_list.setFocus()

    # --- действия ---------------------------------------------------------

    def add_student(self) -> None:
        dialog = StudentDialog(self.store, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.created is None:
            return
        student = dialog.created
        self.refresh_all()
        for row in range(self.students_list.count()):
            item = self.students_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == student.uid:
                self.students_list.setCurrentRow(row)
                item.setSelected(True)
                break
        self.students_list.setFocus()
        used, total = self.store.balance(student.name)
        text = f"{student.name} добавлен."
        if total:
            text += f" Уроки {used} из {total}."
        announce(self, text, assertive=True)

    def pick_student(self, title: str, hint: str) -> data.Student | None:
        """Спросить, кого именно имел в виду: список и кнопка «Дальше»."""
        if not self.store.students:
            announce(self, "В списке пока нет учеников.")
            return None
        dialog = StudentPickDialog(self.store, title, hint, self)
        announce(dialog, hint, assertive=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.picked

    def edit_student(self) -> None:
        student = self.pick_student(
            "Редактировать ученика",
            "Выбери ученика, которого надо изменить, и нажми «Дальше».")
        if student is None:
            return
        dialog = StudentDialog(self.store, self, student)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.refresh_all()
        self.select_uid(student.uid)
        self.students_list.setFocus()
        announce(self, f"{student.name} сохранён.", assertive=True)

    def remove_student(self) -> None:
        student = self.pick_student(
            "Удалить ученика",
            "Выбери ученика, которого надо удалить, и нажми «Дальше».")
        if student is None:
            return
        text = (f"Удалить ученика {student.name}? Вместе с ним пропадут "
                f"его абонементы и уроки, в том числе в расписании. "
                f"Оплаты в журнале останутся.")
        box = QMessageBox(self)
        box.setWindowTitle("Удалить ученика")
        box.setText(text)
        yes = box.addButton("Удалить", QMessageBox.ButtonRole.DestructiveRole)
        no = box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        announce(self, text, assertive=True)
        box.exec()
        if box.clickedButton() is not yes:
            announce(self, "Удаление отменено.")
            return
        self.store.remove_student(student)
        self.refresh_all()
        self.students_list.setFocus()
        announce(self, f"{student.name} удалён вместе с абонементами, "
                       f"уроками и записью в расписании.", assertive=True)

    def open_card(self) -> None:
        student = self.selected_student()
        if student is None:
            announce(self, "Сначала выбери ученика в списке.")
            return
        card = StudentCard(self.store, student, self)
        announce(card, card.card_text())
        card.exec()
        if card.changed:
            self.refresh_all()
        self.students_list.setFocus()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt)
        """Цифровой блок: поймать сочетание до окон, кнопок и полей.

        Num Lock включён — Qt присылает цифру; выключен — ту же клавишу
        как стрелку или End. Здесь цифровой блок узнаётся и в том, и в
        другом положении, поэтому Ctrl с цифрой работает всегда.
        """
        if (event.type() == QEvent.Type.KeyPress
                and not self._settings_open
                and QApplication.activeModalWidget() in (None, self)):
            action = self.numpad_action(event)
            if action:
                self.hotkey(action)
                return True
        return super().eventFilter(obj, event)

    def numpad_action(self, event) -> str | None:
        """Действие, назначенное на это сочетание цифрового блока.

        Цифру блока узнаём по скан-коду, пометке Qt или по самой цифре —
        см. numpad_index. Назначение разбираем на модификаторы и цифру
        (digit_binding) и сравниваем напрямую: так неважно, как именно Qt
        записал сочетание в файле настроек. Если нажата не цифра блока,
        а обычная цифра верхнего ряда, первыми проверяются назначения на
        верхний ряд, и только потом — на блок.
        """
        mods = mods_value(event.modifiers()) & ALL_MODS
        if not mods & COMMAND_MODS:
            return None
        digit, keypad = numpad_index(event)
        if digit is None:
            return None
        exact: list[str] = []
        spare: list[str] = []
        for code, _title, _keys in settings.ALL_ACTIONS:
            parsed = digit_binding(self.bindings.get(code, ""))
            if parsed is None:
                continue
            bind_mods, bind_digit, bind_keypad = parsed
            if bind_digit != digit or bind_mods != mods:
                continue
            if bind_keypad == keypad:
                exact.append(code)
            else:
                spare.append(code)
        for code in exact + spare:
            return code
        return None

    def show_tab(self, page, item_list=None) -> None:
        """Открыть вкладку по клавишам и поставить диктора на её список."""
        index = self.tabs.indexOf(page)
        if index < 0:
            return
        if not self.tabs.isTabVisible(index):
            announce(self, "Эта вкладка сейчас пуста и спрятана.",
                     assertive=True)
            return
        self.tabs.setCurrentIndex(index)
        title = self.tabs.tabText(index)
        if item_list is not None and item_list.count():
            item_list.setFocus()
            current = item_list.currentItem()
            if current is not None:
                announce(self, f"Вкладка {title}. {current.text()}")
                return
        announce(self, f"Вкладка {title}.")

    def hotkey(self, action: str) -> None:
        """Действие по коду из настроек — клавиши человек выбирает сам."""
        if action == "card":
            self.open_card()
        elif action == "today":
            self.speak_day(date.today(), "на сегодня")
        elif action == "tomorrow":
            self.speak_day(date.today() + timedelta(days=1), "на завтра")
        elif action == "earnings":
            self.speak_earnings()
        elif action == "export":
            self.export_text()
        elif action == "subexport":
            self.export_subscription()
        elif action == "past_lessons":
            self.speak_past_lessons()
        elif action == "planned_lessons":
            self.speak_planned_lessons()
        elif action == "settings":
            self.open_settings()
        # Кнопки на вкладках — то же самое, что щелчок по кнопке.
        elif action == "start":
            self.start_lesson()
        elif action == "add_student":
            self.add_student()
        elif action == "edit_student":
            self.edit_student()
        elif action == "remove_student":
            self.remove_student()
        elif action == "toggle_cues":
            self.toggle_cues()
        elif action == "trial":
            self.add_trial_lesson()
        elif action == "single":
            self.add_single_lesson()
        elif action == "sub_add":
            self.add_subscription()
        elif action == "sub_done":
            self.add_done_lesson()
        elif action == "sub_edit":
            self.edit_subscription()
        elif action == "sub_remove":
            self.delete_subscription()
        elif action == "sub_order":
            self.toggle_subs_order()
        elif action == "service_add":
            self.add_service()
        elif action == "service_edit":
            self.edit_service()
        elif action == "service_remove":
            self.delete_service()
        elif action == "block_add":
            self.add_exception()
        # Вкладки главного окна.
        elif action == "tab_students":
            self.show_tab(self.students_page, self.students_list)
        elif action == "tab_schedule":
            self.show_tab(self.schedule_page, self.schedule_list)
        elif action == "tab_active":
            self.show_tab(self.active_subs_page, self.active_subs_list)
        elif action == "tab_finished":
            self.show_tab(self.finished_subs_page, self.finished_subs_list)
        elif action == "tab_one_off":
            self.show_tab(self.one_off_subs_page, self.one_off_subs_list)
        elif action == "tab_services":
            self.show_tab(self.services_page, self.services_list)
        elif action == "tab_notifications":
            self.show_tab(self.notifications_page, self.notifications_list)
        elif action == "tab_blocks":
            self.show_tab(self.blocks_page, self.blocks_list)
        elif action == "tab_future":
            self.show_tab(self.future_subs_page, self.future_subs_list)

    def speak_day(self, day: date, what: str) -> None:
        lessons = [l for l in self.store.lessons
                   if l.day == day and l.status != LESSON_CANCELLED]
        lessons.sort(key=lambda l: l.stamp)
        head = f"Уроки {what}, {dates.human_date(day)}."
        if not lessons:
            announce(self, head + " Уроков нет.", assertive=True)
            return
        word = plural(len(lessons), "урок", "урока", "уроков")
        pieces = [head + f" Всего {len(lessons)} {word}."]
        for lesson in lessons:
            pieces.append(f"В {lesson.time}, {lesson.student}, "
                          f"{lesson.direction.casefold()}, {lesson.status}.")
        announce(self, " ".join(pieces), assertive=True)

    def speak_earnings(self) -> None:
        today = date.today()
        parts = self.store.earnings_in_month(today.year, today.month)
        earned = sum(parts.values())
        done = [l for l in self.store.lessons
                if l.day.month == today.month and l.day.year == today.year
                and l.status == LESSON_DONE]
        lessons_word = plural(len(done), "урок", "урока", "уроков")
        # Разбивка по источникам: слышно, из чего сложилась сумма и нет
        # ли в ней пробного урока.
        pieces = []
        if parts["active"]:
            pieces.append(f"действующие абонементы {rubles(parts['active'])}")
        if parts["finished"]:
            pieces.append(f"законченные абонементы "
                          f"{rubles(parts['finished'])}")
        if parts["future"]:
            pieces.append(f"будущие абонементы {rubles(parts['future'])}")
        if parts["single"]:
            pieces.append(f"разовые уроки {rubles(parts['single'])}")
        if parts["service"]:
            pieces.append(f"услуги {rubles(parts['service'])}")
        if parts["other"]:
            pieces.append(f"прочие поступления {rubles(parts['other'])}")
        breakdown = (" В том числе: " + ", ".join(pieces) + ".") if pieces else ""
        announce(
            self,
            f"За {dates.MONTHS_NOM[today.month - 1]} заработано "
            f"{rubles(earned)}.{breakdown} "
            f"Проведено {len(done)} {lessons_word}.",
            assertive=True)

    def export_text(self) -> None:
        """F9 — выгрузить все данные читаемым текстом в папку Data."""
        try:
            path = self.store.export_text()
        except OSError:
            announce(self, "Не получилось выгрузить данные в текст.",
                     assertive=True)
            return
        announce(self, f"Данные выгружены в файл {path.name}, "
                       f"он лежит в папке Дата.", assertive=True)

    # --- напоминания ------------------------------------------------------

    def _play_then(self, kind: str, action) -> None:
        """Сначала сигнал, потом диктор.

        Одновременно они перебивают друг друга: диктор заговаривает
        сигнал с первых секунд. Поэтому ждём, пока файл отзвучит, и
        только потом начинаем говорить. Громкость ноль — без задержки.
        """
        level = self.volume
        folder = self.store.folder / "Sound"
        if level > 0 and not sounds.play(kind, folder, level):
            QApplication.beep()
        wait = sounds.length(kind, folder) + 0.15 if level > 0 else 0.0
        if wait <= 0:
            action()
            return
        QTimer.singleShot(int(wait * 1000), action)

    def notify(self, text: str) -> None:
        """Сказать вслух и, если окно свёрнуто, показать в трее.

        Всплывающие окна забирали фокус у скринридера, и попасть на них
        было нельзя. Теперь фраза уходит в диктор, а когда программа
        свёрнута — ещё и сообщением трея: NVDA читает его и из трея.
        Всё, что не подтвердили, остаётся на вкладке «Уведомления».
        """
        announce(self, text, assertive=True)
        # Сообщение трея — только когда окно не на экране. Иначе диктор
        # читал одно и то же дважды: объявление и всплывающую подсказку.
        tray = getattr(self, "tray", None)
        hidden = self.isMinimized() or not self.isVisible()
        if tray is not None and tray.isVisible() and hidden:
            tray.showMessage("Мажорный лад Расписание", text,
                             QSystemTrayIcon.MessageIcon.Information, 8000)
        self.refresh_notifications()

    def on_soon(self, lesson: data.Lesson, kind: str) -> None:
        text = soon_text(lesson, kind)
        self._play_then("soon", lambda: self.notify(text))

    def on_service_soon(self, payment: data.Payment, end, days: int) -> None:
        text = service_text(payment, end, days)
        self._play_then("soon", lambda: self.notify(text))

    def on_ask_finish(self, lesson: data.Lesson) -> None:
        text = (f"Вы подтверждаете, что урок прошёл? "
                f"{lesson.student}, в {lesson.time}. Подтвердить можно "
                f"на вкладке «Уведомления».")
        # Если это последний урок абонемента — сначала весёлый сигнал и
        # подсказка про подарок, и только потом обычный вопрос.
        sub = self.store.current_subscription(lesson.student)
        last = sub is not None and sub.total > 0 and sub.left == 1

        if not last:
            self._play_then("finish", lambda: self.notify(text))
            return

        def promo() -> None:
            announce(self, LAST_LESSON_TEXT, assertive=True)
            # Даём диктору проговорить фразу целиком, потом обычный вопрос.
            QTimer.singleShot(3000, lambda: self._play_then(
                "finish", lambda: self.notify(text)))

        self._play_then("last", promo)

    # --- уведомления ------------------------------------------------------

    DISMISSED_FILE = "Уведомления.txt"

    def load_dismissed(self) -> set[str]:
        """Ключи подтверждённых уведомлений, что помним между запусками."""
        path = self.store.folder / self.DISMISSED_FILE
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            return set()
        return {line.strip() for line in text.splitlines() if line.strip()}

    def save_dismissed(self) -> None:
        """Записать подтверждённые ключи. Старые обрезаем, чтобы файл не рос."""
        keys = sorted(self._dismissed)[-1000:]
        path = self.store.folder / self.DISMISSED_FILE
        try:
            path.write_text("\n".join(keys) + "\n", encoding="utf-8")
        except OSError:
            pass

    # --- исключения ------------------------------------------------------

    def _blocks_tab(self) -> None:
        """Вкладка исключений: дни и часы, когда уроков нет."""
        page = QWidget()
        lay = QVBoxLayout(page)
        self.blocks_hint = QLabel()
        self.blocks_hint.setWordWrap(True)
        lay.addWidget(self.blocks_hint)
        self.blocks_list = QListWidget()
        self.blocks_list.setAccessibleName("Исключения")
        lay.addWidget(self.blocks_list, 1)

        row = QHBoxLayout()
        self.block_day_button = QPushButton("Добавить день")
        self.block_day_button.clicked.connect(
            lambda _checked=False: self.add_block_day())
        row.addWidget(self.block_day_button)
        self.block_time_button = QPushButton("Добавить время")
        self.block_time_button.clicked.connect(
            lambda _checked=False: self.add_block_time())
        row.addWidget(self.block_time_button)
        self.block_remove_button = QPushButton("Удалить исключение")
        self.block_remove_button.clicked.connect(
            lambda _checked=False: self.remove_block())
        row.addWidget(self.block_remove_button)
        row.addStretch(1)
        lay.addLayout(row)

        self.blocks_page = page
        self.tabs.addTab(page, "Исключения")

    def block_line(self, block: data.Block) -> str:
        text = dates.human_date(block.day)
        if block.all_day:
            text += " — весь день без уроков"
        else:
            text += f", с {block.start} до {block.end}"
        if block.note:
            text += f". Причина: {block.note}"
        return text + "."

    def refresh_blocks(self) -> None:
        """Список исключений: с сегодняшнего дня и дальше."""
        self.blocks_list.clear()
        for block in self.store.blocks_between(date.today(), date.max):
            item = QListWidgetItem(self.block_line(block))
            item.setData(Qt.ItemDataRole.UserRole, block.uid)
            self.blocks_list.addItem(item)
        self.update_tab_visibility()

    def selected_block(self) -> data.Block | None:
        item = self.blocks_list.currentItem()
        if item is None:
            return None
        uid = item.data(Qt.ItemDataRole.UserRole)
        for block in self.store.blocks:
            if block.uid == uid:
                return block
        return None

    def add_block_day(self) -> None:
        dialog = BlockDialog(whole_day=True, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if self.store.overlapping_block(dialog.day) is not None:
            announce(self, "На этот день исключение уже стоит.",
                     assertive=True)
            return
        block = self.store.add_block(dialog.day, note=dialog.note)
        self.refresh_blocks()
        announce(self, f"День закрыт: {dates.human_date(block.day)}. "
                       "Записаться на него нельзя.")

    def add_block_time(self) -> None:
        dialog = BlockDialog(whole_day=False, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if self.store.overlapping_block(dialog.day, dialog.start,
                                        dialog.end) is not None:
            announce(self, "Это время уже закрыто исключением.",
                     assertive=True)
            return
        block = self.store.add_block(dialog.day, dialog.start, dialog.end,
                                     dialog.note)
        self.refresh_blocks()
        announce(self, f"Закрыто {dates.human_date(block.day)}, с "
                       f"{block.start} до {block.end}. Записаться на это "
                       "время нельзя.")

    def add_exception(self) -> None:
        """Кнопка во вкладке «Ученики»: исключение на день или на часы.

        То же самое, что делать это во вкладке «Исключения», только не
        нужно туда переключаться: окно спрашивает дату и, если нужны
        часы, переключатель «Весь день» снимается.
        """
        dialog = BlockDialog(whole_day=True, parent=self, flexible=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.whole_day:
            if self.store.overlapping_block(dialog.day) is not None:
                announce(self, "На этот день исключение уже стоит.",
                         assertive=True)
                return
            block = self.store.add_block(dialog.day, note=dialog.note)
            text = (f"День закрыт: {dates.human_date(block.day)}. "
                    "Записаться на него нельзя.")
        else:
            if self.store.overlapping_block(dialog.day, dialog.start,
                                            dialog.end) is not None:
                announce(self, "Это время уже закрыто исключением.",
                         assertive=True)
                return
            block = self.store.add_block(dialog.day, dialog.start,
                                         dialog.end, dialog.note)
            text = (f"Закрыто {dates.human_date(block.day)}, с "
                    f"{block.start} до {block.end}. Записаться на это "
                    "время нельзя.")
        self.refresh_blocks()
        self.refresh_schedule()
        announce(self, text, assertive=True)

    def remove_block(self) -> None:
        block = self.selected_block()
        if block is None:
            announce(self, "Сначала выбери исключение в списке.",
                     assertive=True)
            return
        self.store.delete_block(block)
        self.refresh_blocks()
        announce(self, "Исключение убрано: записаться снова можно.")

    def _notifications_tab(self) -> None:
        """Вкладка со всем, что ещё не подтверждено."""
        page = QWidget()
        lay = QVBoxLayout(page)
        self.notifications_hint = QLabel()
        self.notifications_hint.setWordWrap(True)
        lay.addWidget(self.notifications_hint)
        self.notifications_list = QListWidget()
        self.notifications_list.setAccessibleName("Уведомления")
        lay.addWidget(self.notifications_list, 1)
        row = QHBoxLayout()
        self.confirm_button = QPushButton("Подтвердить")
        self.confirm_button.clicked.connect(
            lambda _checked=False: self.confirm_notification())
        row.addWidget(self.confirm_button)
        row.addStretch(1)
        lay.addLayout(row)
        self.notifications_page = page
        self.tabs.addTab(page, "Уведомления")

    def pending_notifications(self) -> list[tuple[str, str, str, object]]:
        """Что ждёт подтверждения: ключ, вид, фраза и запись.

        Список собирается из состояния, а не хранится: напоминание до
        урока, вопрос о конце урока и срок услуги живут, пока это правда.
        Подтверждённое помним по ключу, поэтому оно не возвращается.
        """
        now = datetime.now()
        today = now.date()
        result: list[tuple[str, str, str, object]] = []
        for lesson in self.store.lessons:
            if lesson.status != LESSON_WAIT:
                continue
            stamp = lesson.stamp
            # Напоминания до урока нужны, пока урок не начался: после
            # начала вопрос уже о конце урока.
            if now < stamp:
                # Одна строка на урок: из напоминаний берём самое свежее,
                # которое уже сработало. Иначе за сутки до урока в списке
                # висели бы три строки об одном и том же.
                latest = None
                for kind, delta in KINDS:
                    if stamp - delta <= now:
                        latest = kind
                if latest is not None:
                    key = f"soon:{lesson.uid}:{latest}"
                    if key not in self._dismissed:
                        result.append((key, "soon",
                                       soon_text(lesson, latest), lesson))
            key = f"finish:{lesson.uid}"
            started = self.reminders.started_at(lesson)
            # Урок, начатый в этой сессии, спрашиваем до подтверждения,
            # сколько бы времени ни прошло. Для старых уроков окно в
            # шесть часов — иначе вкладка забилась бы давно прошедшими.
            explicit = started is not None
            if started is None and stamp <= now:
                started = stamp
            if (key not in self._dismissed and started is not None
                    and started + LESSON_LENGTH <= now
                    and (explicit or now < started + LATE_WINDOW)):
                result.append((
                    key, "finish",
                    f"Урок прошёл? {lesson.student}, в {lesson.time}.", lesson))
        window = max(services.DAYS_BEFORE)
        for payment in self.store.payments:
            end = services.service_end(payment)
            if end is None:
                continue
            days = (end - today).days
            key = f"service:{payment.uid}:{end.isoformat()}"
            if key in self._dismissed or days < 0 or days > window:
                continue
            result.append((key, "service",
                           service_text(payment, end, days), payment))
        return result

    def refresh_notifications(self) -> None:
        """Обновить список и число неподтверждённых на ярлыке вкладки."""
        if not hasattr(self, "notifications_list"):
            return
        current = None
        item = self.notifications_list.currentItem()
        if item is not None:
            current = item.data(Qt.ItemDataRole.UserRole)
        self.notifications_list.clear()
        pending = self.pending_notifications()
        for key, kind, text, _record in pending:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setData(Qt.ItemDataRole.UserRole + 1, kind)
            self.notifications_list.addItem(item)
        if current is not None:
            for row in range(self.notifications_list.count()):
                if (self.notifications_list.item(row)
                        .data(Qt.ItemDataRole.UserRole) == current):
                    self.notifications_list.setCurrentRow(row)
                    break
        index = self.tabs.indexOf(self.notifications_page)
        self.tabs.setTabText(
            index, "Уведомления" if not pending
            else f"Уведомления ({len(pending)})")
        self.notifications_hint.setText("")
        self.update_tab_visibility()

    def confirm_notification(self) -> None:
        """Подтвердить выбранное уведомление: оно уходит из списка."""
        item = self.notifications_list.currentItem()
        if item is None:
            announce(self, "В списке уведомлений ничего не выбрано.",
                     assertive=True)
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        kind = item.data(Qt.ItemDataRole.UserRole + 1)
        record = None
        for pending_key, _kind, _text, pending_record in \
                self.pending_notifications():
            if pending_key == key:
                record = pending_record
                break
        if record is None:
            self.refresh_notifications()
            return
        if kind == "finish":
            lesson = record
            lesson.status = LESSON_DONE
            sub = self.store.current_subscription(lesson.student)
            if sub is not None:
                lesson.subscription = sub.uid
            self.store.update_lesson(lesson)
            if sub is not None:
                self.store.bump_used(sub)
            self.reminders.mark_finished(lesson)
            used, total = self.store.balance(lesson.student)
            tail = f" Уроки {used} из {total}." if total else ""
            speech = f"{lesson.student}, урок проведён.{tail}"
        elif kind == "soon":
            lesson = record
            speech = f"Напоминание подтверждено. {lesson.student}, в {lesson.time}."
        else:
            payment = record
            speech = f"Напоминание об услуге подтверждено. {payment.student}."
        self._dismissed.add(key)
        self.save_dismissed()
        self.refresh_all()
        announce(self, speech, assertive=True)

    # --- начало урока -----------------------------------------------------

    def start_lesson(self) -> None:
        """Начать урок выбранного ученика прямо сейчас.

        Урок стартует сам по расписанию; кнопка нужна, если ученик
        пришёл раньше или чуть позже. От нажатия считаются подсказки
        времени и вопрос о конце урока. В расписании урок остаётся на
        своём месте.
        """
        student = self.selected_student()
        if student is None:
            announce(self, "Сначала выбери ученика в списке.",
                     assertive=True)
            return
        now = datetime.now()
        waiting = [lesson for lesson in self.store.lessons
                   if lesson.student == student.name
                   and lesson.status == LESSON_WAIT
                   and lesson.day == now.date()]
        if not waiting:
            announce(self, f"На сегодня у {student.name} урока в "
                           f"расписании нет.", assertive=True)
            return
        lesson = min(waiting,
                     key=lambda l: (abs((l.stamp - now).total_seconds()),
                                    l.stamp))
        self.reminders.mark_started(lesson, now)
        # Подсказки времени пойдут от нажатия, а не от расписания.
        self._cue_anchor = None
        self._cue_done.clear()
        self.refresh_notifications()
        announce(self, f"Сейчас {now:%H:%M}. Урок начат. {student.name}, "
                       f"по расписанию в {lesson.time}. Через сорок пять "
                       f"минут спрошу, прошёл ли урок.", assertive=True)

    # --- трей -------------------------------------------------------------

    def setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("Мажорный лад Расписание")
        menu = QMenu(self)
        show = QAction("Открыть", self)
        show.triggered.connect(self.show_window)
        settings_action = QAction("Настройки", self)
        settings_action.triggered.connect(self.open_settings)
        quit_action = QAction("Выход", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(show)
        menu.addAction(settings_action)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.show_window()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()
        QApplication.setQuitOnLastWindowClosed(False)

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.students_list.setFocus()

    def bring_forward(self) -> None:
        """Показать окно и забрать фокус: тогда диалог прочитает диктор."""
        if self.isMinimized() or not self.isVisible():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self.tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt)
        # Закрытие окна не выключает напоминания: программа живёт в трее.
        event.ignore()
        self.hide()
        announce(self, "Программа свёрнута в трей. Напоминания продолжают работать.")


def run_installer(pid: int, target: Path) -> int:
    """Мы запущены как установщик обновления: поставить новую версию.

    Ждём, пока старая программа закроется и отпустит свой exe, ставим
    новую версию на её место и запускаем. Окна у установщика нет; если
    что-то не вышло — показываем одно окно с причиной, чтобы диктор её
    прочитал и старая версия осталась работать.
    """
    if not getattr(sys, "frozen", False):
        return 1
    try:
        updater.log(f"Установщик: запущен, цель {target}, жду pid {pid}.")
        updater.wait_previous(pid, timeout=120.0)
        updater.log("Установщик: прошлая программа закрылась.")
        ok, message = updater.finish_install(target)
    except Exception as error:  # noqa: BLE001 — установщик не падает молча
        updater.log(f"Установщик: сбой — {error!r}.")
        ok, message = False, f"Обновление не установилось: {error}"
    if ok:
        updater.cleanup_download()
        return 0
    app = QApplication(sys.argv)
    box = QMessageBox()
    box.setWindowTitle("Обновление не установилось")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText(message)
    box.setAccessibleName(message)
    box.exec()
    return 1


_SINGLE_MUTEX = None


def already_running() -> bool:
    """Программа уже запущена? Держим именованный мьютекс Windows.

    Две копии — это двойные напоминания и общий журнал, а ещё обновление
    не может подменить exe, пока его держит хоть одна копия. Мьютекс
    система отпускает сама, когда процесс закрывается, — даже если он
    упал.
    """
    global _SINGLE_MUTEX
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL,
                                      wintypes.LPCWSTR)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.CreateMutexW(None, False, "Local\\MajorLadSchedule")
    if not handle:
        return False
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return True
    _SINGLE_MUTEX = handle
    return False


def main() -> int:
    # Запуск при входе в Windows: ключ `--tray` — окно не открываем,
    # программа живёт в трее.
    background = autostart.TRAY_FLAG in sys.argv
    if background:
        sys.argv.remove(autostart.TRAY_FLAG)
    # Программу могли перенести или обновить — поправляем путь
    # в записи автозапуска, если он включён.
    autostart.refresh()
    # После обновления нас запускает новая версия с `--wait-pid N`:
    # ждём, пока прошлый процесс отпустит exe, и убираем его копию.
    pid = updater.take_wait_pid()
    target = updater.take_install_target()
    if target is not None:
        return run_installer(pid, target)
    updater.wait_previous(pid)
    updater.cleanup_backup()
    updater.cleanup_staged()
    sounds.ensure()

    app = QApplication(sys.argv)
    if already_running():
        box = QMessageBox()
        box.setWindowTitle("Мажорный лад Расписание")
        box.setIcon(QMessageBox.Icon.Information)
        text = ("Программа уже запущена. Посмотри в трее — это значок "
                "рядом с часами: щёлкни по нему и выбери «Открыть». "
                "Второй раз запускать не нужно.")
        box.setText(text)
        box.setAccessibleName(text)
        box.exec()
        return 0
    app.setApplicationName("Мажорный лад Расписание")
    app.setApplicationDisplayName("Мажорный лад Расписание")
    app.setOrganizationName("Мажорный лад")
    window = MainWindow(Store(), background=background)
    if not background:
        window.show()
        announce(window, "Мажорный лад Расписание открыта.")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
