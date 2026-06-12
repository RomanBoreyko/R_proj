# scripts/portfolio — аналитика книги (read-only Bybit)

Все читают ключи из `C:\Users\DoOs\.bybit\credentials.json` (вне git, read-only).
Запуск: `python <script>.py`. Роль: 🧾 [[08-operator/roles/CON|CON]] / 📈 [[08-operator/roles/SPR|SPR]].

| Скрипт | Что делает |
|---|---|
| `analyze_book.py` | Полный срез: эквити/IM/MM, греки по активам, Δ$, перпы, опционы, ratio, спот |
| `recent_ops.py` | Исполнения за 24ч + открытые ордера |
| `pnl_breakdown.py` | Денежные потоки за 48ч по типам (LIQUIDATION/TRADE/DELIVERY/FUNDING) |
| `iv_rv.py` | IV vs RV по корзине BTC/ETH/SOL/MNT/XAUT + терм-структура (гейт №1 перед входом) |
| `payoff.py [ASSET]` | Payoff-кривая субкниги (сейчас + ближняя экспирация) + греки → `payoff_data.json` |
| `parity_scan.py` | Скан пут-колл паритета по цепочкам ETH/XAUT (C6 базис-десک) |
| `metascalp_probe.py` | Read-only зонд MetaScalp Local API (порты 17845–17855) |

Связано: [[03-STRATEGIES/portfolio-operating-plan]] · [[06-RISK/risk-rules]] · [[scripts/webapp/README]]
