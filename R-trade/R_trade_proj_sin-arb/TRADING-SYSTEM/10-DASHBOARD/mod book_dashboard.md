---
type: module
status: active
file: scripts/portfolio/book_dashboard.py
tags:
  - dashboard
  - code
updated: 2026-11-07
---
# Модуль `book_dashboard.py`

Хаб: [[Дашборд]] · Файл: [book_dashboard.py](../scripts/portfolio/book_dashboard.py) · Техдока: [[DASHBOARD]] §4–§6

- `ThreadingHTTPServer` на `127.0.0.1:8899`; маршруты `/`, `/port`, `/gold`, `/tail`, `/pairs`, `/data` (legacy).
- HTTP-хелперы `sget` (подписанный GET, HMAC) и `pub` (публичный) → передаются в модули.
- Весь HTML/JS — одна строка `r'''…'''`; **общий глобальный scope JS** — новые имена проверять grep'ом.
- После правки — перезапуск (HTML живёт в памяти процесса), в браузере Ctrl+F5.

Использует: [[mod portfolio_book]], [[mod tail_pairs]].
Рисует: [[Вкладка Портфель]], [[Вкладка Золото]], [[Вкладка Хвосты ETH]], [[Вкладка Факторные спреды]].
