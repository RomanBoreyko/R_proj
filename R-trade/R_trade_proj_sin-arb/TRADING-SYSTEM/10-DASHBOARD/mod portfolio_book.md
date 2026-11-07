---
type: module
status: active
file: scripts/portfolio/portfolio_book.py
tags:
  - dashboard
  - code
updated: 2026-11-07
---
# Модуль `portfolio_book.py`

Хаб: [[Дашборд]] · Файл: [portfolio_book.py](../scripts/portfolio/portfolio_book.py) · Техдока: [[DASHBOARD]] §2

- `portfolio_snapshot(sget, pub, earn_fn)` → JSON для [[Вкладка Портфель]].
- `GROUPS` = [[ETH]], [[MNT]], [[SOL]], [[BTC]], [[QQQ]], [[ЗОЛОТО]]; `OPT_BASES`, `FLAT_USD=150`.
- `_betas` → [[Бета к ETH]]; `_shock` → [[Стресс ETH против корзины]]; `_vol`/`_rv30` → [[Вол-сенсор IV-RV]];
  payoff/безубыток → [[Payoff и безубыток]]; Δ спот+Earn → [[Истинная дельта]].
- Импортирует утилиты из [[mod tail_pairs]] (`ff`, `_T`, `_bs`, `cached`, `CRYPTO`).
