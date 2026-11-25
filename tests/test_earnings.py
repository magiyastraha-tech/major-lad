"""Проверки расчёта заработка — без Qt и без окна.

Запуск из корня проекта:  python tests/test_earnings.py

Проверки держат главное правило: заработок считается по строкам журнала
оплат, а не по цене в карточке; строка «Урок», которую старые версии
писали при отметке урока по абонементу, идёт в абонемент, а не в разовые.

Windows-сборка запускает этот файл до сборки exe: проверки не прошли —
обновление не выйдет.
"""
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schedapp.store import LESSON_DONE, Store  # noqa: E402

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: получилось {got!r}, ждали {want!r}")


def fresh(tmp):
    return Store(Path(tmp))


def single(store, price, day, bought=None):
    return store.add_subscription("Анна", 1, price=price, bought=bought or day,
                                  single=True)


def n(store, year=2026, month=11):
    return store.earnings_in_month(year, month)


def main():
    tmp = tempfile.mkdtemp(prefix="earn_test_")
    try:
        d = date(2026, 11, 5)

        # 1. строка журнала главнее цены урока
        s = fresh(tmp + "/1")
        single(s, 1600, d)
        s.add_payment("Анна", "Разовый урок", 1100, d)
        check("строка 1100 при цене 1600", n(s)["single"], 1100)

        # 2. подпись строчными
        s = fresh(tmp + "/2")
        single(s, 1600, d)
        s.add_payment("Анна", "разовый урок", 1100, d)
        check("строчная подпись", n(s)["single"], 1100)
        check("строчная подпись без прочих", n(s)["other"], 0)

        # 3. короткая подпись «Разовый»
        s = fresh(tmp + "/3")
        single(s, 1600, d)
        s.add_payment("Анна", "Разовый", 1100, d)
        check("подпись «Разовый»", n(s)["single"], 1100)

        # 4. имя с лишними пробелами
        s = fresh(tmp + "/4")
        s.add_subscription("Анна Иванова", 1, price=1600, bought=d, single=True)
        s.add_payment(" Анна  Иванова ", "Разовый урок", 1100, d)
        check("имя с пробелами", n(s)["single"], 1100)

        # 5. строка далеко по дате
        s = fresh(tmp + "/5")
        single(s, 2600, date(2026, 8, 20))
        s.add_payment("Анна", "Разовый урок", 2600, date(2026, 10, 31))
        check("далеко по дате: деньги в своём месяце",
              n(s, 2026, 10)["single"], 2600)
        check("далеко по дате: в месяце урока пусто",
              n(s, 2026, 8)["single"], 0)

        # 6. совсем без строки — по цене урока
        s = fresh(tmp + "/6")
        single(s, 1600, d)
        check("без строки — цена урока", n(s)["single"], 1600)

        # 7-8. абонементы
        s = fresh(tmp + "/7")
        s.add_subscription("Борис", 8, price=5000, bought=d, finished=True)
        s.add_payment("Борис", "Абонемент, 8 занятий", 5000, d)
        check("абонемент со строкой", n(s)["finished"], 5000)

        s = fresh(tmp + "/8")
        s.add_subscription("Борис", 8, price=1600, bought=d, finished=True)
        s.add_payment("Борис", "Абонемент, 8 занятий", 1100, d)
        check("абонемент: строка 1100 при цене 1600",
              n(s)["finished"], 1100)

        s = fresh(tmp + "/9")
        s.add_subscription("Борис", 8, price=5000, bought=d)
        check("абонемент без строки", n(s)["active"], 5000)

        s = fresh(tmp + "/10")
        s.add_payment("Борис", "Абонемент, 4 занятия", 2400, d)
        check("строка без абонемента", n(s)["finished"], 2400)

        # 11. дубль
        s = fresh(tmp + "/11")
        single(s, 1100, d)
        s.add_payment("Анна", "Разовый урок", 1100, d)
        s.add_payment("Анна", "Разовый урок", 1100, d)
        check("дубль строки", n(s)["single"], 1100)

        # 12-13. пробные
        s = fresh(tmp + "/12")
        s.add_subscription("Анна", 1, price=0, bought=d, trial=True)
        s.add_payment("Анна", "Пробный урок", 1500, d)
        check("пробный не считается", sum(n(s).values()), 0)

        s = fresh(tmp + "/13")
        s.add_subscription("Анна", 1, price=0, bought=d, trial=True)
        s.add_payment("Анна", "пробный урок", 1500, d)
        check("пробный строчными", sum(n(s).values()), 0)

        # 14. услуги строчными
        s = fresh(tmp + "/14")
        s.add_payment("Анна", "аренда гитары", 700, d)
        check("услуга строчными", n(s)["service"], 700)

        # 15. другой месяц
        s = fresh(tmp + "/15")
        s.add_payment("Анна", "Разовый урок", 1100, date(2026, 10, 5))
        check("другой месяц", sum(n(s).values()), 0)

        # 16. будущий абонемент
        s = fresh(tmp + "/16")
        s.add_subscription("Борис", 8, price=4000, bought=d)
        s.add_subscription("Борис", 8, price=4000, bought=d)
        s.add_payment("Борис", "Абонемент, 8 занятий", 4000, d)
        s.add_payment("Борис", "Абонемент, 8 занятий", 4000, d)
        check("будущий абонемент", n(s)["future"], 4000)
        check("действующий абонемент", n(s)["active"], 4000)

        # 17. смешанный месяц
        s = fresh(tmp + "/17")
        single(s, 1100, d)
        s.add_payment("Анна", "Разовый урок", 1100, d)
        s.add_subscription("Борис", 8, price=5000, bought=d, finished=True)
        s.add_payment("Борис", "Абонемент, 8 занятий", 5000, d)
        s.add_payment("Вера", "Настройка гитары", 800, d)
        check("смешанный месяц", sum(n(s).values()), 6900)

        # 18. правка строки в журнале меняет цену урока
        s = fresh(tmp + "/18")
        sub = single(s, 1600, d)
        row = s.add_payment("Анна", "Разовый урок", 1600, d)
        row.amount = 1100
        s.update_payment(row)
        check("правка журнала → цена", int(sub.price), 1100)
        check("правка журнала → заработок", n(s)["single"], 1100)

        # 19. правка цены в карточке возвращается в журнал
        s = fresh(tmp + "/19")
        sub = single(s, 1600, d)
        s.add_payment("Анна", "Разовый урок", 1600, d)
        sub.price = 1300
        s.sync_subscription_payment(sub, d)
        check("правка карточки → строка", int(s.payments[0].amount), 1300)
        check("правка карточки → заработок", n(s)["single"], 1300)

        # 20. расхождение из старых версий сводится при запуске
        s = fresh(tmp + "/20")
        single(s, 1600, d)
        s.add_payment("Анна", "Разовый урок", 1100, d)
        again = Store(Path(tmp + "/20"))
        check("сведение при запуске: цена", int(again.subscriptions[0].price), 1100)
        check("сведение при запуске: заработок", n(again)["single"], 1100)
        check("сведение при запуске: одна строка", len(again.payments), 1)

        # 21. пробный не трогаем
        s = fresh(tmp + "/21")
        s.add_subscription("Анна", 1, price=0, bought=d, trial=True)
        s.add_payment("Анна", "Пробный урок", 900, d)
        again = Store(Path(tmp + "/21"))
        check("пробный: цена не тронута", int(again.subscriptions[0].price), 0)

        # 22. старый абонемент на 1 урок с подписью «Разовый урок»
        s = fresh(tmp + "/22")
        s.add_subscription("Анна", 1, price=1600, bought=d, finished=True)
        s.add_payment("Анна", "Разовый урок", 1100, d)
        check("старый на 1 урок: без задвоения", sum(n(s).values()), 1100)

        # 23. имя в строке не совпало — ровно одна свободная строка на день
        s = fresh(tmp + "/23")
        s.add_subscription("Анна Иванова", 1, price=1600, bought=d, single=True)
        s.add_payment("Аня", "Разовый урок", 1100, d)
        check("чужое имя, одна строка", n(s)["single"], 1100)

        # 24. два разовых в один день у разных учеников — не путаем строки
        s = fresh(tmp + "/24")
        s.add_subscription("Анна", 1, price=1000, bought=d, single=True)
        s.add_subscription("Борис", 1, price=2000, bought=d, single=True)
        s.add_payment("Анна", "Разовый урок", 1000, d)
        s.add_payment("Борис", "Разовый урок", 2000, d)
        check("два разовых в день", n(s)["single"], 3000)

        # 25. «Урок» за проведённый урок абонемента — деньги абонемента.
        # Старые версии при отметке урока писали в журнал строку «Урок»
        # на сумму урока; из-за этого она попадала в разовые.
        s = fresh(tmp + "/25")
        ab = s.add_subscription("Савелий", 8, price=7200, bought=d)
        s.add_payment("Савелий", "Абонемент, 8 уроков", 7200, d)
        lesson = s.add_lesson("Савелий", d, "10:00", status=LESSON_DONE)
        lesson.price = 500
        lesson.subscription = ab.uid
        s.update_lesson(lesson)
        s.add_payment("Савелий", "Урок", 500, d)
        check("урок абонемента: не разовый", n(s)["single"], 0)
        check("урок абонемента: в абонемент", n(s)["active"], 7700)
        check("урок абонемента: всего", sum(n(s).values()), 7700)

        # 26. «Урок» без проведённого урока в тот день — разовый
        s = fresh(tmp + "/26")
        s.add_payment("Анна", "Урок", 500, d)
        check("«Урок» без урока", n(s)["single"], 500)

        # 27. сумма строки не совпала с уроком — не приписываем абонементу
        s = fresh(tmp + "/27")
        ab = s.add_subscription("Борис", 8, price=5000, bought=d)
        s.add_payment("Борис", "Абонемент, 8 уроков", 5000, d)
        lesson = s.add_lesson("Борис", d, "11:00", status=LESSON_DONE)
        lesson.price = 700
        lesson.subscription = ab.uid
        s.update_lesson(lesson)
        s.add_payment("Борис", "Урок", 500, d)
        check("чужой сумме — разовый", n(s)["single"], 500)
        check("чужой сумме — абонемент", n(s)["active"], 5000)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("\n".join(fails))
        print(f"ПРОВАЛОВ: {len(fails)}")
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОШЛИ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
