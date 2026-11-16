# Учётная структура — 6 конструктов

> Версия 2.0 — пересмотр архитектуры 2026-06.
> Файл: `TRADING_BOOK.xlsx` (генерируется скриптом `scripts/gen_excel.py`)

---

## Архитектура конструктов

| ID | Тип | Инструменты | Источник P&L |
|----|-----|-------------|--------------|
| C1 | `LOCKED_SPREAD` | XAUTUSDT спот (long) + XAUTUSDT перп (short) | базис спот↔перп + вола XAUT |
| C2 | `STATARB_PAIR` | XAUUSDT перп (реверсия) + XAGUSDT перп (тренд) | mean-reversion XAU + carry XAG по h* |
| C3 | `SYNTH_RATIO` | XAUTUSDT перп (long) + XAGUSDT перп (short) + опционы OTM куплены + проданы | ratio XAUT/XAG → μ + опц.премия |
| C4 | `OPT_CALENDAR` | XAUTUSDT ближний синт-фьюч + дальний синт-фьюч (ATM опционы) | временной распад + basis-to-spot |

> C3 = пункты 5+7 в одной записи (купленные OTM + продающие OTM + динамический перп-хедж)

---

## Листы Excel

### 1. DASHBOARD (сводка)
Читает формулами из остальных листов. Не редактировать вручную.

| Блок | Колонки |
|------|---------|
| Эквити | equity_total, im_used, mm_used, margin_ratio |
| P&L сегодня | pnl_day, pnl_week, pnl_month |
| По конструктам | pnl_C1, pnl_C2, pnl_C3, pnl_C4 |
| Греки книги | net_delta_usd, net_gamma, net_theta_day, net_vega |
| Риск-светофор | z_score_C2, ratio_z_C3, margin_flag, liq_distance_% |

---

### 2. CONSTRUCTS (реестр конструктов)

| Колонка | Тип | Описание |
|---------|-----|---------|
| `id` | TEXT | C1 / C2 / C3 / C4 |
| `type` | TEXT | LOCKED_SPREAD / STATARB_PAIR / SYNTH_RATIO / OPT_CALENDAR |
| `name` | TEXT | Имя для отображения |
| `status` | TEXT | active / paused / closed |
| `open_date` | DATE | Дата открытия |
| `close_date` | DATE | Дата закрытия (если closed) |
| `notional_usd` | NUMBER | Суммарный ноционал $ |
| `realized_pnl` | NUMBER | Реализованная прибыль (SUMIF из FILLS) |
| `unrealized_pnl` | NUMBER | Нереализованная (вводить вручную / обновлять) |
| `net_delta_usd` | NUMBER | Нетто $-дельта (Σ delta×price по ногам) |
| `net_gamma` | NUMBER | Суммарная гамма |
| `net_theta` | NUMBER | Дневная тета $ |
| `net_vega` | NUMBER | Вега |
| `notes` | TEXT | |

---

### 3. LEGS (ноги конструктов)

| Колонка | Тип | Описание |
|---------|-----|---------|
| `construct_id` | TEXT | Ссылка на CONSTRUCTS.id |
| `leg_id` | TEXT | C1_L1 / C1_L2 / ... |
| `leg_role` | TEXT | см. роли ниже |
| `instrument` | TEXT | XAUTUSDT / XAGUSDT / XAUT-31JUL26-4250-C / ... |
| `category` | TEXT | spot / perp / option / synth_future |
| `direction` | TEXT | long / short |
| `qty` | NUMBER | Количество (XAUT, контракты, лотов) |
| `entry_price` | NUMBER | Средневзвешенная цена входа |
| `current_price` | NUMBER | Текущая цена (обновлять) |
| `notional_usd` | NUMBER | qty × entry_price |
| `unrealized_pnl` | NUMBER | (current - entry) × qty × direction_sign |
| `delta` | NUMBER | Дельта ноги (для опций — из Greeks) |
| `gamma` | NUMBER | |
| `theta` | NUMBER | Дневная |
| `vega` | NUMBER | |
| `status` | TEXT | open / closed / expired |
| `notes` | TEXT | |

**Роли ног (`leg_role`):**

| Роль | Конструкт | Описание |
|------|-----------|---------|
| `spot_long` | C1 | XAUTUSDT спот лонг |
| `perp_short` | C1 | XAUTUSDT перп шорт |
| `reversion_perp` | C2 | XAUUSDT перп (реверсия) |
| `trend_perp` | C2 | XAGUSDT перп (тренд, хедж) |
| `ratio_long` | C3 | XAUTUSDT перп (числитель ratio) |
| `ratio_short` | C3 | XAGUSDT перп (знаменатель ratio) |
| `otm_bought` | C3 | Купленный OTM опцион (защита) |
| `otm_sold` | C3 | Проданный OTM опцион (cover + реверс по ситуации) |
| `synth_near` | C4 | Ближний синт-фьюч (long call + short put ATM) |
| `synth_far` | C4 | Дальний синт-фьюч (short call + long put ATM) |

---

### 4. GRID_FILLS (заполнение сеток)

Каждое исполнение ордера сетки — одна строка.

