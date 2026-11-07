---
type: module
status: active
file: scripts/portfolio/tail_pairs.py
tags:
  - dashboard
  - code
updated: 2026-11-07
---
# Модуль `tail_pairs.py`

Хаб: [[Дашборд]] · Файл: [tail_pairs.py](../scripts/portfolio/tail_pairs.py) · Техдока: [[DASHBOARD]] §3

- `tail_snapshot` → [[Вкладка Хвосты ETH]].
- `pairs_snapshot` (+ фоновый `_pairs_heavy`) → [[Вкладка Факторные спреды]]; `PAIRS`, `FACTORS`, `_sess`, `_fee`.
- Общие утилиты: `ff`, `_T`, `_bs` (Блэк–Шоулз), `cached` (TTL-кэш) — их использует [[mod portfolio_book]].
