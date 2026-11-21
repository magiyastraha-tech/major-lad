"""Главное окно и формы программы «Мажорный лад Расписание».

Правило одно и то же во всех окнах: после каждого действия — ровно одна
фраза диктору. Фокус при этом остаётся там, где ты стоял, поэтому можно
продолжать работать стрелками сразу после ответа.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

from PySide6.QtCore import QDate, QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QIcon,
                           QKeySequence, QPainter, QPixmap)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFormLayout, QGridLayout, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget, QSystemTrayIcon,
)

from . import __version__, dates, settings, sounds, store as data, updater
from .reminders import ReminderService, soon_text
from .store import (DIRECTIONS, FORMATS, LESSON_CANCELLED, LESSON_DONE,
                    LESSON_WAIT, SERVICES, Store, money, plural)
from .uikit import Announcer, ShortcutFilter, announce, key_text

WEEK_LABELS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница",
               "Суббота", "Воскресенье")
MINUTES = [f"{m:02d}" for m in range(0, 60, 5)]


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
        if self.student is None and other is not None:
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
        student.name = name
        student.phone = self.phone.text().strip()
        student.email = self.email.text().strip()
        student.lesson_format = self.format.currentText()
        student.direction = self.direction.currentText()
        self.store.update_student(student)
        self.created = student
        self.accept()


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

        grid = QGridLayout()
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
            grid.addWidget(check, index, 0)
            grid.addWidget(hour, index, 1)
            grid.addWidget(QLabel("часов"), index, 2)
            grid.addWidget(minute, index, 3)
            self.checks.append(check)
            self.hours.append(hour)
            self.minutes.append(minute)
        lay.addLayout(grid)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Добавить расписание")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._add)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _add(self) -> None:
        now = datetime.now()
        picked = [(i, self.hours[i].value(), int(self.minutes[i].currentText()))
                  for i, check in enumerate(self.checks) if check.isChecked()]
        if not picked:
            announce(self, "Ни один день не отмечен. Поставь галочки на нужных днях.")
            return
        today = date.today()
        for offset in range(0, 28):
            day = today + timedelta(days=offset)
            for weekday, hour, minute in picked:
                if day.weekday() != weekday:
                    continue
                time = dates.fmt_time(hour, minute)
                if datetime(day.year, day.month, day.day, hour, minute) <= now:
                    continue
                same = any(l.student == self.student.name and l.day == day
                           and l.time == time and l.status == LESSON_WAIT
                           for l in self.store.lessons)
                if same:
                    continue
                self.store.add_lesson(
                    self.student.name, day, time,
                    lesson_format=self.student.lesson_format,
                    direction=self.student.direction)
                self.added += 1
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
        self.accept()


class DoneLessonDialog(QDialog):
    """Дата уже проведённого урока: один урок за одно открытие окна."""

    def __init__(self, store: Store, sub: data.Subscription, parent=None):
        super().__init__(parent)
        self.store = store
        self.sub = sub
        self.lesson: data.Lesson | None = None
        self.day_value: date | None = None
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
        lay.addLayout(form)

        hint = QLabel("Программа сама поставит урок на нужное место в списке "
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
        try:
            self.lesson = self.store.add_done_lesson(self.sub, day)
        except ValueError as error:
            announce(self, str(error))
            return
        self.day_value = day
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
                ("Добавить расписание", self.add_schedule),
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
        if sub.total:
            text = (f"Урок {dates.fmt_date(dialog.day_value)} записан. "
                    f"В абонементе уроки {sub.used} из {sub.total}.")
        else:
            text = (f"Урок {dates.fmt_date(dialog.day_value)} записан. "
                    f"Всего уроков {sub.used}.")
        self._done(text)

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


class SettingsDialog(QDialog):
    """Настройки: обновление программы и сочетания клавиш."""

    bindings_changed = Signal(dict)

    def __init__(self, store: Store, bindings: dict[str, str], parent=None):
        super().__init__(parent)
        self.store = store
        self.bindings = dict(bindings)
        self._worker: UpdateWorker | None = None
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(520)

        lay = QVBoxLayout(self)
        tabs = QTabWidget(self)
        lay.addWidget(tabs, 1)

        # --- общее
        page = QWidget()
        column = QVBoxLayout(page)
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
        column.addStretch(1)
        tabs.addTab(page, "Общее")

        # --- сочетания клавиш
        page = QWidget()
        column = QVBoxLayout(page)
        hint = QLabel("Поставь курсор в поле действия и нажми нужные клавиши. "
                      "Пустое поле — действие без клавиши.")
        hint.setWordWrap(True)
        column.addWidget(hint)

        form = QFormLayout()
        self.edits: dict[str, QKeySequenceEdit] = {}
        for code, title, _keys in settings.ACTIONS:
            edit = QKeySequenceEdit()
            edit.setMaximumSequenceLength(1)
            edit.setAccessibleName(title)
            edit.setKeySequence(QKeySequence(self.bindings.get(code, "")))
            self.edits[code] = edit
            form.addRow(title, edit)
        column.addLayout(form)
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
        column.addWidget(buttons)
        tabs.addTab(page, "Сочетания клавиш")

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

    # --- клавиши ----------------------------------------------------------

    def reset_bindings(self) -> None:
        for code, keys in settings.defaults().items():
            self.edits[code].setKeySequence(QKeySequence(keys))
        announce(self, "Расставлены клавиши по умолчанию. "
                       "Нажми «Сохранить», чтобы применить.")

    def save_bindings(self) -> None:
        result: dict[str, str] = {}
        seen: dict[str, str] = {}
        for code, title, _keys in settings.ACTIONS:
            text = key_text(self.edits[code].keySequence())
            if text:
                other = seen.get(text)
                if other is not None:
                    announce(self, f"Клавиша {text} назначена дважды: "
                                   f"{other} и {title}. Поменяй одну.",
                             assertive=True)
                    self.edits[code].setFocus()
                    return
                seen[text] = title
            result[code] = text
        try:
            settings.save(self.store.folder, result)
        except OSError:
            announce(self, "Не получилось сохранить настройки.", assertive=True)
            return
        self.bindings = result
        self.bindings_changed.emit(result)
        announce(self, "Сочетания клавиш сохранены.", assertive=True)
        self.accept()


# --- главное окно --------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.bindings = settings.load(store.folder)
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
        self.card_button = QPushButton()
        self.card_button.clicked.connect(self.open_card)
        row.addWidget(self.card_button)
        self.settings_button = QPushButton()
        self.settings_button.clicked.connect(self.open_settings)
        row.addWidget(self.settings_button)
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
        self.update_hints()

        # --- горячие клавиши на всё приложение
        self._filter = ShortcutFilter(self.hotkey, self.bindings, self)
        QApplication.instance().installEventFilter(self._filter)

        # --- напоминания
        self.reminders = ReminderService(self.store, self)
        self.reminders.soon.connect(self.on_soon)
        self.reminders.ask_start.connect(self.on_ask_start)
        self.reminders.ask_finish.connect(self.on_ask_finish)

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

    def refresh_all(self) -> None:
        self.refresh_students()
        self.refresh_schedule()

    def selected_uid(self) -> int | None:
        item = self.students_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def selected_student(self) -> data.Student | None:
        uid = self.selected_uid()
        for student in self.store.students:
            if student.uid == uid:
                return student
        return None

    # --- клавиши и настройки ---------------------------------------------

    def update_hints(self) -> None:
        """Подсказки и кнопки называют те клавиши, что стоят сейчас."""
        pieces = []
        for code, title, _keys in settings.ACTIONS:
            keys = self.bindings.get(code, "")
            if keys:
                pieces.append(f"{keys} — {title.casefold()}")
        tail = " ".join(pieces)
        self.students_hint.setText(
            "Стрелки вверх и вниз — переход по ученикам. " + tail)
        self.schedule_hint.setText(
            "Стрелки вверх и вниз — уроки по порядку. " + tail)
        card_keys = self.bindings.get("card", "")
        self.card_button.setText(
            f"Карточка ученика ({card_keys})" if card_keys
            else "Карточка ученика")
        settings_keys = self.bindings.get("settings", "")
        self.settings_button.setText(
            f"Настройки ({settings_keys})" if settings_keys else "Настройки")

    def apply_bindings(self, bindings: dict[str, str]) -> None:
        self.bindings = dict(bindings)
        self._filter.set_bindings(self.bindings)
        self.update_hints()

    def open_settings(self) -> None:
        # Окно настроек модальное, но перехват клавиш работает и внутри
        # него: без этого флага F10 открыл бы второе такое же окно.
        if getattr(self, "_settings_open", False):
            return
        dialog = SettingsDialog(self.store, self.bindings, self)
        dialog.bindings_changed.connect(self.apply_bindings)
        announce(dialog, f"Настройки. Версия программы {__version__}.")
        self._settings_open = True
        try:
            dialog.exec()
        finally:
            self._settings_open = False
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

    def on_soon(self, lesson: data.Lesson, kind: str) -> None:
        if not sounds.play("soon"):
            QApplication.beep()
        announce(self, soon_text(lesson, kind), assertive=True)

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
        if not sounds.play("start"):
            QApplication.beep()
        announce(self, text, assertive=True)
        box.exec()
        if box.clickedButton() is yes:
            self.reminders.mark_started(lesson)
            announce(self, "Хорошо. Через сорок минут спрошу, прошёл ли урок.")
        else:
            self.reminders.snooze(lesson)
            announce(self, "Напомню через час.")

    def on_ask_finish(self, lesson: data.Lesson) -> None:
        text = (f"Вы подтверждаете, что урок прошёл? "
                f"{lesson.student}, в {lesson.time}.")
        if not sounds.play("finish"):
            QApplication.beep()
        announce(self, text, assertive=True)
        dialog = LessonPayDialog(lesson.student, self, amount=lesson.price,
                                 question=text)
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
        announce(self, f"{lesson.student}, урок проведён.{tail}", assertive=True)

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


def main() -> int:
    # После обновления нас запускает новая версия с `--wait-pid N`:
    # ждём, пока прошлый процесс отпустит exe, и убираем его копию.
    pid = updater.take_wait_pid()
    updater.wait_previous(pid)
    updater.cleanup_backup()
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
