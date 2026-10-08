# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## О проекте

**R_trade_proj_sin-arb** — система торговли криптовалютами, построенная на трёх принципах:
- **Торговля временем** (тета-распад опционов, базисный спред фьюч/спот)
- **Арбитраж волатильности** (IV-спред между биржами, межактивная vol)
- **Радикальная диверсификация** (5 активов × 3 таймфрейма = 15 источников прибыли)

Шортов нет. Плечо ≤ 2×. Ликвидация недопустима.

---

## Репозиторий: структура

```
/
├── CLAUDE.md                          ← этот файл
├── CALC-FORMULAS.md                   ← все формулы системы (канонический источник)
├── formulas.ts                        ← TypeScript формулы (корневая копия)
├── TradingCalculator.html             ← standalone калькулятор (открыть в браузере)
├── TradingSystemCalculator.xlsx       ← Excel-версия (7 листов)
│
├── trading-calc/                      ← TypeScript/Vite проект калькулятора
│   ├── src/
│   │   ├── formulas.ts                ← расчётная библиотека (6 модулей)
│   │   └── types.ts                   ← типы + конфиги активов и комиссий
│   ├── package.json
│   └── tsconfig.json
│
└── TRADING-SYSTEM/                    ← Obsidian-документация
    ├── 00-INDEX.md
    ├── 01-CONCEPT.md
    ├── 02-INSTRUMENTS/                ← боты, опционы, биржи
    ├── 03-STRATEGIES/                 ← стратегии
    ├── 04-FORMULAS/                   ← формулы прибыли и риска
    ├── 05-EXCEL/                      ← структура мастер-лога
    ├── 06-RISK/                       ← правила риска и чек-листы
    └── 07-LINKS/                      ← документация бирж и API
```

---

## Команды: trading-calc (TypeScript/Vite)

```bash
cd trading-calc
npm install       # первая установка
npm run dev       # dev-сервер → http://localhost:5173
npm run build     # tsc + vite build → dist/
npm run preview   # превью собранного
```

TypeScript-конфиг: `strict: true`, `noUnusedLocals: true`, `noUnusedParameters: true`, `noImplicitReturns: true`.  
Нет тестового фреймворка — логику верифицировать через `TradingCalculator.html` или `npm run dev`.

---

## Архитектура: расчётная библиотека

`trading-calc/src/formulas.ts` — шесть независимых пар `(Params → Result)`:

| Функция | Назначение |
|---------|-----------|
| `calcGrid(GridParams)` | Сеточный бот: шаг, APR, ликвидация, срабатывания |
| `calcBasis(BasisParams)` | Базисный арбитраж спот/фьюч: нетто P&L, ROI ann. |
| `calcOptionsWrapper(OptionsWrapperParams)` | Опционная обёртка бота: тета, сценарии |
| `calcIVArb(IVArbParams)` | IV-арбитраж Bybit vs Deribit: vega P&L, break-even |
| `calcCalSpread(CalSpreadParams)` | Календарный спред Deribit: тета+вега+гамма P&L |
| `calcPortfolio(BotSummary[])` | Портфельная сводка: weighted APR, Sharpe, drawdown |

Вспомогательные утилиты: `formatUSD`, `formatPct`, `atrFromCandles`, `recommendedRange`.

`trading-calc/src/types.ts` содержит:
- `ASSETS` — конфиги 5 активов с ценами и ATR по трём таймфреймам (нужно обновлять вручную)
- `FEES` — константы комиссий (Bybit taker/maker, Deribit, Coinbase)

> **Два файла formulas.ts**: `./formulas.ts` (корень) и `./trading-calc/src/formulas.ts`. Канонический — в `trading-calc/src/`. Корневой — резервная копия; синхронизировать при изменениях.

---

## Ключевые формулы (не менять без понимания)

### Ликвидация лонга (Bybit USDT Perp)
```
LiqPrice = EntryPrice × (1 − 1/Leverage + 0.005)
SafetyRule: Lower_сетки > LiqPrice × 1.3
```

### APR сетки
```
FillsPerDay = ATR × 0.6 / Step          // 0.6 = коэффициент заполнения
DailyPnL = ProfitPerFill × FillsPerDay × GridCount × 0.3  // 0.3 = доля активных ячеек
APR = DailyPnL × 365 / Investment × 100
```

### Минимальный шаг (безубыточность)
```
MinStep = 2 × TakerFee × Price         // шаг ниже этого — убыток
```

### Диапазоны по таймфрейму
```
Lower = Price − 1.5 × ATR(20, TF)
Upper = Price + 1.5 × ATR(20, TF)
```

---

## Активы и платформы

| Актив | Плечо | Тип сетки |
|-------|-------|-----------|
| XAUTUSDT | **1×** | Arithmetic |
| ETHUSDT | 1.5× | Arithmetic |
| MNTUSDT | 1.5× | Geometric |
| XRPUSDT | 1.5× | Arithmetic |
| SOLUSDT | 1.5× | Geometric |

| Платформа | Роль | Макс. доля |
|-----------|------|-----------|
| **Bybit** | Ядро: сетки, споты, опционы | 60% |
| **Deribit** | IV-арбитраж, календари | 20% |
| **Coinbase** | Спот для базисного арбитража | 10% |
| **Стейблы/кэш** | Резервный буфер маржи | 10% |

---

## Правила риска (абсолютные)

```
❌ Шорты — ЗАПРЕЩЕНО (фьюч и спот)
❌ Плечо > 2× — ЗАПРЕЩЕНО
❌ Ликвидационный уровень в диапазоне сетки — ЗАПРЕЩЕНО
❌ Более 15% портфеля в одном боте — ЗАПРЕЩЕНО
✅ Lower_сетки > LiqPrice × 1.3
✅ Стресс-тест: макс убыток портфеля при -30% рынка ≈ 8–12%
```

---

## Excel мастер-лог (`TradingSystemCalculator.xlsx`)

Листы: `TRADES | BOTS | OPTIONS | PORTFOLIO | CHART | FORMULAS_REF`

Ключевая формула расчёта фактического APR:
```excel
=((SUMIF(TRADES!K:K,A2,TRADES!J:J))/F2)/((TODAY()-H2))*365*100
```
Проверка ликвидации:
```excel
=IF(lower_price < entry_price*(1-1/leverage)*1.3,"⚠️ РИСК","✅ OK")
```

---

## API бирж (ключевые эндпоинты)

**Bybit API V5** (`https://api.bybit.com`):
- `GET /v5/market/kline` — свечи для ATR
- `GET /v5/market/tickers` — текущая цена
- `GET /v5/position/list` — позиции
- `GET /v5/account/wallet-balance` — баланс

**Deribit API v2** (`https://www.deribit.com/api/v2`):
- `public/ticker` — IV и Greeks опциона
- `public/get_book_summary_by_currency` — все опционы
- WebSocket: `wss://www.deribit.com/ws/api/v2`

**Coinbase Advanced Trade** (`https://api.coinbase.com/api/v3`):
- Spot ордера и балансы

---

## Obsidian-документация (TRADING-SYSTEM/)

Файлы в `TRADING-SYSTEM/` — Obsidian Markdown с wiki-ссылками `[[имя]]`. Редактировать через Obsidian или любой Markdown-редактор. При добавлении нового файла — обновить `00-INDEX.md`.

Канонический источник формул для документации: `CALC-FORMULAS.md` (корень репозитория).
