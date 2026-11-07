---
type: module
status: active
file: scripts/portfolio/arb_book.py
tags:
  - dashboard
  - code
updated: 2026-11-07
---
# Модуль `arb_book.py`

Хаб: [[Дашборд]] · Файл: [arb_book.py](../scripts/portfolio/arb_book.py) · Техдока: [[DASHBOARD]] §3a

- `book(port, pub)` — поверх `/port` ([[mod portfolio_book]]): остаточный риск, связки, открытые ноги, фандинг $/день → [[Вкладка Арбитраж]].
- `scan(sget, pub)` — фоновый сканер арбитража (паритет, боксы, cash-and-carry, календарь, кэрри, инверс vs линейный).
- Утилиты из [[mod tail_pairs]]: `ff`, `_T`, `cached`, `inverse_positions`, `inv_base`. Параметры — `ARB_CFG`.
- Консольные предки: `parity_scan.py`, `cal_synth_arb.py`, `basis_desk.py`, `funding_scan.py`, `synth_screener.py` (не тронуты).
