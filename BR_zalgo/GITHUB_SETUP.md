# 🐙 Настройка GitHub репозитория

## Шаг 1: Создание репозитория на GitHub

1. Перейдите на [github.com](https://github.com) и войдите в свой аккаунт
2. Нажмите кнопку **"New"** (зелёная кнопка) или **"+"** в правом верхнем углу
3. Выберите **"New repository"**

### Настройки репозитория:
- **Repository name:** `BR_zalgo`
- **Description:** `🌀 Autonomous text distortion daemon with CAPS LOCK activation`
- **Visibility:** Выберите **Public** или **Private**
- **НЕ ставьте галочки:**
  - ❌ Add a README file
  - ❌ Add .gitignore  
  - ❌ Choose a license

4. Нажмите **"Create repository"**

## Шаг 2: Получение команд для подключения

После создания репозитория GitHub покажет вам команды. Вам нужна секция:

**"…or push an existing repository from the command line"**

Команды будут выглядеть примерно так:

```bash
git remote add origin https://github.com/ВАШ_НИКНЕЙМ/BR_zalgo.git
git branch -M main
git push -u origin main
```

## Шаг 3: Выполнение команд

**Скопируйте эти команды из вашего GitHub репозитория и вставьте их в терминал.**

⚠️ **Важно:** Замените `ВАШ_НИКНЕЙМ` на ваш реальный GitHub никнейм!

## Альтернативный способ

Если вы хотите использовать SSH вместо HTTPS:

```bash
git remote add origin git@github.com:ВАШ_НИКНЕЙМ/BR_zalgo.git
git branch -M main
git push -u origin main
```

## Что произойдёт после push

После выполнения команд:
- ✅ Весь код отправится на GitHub
- ✅ README.md отобразится на главной странице репозитория
- ✅ Файлы будут доступны для скачивания
- ✅ Можно будет создавать Issues и Pull Requests

## Дополнительные возможности

После загрузки на GitHub можно:
- 🔗 Поделиться ссылкой на репозиторий
- 📦 Создать Release с готовым exe файлом
- 🌟 Получить звёзды от пользователей
- 🤝 Принимать вклад от других разработчиков

---

**Готово!** Ваш Demon Zalgo будет доступен всему миру! 🌀