| Колонка | Тип | Описание |
|---------|-----|---------|
| `date` | DATETIME | Время исполнения |
| `construct_id` | TEXT | C1 / C2 |
| `leg_id` | TEXT | Нога |
| `instrument` | TEXT | |
| `fill_type` | TEXT | limit_buy / limit_sell / stop_buy / stop_sell |
| `direction` | TEXT | buy / sell |
| `qty` | NUMBER | |
| `price` | NUMBER | |
| `value_usd` | NUMBER | qty × price |
| `fee_usd` | NUMBER | |
| `pnl_usd` | NUMBER | Прибыль по закрытой ячейке (0 если открытие) |
| `grid_level` | NUMBER | Номер уровня сетки |
| `notes` | TEXT | |

---

### 5. OPT_EVENTS (опционные события)

Каждое событие с опционом — одна строка.

| Колонка | Тип | Описание |
|---------|-----|---------|
| `date` | DATETIME | |
| `construct_id` | TEXT | C3 / C4 |
| `leg_id` | TEXT | |
| `event_type` | TEXT | open / close / expire / exercise / roll / delta_hedge |
| `symbol` | TEXT | XAUT-31JUL26-4250-C |
| `option_type` | TEXT | call / put |
| `strike` | NUMBER | |
| `expiry` | DATE | |
| `direction` | TEXT | long / short |
| `qty` | NUMBER | |
| `premium_usd` | NUMBER | Премия за 1 контракт |
| `total_usd` | NUMBER | premium × qty (+ для продажи, - для покупки) |
| `fee_usd` | NUMBER | |
| `delta` | NUMBER | На момент события |
| `gamma` | NUMBER | |
| `theta` | NUMBER | |
| `vega` | NUMBER | |
| `iv` | NUMBER | IV % |
| `pnl_usd` | NUMBER | Реализованная (при close/expire) |
| `notes` | TEXT | |

---

### 6. DAILY (дневной снапшот)

Заполнять раз в день (или скриптом через Bybit API).

| Колонка | Описание |
|---------|---------|
| `date` | |
| `equity_usd` | Полная эквити |
| `im_usd` | Initial margin |
| `mm_usd` | Maintenance margin |
| `pnl_day` | P&L за день |
| `pnl_C1` | Locked spread |
| `pnl_C2` | Stat-arb пара |
| `pnl_C3` | Synth ratio |
| `pnl_C4` | Opt calendar |
| `funding_day` | Суммарный фандинг |
| `theta_day` | Суммарная тета |
| `basis_C1` | Спот↔перп базис XAUT |
| `z_score_C2` | Z-score реверсионной сетки |
| `ratio_C3` | XAUT/XAG ratio |
| `ratio_z_C3` | Z-score ratio |
| `notes` | |

---

### 7. PARAMS (параметры конструктов)

Текущие конфигурационные параметры. Обновлять при ребалансировке.

**C1 — Locked Spread:**
| Параметр | Описание |
|----------|---------|
| `spot_lower` / `spot_upper` | Диапазон спот-сетки |
| `spot_steps` | Уровней |
| `perp_lower` / `perp_upper` | Диапазон перп-сетки |
| `perp_steps` | |
| `leverage_perp` | Плечо перп-ноги |
| `qty_per_step_spot` | XAUT на уровень |
| `qty_per_step_perp` | Контрактов на уровень |

**C2 — Stat-Arb Pair:**
| Параметр | Описание |
|----------|---------|
| `xau_mu` | Среднее XAUUSDT (скользящее или фикс) |
| `xau_sigma` | σ |
| `half_life_bars` | AR(1) полужизнь |
| `k_entry` | Z-порог входа |
| `k_exit` | Z-порог выхода |
| `xau_qty` | Размер позиции XAU |
| `h_star` | Хедж-коэф к XAG |
| `xag_notional` | = xau_notional × h* |
| `xag_lower` / `xag_upper` | Диапазон серебряной сетки |

**C3 — Synth Ratio:**
| Параметр | Описание |
|----------|---------|
| `entry_ratio` | XAUT/XAG при входе |
| `target_ratio` | Цель (μ) |
| `ratio_mu` | Скользящее среднее |
| `ratio_sigma` | σ ratio |
| `h_star` | Хедж-коэф |
| `xaut_notional` | |
| `xag_notional` | = xaut × h* |
| `otm_bought_strikes` | Страйки купленных (список) |
| `otm_sold_strikes` | Страйки проданных (список) |

**C4 — Opt Calendar:**
| Параметр | Описание |
|----------|---------|
| `near_expiry` | |
| `far_expiry` | |
| `strike_atm` | ATM страйк (≈ forward near) |
| `entry_spread` | near_price - far_price при входе |
| `target_spread` | При схождении к споту |
| `qty` | Лотов |

---

### 8. FORMULAS_REF (справочник)

```
h*              = ρ × σ_XAU / σ_XAG
Z-score         = (value - μ) / σ
Basis           = futures_price - spot_price
Synth future    = long_call(K) + short_put(K), same K+expiry
Calendar spread = near_synth_price - far_synth_price
$-delta         = delta × current_price  (НЕ сумма дельт в штуках!)
Net gamma book  = Σ(gamma_i × price_i²)  (нормированная по $ нотиону)
APR grid        = (realized_pnl / investment) / days * 365 * 100
```

---

*Версия: 2.0 | Генератор: `scripts/gen_excel.py`*
