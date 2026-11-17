@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Сборка портативной программы «Мажорный лад».
rem Запускать на Windows, где установлен Python 3.10-3.12 (64 бита).
rem Всё, что происходит, записывается в build_log.txt рядом с этим файлом.
rem Результат сборки — два exe в папке dist.

set "LOG=%~dp0build_log.txt"
> "%LOG%" echo === Сборка «Мажорный лад» ===
>> "%LOG%" echo Папка сборки: %CD%

set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)

>> "%LOG%" echo.
>> "%LOG%" echo --- Поиск Python ---
where py >> "%LOG%" 2>&1
where python >> "%LOG%" 2>&1

if not defined PY (
    >> "%LOG%" echo Python НЕ найден.
    echo.
    echo Python не найден.
    echo Установите Python 3.12 с сайта python.org.
    echo При установке отметьте галочку "Add Python to PATH".
    echo.
    echo Подробности в файле build_log.txt рядом с программой.
    pause
    exit /b 1
)

>> "%LOG%" echo Найден Python:
%PY% --version >> "%LOG%" 2>&1

echo.
echo Шаг 1 из 3: установка зависимостей.
%PY% -m pip install --user --upgrade pip >> "%LOG%" 2>&1
%PY% -m pip install --user -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
    echo.
    echo Не удалось установить зависимости.
    echo Подробности в файле build_log.txt рядом с программой.
    pause
    exit /b 1
)

echo Шаг 2 из 3: сборка преподавателя.
echo. >> "%LOG%"
echo --- PyInstaller: преподаватель --- >> "%LOG%"
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name MajorLad-Teacher --collect-all sounddevice --collect-all rtmidi --collect-all cv2 --exclude-module tkinter main_teacher.py >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo Сборка преподавателя не удалась, код %RC%.
    echo Подробности в файле build_log.txt рядом с программой.
    pause
    exit /b 1
)

echo Шаг 3 из 3: сборка ученика.
echo --- PyInstaller: ученик --- >> "%LOG%"
%PY% -m PyInstaller --noconfirm --onefile --windowed --name MajorLad-Student --collect-all sounddevice --collect-all rtmidi --collect-all cv2 --exclude-module tkinter main_student.py >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo Сборка ученика не удалась, код %RC%.
    echo Подробности в файле build_log.txt рядом с программой.
    pause
    exit /b 1
)

echo.
echo Готово. В папке dist два файла:
echo   MajorLad-Teacher.exe  — для преподавателя
echo   MajorLad-Student.exe  — для ученика
echo Настройки сохраняются в файле рядом с exe.
echo.
pause
exit /b 0
