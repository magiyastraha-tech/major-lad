"""Данные программы: ученики, абонементы, уроки и оплаты.

Всё лежит в одном файле базы данных в папке Data рядом с программой:
`Мажорный лад Расписание.db`. База — обычный файл SQLite, его не нужно
открывать блокнотом: программе он нужен целиком, а руками данные правятся
через саму программу. Зато одна база вместо четырёх текстовых файлов не
рассыпается на половину записи и не путает поля при ручной правке.

Что было раньше: четыре текстовых файла (`Ученики.txt`, `Абонементы.txt`,
`Расписание.txt`, `Журнал оплат.txt`). При первом запуске новой версии
программа сама переносит из них всё в базу, а сами файлы убирает в папку
`Data/Архив старой версии` — на случай, если понадобится посмотреть, как
было. Ничего не пропадает.

Каждый запуск перед открытием базы делает резервную копию в `Data/Резерв`
(последние 14 дней). Если что-то случится с файлом базы, копия под рукой.

Записи хранятся с номером в первом поле: по нему программа отличает один
урок от другого, даже если у двух учеников одинаковое имя или урок
перенесли на другой день.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import dates, mirror, paths

DB_FILE = "Мажорный лад Расписание.db"
BACKUP_DIR = "Резерв"
ARCHIVE_DIR = "Архив старой версии"
BACKUP_KEEP = 14
SCHEMA_VERSION = 2

# Старые текстовые файлы: из них переносим данные при первом запуске.
STUDENTS_FILE = "Ученики.txt"
SUBSCRIPTIONS_FILE = "Абонементы.txt"
LESSONS_FILE = "Расписание.txt"
PAYMENTS_FILE = "Журнал оплат.txt"
LEGACY_FILES = (STUDENTS_FILE, SUBSCRIPTIONS_FILE, LESSONS_FILE,
                PAYMENTS_FILE)

LESSON_WAIT = "ожидается"
LESSON_DONE = "проведён"
LESSON_CANCELLED = "отменён"
LESSON_STATES = (LESSON_WAIT, LESSON_DONE, LESSON_CANCELLED)

FORMATS = ("Очно", "Онлайн")
DIRECTIONS = ("Вокал", "Аккомпанирующая гитара", "Акустическая гитара",
              "Электрогитара")
SERVICES = ("Аренда гитары", "Настройка гитары", "Студийная работа")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    uid INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    lesson_format TEXT NOT NULL DEFAULT 'Очно',
    direction TEXT NOT NULL DEFAULT 'Акустическая гитара',
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS subscriptions (
    uid INTEGER PRIMARY KEY,
    bought TEXT NOT NULL,
    started TEXT NOT NULL DEFAULT '',
    student TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL DEFAULT 0,
    used INTEGER NOT NULL DEFAULT 0,
    price INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lessons (
    uid INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    time TEXT NOT NULL DEFAULT '',
    student TEXT NOT NULL,
    lesson_format TEXT NOT NULL DEFAULT 'Очно',
    direction TEXT NOT NULL DEFAULT 'Акустическая гитара',
    status TEXT NOT NULL DEFAULT 'ожидается',
    price INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payments (
    uid INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    student TEXT NOT NULL,
    what TEXT NOT NULL DEFAULT '',
    amount INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


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
    # С какого числа абонемент действует. Может не совпадать с датой
    # покупки: купили в конце месяца — уроки начались с первого.
    started: date | None = None

    @property
    def left(self) -> int:
        return max(0, int(self.total) - int(self.used))

    @property
    def begins(self) -> date:
        return self.started or self.bought


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
    """Строки старого текстового файла, разбитые на поля.

    Нужна только для переноса из версии с txt: пояснения пропускаются.
    """
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


def _int(text, default: int = 0) -> int:
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
    """Все данные программы в памяти, с записью в базу после изменения."""

    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder) if folder is not None else paths.data_dir()
        self.folder.mkdir(parents=True, exist_ok=True)
        # Одна фраза для диктора о переносе старых данных — пусто, если
        # переносить было нечего.
        self.notice = ""
        self.students: list[Student] = []
        self.subscriptions: list[Subscription] = []
        self.lessons: list[Lesson] = []
        self.payments: list[Payment] = []

        self._db_path = self.folder / DB_FILE
        migrated = not self._db_path.exists()
        if not migrated and self._db_empty():
            # Базу стёрли или она осталась пустой — старые файлы ещё
            # могут лежать рядом, тогда поднимаем данные из них.
            migrated = any((self.folder / name).exists()
                           for name in LEGACY_FILES)
        self._backup()
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)
        self._migrate()
        self.load()
        if migrated:
            self._import_legacy()
        self._refresh_files()

    # --- база ------------------------------------------------------------

    def _migrate(self) -> None:
        """Довести старую базу до текущей схемы, ничего не теряя."""
        columns = {row["name"] for row in
                   self._conn.execute("PRAGMA table_info(subscriptions)")}
        if "started" not in columns:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE subscriptions ADD COLUMN started "
                    "TEXT NOT NULL DEFAULT ''")
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)",
                (str(SCHEMA_VERSION),))

    def _refresh_files(self) -> None:
        """Пересобрать читаемые файлы рядом с базой.

        База — источник правды; если txt не записался (папка только для
        чтения, файл занят), программа продолжает работать.
        """
        try:
            mirror.refresh(self)
        except OSError:
            pass

    def _backup(self) -> None:
        """Копия базы перед запуском, по одной на день, последние 14."""
        if not self._db_path.exists():
            return
        folder = self.folder / BACKUP_DIR
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{date.today():%Y-%m-%d}{self._db_path.suffix}"
            if not target.exists():
                shutil.copy2(self._db_path, target)
            copies = sorted(folder.glob("*.db"))
            for old in copies[:-BACKUP_KEEP]:
                old.unlink(missing_ok=True)
        except OSError:
            # Резервная копия — страховка, а не условие работы: не вышло,
            # программа всё равно должна открыться.
            pass

    def _db_empty(self) -> bool:
        try:
            conn = sqlite3.connect(self._db_path)
            try:
                for table in ("students", "subscriptions", "lessons",
                              "payments"):
                    row = conn.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()
                    if row and row[0]:
                        return False
                return True
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return False

    def _write(self, table: str, columns: tuple[str, ...],
               rows: list[tuple]) -> None:
        """Таблица переписывается целиком в одной транзакции.

        Данных немного (сотни строк), зато не бывает полузаписанного
        состояния: либо вся таблица новая, либо вся старая.
        """
        marks = ", ".join("?" for _ in columns)
        with self._conn:
            self._conn.execute(f"DELETE FROM {table}")
            self._conn.executemany(
                f"INSERT INTO {table}({', '.join(columns)}) VALUES ({marks})",
                rows)

    def load(self) -> None:
        self.students = [
            Student(uid=row["uid"], name=row["name"], phone=row["phone"],
                    email=row["email"],
                    lesson_format=row["lesson_format"] or FORMATS[0],
                    direction=row["direction"] or DIRECTIONS[2],
                    note=row["note"])
            for row in self._conn.execute(
                "SELECT * FROM students ORDER BY uid")]

        self.subscriptions = []
        for row in self._conn.execute(
                "SELECT * FROM subscriptions ORDER BY uid"):
            day = dates.parse_date(row["bought"]) or date.today()
            started = dates.parse_date(row["started"]) if row["started"] else None
            self.subscriptions.append(Subscription(
                uid=row["uid"], bought=day, student=row["student"],
                direction=row["direction"], total=_int(row["total"]),
                used=_int(row["used"]), price=_int(row["price"]),
                started=started))

        self.lessons = []
        for row in self._conn.execute("SELECT * FROM lessons ORDER BY uid"):
            day = dates.parse_date(row["day"])
            if day is None:
                continue
            status = (row["status"] if row["status"] in LESSON_STATES
                      else LESSON_WAIT)
            self.lessons.append(Lesson(
                uid=row["uid"], day=day, time=row["time"],
                student=row["student"],
                lesson_format=row["lesson_format"] or FORMATS[0],
                direction=row["direction"] or DIRECTIONS[2],
                status=status, price=_int(row["price"]), note=row["note"]))

        self.payments = []
        for row in self._conn.execute("SELECT * FROM payments ORDER BY uid"):
            day = dates.parse_date(row["day"])
            if day is None:
                continue
            self.payments.append(Payment(
                uid=row["uid"], day=day, student=row["student"],
                what=row["what"], amount=_int(row["amount"])))

    # --- перенос старых txt ----------------------------------------------

    def _import_legacy(self) -> None:
        """Однократный перенос из четырёх txt в базу."""
        folder = self.folder
        if not any((folder / name).exists() for name in LEGACY_FILES):
            return

        students: list[Student] = []
        for row in _read_rows(folder / STUDENTS_FILE):
            if len(row) < 2 or not row[1]:
                continue
            row += [""] * (7 - len(row))
            students.append(Student(
                uid=_int(row[0]),
                name=row[1],
                phone=row[2],
                email=row[3],
                lesson_format=row[4] or FORMATS[0],
                direction=row[5] or DIRECTIONS[2],
                note=row[6],
            ))

        subscriptions: list[Subscription] = []
        for row in _read_rows(folder / SUBSCRIPTIONS_FILE):
            if len(row) < 5:
                continue
            row += [""] * (7 - len(row))
            day = dates.parse_date(row[1]) or date.today()
            subscriptions.append(Subscription(
                uid=_int(row[0]), bought=day, student=row[2],
                direction=row[3], total=_int(row[4]), used=_int(row[5]),
                price=_int(row[6])))

        lessons: list[Lesson] = []
        for row in _read_rows(folder / LESSONS_FILE):
            if len(row) < 4:
                continue
            row += [""] * (9 - len(row))
            day = dates.parse_date(row[1])
            if day is None:
                continue
            status = row[6] if row[6] in LESSON_STATES else LESSON_WAIT
            lessons.append(Lesson(
                uid=_int(row[0]), day=day, time=row[2], student=row[3],
                lesson_format=row[4] or FORMATS[0],
                direction=row[5] or DIRECTIONS[2], status=status,
                price=_int(row[7]), note=row[8]))

        payments: list[Payment] = []
        for row in _read_rows(folder / PAYMENTS_FILE):
            if len(row) < 4:
                continue
            row += [""] * (5 - len(row))
            day = dates.parse_date(row[1])
            if day is None:
                continue
            payments.append(Payment(
                uid=_int(row[0]), day=day, student=row[2], what=row[3],
                amount=_int(row[4])))

        # Нумерация: если в txt номеров не было, проставляем по порядку.
        for items in (students, subscriptions, lessons, payments):
            seen: set[int] = set()
            for item in items:
                if item.uid <= 0 or item.uid in seen:
                    item.uid = max(seen, default=0) + 1
                seen.add(item.uid)

        self.students, self.subscriptions = students, subscriptions
        self.lessons, self.payments = lessons, payments
        self.save_students()
        self.save_subscriptions()
        self.save_lessons()
        self.save_payments()
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("legacy_migrated", datetime.now().strftime("%Y-%m-%d %H:%M")))

        # Старые файлы не удаляем — убираем в архив.
        archive = folder / ARCHIVE_DIR
        try:
            archive.mkdir(parents=True, exist_ok=True)
            for name in LEGACY_FILES:
                source = folder / name
                if source.exists():
                    os.replace(source, archive / name)
        except OSError:
            pass

        if students or lessons or payments:
            self.notice = (
                f"Данные из старых файлов перенесены в базу: "
                f"{len(students)} {plural(len(students), 'ученик', 'ученика', 'учеников')}, "
                f"{len(lessons)} {lessons_word(len(lessons))}, "
                f"{len(payments)} {plural(len(payments), 'оплата', 'оплаты', 'оплат')}. "
                f"Старые файлы лежат в папке Архив старой версии.")

    # --- сохранение ------------------------------------------------------

    def save_students(self) -> None:
        self._write("students",
                    ("uid", "name", "phone", "email", "lesson_format",
                     "direction", "note"),
                    [(s.uid, s.name, s.phone, s.email, s.lesson_format,
                      s.direction, s.note) for s in self.students])
        self._refresh_files()

    def save_subscriptions(self) -> None:
        self._write("subscriptions",
                    ("uid", "bought", "started", "student", "direction",
                     "total", "used", "price"),
                    [(s.uid, dates.fmt_date(s.bought),
                      dates.fmt_date(s.begins), s.student,
                      s.direction, s.total, s.used, s.price)
                     for s in self.subscriptions])
        self._refresh_files()

    def save_lessons(self) -> None:
        self._write("lessons",
                    ("uid", "day", "time", "student", "lesson_format",
                     "direction", "status", "price", "note"),
                    [(l.uid, dates.fmt_date(l.day), l.time, l.student,
                      l.lesson_format, l.direction, l.status, l.price,
                      l.note) for l in self.lessons])
        self._refresh_files()

    def save_payments(self) -> None:
        self._write("payments", ("uid", "day", "student", "what", "amount"),
                    [(p.uid, dates.fmt_date(p.day), p.student, p.what,
                      p.amount) for p in self.payments])
        self._refresh_files()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # --- выгрузка в текст -------------------------------------------------

    def export_text(self, path: Path | None = None) -> Path:
        """Читаемый текстовый отчёт по всем данным — для диктора и глаз."""
        target = Path(path) if path is not None else self.folder / "Выгрузка.txt"
        lines = ["Мажорный лад Расписание. Выгрузка данных.",
                 f"Сделана {date.today():%d.%m.%Y}.", ""]
        lines.append(f"Ученики: {len(self.students)}.")
        for student in sorted(self.students, key=lambda s: s.name.casefold()):
            used, total = self.balance(student.name)
            balance = (f"Уроки {used} из {total}" if total
                       else "Абонемента нет")
            lines.append(f"{student.name}. {balance}. "
                         f"{student.lesson_format}, {student.direction}.")
            if student.phone:
                lines.append(f"Телефон: {student.phone}.")
        lines.append("")
        lines.append(f"Уроки: {len(self.lessons)}.")
        for lesson in sorted(self.lessons, key=lambda l: l.stamp):
            lines.append(f"{dates.fmt_date(lesson.day)} в {lesson.time}, "
                         f"{lesson.student}, {lesson.direction}, "
                         f"{lesson.status}.")
        lines.append("")
        lines.append(f"Оплаты: {len(self.payments)}.")
        for payment in sorted(self.payments, key=lambda p: (p.day, p.uid)):
            lines.append(f"{dates.fmt_date(payment.day)}, {payment.student}, "
                         f"{payment.what}, {money(payment.amount)} рублей.")
        lines.append("")
        today = date.today()
        month = dates.MONTHS_NOM[today.month - 1]
        lines.append(f"Заработано за {month} {today.year}: "
                     f"{money(self.earned_in_month(today.year, today.month))} "
                     f"рублей.")
        tmp = target.parent / (target.name + ".tmp")
        tmp.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        os.replace(tmp, target)
        return target

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
                         bought: date | None = None,
                         started: date | None = None) -> Subscription:
        bought_day = bought or date.today()
        item = Subscription(
            uid=self._next_uid(self.subscriptions),
            bought=bought_day,
            student=student,
            direction=direction,
            total=max(0, int(total)),
            used=0,
            price=int(price),
            started=started or bought_day,
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
                   lesson_format: str | None = None,
                   direction: str | None = None,
                   note: str = "") -> Lesson:
        # Не указали формат или тип — берём из карточки ученика: у него
        # занятия онлайн, а урок не должен молча стать «очно».
        owner = self.find_student(student)
        if lesson_format is None:
            lesson_format = owner.lesson_format if owner else FORMATS[0]
        if direction is None:
            direction = owner.direction if owner else DIRECTIONS[2]
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
