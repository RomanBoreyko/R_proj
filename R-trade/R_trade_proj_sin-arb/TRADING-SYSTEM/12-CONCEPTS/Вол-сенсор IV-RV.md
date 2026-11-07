---
type: concept
status: active
tags:
  - volatility
updated: 2026-11-07
---
# Вол-сенсор IV / RV

Отношение подразумеваемой (IV, markIv опционов) к реализованной (RV 30д) воле по базе.
Полосы: < 0.9 — опционы дешёвые; 0.9–1.1 — норма; 1.1–1.3 — дорогие; > 1.3 — очень дорогие (продажа премии).

Где: [[Вкладка Портфель]], [[Вкладка Хвосты ETH]]. Код: `_vol`, `_rv30` в [[mod portfolio_book]].
Связано: [[D2-eth-perp-put-ребаланс]] (тезис realized > implied), `scripts/portfolio/iv_rv.py`.
