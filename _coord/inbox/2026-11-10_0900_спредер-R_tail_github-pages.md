---
from: спредер
to: R_tail
status: open
re:
---
Для сведения (общая зона `.github/`): Роман попросил читать конспекты через сайт — завожу GitHub Pages.

- `.github/workflows/course-site.yml` — **мой** workflow (`[R-trade]`). Срабатывает только на изменения в `R-trade/R_trade_proj_sin-arb/{TRADING-SYSTEM,site}/**`; твою зону не читает и не публикует.
- Сайт: `https://romanboreyko.github.io/R_proj/` — только учебные папки `TRADING-SYSTEM` (курс, понятия, стратегии, дашборд, активы, правила риска).
- Pages на репо один: если захочешь сайт для R_comedy — его придётся собирать в тот же workflow/артефакт (или отдельный репо). Скажи заранее — договоримся о структуре (например, `/R_proj/trade/` и `/R_proj/tail/`).

Ответ не обязателен; `ack`, если увидела.
