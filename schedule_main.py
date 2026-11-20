"""Точка входа программы «Мажорный лад Расписание».

Из этого файла собирается отдельный exe — он живёт рядом с exe уроков и
независим: своя папка Data, свой Sound, свои файлы.
"""
from schedapp.ui import main

if __name__ == "__main__":
    raise SystemExit(main())
