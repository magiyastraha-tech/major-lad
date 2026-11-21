"""Главное окно и формы программы «Мажорный лад Расписание».

Правило одно и то же во всех окнах: после каждого действия — ровно одна
фраза диктору. Фокус при этом остаётся там, где ты стоял, поэтому можно
продолжать работать стрелками сразу после ответа.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QIcon,
                           QKeySequence, QPainter, QPixmap, QShortcut)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget, QSystemTrayIcon,
)

from . import __version__, dates, settings, sounds, store as data, updater
from .reminders import ReminderService, soon_text
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


def lesson_line(lesson: data.Lesson, with_status: bool = True) -> str:
    """Урок одной фразой: как его прочитает диктор."""
    parts = [dates.human_date(lesson.day), f"в {lesson.time}",
             lesson.student, lesson.direction.casefold(),
             lesson.lesson_format.casefold()]
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
    """Карточка нового ученика: имя, контакты, формат, направление, абонемент."""

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

        self.name = QLineEdit(student.name if student else "")
        self.name.setAccessibleName("Имя и фамилия")
        self.name.setPlaceholderText("Андрей Булкин")
        form.addRow("Имя и фамилия", self.name)

        self.phone = QLineEdit(student.phone if student else "")
        self.phone.setAccessibleName("Телефон")
        form.addRow("Телефон", self.phone)

        self.email = QLineEdit(student.email if student else "")
        self.email.setAccessibleName("Почта")
        form.addRow("Почта", self.email)

        self.format = QComboBox()
        self.format.addItems(FORMATS)
        self.format.setAccessibleName("Очно или онлайн")
        if student:
            self.format.setCurrentText(student.lesson_format)
        form.addRow("Очно или онлайн", self.format)

        self.direction = QComboBox()
        self.direction.addItems(DIRECTIONS)
        self.direction.setAccessibleName("Направление")
        if student:
            self.direction.setCurrentText(student.direction)
        form.addRow("Направление", self.direction)

        # Абонемент спрашивается только у нового ученика. У существующего
        # уроки добавляются кнопкой «Докупить уроки» в карточке — иначе
        # правка контактов молча обнуляла бы счётчик.
        self.lessons = None
        self.price = None
        self.start = None
        if adding:
            self.lessons = QSpinBox()
            self.lessons.setRange(0, 500)
            self.lessons.setAccessibleName("Сколько уроков куплено")
            self.lessons.setValue(8)
            form.addRow("Куплено уроков", self.lessons)

            self.price = QSpinBox()
            self.price.setRange(0, 500_000)
            self.price.setSingleStep(500)
            self.price.setSuffix(" ₽")
            self.price.setAccessibleName("Цена абонемента")
            form.addRow("Цена абонемента", self.price)

            # Покупка и начало абонемента могут быть разными днями: человек
            # оплатил сегодня, а уроки начинаются с понедельника.
            self.start = make_date_edit("С какого числа действует абонемент")
            form.addRow("Абонемент с числа", self.start)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Добавить ученика" if adding else "Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self.name.setFocus()

    def _accept(self) -> None:
        name = self.name.text().strip()
        if not name:
            announce(self, "Имя не заполнено. Напиши имя и фамилию ученика.")
            self.name.setFocus()
            return
        other = self.store.find_student(name)
        if other is not None and other is not self.student:
            announce(self, f"Ученик {name} уже есть в списке.")
            self.name.setFocus()
            return
        if self.student is None:
            student = self.store.add_student(
                name, phone=self.phone.text(), email=self.email.text(),
                lesson_format=self.format.currentText(),
                direction=self.direction.currentText())
            self.created = student
            count = self.lessons.value()
            if count > 0:
                price = self.price.value()
                self.store.add_subscription(
                    student.name, count, price=price,
                    direction=student.direction,
                    started=picked_date(self.start))
                if price > 0:
                    word = plural(count, "урок", "урока", "уроков")
                    self.store.add_payment(
                        student.name, f"Абонемент, {count} {word}", price)
            self.accept()
            return
        student = self.student
        old_name = student.name
        student.name = name
        student.phone = self.phone.text().strip()
        student.email = self.email.text().strip()
        student.lesson_format = self.format.currentText()
        student.direction = self.direction.currentText()
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


class TopUpDialog(QDialog):
    """Докупить уроки к действующему абонементу.

    Докупка не заводит второй абонемент: уроки прибавляются к текущему,
    поэтому счётчик остаётся понятным — «уроки 3 из 16». Деньги при этом
    уходят отдельной строкой в журнал оплат.
    """

    def __init__(self, student: str, used: int, total: int, parent=None):
        super().__init__(parent)
        self.lessons = 0
        self.price = 0
        self.started: date | None = None
        self.setWindowTitle("Докупить уроки")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        if total:
            text = (f"{student}. Сейчас уроки {used} из {total}. "
                    f"Сколько уроков докупить?")
        else:
            text = f"{student}. Абонемента нет. Сколько уроков купить?"
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAccessibleName(text)
        lay.addWidget(label)

        form = QFormLayout()
        self.count = QSpinBox()
        self.count.setRange(1, 500)
        self.count.setValue(8)
        self.count.setAccessibleName("Сколько уроков докупить")
        form.addRow("Уроков", self.count)

        self.money = QSpinBox()
        self.money.setRange(0, 500_000)
        self.money.setSingleStep(500)
        self.money.setSuffix(" ₽")
        self.money.setAccessibleName("Сумма оплаты")
        form.addRow("Оплата", self.money)

        # Начало абонемента спрашиваем, только когда абонемент заводится
        # с нуля: при докупке уроки прибавляются к уже идущему.
        self.start = None
        if not total:
            self.start = make_date_edit("С какого числа действует абонемент")
            form.addRow("Абонемент с числа", self.start)
        lay.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Докупить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.count.setFocus()
        self.count.selectAll()

    def accept(self) -> None:
        self.lessons = self.count.value()
        self.price = self.money.value()
        if self.start is not None:
            self.started = picked_date(self.start)
        super().accept()


class AddDateDialog(QDialog):
    """Один урок на выбранный день: день, час, минуты."""

    def __init__(self, store: Store, student: data.Student, parent=None):
        super().__init__(parent)
        self.store = store
        self.student = student
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

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Добавить дату")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._add)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _add(self) -> None:
        day = self.day.currentData()
        time = dates.fmt_time(self.hour.value(), int(self.minute.currentText()))
        self.store.add_lesson(
            self.student.name, day, time,
            lesson_format=self.student.lesson_format,
            direction=self.student.direction)
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


def add_weekly_lessons(store: Store, student: data.Student,
                       picked: list[tuple[int, int, int]],
                       first: date | None = None) -> int:
    """Записать уроки по отмеченным дням недели на четыре недели вперёд.

    Начало — сегодня или первый день абонемента, если он позже. Уже
    прошедшее время и уроки, которые в это же время уже стоят, пропускаются.
    """
    now = datetime.now()
    start = first or date.today()
    added = 0
    for offset in range(0, 28):
        day = start + timedelta(days=offset)
        for weekday, hour, minute in picked:
            if day.weekday() != weekday:
                continue
            time = dates.fmt_time(hour, minute)
            if datetime(day.year, day.month, day.day, hour, minute) <= now:
                continue
            same = any(l.student == student.name and l.day == day
                       and l.time == time and l.status == LESSON_WAIT
                       for l in store.lessons)
            if same:
                continue
            store.add_lesson(
                student.name, day, time,
                lesson_format=student.lesson_format,
                direction=student.direction)
            added += 1
    return added


class ScheduleDialog(QDialog):
    """Постоянное расписание: галочки по дням недели и время каждого дня."""

    def __init__(self, store: Store, student: data.Student, parent=None):
        super().__init__(parent)
        self.store = store
        self.student = student
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
        self.added = add_weekly_lessons(self.store, self.student, picked)
        self.accept()


class ServiceDialog(QDialog):
    """Услуга: аренда, настройка, студия — и сумма в журнал оплат."""

    def __init__(self, store: Store, student: data.Student, parent=None):
        super().__init__(parent)
        self.store = store
        self.student = student
        self.setWindowTitle("Добавить услугу")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.service = QComboBox()
        self.service.addItems(SERVICES)
        self.service.setAccessibleName("Услуга")
        form.addRow("Услуга", self.service)

        self.amount = QSpinBox()
        self.amount.setRange(0, 500_000)
        self.amount.setSingleStep(500)
        self.amount.setSuffix(" ₽")
        self.amount.setAccessibleName("Сумма услуги")
        form.addRow("Сумма", self.amount)
        lay.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Готово")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._add)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _add(self) -> None:
        self.store.add_payment(self.student.name, self.service.currentText(),
                               self.amount.value())
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
            self.store.add_payment(
                self.student.name, f"Абонемент, {count} {word}", price)
        if self.schedule_box is not None and self.schedule_box.isChecked():
            picked = self.grid.picked()
            if picked:
                # Расписание начинается не раньше сегодняшнего дня: то, что
                # уже прошло, вперёд не записывается.
                first = max(date.today(), self.subscription.begins)
                self.schedule_added = add_weekly_lessons(
                    self.store, self.student, picked, first=first)
            else:
                self.schedule_empty = True
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
        if sub.total:
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


class LessonEditDialog(QDialog):
    """Правка проведённого урока: дата и, если нужно, время."""

    def __init__(self, lesson: data.Lesson, parent=None):
        super().__init__(parent)
        self.lesson = lesson
        self.new_day: date | None = None
        self.new_time = ""
        self.setWindowTitle("Редактировать урок")
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
        if day > date.today():
            announce(self, "Дата урока не может быть в будущем: урок уже прошёл.",
                     assertive=True)
            return
        self.new_day = day
        self.new_time = (dates.fmt_time(self.hour.value(), self.minute.value())
                         if self.has_time.isChecked() else "")
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
                ("Докупить уроки", self.top_up),
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
                      "«Добавить новый абонемент» или докупить уроки кнопкой "
                      "«Докупить уроки».")
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
                    "по одному кнопкой «Добавить законченный абонемент».")
        else:
            word = plural(len(history), "абонемент", "абонемента", "абонементов")
            text = (f"Законченных {word}: {len(history)}. Выбери абонемент "
                    "в списке.")
        self.history_info.setText(text)
        self.history_info.setAccessibleName(text)

        chosen = self.history_pick.currentData()
        self.history_pick.blockSignals(True)
        self.history_pick.clear()
        for item in history:
            if item.total:
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
        dialog = AddDateDialog(self.store, self.student, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Дата не добавлена.")
            return
        near = self.store.nearest_lesson(self.student.name)
        when = (f"{dates.human_date(near.day)} в {near.time}"
                if near else "")
        self._done(f"Дата добавлена. Ближайший урок {when}."
                   if when else "Дата добавлена.")

    def add_schedule(self) -> None:
        dialog = ScheduleDialog(self.store, self.student, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Расписание не добавлено.")
            return
        count = dialog.added
        word = plural(count, "урок", "урока", "уроков")
        self._done(f"{self.student.name}, на месяц вперёд записано "
                   f"{count} {word}.")

    def top_up(self) -> None:
        name = self.student.name
        used, total = self.store.balance(name)
        dialog = TopUpDialog(name, used, total, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            announce(self, "Уроки не добавлены.")
            return
        count = dialog.lessons
        word = plural(count, "урок", "урока", "уроков")
        sub = self.store.current_subscription(name)
        if sub is None:
            self.store.add_subscription(
                name, count, price=dialog.price,
                direction=self.student.direction,
                started=dialog.started)
        else:
            sub.total += count
            sub.price += max(0, dialog.price)
            self.store.save_subscriptions()
        if dialog.price > 0:
            self.store.add_payment(
                name, f"Абонемент, {count} {word}", dialog.price)
        used, total = self.store.balance(name)
        self._done(f"{name}, докуплено {count} {word}. "
                   f"Уроки {used} из {total}.")

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

            path = updater.download(update["url"], on_progress, update["size"])
            self.status.emit("Скачано. Устанавливаю новую версию.")
            ok, message = updater.install_and_restart(path)
            if ok:
                self.installed.emit(message)
            else:
                self.failed.emit(message)
        except updater.UpdateError as error:
            self.failed.emit(str(error))
        except Exception as error:  # noqa: BLE001 — поток не должен падать молча
            self.failed.emit(f"Обновление не удалось: {error}")


def norm_keys(text: str) -> str:
    """Клавиши единой записью: «ctrl+p», «Ctrl+P» и «CTRL+P» — одно и то же."""
    text = (text or "").strip()
    if not text:
        return ""
    sequence = QKeySequence(text)
    return key_text(sequence) if not sequence.isEmpty() else ""


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
    parts = []
    if mods & Qt.KeyboardModifier.ControlModifier:
        parts.append("Ctrl")
    if mods & Qt.KeyboardModifier.AltModifier:
        parts.append("Alt")
    if mods & Qt.KeyboardModifier.ShiftModifier:
        parts.append("Shift")
    if mods & Qt.KeyboardModifier.MetaModifier:
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
        mods = event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                    | Qt.KeyboardModifier.ShiftModifier
                                    | Qt.KeyboardModifier.AltModifier
                                    | Qt.KeyboardModifier.MetaModifier)
        self.keys = combo_text(mods, key)
        self.accept()


class SettingsDialog(QDialog):
    """Настройки: «Общее», «Звуки» и «Сочетания клавиш» одним столбцом."""

    bindings_changed = Signal(dict)
    volume_changed = Signal(int)
    cue_volume_changed = Signal(int)

    def __init__(self, store: Store, bindings: dict[str, str],
                 volume: int = settings.DEFAULT_VOLUME,
                 cue_volume: int = settings.DEFAULT_CUE_VOLUME, parent=None):
        super().__init__(parent)
        self.store = store
        self.bindings = dict(bindings)
        self.volume = settings.volume_value(volume)
        self.cue_volume = settings.cue_volume_value(cue_volume)
        self._worker: UpdateWorker | None = None
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
        for code, title, _keys in settings.ACTIONS:
            button = QPushButton()
            button.clicked.connect(
                lambda _checked=False, c=code: self.assign_key(c))
            self.key_buttons[code] = button
            column.addWidget(button)
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
        QApplication.instance().quit()

    def on_worker_finished(self) -> None:
        self.check_button.setEnabled(True)

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
        for code, title, _keys in settings.ACTIONS:
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
        for code, title, _keys in settings.ACTIONS:
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
        announce(self, f"Настройки сохранены. {first} {second}", assertive=True)
        self.accept()


# --- главное окно --------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.bindings = settings.load(store.folder)
        self.volume = settings.load_volume(store.folder)
        self.cue_volume = settings.load_cue_volume(store.folder)
        self.cues = settings.load_cues(store.folder)
        self.setWindowTitle(f"Мажорный лад Расписание {__version__}")
        self.resize(820, 620)
        self.announcer = Announcer(self)

        self.tabs = QTabWidget(self)
        self.setCentralWidget(self.tabs)

        # --- вкладка учеников
        page = QWidget()
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
        self.card_button = QPushButton()
        self.card_button.clicked.connect(self.open_card)
        row.addWidget(self.card_button)
        self.settings_button = QPushButton()
        self.settings_button.clicked.connect(self.open_settings)
        row.addWidget(self.settings_button)
        self.cues_button = QPushButton()
        self.cues_button.clicked.connect(self.toggle_cues)
        row.addWidget(self.cues_button)
        row.addStretch(1)
        lay.addLayout(row)
        self.tabs.addTab(page, "Ученики")

        # --- вкладка расписания
        page = QWidget()
        lay = QVBoxLayout(page)
        self.schedule_hint = QLabel()
        self.schedule_hint.setWordWrap(True)
        lay.addWidget(self.schedule_hint)
        self.schedule_list = QListWidget()
        self.schedule_list.setAccessibleName("Расписание уроков")
        lay.addWidget(self.schedule_list, 1)
        self.tabs.addTab(page, "Расписание")

        # --- вкладки абонементов
        (self.active_subs_page, self.active_subs_hint,
         self.active_subs_list) = self._subs_tab(
            "Действующие абонементы", "Список действующих абонементов")
        (self.finished_subs_page, self.finished_subs_hint,
         self.finished_subs_list) = self._subs_tab(
            "Законченные абонементы", "Список законченных абонементов")
        self.update_hints()

        # --- горячие клавиши на всё приложение
        self._settings_open = False
        self._shortcuts: list[QShortcut] = []
        self.install_shortcuts()

        # --- напоминания
        self.reminders = ReminderService(self.store, self)
        self.reminders.soon.connect(self.on_soon)
        self.reminders.ask_start.connect(self.on_ask_start)
        self.reminders.ask_finish.connect(self.on_ask_finish)

        # --- подсказки времени урока
        self._cue_anchor: datetime | None = None
        self._cue_done: set[int] = set()
        self._cue_timer = QTimer(self)
        self._cue_timer.setInterval(1000)
        self._cue_timer.timeout.connect(self.check_cues)
        self._cue_timer.start()

        self.refresh_all()
        self.setup_tray()
        QTimer.singleShot(600, self.hello)

    # --- общее ------------------------------------------------------------

    def hello(self) -> None:
        count = len(self.store.students)
        word = plural(count, "ученик", "ученика", "учеников")
        text = f"Мажорный лад Расписание. В списке {count} {word}."
        if self.store.notice:
            text += " " + self.store.notice
            self.store.notice = ""
        announce(self, text)
        self.students_list.setFocus()

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
            item = QListWidgetItem(lesson_line(lesson))
            item.setData(Qt.ItemDataRole.UserRole, lesson.uid)
            self.schedule_list.addItem(item)

    def _subs_tab(self, title: str,
                  name: str) -> tuple[QWidget, QLabel, QListWidget]:
        """Вкладка со списком абонементов: подсказка сверху, список ниже.

        Двойной щелчок открывает карточку ученика, чей это абонемент.
        """
        page = QWidget()
        lay = QVBoxLayout(page)
        hint = QLabel()
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lst = QListWidget()
        lst.setAccessibleName(name)
        lst.itemDoubleClicked.connect(self.open_sub_card)
        lay.addWidget(lst, 1)
        self.tabs.addTab(page, title)
        return page, hint, lst

    def sub_line(self, sub: data.Subscription) -> str:
        """Абонемент одной фразой: как его прочитает диктор."""
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

    def _fill_subs(self, lst: QListWidget, subs) -> None:
        """Наполнить список абонементов; новые сверху."""
        current = None
        item = lst.currentItem()
        if item is not None:
            current = item.data(Qt.ItemDataRole.UserRole)
        lst.clear()
        for sub in sorted(subs, key=lambda s: (s.begins, s.uid), reverse=True):
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
        self._fill_subs(self.active_subs_list,
                        [s for s in self.store.subscriptions
                         if not s.finished])
        self._fill_subs(self.finished_subs_list,
                        [s for s in self.store.subscriptions if s.finished])

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
        """Абонемент, выбранный в одной из вкладок абонементов."""
        page = self.tabs.currentWidget()
        if page is self.active_subs_page:
            lst = self.active_subs_list
        elif page is self.finished_subs_page:
            lst = self.finished_subs_list
        else:
            return None
        item = lst.currentItem()
        if item is None:
            return None
        return self.store.subscription_by_uid(
            item.data(Qt.ItemDataRole.UserRole))

    def export_subscription(self) -> None:
        """F6 — выгрузить выбранный абонемент читаемым текстом в папку Data."""
        sub = self.selected_subscription()
        if sub is None:
            announce(self,
                     "Чтобы выгрузить абонемент, открой вкладку действующих "
                     "или законченных абонементов, выбери его в списке и "
                     "нажми ещё раз.", assertive=True)
            return
        try:
            path = self.store.export_subscription(sub)
        except OSError:
            announce(self, "Не получилось выгрузить абонемент.",
                     assertive=True)
            return
        announce(self, f"Абонемент {sub.student} выгружен в файл "
                       f"{path.name}, он лежит в папке Дата.", assertive=True)

    def refresh_all(self) -> None:
        self.refresh_students()
        self.refresh_schedule()
        self.refresh_subscriptions()

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
        """Подсказки и кнопки называют те клавиши, что стоят сейчас."""
        pieces = []
        for code, title, _keys in settings.ACTIONS:
            keys = norm_keys(self.bindings.get(code, ""))
            if keys:
                pieces.append(f"{keys} — {title.casefold()}")
        tail = " ".join(pieces)
        self.students_hint.setText(
            "Стрелки вверх и вниз — переход по ученикам. " + tail)
        self.schedule_hint.setText(
            "Стрелки вверх и вниз — уроки по порядку. " + tail)
        sub_keys = norm_keys(self.bindings.get("subexport", ""))
        sub_tip = (f"{sub_keys} — выгрузить выбранный абонемент "
                   f"в текстовый файл." if sub_keys
                   else "Выгрузка абонемента в текстовый файл без клавиши.")
        self.active_subs_hint.setText(
            "Все действующие абонементы, новые сверху. " + sub_tip)
        self.finished_subs_hint.setText(
            "Все законченные абонементы, новые сверху. " + sub_tip)
        card_keys = norm_keys(self.bindings.get("card", ""))
        self.card_button.setText(
            f"Карточка ученика ({card_keys})" if card_keys
            else "Карточка ученика")
        settings_keys = norm_keys(self.bindings.get("settings", ""))
        self.settings_button.setText(
            f"Настройки ({settings_keys})" if settings_keys else "Настройки")
        self.update_cues_button()

    def update_cues_button(self) -> None:
        """Кнопка называет своё состояние словами — диктор прочитает его."""
        if self.cues:
            text = "Подсказки времени: включены"
            name = ("Звуковые подсказки времени урока включены. Нажми, "
                    "чтобы выключить")
        else:
            text = "Подсказки времени: выключены"
            name = ("Звуковые подсказки времени урока выключены. Нажми, "
                    "чтобы включить")
        self.cues_button.setText(text)
        self.cues_button.setAccessibleName(name)

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
        for code, _title, _keys in settings.ACTIONS:
            text = norm_keys(self.bindings.get(code, ""))
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
        elif action == "settings":
            self.open_settings()

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
        earned = self.store.earned_in_month(today.year, today.month)
        done = [l for l in self.store.lessons
                if l.day.month == today.month and l.day.year == today.year
                and l.status == LESSON_DONE]
        lessons_word = plural(len(done), "урок", "урока", "уроков")
        announce(
            self,
            f"За {dates.MONTHS_NOM[today.month - 1]} заработано "
            f"{rubles(earned)}. Проведено {len(done)} {lessons_word}.",
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

    def on_soon(self, lesson: data.Lesson, kind: str) -> None:
        text = soon_text(lesson, kind)
        self._play_then(
            "soon", lambda: announce(self, text, assertive=True))

    def on_ask_start(self, lesson: data.Lesson) -> None:
        now = datetime.now()
        text = (f"Сейчас {now:%H:%M}. Вы подтверждаете, что урок идёт? "
                f"{lesson.student}, в {lesson.time}.")
        box = QMessageBox(self)
        box.setWindowTitle("Начало урока")
        box.setText(text)
        yes = box.addButton("Да, урок идёт", QMessageBox.ButtonRole.YesRole)
        later = box.addButton("Напомнить через час",
                              QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(yes)

        def ask() -> None:
            announce(self, text, assertive=True)
            box.exec()
            if box.clickedButton() is yes:
                self.reminders.mark_started(lesson)
                announce(
                    self, "Хорошо. Через сорок минут спрошу, прошёл ли урок.")
            else:
                self.reminders.snooze(lesson)
                announce(self, "Напомню через час.")

        self._play_then("start", ask)

    def on_ask_finish(self, lesson: data.Lesson) -> None:
        text = (f"Вы подтверждаете, что урок прошёл? "
                f"{lesson.student}, в {lesson.time}.")
        # Если это последний урок абонемента — сначала весёлый сигнал и
        # подсказка про подарок, и только потом обычный вопрос.
        sub = self.store.current_subscription(lesson.student)
        last = sub is not None and sub.total > 0 and sub.left == 1

        def ask() -> None:
            announce(self, text, assertive=True)
            dialog = LessonPayDialog(lesson.student, self,
                                     amount=lesson.price, question=text)
            dialog.setWindowTitle("Урок прошёл?")
            if dialog.exec() != QDialog.DialogCode.Accepted:
                lesson.status = LESSON_CANCELLED
                self.store.update_lesson(lesson)
                self.reminders.mark_cancelled(lesson)
                self.refresh_all()
                announce(self, f"Урок отменён. {lesson.student}, "
                               f"в {lesson.time}.", assertive=True)
                return
            amount = dialog.amount
            lesson.status = LESSON_DONE
            lesson.price = amount
            sub = self.store.current_subscription(lesson.student)
            if sub is not None:
                lesson.subscription = sub.uid
            self.store.update_lesson(lesson)
            if sub is not None:
                self.store.bump_used(sub)
            if amount > 0:
                self.store.add_payment(lesson.student, "Урок", amount,
                                       date.today())
            self.reminders.mark_finished(lesson)
            self.refresh_all()
            used, total = self.store.balance(lesson.student)
            tail = f" Уроки {used} из {total}." if total else ""
            announce(self, f"{lesson.student}, урок проведён.{tail}",
                     assertive=True)

        if not last:
            self._play_then("finish", ask)
            return

        def promo() -> None:
            announce(self, LAST_LESSON_TEXT, assertive=True)
            # Даём диктору проговорить фразу целиком, потом обычный вопрос.
            QTimer.singleShot(3000, lambda: self._play_then("finish", ask))

        self._play_then("last", promo)

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
    updater.wait_previous(pid, timeout=120.0)
    ok, message = updater.finish_install(target)
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


def main() -> int:
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
    app.setApplicationName("Мажорный лад Расписание")
    app.setApplicationDisplayName("Мажорный лад Расписание")
    app.setOrganizationName("Мажорный лад")
    window = MainWindow(Store())
    window.show()
    announce(window, "Мажорный лад Расписание открыта.")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
