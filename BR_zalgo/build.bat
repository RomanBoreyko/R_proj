@echo off
echo 🌀 Сборка Demon Zalgo...
echo.

REM Проверка Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python не найден! Установите Python и добавьте в PATH
    pause
    exit /b 1
)

REM Установка зависимостей
echo 📦 Установка зависимостей...
pip install -r requirements.txt

REM Сборка exe файла
echo 🔨 Сборка исполняемого файла...
pyinstaller --noconsole --onefile --name "zalgo_demon" --icon=NONE zalgo_demon.py

REM Проверка результата
if exist "dist\zalgo_demon.exe" (
    echo ✅ Сборка успешна!
    echo 📁 Исполняемый файл: dist\zalgo_demon.exe
    echo 💡 Скопируйте его на рабочий стол для запуска
) else (
    echo ❌ Ошибка сборки!
)

echo.
pause
