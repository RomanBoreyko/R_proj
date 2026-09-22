# КАЛЬКУЛЯТОР — ФОРМУЛЫ СИСТЕМЫ**
> Excel: `TradingSystemCalculator.xlsx`

---

## СЕТОЧНЫЙ БОТ

### Шаг сетки

```
Arithmetic: Шаг = (Upper - Lower) / N

Geometric:  Ratio = (Upper / Lower)^(1/N)
            Шаг = Lower × (Ratio - 1)
```

### Прибыль на срабатывание

```
Arithmetic:
  Cell_Capital = Инвестиции / N
  Profit_cell  = Шаг × (Cell_Capital / Price) - 2 × TakerFee × Cell_Capital

Geometric:
  Profit_cell% = (Ratio - 1) - 2 × TakerFee
  Profit_cell$ = Cell_Capital × Profit_cell%
```

### APR сетки

```
Fills_per_day = ATR × 0.6 / Шаг

APR = (Profit_cell × Fills_per_day × N × 0.3 × 365) / Инвестиции × 100
```

### Ликвидация

```
Liq_Price = Entry × (1 - 1/Leverage + 0.005)
Buffer    = (Lower - Liq_Price) / Liq_Price × 100
Safe?     = Lower > Liq_Price × 1.3  ← правило системы

Min_Step  = 2 × TakerFee × Price   ← шаг ниже этого = убыток
```

---

## БАЗИСНЫЙ АРБИТРАЖ (Bybit Spot + Deribit Futures)

```
Basis$      = Futures_Price - Spot_Price
Basis%      = Basis$ / Spot_Price × 100
Ann_Basis   = Basis% × (365 / Days_to_Expiry)

Gross_PnL   = Basis$ × Lots
Fees        = (Spot × SpotFee + Fut × FutFee) × Lots × 2
Net_PnL     = Gross_PnL - Fees

Capital     = Spot × Lots + Futures × Lots × 0.05
ROI         = Net_PnL / Capital × 100
ROI_Ann     = ROI × (365 / Days_to_Expiry)
Daily_Decay = Net_PnL / Days_to_Expiry
```

**Вход**: `Ann_Basis > 2%`
**Выход**: экспирация фьюча (Basis → 0 автоматически)

---

## ОПЦИОННАЯ ОБЁРТКА

```
Страйки:
  Sold_Call  = Upper_Grid × 1.05
  Sold_Put   = Lower_Grid × 0.90
  Bought_Put = Lower_Grid × 0.95   ← защита

Net_Premium     = (SoldCall_Prem + SoldPut_Prem - BoughtPut_Prem) × Lots
Net_Theta_day   = (Theta_SoldCall + Theta_SoldPut - Theta_BoughtPut) × Lots
APR_wrapper     = (Net_Premium × 365/Days) / Bot_Investment × 100

Max_Loss_PutSpread = (BoughtPut_Strike - SoldPut_Strike) × Lots - Net_Premium
Break_even_down    = SoldPut_Strike - Net_Premium / Lots
Break_even_up      = SoldCall_Strike + Net_Premium / Lots
```

**Правило**: `Net_Premium > 0` → кредитная конструкция (предпочтительно)
**Правило**: `Max_Loss < 20% × Bot_Investment`

---

## IV АРБИТРАЖ (Bybit vs Deribit Options)

```
IV_Spread   = IV_Deribit - IV_Bybit      (продаём дороже, покупаем дешевле)
Vega_PnL    = IV_Spread × Vega × Lots
Fees        = (Fee_Sell + Fee_Buy) × Spot × Lots × 2
Net_PnL     = Vega_PnL - Fees

Break_even_spread = Fees / (Vega × Lots)

IV_Premium  = IV_ATM - RV_30d            (рыночная премия над реализованной vol)
```

**Вход**: `IV_Spread > Break_even × 1.5` и `IV_Spread > 3%`
**Выход**: схождение `IV_Spread < 1%` или экспирация

---

## КАЛЕНДАРНЫЙ СПРЕД (Deribit)

```
Net_Debit     = (Far_Premium - Near_Premium) × Lots
Net_Theta_day = (Theta_Near - Theta_Far) × Lots
Net_Vega      = (Vega_Far - Vega_Near) × Lots
Net_Gamma     = (Gamma_Far - Gamma_Near) × Lots

Break_even_move = √(2 × |Net_Theta| / |Net_Gamma|) / Spot × 100

P&L_theta = Net_Theta_day × Days_to_Near_Expiry
P&L_vega  = Net_Vega × ΔIV
P&L_gamma = Net_Gamma × ΔS² / 2
Total_PnL = P&L_theta + P&L_vega + P&L_gamma - Net_Debit
```

**Вход**: `IV_Near > IV_Far` (нормальная term structure), `IV_Spread > 3%`
**Макс прибыль**: цена = страйк на момент экспирации ближнего
**Выход**: экспирация ближнего опциона

---

## ПОРТФЕЛЬ / РЕБАЛАНСИРОВКА

```
Weighted_APR = Σ(APR_i × Capital_i) / Total_Capital

Rebalance_trigger = |Current_Weight - Target_Weight| > 5%
Rebalance_size    = (Target_Weight - Current_Weight) × Total_Capital

Rebalance_bonus  ≈ σ² × (1 - ρ̄) / 2   (теоретическая прибавка от ребаланса)
где ρ̄ = средняя попарная корреляция

Sharpe_estimate = (Weighted_APR - 5%) / Portfolio_Vol
```

**Целевые веса**: XAUT 25% | ETH 25% | SOL 20% | XRP 15% | MNT 15%
**Платформы**: Bybit 60% | Deribit 20% | Coinbase 10% | Cash 10%

---

## КОНСТАНТЫ

| Константа | Значение | Применение |
|-----------|---------|-----------|
| Bybit Taker fee | 0.055% | USDT Perpetual |
| Bybit Maker fee | 0.002% | Limit ордера |
| Deribit Taker fee | 0.03% | Options + Futures |
| Coinbase Taker fee | 0.6% | Spot |
| Maintenance Margin | 0.5% | Bybit USDT perp |
| Макс плечо | 2× | Правило системы |
| Мин буфер ликвид | ×1.3 | Lower > Liq × 1.3 |
| ATR эффективность | ×0.6 | Fills per day estimation |

---

## ФАЙЛЫ КАЛЬКУЛЯТОРА

| Файл | Описание |
|------|---------|
| `TradingCalculator.html` | Standalone калькулятор (открыть в браузере) |
| `TradingSystemCalculator.xlsx` | Excel версия (7 листов) |
| `formulas.ts` | TypeScript исходник формул |
| `trading-calc-typescript.zip` | Полный TypeScript проект (Vite) |

### Запуск TypeScript проекта

```bash
cd trading-calc
npm install
npm run dev    # → http://localhost:5173
```

---

*Связанные ноты: [[bybit-grid-bots]] | [[bybit-options-wrapper]] | [[deribit-strategies]] | [[profit-formulas]]*
