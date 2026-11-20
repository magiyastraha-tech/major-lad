"""Данные программы: ученики, абонементы, уроки и оплаты.

Всё лежит простыми текстовыми файлами в папке Data рядом с программой:
их можно открыть блокнотом, прочитать и поправить руками. Строка файла —
поля через вертикальную черту, строки с решётки в начале — пояснения,
программа их не читает. При запуске программа читает файлы заново,
поэтому правка из блокнота не теряется.

Записи хранятся не «как попало», а с номером в первом поле: по нему
программа отличает один урок от другого, даже если у двух учеников
одинаковое имя или урок перенесли на другой день.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import dates, paths

SEP = " | "

STUDENTS_FILE = "Ученики.txt"
SUBSCRIPTIONS_FILE = "Абонементы.txt"
LESSONS_FILE = "Расписание.txt"
PAYMENTS_FILE = "Журнал оплат.txt"

LESSON_WAIT = "ожидается"
LESSON_DONE = "проведён"
LESSON_CANCELLED = "отменён"
LESSON_STATES = (LESSON_WAIT, LESSON_DONE, LESSON_CANCELLED)

FORMATS = ("Очно", "Онлайн")
DIRECTIONS = ("Вокал", "Аккомпанирующая гитара", "Акустическая гитара",
              "Электрогитара")
SERVICES = ("Аренда гитары", "Настройка гитары", "Студийная работа")

# Заголовки файлов. Пишутся при первом запуске и остаются в файле, чтобы
# через месяц было видно, где какое поле.
_STUDENTS_HEAD = (
    "# Ученики программы «Мажорный лад Расписание».",
    "# Поля через вертикальную черту:",
    "# номер | имя | телефон | почта | очно или онлайн | направление | заметка",
)
_SUBSCRIPTIONS_HEAD = (
    "# Абонементы: кто сколько уроков купил и сколько уже прошло.",
    "# Поля: номер | дата покупки | ученик | направление | куплено | прошло | сумма",
)
_LESSONS_HEAD = (
    "# Расписание уроков.",
    "# Поля: номер | дата | время | ученик | очно или онлайн | направление |"
    " состояние | сумма | заметка",
    "# Состояние: ожидается, проведён, отменён.",
)
_PAYMENTS_HEAD = (
    "# Журнал оплат: каждая строка — приход денег.",
    "# Поля: номер | дата | ученик | за что | сумма",
)


@dataclass
class Student:
    uid: int
    name: str
    phone: str = ""
    email: str = ""
    lesson_format: str = FORMATS[0]
    direction: str = DIRECTIONS[2]
    note: str = ""


@dataclass
class Subscription:
    uid: int
    bought: date
    student: str
    direction: str
    total: int
    used: int = 0
    price: int = 0

    @property
    def left(self) -> int:
        return max(0, int(self.total) - int(self.used))


@dataclass
class Lesson:
    uid: int
    day: date
    time: str
    student: str
    lesson_format: str = FORMATS[0]
    direction: str = DIRECTIONS[2]
    status: str = LESSON_WAIT
    price: int = 0
    note: str = ""

    @property
    def stamp(self) -> datetime:
        """Дата и время урока одной величиной — удобно сравнивать."""
        hour, minute = dates.parse_time(self.time) or (0, 0)
        return datetime(self.day.year, self.day.month, self.day.day,
                        hour, minute)


@dataclass
class Payment:
    uid: int
    day: date
    student: str
    what: str
    amount: int


def _read_rows(path: Path) -> list[list[str]]:
    """Строки файла, уже разбитые на поля. Пояснения пропускаются."""
    if not path.exists():
        return []
    rows: list[list[str]] = []
    try:
        # utf-8-sig: блокнот на Windows любит дописывать метку BOM.
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append([part.strip() for part in line.split("|")])
    return rows


def _write_rows(path: Path, head: tuple[str, ...],
                rows: list[list[str]]) -> None:
    """Пишет файл целиком: сначала во временный, потом подменяет.

    Подмена нужна, чтобы при сбое посреди записи старый файл остался
    целым, а не превратился в половину строки.
    """
    lines = list(head) + [SEP.join(str(part) for part in row) for row in rows]
    text = "\r\n".join(lines) + "\r\n"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _int(text: str, default: int = 0) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def money(amount: int) -> str:
    """«1 200» — так диктору и глазам проще, чем «1200»."""
    return f"{int(amount):,}".replace(",", " ")


def plural(count: int, one: str, few: str, many: str) -> str:
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def lessons_word(count: int) -> str:
    return plural(count, "урок", "урока", "уроков")


class Store:
    """Все данные программы в памяти, с записью в файлы после изменения."""

    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder) if folder is not None else paths.data_dir()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.students: list[Student] = []
        self.subscriptions: list[Subscription] = []
        self.lessons: list[Lesson] = []
        self.payments: list[Payment] = []
        self.load()

    # --- файлы -----------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.folder / name

    def load(self) -> None:
        self.students = []
        for row in _read_rows(self._path(STUDENTS_FILE)):
            if len(row) < 2 or not row[1]:
                continue
            row += [""] * (7 - len(row))
            self.students.append(Student(
                uid=_int(row[0]),
                name=row[1],
                phone=row[2],
                email=row[3],
                lesson_format=row[4] or FORMATS[0],
                direction=row[5] or DIRECTIONS[2],
                note=row[6],
            ))

        self.subscriptions = []
        for row in _read_rows(self._path(SUBSCRIPTIONS_FILE)):
            if len(row) < 5:
                continue
            row += [""] * (7 - len(row))
            day = dates.parse_date(row[1]) or date.today()
            self.subscriptions.append(Subscription(
                uid=_int(row[0]),
                bought=day,
                student=row[2],
                direction=row[3],
                total=_int(row[4]),
                used=_int(row[5]),
                price=_int(row[6]),
            ))

        self.lessons = []
        for row in _read_rows(self._path(LESSONS_FILE)):
            if len(row) < 4:
                continue
            row += [""] * (9 - len(row))
            day = dates.parse_date(row[1])
            if day is None:
                continue
            status = row[6] if row[6] in LESSON_STATES else LESSON_WAIT
            self.lessons.append(Lesson(
                uid=_int(row[0]),
                day=day,
                time=row[2],
                student=row[3],
                lesson_format=row[4] or FORMATS[0],
                direction=row[5] or DIRECTIONS[2],
                status=status,
                price=_int(row[7]),
                note=row[8],
            ))

        self.payments = []
        for row in _read_rows(self._path(PAYMENTS_FILE)):
            if len(row) < 4:
                continue
            row += [""] * (5 - len(row))
            day = dates.parse_date(row[1])
            if day is None:
                continue
            self.payments.append(Payment(
                uid=_int(row[0]),
                day=day,
                student=row[2],
                what=row[3],
                amount=_int(row[4]),
            ))

        # Файлов ещё нет — создаём их с пояснениями: так в папке Data
        # сразу видно, что за что отвечает.
        for name, head, items in (
                (STUDENTS_FILE, _STUDENTS_HEAD, self.students),
                (SUBSCRIPTIONS_FILE, _SUBSCRIPTIONS_HEAD, self.subscriptions),
                (LESSONS_FILE, _LESSONS_HEAD, self.lessons),
                (PAYMENTS_FILE, _PAYMENTS_HEAD, self.payments)):
            if not self._path(name).exists():
                _write_rows(self._path(name), head, [])

    def save_students(self) -> None:
        _write_rows(
            self._path(STUDENTS_FILE), _STUDENTS_HEAD,
            [[s.uid, s.name, s.phone, s.email, s.lesson_format,
              s.direction, s.note] for s in self.students])

    def save_subscriptions(self) -> None:
        _write_rows(
            self._path(SUBSCRIPTIONS_FILE), _SUBSCRIPTIONS_HEAD,
            [[s.uid, dates.fmt_date(s.bought), s.student, s.direction,
              s.total, s.used, s.price] for s in self.subscriptions])

    def save_lessons(self) -> None:
        _write_rows(
            self._path(LESSONS_FILE), _LESSONS_HEAD,
            [[l.uid, dates.fmt_date(l.day), l.time, l.student,
              l.lesson_format, l.direction, l.status, l.price, l.note]
             for l in self.lessons])

    def save_payments(self) -> None:
        _write_rows(
            self._path(PAYMENTS_FILE), _PAYMENTS_HEAD,
            [[p.uid, dates.fmt_date(p.day), p.student, p.what, p.amount]
             for p in self.payments])

    # --- ученики ---------------------------------------------------------

    @staticmethod
    def _next_uid(items) -> int:
        return max((item.uid for item in items), default=0) + 1

    def find_student(self, name: str) -> Student | None:
        name = (name or "").strip().casefold()
        for student in self.students:
            if student.name.strip().casefold() == name:
                return student
        return None

    def add_student(self, name: str, phone: str = "", email: str = "",
                    lesson_format: str = FORMATS[0],
                    direction: str = DIRECTIONS[2], note: str = "") -> Student:
        student = Student(
            uid=self._next_uid(self.students),
            name=name.strip(),
            phone=phone.strip(),
            email=email.strip(),
            lesson_format=lesson_format or FORMATS[0],
            direction=direction or DIRECTIONS[2],
            note=note.strip(),
        )
        self.students.append(student)
        self.save_students()
        return student

    def update_student(self, student: Student) -> None:
        self.save_students()

    def remove_student(self, student: Student) -> None:
        self.students = [s for s in self.students if s.uid != student.uid]
        self.save_students()

    def add_subscription(self, student: str, total: int, price: int = 0,
                         direction: str = DIRECTIONS[2],
                         bought: date | None = None) -> Subscription:
        item = Subscription(
            uid=self._next_uid(self.subscriptions),
            bought=bought or date.today(),
            student=student,
            direction=direction,
            total=max(0, int(total)),
            used=0,
            price=int(price),
        )
        self.subscriptions.append(item)
        self.save_subscriptions()
        return item

    def current_subscription(self, student: str) -> Subscription | None:
        """Действующий абонемент: последний, где ещё остались уроки.

        Если остатков нет ни в одном, возвращается последний купленный —
        по нему понятно, сколько уроков прошло всего.
        """
        mine = [s for s in self.subscriptions if s.student == student]
        if not mine:
            return None
        with_left = [s for s in mine if s.left > 0]
        return max(with_left or mine, key=lambda s: (s.bought, s.uid))

    def balance(self, student: str) -> tuple[int, int]:
        """(прошло, куплено) по действующему абонементу."""
        item = self.current_subscription(student)
        if item is None:
            return 0, 0
        return int(item.used), int(item.total)

    def nearest_lesson(self, student: str) -> Lesson | None:
        """Ближайший будущий урок ученика, если он записан."""
        now = datetime.now()
        ahead = [l for l in self.lessons
                 if l.student == student and l.status == LESSON_WAIT
                 and l.stamp >= now]
        return min(ahead, key=lambda l: l.stamp) if ahead else None

    # --- уроки -----------------------------------------------------------

    def add_lesson(self, student: str, day: date, time: str,
                   lesson_format: str = FORMATS[0],
                   direction: str = DIRECTIONS[2],
                   note: str = "") -> Lesson:
        lesson = Lesson(
            uid=self._next_uid(self.lessons),
            day=day,
            time=time,
            student=student,
            lesson_format=lesson_format,
            direction=direction,
            note=note,
        )
        self.lessons.append(lesson)
        self.save_lessons()
        return lesson

    def update_lesson(self, lesson: Lesson) -> None:
        self.save_lessons()

    def lessons_between(self, start: date, end: date) -> list[Lesson]:
        return sorted(
            [l for l in self.lessons if start <= l.day <= end],
            key=lambda l: l.stamp)

    # --- оплаты ----------------------------------------------------------

    def add_payment(self, student: str, what: str, amount: int,
                    day: date | None = None) -> Payment:
        item = Payment(
            uid=self._next_uid(self.payments),
            day=day or date.today(),
            student=student,
            what=what,
            amount=int(amount),
        )
        self.payments.append(item)
        self.save_payments()
        return item

    def earned_in_month(self, year: int, month: int) -> int:
        return sum(p.amount for p in self.payments
                   if p.day.year == year and p.day.month == month)
