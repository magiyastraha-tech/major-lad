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
SCHEMA_VERSION = 5

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
    finished INTEGER NOT NULL DEFAULT 0,
    trial INTEGER NOT NULL DEFAULT 0,
    single INTEGER NOT NULL DEFAULT 0,
    lesson_format TEXT NOT NULL DEFAULT '',
    future INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS blocks (
    uid INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    time_from TEXT NOT NULL DEFAULT '',
    time_to TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
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
    # Пробный урок. Не абонемент, а одно занятие: уроков всегда один,
    # денег нет, в истории он так и называется — «пробный урок».
    # Формат («Очно», «Онлайн») нужен только разовым занятиям: у
    # абонемента он у ученика, а такой урок бывает отдельным.
    trial: bool = False
    # Разовый урок. Тоже одно занятие, но оплаченное: цена своя, в
    # историю он попадает сразу и действующий абонемент не закрывает.
    single: bool = False
    lesson_format: str = ""
    # Будущий абонемент. Куплен заранее, пока предыдущий ещё не кончился.
    # Уроки из него не списываются, действующим он не считается, пока
    # прежний не заполнится. Тогда он сам становится действующим.
    future: bool = False

    @property
    def one_off(self) -> bool:
        """Одно занятие вместо абонемента: пробный или разовый урок."""
        return self.trial or self.single

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


@dataclass
class Block:
    """Исключение: день или часы, когда уроков нет.

    Пустые `start` и `end` — закрыт весь день. Причина (`note`) нужна
    только преподавателю: ученику программа скажет просто, что это время
    занято.
    """
    uid: int
    day: date
    start: str = ""
    end: str = ""
    note: str = ""

    @property
    def all_day(self) -> bool:
        return not self.start or not self.end


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


def minutes(time: str) -> int:
    """«13:30» → 810. Пустая строка или мусор — 0."""
    hour, minute = dates.parse_time(time) or (0, 0)
    return hour * 60 + minute


def plural(count: int, one: str, few: str, many: str) -> str:
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def lessons_word(count: int) -> str:
    return plural(count, "урок", "урока", "уроков")


def plan_move_to_end(lessons: list[Lesson], lesson: Lesson
                     ) -> tuple[date, str] | None:
    """Куда встанет урок, если увести его в конец графика.

    Урок сохраняет свой день недели и время — вторник 18:00 остаётся
    вторником 18:00 — и уезжает на ближайший такой день после последнего
    урока его графика. Количество уроков от этого не меняется, меняется
    только дата.
    """
    graph = [row for row in lessons
             if row.student == lesson.student
             and row.subscription == lesson.subscription]
    if not graph:
        return None
    weekday = lesson.day.weekday()
    last_day = max(row.day for row in graph)
    busy = {(row.day, row.time) for row in lessons if row is not lesson}
    day = last_day
    # Горизонт поиска: два года вперёд. Дальше искать нечего — расписание
    # не может быть занято так плотно.
    for _ in range(730):
        day = day + timedelta(days=1)
        if day.weekday() != weekday:
            continue
        if lesson.time:
            if (day, lesson.time) in busy:
                continue
        elif any(row.day == day and row.student == lesson.student
                 for row in lessons if row is not lesson):
            continue
        return day, lesson.time
    return None


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
        self.blocks: list[Block] = []

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
        if "trial" not in sub_columns:
            statements.append(
                "ALTER TABLE subscriptions ADD COLUMN trial "
                "INTEGER NOT NULL DEFAULT 0")
        if "single" not in sub_columns:
            statements.append(
                "ALTER TABLE subscriptions ADD COLUMN single "
                "INTEGER NOT NULL DEFAULT 0")
        if "lesson_format" not in sub_columns:
            statements.append(
                "ALTER TABLE subscriptions ADD COLUMN lesson_format "
                "TEXT NOT NULL DEFAULT ''")
        if "future" not in sub_columns:
            statements.append(
                "ALTER TABLE subscriptions ADD COLUMN future "
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

    def ensure_schema(self) -> None:
        """Создать недостающие таблицы в уже открытой базе.

        Нужна после взятия копии из облака: там база может быть от
        прежней версии, без новых таблиц, — а `load()` читает их все.
        """
        with self._conn:
            self._conn.executescript(_SCHEMA)

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
                started=started, finished=bool(row["finished"]),
                trial=bool(row["trial"]), single=bool(row["single"]),
                lesson_format=row["lesson_format"] or "",
                future=bool(row["future"])))

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

        self.blocks = []
        for row in self._conn.execute("SELECT * FROM blocks ORDER BY uid"):
            day = dates.parse_date(row["day"])
            if day is None:
                continue
            self.blocks.append(Block(
                uid=row["uid"], day=day, start=row["time_from"],
                end=row["time_to"], note=row["note"]))

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
                     "total", "used", "price", "finished", "trial",
                     "single", "lesson_format", "future"),
                    [(s.uid, dates.fmt_date(s.bought),
                      dates.fmt_date(s.begins), s.student,
                      s.direction, s.total, s.used, s.price,
                      1 if s.finished else 0, 1 if s.trial else 0,
                      1 if s.single else 0, s.lesson_format,
                      1 if s.future else 0)
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

    def save_blocks(self) -> None:
        self._write("blocks", ("uid", "day", "time_from", "time_to", "note"),
                    [(b.uid, dates.fmt_date(b.day), b.start, b.end, b.note)
                     for b in self.blocks])
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
                if sub.one_off:
                    title = "Пробный урок" if sub.trial else "Разовый урок"
                    line = f"{title}: {dates.fmt_date(sub.bought)}"
                    parts = [sub.lesson_format.casefold(), sub.direction]
                    if sub.single and sub.price:
                        parts.append(f"{money(sub.price)} рублей")
                    tail = ", ".join(part for part in parts if part)
                    if tail:
                        line += f", {tail}"
                    line += ", проведён." if sub.used else "."
                    lines.append(line)
                    continue
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
        lines.append(f"Исключения: {len(self.blocks)}.")
        for block in sorted(self.blocks, key=lambda b: (b.day, b.start)):
            when = dates.fmt_date(block.day)
            if block.all_day:
                when += ", весь день"
            else:
                when += f", с {block.start} до {block.end}"
            if block.note:
                when += f", причина: {block.note}"
            lines.append(when + ".")
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
            if sub.trial:
                prefix = "Пробный урок"
            elif sub.single:
                prefix = "Разовый урок"
            else:
                prefix = "Абонемент"
            target = self.folder / SUBS_DIR / (
                f"{prefix} {name} {dates.fmt_date(sub.bought)}.txt")
        if sub.one_off:
            title = "Пробный урок" if sub.trial else "Разовый урок"
            date_label = "Дата пробного урока" if sub.trial else "Дата разового урока"
            lines = [f"Мажорный лад Расписание. {title}.", ""]
            lines.append(f"Ученик: {sub.student}.")
            lines.append(f"Направление: {sub.direction}.")
            if sub.lesson_format:
                lines.append(f"Формат: {sub.lesson_format}.")
            lines.append(f"{date_label}: {dates.fmt_date(sub.bought)}.")
            if sub.price:
                lines.append(f"Стоимость: {money(sub.price)} рублей.")
            lines.append("Состояние: "
                         + ("проведён." if sub.used else "записан."))
            lines.append("")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.parent / (target.name + ".tmp")
            tmp.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
            os.replace(tmp, target)
            return target
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
                         finished: bool = False,
                         trial: bool = False,
                         single: bool = False,
                         lesson_format: str = "") -> Subscription:
        """Завести абонемент.

        `finished=True` — старый, для истории: его уроки вводятся вручную
        кнопкой «Добавить проведённый урок», деньги в журнал не идут.

        Если у ученика ещё есть действующий абонемент, новый не закрывает
        его, а ложится в «Будущие абонементы». Когда в действующем
        кончатся уроки, он уйдёт в историю, а будущий станет действующим.

        `trial=True` — пробный урок: одно занятие вместо абонемента. По
        смыслу это история, поэтому он сразу законченный и действующего
        абонемента ученику не закрывает. Уроков у него всегда один,
        цена не спрашивается и в журнал оплат не идёт.

        `single=True` — разовый урок: тоже одно занятие, но оплаченное,
        с собственной ценой. Устроен так же: сразу в истории, действующий
        абонемент не закрывает.
        """
        bought_day = bought or date.today()
        if trial:
            total, price, finished = 1, 0, True
        elif single:
            total, finished = 1, True
        future = False
        if not finished:
            # Абонемент, где уроки уже выбраны, — закрываем: он не может
            # держать очередь, даже если отметку ещё не поставили.
            for other in self.subscriptions:
                if (other.student == student and not other.finished
                        and not other.future and not other.one_off
                        and other.total > 0 and other.used >= other.total):
                    other.finished = True
            active = [s for s in self.subscriptions
                      if s.student == student and not s.finished
                      and not s.future and not s.one_off]
            if active:
                # Действующий ещё не кончился — новый ждёт своей очереди.
                future = True
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
            trial=trial,
            single=single,
            lesson_format=lesson_format if (trial or single) else "",
            future=future,
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
        if not sub.finished and not sub.future:
            for other in self.subscriptions:
                if (other.uid != sub.uid and other.student == sub.student
                        and not other.finished and not other.future
                        and not other.one_off):
                    raise ValueError(
                        f"У ученика уже есть действующий абонемент от "
                        f"{dates.fmt_date(other.bought)}. Сначала отметь "
                        f"его законченным в карточке ученика.")
        closed = (not sub.finished and sub.total > 0
                  and sub.used >= sub.total)
        if closed:
            sub.finished = True
            self._promote_future(sub.student, save=False)
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
        """Действующий абонемент: последний незаконченный и не будущий."""
        mine = [s for s in self.subscriptions
                if s.student == student and not s.finished
                and not s.future and not s.one_off]
        if not mine:
            return None
        return max(mine, key=lambda s: (s.bought, s.uid))

    def future_subscriptions(self, student: str) -> list[Subscription]:
        """Будущие абонементы ученика: куплены заранее, ещё не начались."""
        mine = [s for s in self.subscriptions
                if s.student == student and s.future and not s.finished]
        return sorted(mine, key=lambda s: (s.begins, s.bought, s.uid))

    def finished_subscriptions(self, student: str) -> list[Subscription]:
        """Законченные абонементы ученика, новые сверху."""
        mine = [s for s in self.subscriptions
                if s.student == student and s.finished]
        return sorted(mine, key=lambda s: (s.begins, s.uid), reverse=True)

    def _promote_future(self, student: str, save: bool = True) -> bool:
        """Разбудить ближайший будущий абонемент, если действующего нет.

        Действующий кончился и ушёл в историю — следующий по очереди
        (самый ранний по началу) становится действующим. Будущих
        абонементов у ученика может быть несколько: они ждут по порядку.
        """
        active = [s for s in self.subscriptions
                  if s.student == student and not s.finished
                  and not s.future and not s.one_off]
        if active:
            return False
        waiting = [s for s in self.subscriptions
                   if s.student == student and s.future and not s.finished]
        if not waiting:
            return False
        winner = min(waiting, key=lambda s: (s.begins, s.bought, s.uid))
        winner.future = False
        if save:
            self.save_subscriptions()
        return True

    def last_done_day(self, sub: Subscription) -> date | None:
        """Дата последнего проведённого урока в этом абонементе."""
        days = [l.day for l in self.lessons if l.subscription == sub.uid]
        return max(days) if days else None

    def _mark_exhausted(self) -> bool:
        """Абонемент с выбранными уроками — законченный.

        Освободившееся место занимает ближайший будущий абонемент.
        """
        changed = False
        students: set[str] = set()
        for sub in self.subscriptions:
            if (not sub.finished and not sub.future and sub.total > 0
                    and sub.used >= sub.total):
                sub.finished = True
                students.add(sub.student)
                changed = True
        for student in students:
            if self._promote_future(student, save=False):
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
            if sub.future or sub.one_off:
                continue
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
                  and not sub.one_off
                  and not sub.future
                  and latest.get(sub.student) is sub):
                sub.finished = False
                changed = True
        # Действующего нет, а будущий ждёт — пора ему начинаться.
        for student in {s.student for s in self.subscriptions}:
            if self._promote_future(student, save=False):
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
            self._promote_future(sub.student, save=False)
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
                lesson_format=(sub.lesson_format
                               or (owner.lesson_format if owner
                                   else FORMATS[0])),
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
            self._promote_future(sub.student, save=False)
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
            if was_full and not sub.one_off and not sub.future:
                mine = [s for s in self.subscriptions
                        if s.student == sub.student and not s.future
                        and not s.one_off]
                latest = max(mine, key=lambda s: (s.bought, s.uid), default=None)
                if latest is not None and latest.uid == sub.uid:
                    sub.finished = False
            self.save_subscriptions()
        self.save_lessons()

    def delete_future_waiting_lessons(self, student_name: str,
                                      today: date | None = None) -> int:
        """Убрать будущие непроведённые уроки ученика перед перестройкой.

        Прошедшие и проведённые уроки остаются как есть: расписание меняется
        вперёд, история назад не переписывается.
        """
        edge = today or date.today()
        keep: list[Lesson] = []
        removed = 0
        for lesson in self.lessons:
            if (lesson.student == student_name
                    and lesson.status == LESSON_WAIT
                    and lesson.day > edge):
                removed += 1
                continue
            keep.append(lesson)
        if removed:
            self.lessons = keep
            self.save_lessons()
        return removed

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

    def _subs_payment_label(self, total: int) -> str:
        """Подпись записи журнала для абонемента с таким числом уроков."""
        if total:
            return (f"Абонемент, {total} "
                    + plural(total, "занятие", "занятия", "занятий"))
        return "Абонемент"

    def sync_subscription_payment(self, sub, old_day: date | None = None,
                                  old_total: int | None = None) -> None:
        """Привести запись журнала оплат в соответствие с абонементом.

        Запись, заведённая вместе с абонементом или разовым уроком, ищется
        по ученику, дате покупки и подписи. Нашлась — правятся дата,
        подпись и сумма: заработок и журнал держат одну и ту же цифру.
        Не нашлась, а урок разовый и платный — запись заводится. Сумма
        ноль — парная запись убирается, денег не брали.
        """
        if sub.trial:
            return
        days = {day for day in (sub.bought, old_day) if day is not None}
        found = []
        for payment in self.payments:
            if payment.student != sub.student or payment.day not in days:
                continue
            what = payment.what.strip()
            if sub.single:
                if what in ("Разовый урок", "Урок"):
                    found.append(payment)
            elif what.startswith("Абонемент"):
                found.append(payment)
        pair = None
        if old_total is not None and not sub.single:
            old_label = self._subs_payment_label(old_total)
            for payment in found:
                if payment.what.strip() == old_label:
                    pair = payment
                    break
        if pair is None and found:
            pair = found[0]
        if pair is None:
            if sub.single and sub.price > 0:
                self.add_payment(sub.student, "Разовый урок",
                                 sub.price, sub.bought)
            return
        if sub.price <= 0:
            self.delete_payment(pair)
            return
        pair.day = sub.bought
        pair.what = ("Разовый урок" if sub.single
                     else self._subs_payment_label(sub.total))
        pair.amount = int(sub.price)
        self.save_payments()

    # --- исключения ------------------------------------------------------

    def add_block(self, day: date, start: str = "", end: str = "",
                  note: str = "") -> Block:
        """Закрыть день целиком или промежуток «с … до …»."""
        block = Block(uid=self._next_uid(self.blocks), day=day,
                      start=start, end=end, note=note)
        self.blocks.append(block)
        self.save_blocks()
        return block

    def delete_block(self, block: Block) -> None:
        """Убрать исключение: запись снова открыта."""
        self.blocks = [b for b in self.blocks if b.uid != block.uid]
        self.save_blocks()

    def blocks_between(self, start: date, end: date) -> list[Block]:
        """Исключения за период, по порядку дней и часов."""
        return sorted((b for b in self.blocks if start <= b.day <= end),
                      key=lambda b: (b.day, b.start))

    def overlapping_block(self, day: date, start: str = "",
                          end: str = "") -> Block | None:
        """Уже стоящее исключение, которое накроет это.

        Для целого дня — любое исключение на этот день (и на весь день,
        и на часы: ставить «весь день» поверх них бессмысленно, но
        безвредно, поэтому запрещаем только когда день уже закрыт
        целиком). Для промежутка — то, что пересекается с ним по часам.
        """
        for block in self.blocks:
            if block.day != day:
                continue
            if block.all_day:
                return block
            if not start or not end:
                continue
            if minutes(start) < minutes(block.end) and \
                    minutes(block.start) < minutes(end):
                return block
        return None

    def is_blocked(self, day: date, time: str = "") -> Block | None:
        """Закрыт ли этот день или час — пригодится боту записи."""
        for block in self.blocks:
            if block.day != day:
                continue
            if block.all_day:
                return block
            if not time:
                continue
            if minutes(block.start) <= minutes(time) < minutes(block.end):
                return block
        return None

    def earnings_in_month(self, year: int, month: int) -> dict[str, int]:
        """Из чего сложился заработок месяца.

        Абонементы и разовые уроки считаются по самой записи абонемента:
        деньги получены при покупке, и в заработок идёт ровно та сумма,
        что стоит в абонементе или в разовом уроке. Запись журнала оплат,
        заведённая вместе с ними, второй раз не прибавляется. Пробные
        уроки не считаются вовсе. Услуги и прочие поступления берутся из
        журнала оплат.

        Ключи: active — действующие абонементы, finished — законченные,
        future — купленные заранее, single — разовые уроки, service —
        услуги, other — прочие поступления из журнала.
        """
        def same_month(day: date) -> bool:
            return day.year == year and day.month == month

        parts = {"active": 0, "finished": 0, "future": 0,
                 "single": 0, "service": 0, "other": 0}
        singles = []
        for sub in self.subscriptions:
            if sub.trial:
                continue
            if sub.single:
                # Сумма берётся из самого урока, а не из журнала: правка
                # стоимости меняет заработок сразу.
                if same_month(sub.bought):
                    parts["single"] += int(sub.price)
                singles.append(sub)
                continue
            if not same_month(sub.bought):
                continue
            if sub.finished:
                parts["finished"] += int(sub.price)
            elif sub.future:
                parts["future"] += int(sub.price)
            else:
                parts["active"] += int(sub.price)
        paid_for = set()
        for payment in self.payments:
            if not same_month(payment.day):
                continue
            what = payment.what.strip()
            if what.startswith("Абонемент"):
                continue  # уже посчитан по самому абонементу
            if what == "Пробный урок":
                continue  # пробные уроки не считаем
            if what in ("Разовый урок", "Урок"):
                # Одна запись журнала на каждый разовый урок: она уже
                # посчитана по абонементу, второй раз не прибавляем.
                pair = None
                for sub in singles:
                    if (sub.uid not in paid_for
                            and sub.student == payment.student
                            and sub.bought == payment.day):
                        pair = sub
                        break
                if pair is not None:
                    paid_for.add(pair.uid)
                    continue
                parts["single"] += int(payment.amount)
            elif what in SERVICES:
                parts["service"] += int(payment.amount)
            else:
                parts["other"] += int(payment.amount)
        return parts

    def earned_in_month(self, year: int, month: int) -> int:
        return sum(self.earnings_in_month(year, month).values())
