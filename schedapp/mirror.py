"""Читаемые копии данных в папке Data.

Источник правды — база. Эти файлы программа собирает сама: при запуске и
после каждого изменения. Так данные всегда лежат под рукой обычным
текстом — их удобно читать диктором, показывать и распечатывать, — но
ручная правка обратно не читается. Иначе получились бы две версии одних
и тех же данных, и рано или поздно они разошлись бы.

Что здесь лежит:

- `Students` — по файлу на ученика, имя файла — имя ученика;
- `Проведённые уроки.txt` — прошедшие уроки, новые сверху;
- `расписание.txt` — уроки на месяц вперёд от сегодняшнего дня;
- `Исключения.txt` — дни и часы, когда уроков нет.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from . import dates

STUDENTS_DIR = "Students"
DONE_FILE = "Проведённые уроки.txt"
SCHEDULE_FILE = "расписание.txt"
BLOCKS_FILE = "Исключения.txt"

STUDENT_HEADER = "Мажорный лад Расписание. Данные ученика."
DONE_HEADER = "Проведённые уроки. Новые сверху."
SCHEDULE_HEADER = "Расписание уроков."
BLOCKS_HEADER = "Исключения: дни и часы, когда уроков нет."
NOTE = "Файл собран программой. Правки здесь не сохраняются — меняй данные в программе."

DONE_STATUS = "проведён"
CANCELLED_STATUS = "отменён"

# Символы, которые Windows не разрешает в именах файлов.
BAD_CHARS = '<>:"/\\|?*'


def safe_name(name: str) -> str:
    """Имя ученика, пригодное для имени файла."""
    text = "".join(" " if ch in BAD_CHARS else ch for ch in str(name or ""))
    text = " ".join(text.split()).strip(" .")
    return text[:100] or "Без имени"


def _money(amount: int) -> str:
    return f"{int(amount):,}".replace(",", " ")


def _write(path: Path, lines: list[str]) -> None:
    """Пишет файл целиком: недописанного файла не остаётся."""
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    tmp.replace(path)


# --- файлы учеников ------------------------------------------------------


def _student_lines(store, student) -> list[str]:
    sub = store.current_subscription(student.name)
    near = store.nearest_lesson(student.name)
    lines = [STUDENT_HEADER, NOTE, "", f"Ученик: {student.name}."]
    if student.phone:
        lines.append(f"Телефон: {student.phone}.")
    else:
        lines.append("Телефон: не записан.")
    if student.email:
        lines.append(f"Почта: {student.email}.")
    lines.append(f"Занятия: {student.lesson_format.casefold()}.")
    lines.append(f"Тип занятия: {student.direction}.")
    if student.note:
        lines.append(f"Заметка: {student.note}.")
    lines.append("")
    if sub is None:
        lines.append("Действующий абонемент: нет.")
    else:
        start = sub.started or sub.bought
        lines.append("Действующий абонемент.")
        lines.append(f"Куплен: {dates.fmt_date(sub.bought)}.")
        lines.append(f"Действует с: {dates.fmt_date(start)}.")
        lines.append(f"Уроков в абонементе: {sub.total}.")
        lines.append(f"Проведено: {sub.used}.")
        lines.append(f"Осталось: {sub.left}.")
        if sub.price:
            lines.append(f"Стоимость: {_money(sub.price)} рублей.")
    lines.append("")
    history = store.finished_subscriptions(student.name)
    if not history:
        lines.append("Законченных абонементов: нет.")
    else:
        lines.append(f"Законченных абонементов: {len(history)}.")
        for item in history:
            if item.one_off:
                title = "Пробный урок" if item.trial else "Разовый урок"
                parts = [item.lesson_format.casefold(), item.direction]
                if item.single and item.price:
                    parts.append(f"{_money(item.price)} рублей")
                tail = ", ".join(part for part in parts if part)
                line = f"{title}: {dates.fmt_date(item.bought)}"
                if tail:
                    line += f", {tail}"
                line += ", проведён." if item.used else "."
                lines.append(line)
                continue
            tail = f"Стоимость: {_money(item.price)} рублей." if item.price else ""
            lines.append(
                f"Законченный абонемент с {dates.fmt_date(item.begins)}: "
                f"уроки {item.used} из {item.total}, куплен "
                f"{dates.fmt_date(item.bought)}."
                + (f" {tail}" if tail else ""))
    lines.append("")
    if near is not None:
        lines.append(f"Ближайший урок: {dates.human_date(near.day)}, "
                     f"в {near.time}.")
    else:
        lines.append("Ближайший урок: не записан.")
    return lines


def _student_files(store) -> set[str]:
    folder = Path(store.folder) / STUDENTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    for student in sorted(store.students, key=lambda s: s.name.casefold()):
        base = safe_name(student.name)
        name = f"{base}.txt"
        number = 2
        while name in used:
            name = f"{base} ({number}).txt"
            number += 1
        used.add(name)
        _write(folder / name, _student_lines(store, student))

    # Ученика удалили или переименовали — старый файл убираем. Трогаем
    # только файлы со своим заголовком: чужие txt в папке не наши.
    for path in folder.glob("*.txt"):
        if path.name in used:
            continue
        try:
            first = path.read_text(encoding="utf-8-sig",
                                   errors="ignore").splitlines()
        except OSError:
            continue
        if first and first[0].strip() == STUDENT_HEADER:
            try:
                path.unlink()
            except OSError:
                pass
    return used


# --- проведённые уроки ---------------------------------------------------


def _total_at(subscriptions, day: date) -> int:
    """Сколько уроков было в абонементе на этот день."""
    if not subscriptions:
        return 0
    before = [s for s in subscriptions if (s.started or s.bought) <= day]
    if before:
        sub = max(before, key=lambda s: (s.started or s.bought, s.uid))
    else:
        sub = min(subscriptions, key=lambda s: (s.started or s.bought, s.uid))
    return int(sub.total)


def _counters(store) -> dict[int, tuple[int, int]]:
    """Номер урока и размер абонемента для каждого проведённого урока.

    Урок, записанный в абонемент, считается внутри своего абонемента:
    у законченного счётчик идёт от 1 до его размера, и в действующем
    номера не продолжают номера старого. Уроки до версии 0.3.0 привязки
    не имеют — для них номер по-прежнему считается по датам.
    """
    result: dict[int, tuple[int, int]] = {}
    subs_by_uid = {s.uid: s for s in store.subscriptions}
    linked = sorted(
        [l for l in store.lessons
         if l.status == DONE_STATUS and l.subscription],
        key=lambda l: l.stamp)
    per_sub: dict[int, int] = {}
    for lesson in linked:
        sub = subs_by_uid.get(lesson.subscription)
        if sub is None:
            continue
        per_sub[sub.uid] = per_sub.get(sub.uid, 0) + 1
        result[lesson.uid] = (per_sub[sub.uid], int(sub.total))

    names = {lesson.student for lesson in store.lessons
             if lesson.status == DONE_STATUS and not lesson.subscription}
    for name in names:
        subs = [s for s in store.subscriptions if s.student == name]
        done = sorted(
            [l for l in store.lessons
             if l.student == name and l.status == DONE_STATUS
             and not l.subscription],
            key=lambda l: l.stamp)
        count = 0
        for lesson in done:
            count += 1
            total = _total_at(subs, lesson.day)
            number = min(count, total) if total else count
            result[lesson.uid] = (number, total)
    return result


def _done_lines(store) -> list[str]:
    lines = [DONE_HEADER, NOTE, ""]
    counters = _counters(store)
    done = [l for l in store.lessons if l.status == DONE_STATUS]
    done.sort(key=lambda l: l.stamp, reverse=True)
    if not done:
        lines.append("Пока ни одного урока.")
        return lines
    for lesson in done:
        number, total = counters.get(lesson.uid, (0, 0))
        tail = (f"уроки {number} из {total}" if total
                else f"уроки {number}")
        lines.append(f"{lesson.student}, {dates.fmt_date(lesson.day)}, {tail}.")
    return lines


# --- расписание на месяц вперёд -----------------------------------------


def _schedule_lines(store, today: date) -> list[str]:
    end = today + timedelta(days=31)
    lines = [SCHEDULE_HEADER, NOTE, "",
             f"С {dates.fmt_date(today)} по {dates.fmt_date(end)}.", ""]
    lessons = sorted(
        [l for l in store.lessons
         if today <= l.day <= end and l.status != CANCELLED_STATUS],
        key=lambda l: l.stamp)
    if not lessons:
        lines.append("Уроков не записано.")
        return lines
    for lesson in lessons:
        when = dates.fmt_date(lesson.day)
        if lesson.time:
            when += f", {lesson.time}"
        lines.append(f"{when}, "
                     f"{lesson.student}, {lesson.direction.casefold()}, "
                     f"{lesson.lesson_format.casefold()}, {lesson.status}.")
    return lines


# --- исключения ----------------------------------------------------------


def _blocks_lines(store, today: date) -> list[str]:
    lines = [BLOCKS_HEADER, NOTE, ""]
    blocks = store.blocks_between(today, date.max)
    if not blocks:
        lines.append("Исключений нет: записаться можно на любой день.")
        return lines
    for block in blocks:
        when = dates.human_date(block.day)
        if block.all_day:
            when += ": весь день без уроков"
        else:
            when += f": с {block.start} до {block.end}"
        if block.note:
            when += f". Причина: {block.note}"
        lines.append(when + ".")
    return lines


# --- наружу --------------------------------------------------------------


def refresh(store, today: date | None = None) -> None:
    """Пересобрать все читаемые файлы. Ошибки ввода-вывода — на совести
    вызывающего: база важнее, её сбой прерывать нельзя."""
    folder = Path(store.folder)
    today = today or date.today()
    _student_files(store)
    _write(folder / DONE_FILE, _done_lines(store))
    _write(folder / SCHEDULE_FILE, _schedule_lines(store, today))
    _write(folder / BLOCKS_FILE, _blocks_lines(store, today))
