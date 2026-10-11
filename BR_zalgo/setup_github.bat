@echo off
echo 🐙 Настройка GitHub репозитория для Demon Zalgo
echo.

REM Проверка наличия git
git --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Git не найден! Установите Git с https://git-scm.com
    pause
    exit /b 1
)

echo ✅ Git найден
echo.

REM Проверка статуса репозитория
git status >nul 2>&1
if errorlevel 1 (
    echo ❌ Это не git репозиторий!
    pause
    exit /b 1
)

echo ✅ Git репозиторий обнаружен
echo.

REM Проверка наличия изменений
git status --porcelain | findstr /R "M A D" >nul
if not errorlevel 1 (
    echo 📝 Обнаружены незакоммиченные изменения
    echo 💡 Сначала закоммитьте все изменения:
    echo    git add .
    echo    git commit -m "Your commit message"
    echo.
    pause
    exit /b 1
)

echo ✅ Все изменения закоммичены
echo.

REM Проверка наличия remote
git remote -v | findstr "origin" >nul
if not errorlevel 1 (
    echo ⚠️ Remote 'origin' уже настроен
    echo.
    echo Текущие remotes:
    git remote -v
    echo.
    echo Для отправки на GitHub используйте:
    echo    git push origin master
    echo.
    pause
    exit /b 0
)

echo 📋 Инструкции для настройки GitHub:
echo.
echo 1. Создайте новый репозиторий на https://github.com
echo    - Название: BR_zalgo
echo    - Описание: Autonomous text distortion daemon
echo    - НЕ добавляйте README, .gitignore или лицензию
echo.
echo 2. После создания скопируйте команды из GitHub
echo    (секция "push an existing repository from the command line")
echo.
echo 3. Выполните команды в терминале:
echo    git remote add origin https://github.com/ВАШ_НИКНЕЙМ/BR_zalgo.git
echo    git branch -M main
echo    git push -u origin main
echo.
echo 📄 Подробные инструкции в файле GITHUB_SETUP.md
echo.

pause
