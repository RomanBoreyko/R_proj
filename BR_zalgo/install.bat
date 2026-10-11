@echo off
echo 🌀 Установка Demon Zalgo...
echo.

REM Проверка Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python не найден!
    echo 💡 Скачайте Python с https://python.org
    echo 💡 При установке отметьте "Add Python to PATH"
    pause
    exit /b 1
)

REM Обновление pip
echo 📦 Обновление pip...
python.exe -m pip install --upgrade pip

REM Установка зависимостей
echo 📦 Установка зависимостей...
pip install -r requirements.txt

if errorlevel 1 (
    echo ❌ Ошибка установки зависимостей!
    echo 💡 Попробуйте установить зависимости по одной:
    echo pip install keyboard
    echo pip install pyautogui
    echo pip install pyinstaller
    echo pip install pystray
    echo pip install Pillow
    pause
    exit /b 1
)

echo ✅ Установка завершена!
echo.
echo 🚀 Запуск Demon Zalgo...
python zalgo_demon.py

pause
