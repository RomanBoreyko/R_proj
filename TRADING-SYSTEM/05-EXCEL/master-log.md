# EXCEL — МАСТЕР-ЛОГ

## Структура файла

Файл: `TRADING_MASTER_LOG.xlsx`

### Листы (Sheets)

```
1. TRADES          → каждая сделка
2. BOTS            → параметры и статус каждого бота
3. OPTIONS         → все опционные позиции
4. PORTFOLIO       → сводка портфеля по дням
5. CHART           → график роста портфеля
6. FORMULAS_REF    → справочник формул
```

---

## Лист 1: TRADES (каждая операция)

| Колонка | Тип | Описание |
|---------|-----|---------|
| A: `date` | DATE | Дата-время сделки |
| B: `platform` | TEXT | Bybit / Deribit / Coinbase |
| C: `type` | TEXT | GridFill / OptionOpen / OptionClose / Rebalance / Deposit / Арбитраж |
| D: `asset` | TEXT | XAUTUSDT / ETHUSDT / ... |
| E: `direction` | TEXT | Buy / Sell / Long / Short |
| F: `qty` | NUMBER | Количество |
| G: `price` | NUMBER | Цена исполнения |
| H: `value_usdt` | NUMBER | Объём в USDT (=qty×price) |
| I: `fee_usdt` | NUMBER | Комиссия |
| J: `pnl_usdt` | NUMBER | Реализованная прибыль по этой операции |
| K: `bot_id` | TEXT | ID бота (напр. XAUT_1D_BOT1) |
| L: `leg` | TEXT | Для многоногих: Leg1 / Leg2 / Leg3 |
| M: `strategy` | TEXT | Grid / OptionWrapper / CalSpread / IVArb / Rebalance |
| N: `notes` | TEXT | Комментарий |

### Пример записи

```
2025-01-15 | Bybit | GridFill | ETHUSDT | Buy→Sell | 0.1 | 3250 | 325 | 0.18 | 2.50 | ETH_1D_BOT1 | — | Grid | —
2025-01-15 | Bybit | OptionOpen | ETHUSDT | Sell | 1 | 45 | 45 | 0.10 | — | ETH_1D_BOT1 | Leg2_SoldCall | OptionWrapper | Strike 3500 Mar
```

---

## Лист 2: BOTS (параметры ботов)

| Колонка | Описание |
|---------|---------|
| `bot_id` | Уникальный ID |
| `asset` | Торговая пара |
| `timeframe` | 1H / 1D / 1W |
| `lower_price` | Нижняя граница |
| `upper_price` | Верхняя граница |
| `grid_count` | Кол-во ячеек |
| `grid_type` | Arithmetic / Geometric |
| `leverage` | Плечо |
| `investment_usdt` | Объём |
| `start_date` | Дата запуска |
| `status` | Active / Paused / Closed |
| `total_pnl_usdt` | Суммарная прибыль (SUMIF из TRADES) |
| `apr_actual` | Фактический APR |
| `option_wrapper` | Да / Нет |
| `option_expiry` | Экспирация обёртки |
| `review_date` | Дата следующего пересмотра диапазонов |

---

## Лист 3: OPTIONS (опционные позиции)

| Колонка | Описание |
|---------|---------|
| `position_id` | ID позиции |
| `bot_id` | Привязка к боту |
| `platform` | Bybit / Deribit |
| `asset` | Базовый актив |
| `type` | Call / Put |
| `direction` | Long / Short |
| `strike` | Страйк |
| `expiry` | Дата экспирации |
| `qty` | Лотов |
| `entry_premium` | Премия при входе |
| `current_value` | Текущая стоимость |
| `unrealized_pnl` | Нереализованная прибыль |
| `theta_daily` | Дневной тета (из Greeks) |
| `delta` | Дельта |
| `vega` | Вега |
| `status` | Open / Expired / Closed |

---

## Лист 4: PORTFOLIO (дневная сводка)

| Колонка | Описание |
|---------|---------|
| `date` | Дата |
| `total_equity_usdt` | Суммарный портфель |
| `bybit_balance` | Байбит (споты + маржа) |
| `deribit_balance` | Дерибит |
| `coinbase_balance` | Коинбейс |
| `deposit_income` | Доход от депозитов за день |
| `grid_pnl_day` | Прибыль сеток за день |
| `option_theta_day` | Тета-доход опций за день |
| `arb_pnl_day` | Арбитраж за день |
| `rebalance_pnl_day` | Ребалансировка за день |
| `total_pnl_day` | Итого прибыль за день |
| `cumulative_pnl` | Накопленная прибыль |
| `portfolio_return%` | Return% с начала |

---

## Лист 5: CHART (график роста)

### Данные для графика
```
X = колонка date из PORTFOLIO
Y1 = total_equity_usdt (линия портфеля)
Y2 = cumulative_pnl (линия прибыли)
Y3 = Benchmark (BTC или USDT+5% годовых)
```

### Настройка в Excel
```
1. Выделить date + total_equity_usdt + cumulative_pnl
2. Вставить → График → Линейный
3. Добавить вторую ось Y для cumulative_pnl
4. Форматировать: дата на X, USDT на Y
5. Добавить аннотации: даты запуска ботов, ребалансировок
```

---

## Формулы Excel для автоматизации

```excel
APR_фактический = (total_pnl_usdt / investment_usdt) / дней_работы * 365 * 100
  → =((SUMIF(TRADES!K:K,A2,TRADES!J:J))/F2)/((TODAY()-H2))*365*100

Дневная_тета_портфеля = SUMIF(OPTIONS!B:B,"Open",OPTIONS!M:M)

Уровень_ликвидации_проверка:
  → =IF(lower_price < entry_price*(1-1/leverage)*1.3,"⚠️ РИСК","✅ OK")
```

---

## Инструкция: добавить сделку

1. Открыть лист **TRADES**
2. Перейти на первую пустую строку
3. Заполнить все колонки A–N
4. Лист **PORTFOLIO** обновится через формулы автоматически
5. График на листе **CHART** обновится автоматически

> ⚠️ Вносить каждую операцию в день исполнения
> ⚠️ Для многоногих конструкций — каждая нога отдельной строкой с одинаковым `position_id`
