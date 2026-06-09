# TRADING SYSTEM — INDEX

> **Торговля временем. Арбитраж волатильности. Радикальная диверсификация.**

---

## Философия

Система не угадывает направление. Система **собирает премии** за:
- Предоставление ликвидности (сетки)
- Временной распад опционов (тета)
- Базисный спред (спот–фьюч–перп)
- Раскорреляцию инструментов (календари)
- Волатильность спредов (ребалансировка гаммы)

---

## Дерево

```
TRADING-SYSTEM/
├── 00-INDEX.md                  ← этот файл
├── 01-CONCEPT.md                ← философия, принципы, карта прибыли
│
├── 02-INSTRUMENTS/
│   ├── bybit-grid-bots.md       ← сеточные боты: параметры, формулы
│   ├── bybit-options-wrapper.md ← опционная обёртка каждого бота
│   ├── bybit-spot-deposits.md   ← споты и депозиты
│   ├── deribit-strategies.md    ← дерибит: календари, арб
│   ├── coinbase-arbitrage.md    ← коинбейс: арб к байбиту
│   └── api-references.md        ← ссылки на документацию бирж
│
├── 03-STRATEGIES/
│   ├── time-trading.md          ← торговля временем (тета + календари)
│   ├── volatility-arbitrage.md  ← арбитраж волатильности
│   ├── xau-synthetic-spread.md  ← XAU: синт-спред спот/перп + вола стренглами
│   ├── spread-collection.md     ← сбор спредов
│   ├── gamma-rebalancing.md     ← ребалансировка гаммы
│   └── radical-diversification.md ← радикальная диверсификация
│
├── 04-FORMULAS/
│   ├── profit-formulas.md       ← формулы прибыли по каждому инструменту
│   ├── risk-formulas.md         ← формулы риска, маржа, ликвидация
│   └── spread-formulas.md       ← базис, спред, Z-score
│
├── 05-EXCEL/
│   ├── master-log.md            ← структура мастер-лога всех сделок
│   ├── bot-parameters.md        ← параметры каждого бота в таблице
│   └── portfolio-chart.md       ← инструкция: график роста портфеля
│
├── 06-RISK/
│   ├── risk-rules.md            ← правила: плечо, ликвидация, диапазоны
│   └── liquidation-prevention.md ← защита от ликвидации
│
└── 07-LINKS/
    └── documentation.md         ← все ссылки на документацию
```

---

## Быстрые ссылки

| Раздел | Файл |
|--------|------|
| Философия | [[01-CONCEPT]] |
| Сеточные боты | [[02-INSTRUMENTS/bybit-grid-bots]] |
| Опционная обёртка | [[02-INSTRUMENTS/bybit-options-wrapper]] |
| Торговля временем | [[03-STRATEGIES/time-trading]] |
| Арбитраж волатильности | [[03-STRATEGIES/volatility-arbitrage]] |
| XAU синт-спред + вола | [[03-STRATEGIES/xau-synthetic-spread]] |
| Опционный кондор + зонный хедж | [[03-STRATEGIES/options-condor-zone-hedge]] |
| Портфель — операционный план | [[03-STRATEGIES/portfolio-operating-plan]] |
| Мультиактивная опц. книга (C5) | [[03-STRATEGIES/multi-asset-options]] |
| Формулы прибыли | [[04-FORMULAS/profit-formulas]] |
| Excel мастер-лог | [[05-EXCEL/master-log]] |
| Правила риска | [[06-RISK/risk-rules]] |
| Документация бирж | [[07-LINKS/documentation]] |
| Инфраструктура (стек) | [[09-INFRASTRUCTURE]] |
| Калькулятор (веб-апп) | [[scripts/webapp/README]] |

---

## Статус портфеля

| Платформа | Роль |
|-----------|------|
| **Bybit** | Ядро. Сетки, споты, депозиты, опционы |
| **Deribit** | Периферия. Календарные спреды, арбитраж волатильности |
| **Coinbase** | Периферия. Арбитраж к байбит-споту/фьючу |
| **MetaScalp** | Аналитика, сигналы |
| **TradingView** | Визуализация спредов, корреляций |
| **Excel** | Единая точка правды — все сделки, все боты |
| **VSCode + GitHub** | Код ботов, скрипты, автоматизация |

---

*Версия: 1.0 | Обновлять при каждом изменении конструкции*
