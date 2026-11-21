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
from datetime import date, datetime, timedelta
from pathlib import Path

from . import dates, mirror, paths

DB_FILE = "Мажорный лад Расписание.db"
BACKUP_DIR = "Резерв"
ARCHIVE_DIR = "Архив старой версии"
# Выгруженные абонементы лежат отдельной папкой внутри `Data`.
SUBS_DIR = "Абонементы"
BACKUP_KEEP = 14
SCHEMA_VERSION = 3

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
    price INTEGER NOT NULL DEFAULT 0,
    finished INTEGER NOT NULL DEFAULT 0
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
    note TEXT NOT NULL DEFAULT '',
    subscription INTEGER NOT NULL DEFAULT 0
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
    # Законченный абонемент — история: уроки из него не списываются и
    # новый урок на него не записывается. Помечается сам, когда уроки
    # кончились, либо вручную, когда заводится следующий абонемент.
    finished: bool = False

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
    # Номер абонемента, из которого списан урок. Ноль — урок записан до
    # того, как абонементы стали вести историю; для него счётчик считается
    # по старому, по датам.
    subscription: int = 0

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


def plan_shift(lessons: list[Lesson], lesson: Lesson, new_day: date,
               new_time: str) -> list[tuple[Lesson, date, str]]:
    """Куда поедут уроки графика при переносе одного из них.

    Сдвиг такой же, как задали у выбранного урока: столько же дней и,
    если время есть у обоих, столько же часов и минут. Сдвигается весь
    график того же ученика из того же абонемента после выбранного дня —
    чужой урок ученика на тот же день остаётся на месте.
    """
    day_delta = (new_day - lesson.day).days
    old_time = dates.parse_time(lesson.time)
    new_time_parsed = dates.parse_time(new_time)
    minute_delta = 0
    if old_time is not None and new_time_parsed is not None:
        minute_delta = ((new_time_parsed[0] * 60 + new_time_parsed[1])
                        - (old_time[0] * 60 + old_time[1]))
    moved: list[tuple[Lesson, date, str]] = []
    for row in lessons:
        if (row is lesson or row.student != lesson.student
                or row.subscription != lesson.subscription
                or row.day <= lesson.day):
            continue
        day = row.day + timedelta(days=day_delta)
        time = row.time
        minutes = dates.parse_time(row.time)
        if minutes is not None and minute_delta:
            total = minutes[0] * 60 + minutes[1] + minute_delta
            day += timedelta(days=total // 1440)
            total %= 1440
            time = dates.fmt_time(total // 60, total % 60)
        moved.append((row, day, time))
    return moved


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
        # Номер сохранения — «часы» для синхронизации: по нему видно,
        # чья копия в облаке свежее, не сверяя время на устройствах.
        self.save_no = self._read_save_no()
        # Сюда окно кладёт действие «положить копию в облако»; пусто —
        # копирование выключено.
        self.sync = None
        if migrated:
            self._import_legacy()
        # Счётчик уроков — это число уроков, записанных в абонемент, а не
        # цифра, которую кто-то вписал руками. Сверяем её при каждом
        # запуске: иначе «вписал 4 из 8, а дат не внёс» так и висело бы.
        self.recount_used()
        self._refresh_files()

    # --- база ------------------------------------------------------------

    def _migrate(self) -> None:
        """Довести старую базу до текущей схемы, ничего не теряя."""
        sub_columns = {row["name"] for row in
                       self._conn.execute("PRAGMA table_info(subscriptions)")}
        lesson_columns = {row["name"] for row in
                          self._conn.execute("PRAGMA table_info(lessons)")}
        statements = []
        if "started" not in sub_columns:
            statements.append(
                "ALTER TABLE subscriptions ADD COLUMN started "
                "TEXT NOT NULL DEFAULT ''")
        if "finished" not in sub_columns:
            statements.append(
                "ALTER TABLE subscriptions ADD COLUMN finished "
                "INTEGER NOT NULL DEFAULT 0")
        if "subscription" not in lesson_columns:
            statements.append(
                "ALTER TABLE lessons ADD COLUMN subscription "
                "INTEGER NOT NULL DEFAULT 0")
        with self._conn:
            for statement in statements:
                self._conn.execute(statement)
            # Старый учёт не знал про историю: абонемент, у которого все
            # уроки уже выбраны, сразу становится законченным.
            self._conn.execute(
                "UPDATE subscriptions SET finished = 1 "
                "WHERE total > 0 AND used >= total")
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

    def _read_save_no(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'save_no'").fetchone()
            return int(row[0]) if row else 0
        except (sqlite3.Error, TypeError, ValueError):
            return 0

    def set_save_no(self, value: int) -> None:
        """Поставить номер сохранения (после взятия копии из облака)."""
        self.save_no = max(0, int(value))
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES "
                "('save_no', ?)", (str(self.save_no),))

    def _write(self, table: str, columns: tuple[str, ...],
               rows: list[tuple]) -> None:
        """Таблица переписывается целиком в одной транзакции.

        Данных немного (сотни строк), зато не бывает полузаписанного
        состояния: либо вся таблица новая, либо вся старая. Номер
        сохранения растёт здесь же — вместе с данными, одной транзакцией.
        """
        marks = ", ".join("?" for _ in columns)
        with self._conn:
            self._conn.execute(f"DELETE FROM {table}")
            self._conn.executemany(
                f"INSERT INTO {table}({', '.join(columns)}) VALUES ({marks})",
                rows)
            self.save_no += 1
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES "
                "('save_no', ?)", (str(self.save_no),))
        if self.sync is not None:
            try:
                self.sync(self)
            except Exception:  # noqa: BLE001 — копия не ломает сохранение
                pass

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
                started=started, finished=bool(row["finished"])))

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
                status=status, price=_int(row["price"]), note=row["note"],
                subscription=_int(row["subscription"])))

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

        # В старом учёте абонемент с выбранными уроками ничем не
        # отличался от действующего — теперь он уходит в историю.
        self._mark_exhausted()

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
                     "total", "used", "price", "finished"),
                    [(s.uid, dates.fmt_date(s.bought),
                      dates.fmt_date(s.begins), s.student,
                      s.direction, s.total, s.used, s.price,
                      1 if s.finished else 0)
                     for s in self.subscriptions])
        self._refresh_files()

    def save_lessons(self) -> None:
        self._write("lessons",
                    ("uid", "day", "time", "student", "lesson_format",
                     "direction", "status", "price", "note", "subscription"),
                    [(l.uid, dates.fmt_date(l.day), l.time, l.student,
                      l.lesson_format, l.direction, l.status, l.price,
                      l.note, l.subscription) for l in self.lessons])
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
            history = self.finished_subscriptions(student.name)
            if history:
                lines.append(f"Законченных абонементов: {len(history)}.")
            for sub in history:
                lines.append(
                    f"Законченный абонемент: куплен "
                    f"{dates.fmt_date(sub.bought)}, действует с "
                    f"{dates.fmt_date(sub.begins)}, уроков {sub.total}, "
                    f"проведено {sub.used}.")
        lines.append("")
        lines.append(f"Уроки: {len(self.lessons)}.")
        for lesson in sorted(self.lessons, key=lambda l: l.stamp):
            when = dates.fmt_date(lesson.day)
            if lesson.time:
                when += f" в {lesson.time}"
            lines.append(f"{when}, {lesson.student}, {lesson.direction}, "
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
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / (target.name + ".tmp")
        tmp.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        os.replace(tmp, target)
        return target

    def subscription_lessons(self, sub: Subscription) -> list[Lesson]:
        """Все уроки абонемента по порядку — что записано, то и есть."""
        return sorted((l for l in self.lessons if l.subscription == sub.uid),
                      key=lambda l: l.stamp)

    def export_subscription(self, sub: Subscription,
                            path: Path | None = None) -> Path:
        """Один абонемент текстом: покупка, начало и все уроки по датам.

        Отдельный файл на каждый абонемент: его удобно отдать ученику или
        просто прочитать диктором, не продираясь через всю базу.
        """
        if path is not None:
            target = Path(path)
        else:
            name = sub.student
            for bad in '<>:"/\\|?*':
                name = name.replace(bad, "-")
            name = name.strip().strip(".") or "Без имени"
            target = self.folder / SUBS_DIR / (
                f"Абонемент {name} {dates.fmt_date(sub.bought)}.txt")
        lines = ["Мажорный лад Расписание. Абонемент.", ""]
        lines.append(f"Ученик: {sub.student}.")
        lines.append(f"Направление: {sub.direction}.")
        lines.append(f"Дата покупки: {dates.fmt_date(sub.bought)}.")
        lines.append(f"Абонемент действует с: {dates.fmt_date(sub.begins)}.")
        if sub.total > 0:
            word = plural(sub.total, "урок", "урока", "уроков")
            lines.append(f"Уроков в абонементе: {sub.total} {word}.")
            lines.append(f"Проведено: {sub.used} из {sub.total}. "
                         f"Осталось: {sub.left}.")
        else:
            word = plural(sub.used, "урок", "урока", "уроков")
            lines.append(f"Проведено уроков: {sub.used} {word}.")
        lines.append("Состояние: "
                     + ("законченный." if sub.finished else "действующий."))
        if sub.price:
            lines.append(f"Стоимость: {money(sub.price)} рублей.")
        lines.append("")
        lessons = self.subscription_lessons(sub)
        if lessons:
            lines.append(f"Уроки: {len(lessons)}.")
            for lesson in lessons:
                when = dates.fmt_date(lesson.day)
                if lesson.time:
                    when += f" в {lesson.time}"
                lines.append(f"{when}, {lesson.status}.")
        else:
            lines.append("Уроков в этом абонементе пока нет.")
        target.parent.mkdir(parents=True, exist_ok=True)
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

    def rename_student(self, old: str, new: str) -> None:
        """Переименовать ученика везде: абонементы, уроки и оплаты.

        Абонементы, уроки и оплаты держат имя ученика, а не его номер.
        Без этой замены переименование оставило бы их висеть на старом
        имени, и половина карточки оказалась бы чужой.
        """
        old = (old or "").strip()
        new = (new or "").strip()
        if not old or not new or old == new:
            return
        for item in self.subscriptions:
            if item.student == old:
                item.student = new
        for lesson in self.lessons:
            if lesson.student == old:
                lesson.student = new
        for payment in self.payments:
            if payment.student == old:
                payment.student = new
        for student in self.students:
            if student.name == old:
                student.name = new
        self.save_subscriptions()
        self.save_lessons()
        self.save_payments()
        self.save_students()

    def remove_student(self, student: Student) -> None:
        """Убрать ученика вместе с его абонементами и уроками.

        Оплаты остаются в журнале: деньги были настоящими, и заработок
        за прошлые месяцы не должен меняться из-за удаления карточки.
        """
        name = student.name
        self.students = [s for s in self.students if s.uid != student.uid]
        self.subscriptions = [s for s in self.subscriptions
                              if s.student != name]
        self.lessons = [l for l in self.lessons if l.student != name]
        self.save_students()
        self.save_subscriptions()
        self.save_lessons()

    def add_subscription(self, student: str, total: int, price: int = 0,
                         direction: str = DIRECTIONS[2],
                         bought: date | None = None,
                         started: date | None = None,
                         finished: bool = False) -> Subscription:
        """Завести абонемент.

        `finished=True` — старый, для истории: его уроки вводятся вручную
        кнопкой «Добавить проведённый урок», деньги в журнал не идут.
        Новый действующий абонемент сам закрывает предыдущий: уроки из
        старого больше не списываются, он остаётся в истории.
        """
        bought_day = bought or date.today()
        if not finished:
            for other in self.subscriptions:
                if other.student == student and not other.finished:
                    other.finished = True
        item = Subscription(
            uid=self._next_uid(self.subscriptions),
            bought=bought_day,
            student=student,
            direction=direction,
            total=max(0, int(total)),
            used=0,
            price=int(price),
            started=started or bought_day,
            finished=finished,
        )
        self.subscriptions.append(item)
        self.save_subscriptions()
        return item

    def update_subscription(self, sub: Subscription) -> bool:
        """Сохранить правки абонемента. Проверяет то, что можно сломать.

        Уроков не может быть меньше, чем уже проведено, и действующий
        абонемент у ученика бывает только один — иначе уроки непонятно из
        чего списывать. Ошибку отдаём ValueError с готовой фразой для
        человека. Возвращаем True, если абонемент сам закрылся: уроки
        досчитались до конца.
        """
        if sub.total > 0 and sub.total < sub.used:
            raise ValueError(
                f"В абонементе уже проведено {sub.used} "
                f"{lessons_word(sub.used)}. Поставь уроков не меньше, "
                f"или ноль — тогда без ограничения.")
        if not sub.finished:
            for other in self.subscriptions:
                if (other.uid != sub.uid and other.student == sub.student
                        and not other.finished):
                    raise ValueError(
                        f"У ученика уже есть действующий абонемент от "
                        f"{dates.fmt_date(other.bought)}. Сначала отметь "
                        f"его законченным в карточке ученика.")
        closed = (not sub.finished and sub.total > 0
                  and sub.used >= sub.total)
        if closed:
            sub.finished = True
        self.save_subscriptions()
        return closed

    def delete_subscription(self, sub: Subscription) -> None:
        """Удалить абонемент совсем.

        Сначала из него надо убрать уроки: иначе они остались бы ни при
        чём, а счётчики разъехались. Оплаты в журнале остаются — деньги
        были настоящими.
        """
        count = len(self.subscription_lessons(sub))
        if count:
            raise ValueError(
                f"В абонементе записано {count} {lessons_word(count)}. "
                f"Сначала убери уроки в карточке ученика, потом удаляй.")
        self.subscriptions = [s for s in self.subscriptions
                              if s.uid != sub.uid]
        self.save_subscriptions()

    def current_subscription(self, student: str) -> Subscription | None:
        """Действующий абонемент: последний незаконченный."""
        mine = [s for s in self.subscriptions
                if s.student == student and not s.finished]
        if not mine:
            return None
        return max(mine, key=lambda s: (s.bought, s.uid))

    def finished_subscriptions(self, student: str) -> list[Subscription]:
        """Законченные абонементы ученика, новые сверху."""
        mine = [s for s in self.subscriptions
                if s.student == student and s.finished]
        return sorted(mine, key=lambda s: (s.begins, s.uid), reverse=True)

    def last_done_day(self, sub: Subscription) -> date | None:
        """Дата последнего проведённого урока в этом абонементе."""
        days = [l.day for l in self.lessons if l.subscription == sub.uid]
        return max(days) if days else None

    def _mark_exhausted(self) -> bool:
        """Абонемент с выбранными уроками — законченный."""
        changed = False
        for sub in self.subscriptions:
            if not sub.finished and sub.total > 0 and sub.used >= sub.total:
                sub.finished = True
                changed = True
        if changed:
            self.save_subscriptions()
        return changed

    def _link_lessons(self) -> bool:
        """Привязать старые проведённые уроки к их абонементу по дате.

        Уроки, внесённые до появления этой связи, лежали без абонемента.
        Берём тот, что действовал в день урока (последний начавшийся не
        позже этого дня); если урок раньше всех — самый ранний абонемент
        ученика. Так счётчик видит все его уроки, а не только новые.
        """
        by_student: dict[str, list[Subscription]] = {}
        for sub in self.subscriptions:
            by_student.setdefault(sub.student, []).append(sub)
        changed = False
        for lesson in self.lessons:
            if lesson.subscription or lesson.status != LESSON_DONE:
                continue
            subs = by_student.get(lesson.student)
            if not subs:
                continue
            before = [s for s in subs if s.begins <= lesson.day]
            if before:
                sub = max(before, key=lambda s: (s.begins, s.uid))
            else:
                sub = min(subs, key=lambda s: (s.begins, s.uid))
            lesson.subscription = sub.uid
            changed = True
        if changed:
            self.save_lessons()
        return changed

    def recount_used(self) -> bool:
        """Пересчитать счётчики уроков по самим урокам.

        В абонементе хранится только число проведённых уроков, и раньше
        его можно было вписать руками, не заведя ни одного урока: «4 из 8»
        при пустом списке дат. Теперь цифра считается заново — сколько
        уроков с отметкой «проведён» записано в абонемент, столько и
        показывается.

        Абонемент, где уроки кончились, закрывается сам. Открывается
        обратно только тот, что закрылся именно из-за нехватки уроков и
        теперь имеет свободные места, — старый, законченный вручную,
        остаётся в истории.
        """
        self._link_lessons()
        counts: dict[int, int] = {}
        for lesson in self.lessons:
            if lesson.status == LESSON_DONE and lesson.subscription:
                counts[lesson.subscription] = (
                    counts.get(lesson.subscription, 0) + 1)
        latest: dict[str, Subscription] = {}
        for sub in self.subscriptions:
            old = latest.get(sub.student)
            if old is None or (sub.bought, sub.uid) > (old.bought, old.uid):
                latest[sub.student] = sub
        changed = False
        for sub in self.subscriptions:
            used = counts.get(sub.uid, 0)
            if sub.total > 0:
                used = min(used, sub.total)
            was_full = sub.total > 0 and sub.used >= sub.total
            if sub.used != used:
                sub.used = used
                changed = True
            full = sub.total > 0 and sub.used >= sub.total
            if full and not sub.finished:
                sub.finished = True
                changed = True
            elif (was_full and not full and sub.finished
                  and latest.get(sub.student) is sub):
                sub.finished = False
                changed = True
        if changed:
            self.save_subscriptions()
        return changed

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
                   note: str = "",
                   status: str = LESSON_WAIT,
                   subscription: int = 0) -> Lesson:
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
            status=status if status in LESSON_STATES else LESSON_WAIT,
            note=note,
            subscription=int(subscription or 0),
        )
        self.lessons.append(lesson)
        self.save_lessons()
        return lesson

    def bump_used(self, sub: Subscription) -> None:
        """Записать один проведённый урок в абонемент.

        Сверх купленного счётчик не растёт: когда уроки кончились,
        абонемент сам становится законченным.
        """
        if sub.total > 0:
            sub.used = min(sub.total, sub.used + 1)
        else:
            sub.used += 1
        if sub.total > 0 and sub.used >= sub.total:
            sub.finished = True
        self.save_subscriptions()

    def add_done_lesson(self, sub: Subscription, day: date,
                        count: int = 1) -> list[Lesson]:
        """Проведённые уроки в абонемент, без времени — они уже прошли.

        Так вводятся старые уроки и дописываются пропущенные в действующем
        абонементе. За один день уроков бывает несколько: `count` задаёт,
        сколько записать сразу. Счётчик прибавляется сам, порядок в файле
        «Проведённые уроки» программа расставляет по датам.
        """
        try:
            count = max(1, int(count))
        except (TypeError, ValueError):
            count = 1
        if sub.total > 0 and sub.left <= 0:
            raise ValueError("В абонементе не осталось уроков.")
        if sub.total > 0 and count > sub.left:
            left = sub.left
            word = plural(left, "урок", "урока", "уроков")
            raise ValueError(
                f"В абонементе осталось {left} {word}, а записать просят "
                f"{count}. Уменьши количество.")
        # Проверка «урок на эту дату уже есть» нужна против случайного
        # двойного ввода одного урока. Когда количество задано явно,
        # это уже намерение: несколько уроков в один день — обычное дело.
        if count == 1:
            for lesson in self.lessons:
                if (lesson.subscription == sub.uid and lesson.day == day
                        and lesson.status == LESSON_DONE):
                    raise ValueError(
                        "У этого абонемента уже есть проведённый урок "
                        f"{dates.fmt_date(day)}.")
        owner = self.find_student(sub.student)
        added: list[Lesson] = []
        for _ in range(count):
            lesson = Lesson(
                uid=self._next_uid(self.lessons),
                day=day,
                time="",
                student=sub.student,
                lesson_format=owner.lesson_format if owner else FORMATS[0],
                direction=sub.direction
                or (owner.direction if owner else DIRECTIONS[2]),
                status=LESSON_DONE,
                subscription=sub.uid,
            )
            self.lessons.append(lesson)
            added.append(lesson)
        self.save_lessons()
        if sub.total > 0:
            sub.used = min(sub.total, sub.used + count)
        else:
            sub.used += count
        if sub.total > 0 and sub.used >= sub.total:
            sub.finished = True
        self.save_subscriptions()
        return added

    def update_lesson(self, lesson: Lesson) -> None:
        self.save_lessons()

    def subscription_by_uid(self, uid: int) -> Subscription | None:
        for sub in self.subscriptions:
            if sub.uid == uid:
                return sub
        return None

    def done_lessons(self, sub: Subscription) -> list[Lesson]:
        """Проведённые уроки абонемента по датам — для правки и удаления."""
        return sorted((l for l in self.lessons
                       if l.subscription == sub.uid and l.status == LESSON_DONE),
                      key=lambda l: (l.day, l.time))

    def delete_lesson(self, lesson: Lesson) -> None:
        """Убрать урок из журнала.

        Если урок был проведён и привязан к абонементу — счётчик
        возвращается на один назад. Абонемент, который закрылся только
        потому, что уроки кончились, снова становится действующим: урок
        удалили, значит, он снова не до конца пройден.
        """
        if lesson not in self.lessons:
            return
        self.lessons.remove(lesson)
        if lesson.status != LESSON_DONE or not lesson.subscription:
            self.save_lessons()
            return
        sub = self.subscription_by_uid(lesson.subscription)
        if sub is not None:
            was_full = sub.total > 0 and sub.used >= sub.total
            sub.used = max(0, sub.used - 1)
            if was_full:
                mine = [s for s in self.subscriptions if s.student == sub.student]
                latest = max(mine, key=lambda s: (s.bought, s.uid), default=None)
                if latest is not None and latest.uid == sub.uid:
                    sub.finished = False
            self.save_subscriptions()
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

    def payment_by_uid(self, uid: int) -> Payment | None:
        for payment in self.payments:
            if payment.uid == uid:
                return payment
        return None

    def update_payment(self, payment: Payment) -> None:
        """Сохранить правку оплаты: поля уже изменены на месте."""
        if any(p.uid == payment.uid for p in self.payments):
            self.save_payments()

    def delete_payment(self, payment: Payment) -> None:
        """Убрать оплату из журнала совсем."""
        self.payments = [p for p in self.payments if p.uid != payment.uid]
        self.save_payments()

    def earned_in_month(self, year: int, month: int) -> int:
        return sum(p.amount for p in self.payments
                   if p.day.year == year and p.day.month == month)
