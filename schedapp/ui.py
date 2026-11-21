"""Главное окно и формы программы «Мажорный лад Расписание».

Правило одно и то же во всех окнах: после каждого действия — ровно одна
фраза диктору. Фокус при этом остаётся там, где ты стоял, поэтому можно
продолжать работать стрелками сразу после ответа.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget, QSystemTrayIcon,
)

from . import dates, store as data
from .reminders import ReminderService, soon_text
from .store import (DIRECTIONS, FORMATS, LESSON_CANCELLED, LESSON_DONE,
                    LESSON_WAIT, SERVICES, Store, money, plural)
from .uikit import Announcer, ShortcutFilter, announce

WEEK_LABELS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница",
               "Суббота", "Воскресенье")
MINUTES = [f"{m:02d}" for m in range(0, 60, 5)]


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
                    direction=student.direction)
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


class StudentCard(QDialog):
    """Карточка ученика: телефон и четыре кнопки действий."""

    def __init__(self, store: Store, student: data.Student, parent=None):
        super().__init__(parent)
        self.store = store
        self.student = student
        self.changed = False
        self.setWindowTitle(student.name)
        self.setMinimumWidth(480)

        lay = QVBoxLayout(self)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.info)
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
        used, total = self.store.balance(student.name)
        parts = [student.name]
        if student.phone:
            parts.append(f"телефон {student.phone}")
        if student.email:
            parts.append(f"почта {student.email}")
        parts.append(f"{student.direction}, {student.lesson_format.casefold()}")
        if total:
            parts.append(f"уроки {used} из {total}")
        else:
            parts.append("абонемента нет")
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

    def _done(self, message: str) -> None:
        self.changed = True
        self.refresh()
        announce(self, message)

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
        self.store.update_lesson(lesson)

        used, total = self.store.balance(name)
        if sub is not None:
            sub.used = min(sub.total, sub.used + 1) if sub.total else sub.used + 1
            self.store.save_subscriptions()
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
                direction=self.student.direction)
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


# --- главное окно --------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.setWindowTitle("Мажорный лад Расписание")
        self.resize(820, 620)
        self.announcer = Announcer(self)

        self.tabs = QTabWidget(self)
        self.setCentralWidget(self.tabs)

        # --- вкладка учеников
        page = QWidget()
        lay = QVBoxLayout(page)
        hint = QLabel("Стрелки вверх и вниз — переход по ученикам. "
                      "F2 — карточка ученика.")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.students_list = QListWidget()
        self.students_list.setAccessibleName("Список учеников")
        self.students_list.itemDoubleClicked.connect(lambda _item: self.open_card())
        lay.addWidget(self.students_list, 1)

        row = QHBoxLayout()
        self.add_button = QPushButton("Добавить ученика")
        self.add_button.setDefault(True)
        self.add_button.clicked.connect(self.add_student)
        row.addWidget(self.add_button)
        card = QPushButton("Карточка ученика (F2)")
        card.clicked.connect(self.open_card)
        row.addWidget(card)
        row.addStretch(1)
        lay.addLayout(row)
        self.tabs.addTab(page, "Ученики")

        # --- вкладка расписания
        page = QWidget()
        lay = QVBoxLayout(page)
        hint = QLabel("Стрелки вверх и вниз — уроки по порядку. "
                      "F3 — уроки на сегодня, F4 — на завтра, "
                      "F5 — заработок за месяц, F9 — выгрузить данные "
                      "в текст.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.schedule_list = QListWidget()
        self.schedule_list.setAccessibleName("Расписание уроков")
        lay.addWidget(self.schedule_list, 1)
        self.tabs.addTab(page, "Расписание")

        # --- горячие клавиши на всё приложение
        self._filter = ShortcutFilter(self.hotkey, self)
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

    def hotkey(self, key) -> None:
        if key == Qt.Key.Key_F2:
            self.open_card()
        elif key == Qt.Key.Key_F3:
            self.speak_day(date.today(), "на сегодня")
        elif key == Qt.Key.Key_F4:
            self.speak_day(date.today() + timedelta(days=1), "на завтра")
        elif key == Qt.Key.Key_F5:
            self.speak_earnings()
        elif key == Qt.Key.Key_F9:
            self.export_text()

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
        self.store.update_lesson(lesson)
        sub = self.store.current_subscription(lesson.student)
        if sub is not None:
            sub.used = min(sub.total, sub.used + 1) if sub.total else sub.used + 1
            self.store.save_subscriptions()
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
        quit_action = QAction("Выход", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(show)
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
