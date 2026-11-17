@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Проверка окружения. Ничего не запускает и не меняет.
rem Результат — файл check_log.txt рядом с этим файлом.
rem Если программа не запускается, запустите этот файл и пришлите check_log.txt.

set "OUT=%~dp0check_log.txt"
> "%OUT%" echo === Отчёт о проверке «Мажорный лад» ===
>> "%OUT%" echo Дата: %DATE% %TIME%
>> "%OUT%" echo Папка: %CD%
>> "%OUT%" echo.

>> "%OUT%" echo --- Файлы программы ---
if exist main.py (>> "%OUT%" echo main.py: есть) else (>> "%OUT%" echo main.py: НЕТ)
if exist requirements.txt (>> "%OUT%" echo requirements.txt: есть) else (>> "%OUT%" echo requirements.txt: НЕТ)
if exist gcapp\ui.py (>> "%OUT%" echo gcapp\ui.py: есть) else (>> "%OUT%" echo gcapp\ui.py: НЕТ)
if exist gcapp\audio.py (>> "%OUT%" echo gcapp\audio.py: есть) else (>> "%OUT%" echo gcapp\audio.py: НЕТ)
>> "%OUT%" echo.

>> "%OUT%" echo --- Python ---
where py >> "%OUT%" 2>&1
where python >> "%OUT%" 2>&1
py -3 --version >> "%OUT%" 2>&1
python --version >> "%OUT%" 2>&1
>> "%OUT%" echo.

>> "%OUT%" echo --- Модули ---
py -3 -c "import PySide6; print('PySide6 ok')" >> "%OUT%" 2>&1
py -3 -c "import numpy; print('numpy ok')" >> "%OUT%" 2>&1
py -3 -c "import sounddevice; print('sounddevice ok')" >> "%OUT%" 2>&1
py -3 -c "import rtmidi; print('rtmidi ok')" >> "%OUT%" 2>&1
py -3 -c "import cv2; print('cv2 ok')" >> "%OUT%" 2>&1
>> "%OUT%" echo.

>> "%OUT%" echo --- Аудиоустройства ---
py -3 -c "import sounddevice; print(sounddevice.query_devices())" >> "%OUT%" 2>&1
>> "%OUT%" echo.
>> "%OUT%" echo --- Конец отчёта ---

echo.
echo Проверка закончена.
echo Отчёт сохранён в файл check_log.txt рядом с этим файлом.
echo Пришлите этот файл в чат.
echo.
pause
exit /b 0
