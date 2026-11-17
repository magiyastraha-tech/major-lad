@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Запуск «Мажорного лада» из исходников, без сборки exe.
rem Всё, что происходит, записывается в run_log.txt рядом с этим файлом.

set "LOG=%~dp0run_log.txt"
> "%LOG%" echo === «Мажорный лад»: запуск ===
>> "%LOG%" echo Папка программы: %CD%

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
    echo Подробности в файле run_log.txt рядом с программой.
    pause
    exit /b 1
)

>> "%LOG%" echo Найден Python:
%PY% --version >> "%LOG%" 2>&1

echo.
echo Python найден. Устанавливаю зависимости, это может занять пару минут...
%PY% -m pip install --user -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
    echo.
    echo Не удалось установить зависимости.
    echo Подробности в файле run_log.txt рядом с программой.
    pause
    exit /b 1
)

echo Запускаю программу...
echo. >> "%LOG%"
echo --- Запуск main.py --- >> "%LOG%"
%PY% main.py >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"

>> "%LOG%" echo.
>> "%LOG%" echo --- Программа завершилась, код %RC% ---

if not "%RC%"=="0" (
    echo.
    echo Программа завершилась с ошибкой, код %RC%.
    echo Подробности в файле run_log.txt рядом с программой.
    pause
    exit /b 1
)

exit /b 0
