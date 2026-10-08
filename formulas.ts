// ============================================================
// TRADING SYSTEM — FORMULAS
// Bybit + Deribit + Coinbase арбитраж, сетки, опционы
// ============================================================

export const TAKER_FEE = 0.00055; // Bybit USDT perp taker
export const MAKER_FEE = 0.00002; // Bybit USDT perp maker
export const DERIBIT_FEE = 0.0003; // Deribit taker

// ─────────────────────────────────────────────────────────
// 1. GRID BOT
// ─────────────────────────────────────────────────────────

export interface GridParams {
  upper: number;       // верхняя граница
  lower: number;       // нижняя граница
  gridCount: number;   // количество ячеек
  gridType: 'arithmetic' | 'geometric';
  investment: number;  // объём USDT
  leverage: number;    // плечо 1-2
  entryPrice: number;  // текущая цена
  atr: number;         // ATR выбранного ТФ
  takerFee?: number;   // ставка комиссии
}

export interface GridResult {
  step: number;              // шаг сетки ($)
  stepPct: number;           // шаг в %
  profitPerFill: number;     // прибыль одного срабатывания ($)
  profitPerFillPct: number;  // прибыль в %
  fillsPerDay: number;       // срабатываний в сутки (оценка)
  dailyPnl: number;          // дневная прибыль ($)
  apr: number;               // годовых (%)
  liqPrice: number;          // цена ликвидации
  liqBuffer: number;         // буфер до ликвидации (%)
  liqSafe: boolean;          // безопасно (lower > liqPrice * 1.3)
  minStep: number;           // минимальный шаг для безубыточности
  rangeWidth: number;        // ширина диапазона (%)
  capitalPerGrid: number;    // капитал на ячейку
}

export function calcGrid(p: GridParams): GridResult {
  const fee = p.takerFee ?? TAKER_FEE;
  const range = p.upper - p.lower;
  const rangeWidth = (range / p.lower) * 100;

  let step: number, stepPct: number, profitPerFill: number, profitPerFillPct: number;

  if (p.gridType === 'arithmetic') {
    step = range / p.gridCount;
    stepPct = (step / p.lower) * 100;
    profitPerFill = step - 2 * fee * p.entryPrice * (p.investment / p.gridCount / p.entryPrice);
    // упрощённо: прибыль = шаг - двойная комиссия от цены ячейки
    const cellValue = p.investment / p.gridCount;
    profitPerFill = step * (cellValue / p.entryPrice) - 2 * fee * cellValue;
    profitPerFillPct = profitPerFill / cellValue * 100;
  } else {
    // geometric
    const ratio = Math.pow(p.upper / p.lower, 1 / p.gridCount);
    step = p.lower * (ratio - 1); // средний шаг
    stepPct = (ratio - 1) * 100;
    profitPerFillPct = stepPct - 2 * fee * 100;
    const cellValue = p.investment / p.gridCount;
    profitPerFill = cellValue * profitPerFillPct / 100;
  }

  // Оценка срабатываний: ATR / шаг * коэффициент заполнения
  const fillsPerDay = Math.max(0, (p.atr * 0.6) / step);
  const dailyPnl = profitPerFill * fillsPerDay * p.gridCount * 0.3; // ~30% ячеек активны
  const apr = (dailyPnl * 365) / p.investment * 100;

  // Ликвидация (для LONG)
  // Цена ликвидации ≈ Entry × (1 - 1/leverage + maintenanceMargin)
  const maintMargin = 0.005; // 0.5% maintenance margin
  const liqPrice = p.entryPrice * (1 - 1 / p.leverage + maintMargin);
  const liqBuffer = ((p.lower - liqPrice) / liqPrice) * 100;
  const liqSafe = p.lower > liqPrice * 1.3;

  // Минимальный шаг для безубыточности
  const minStep = 2 * fee * p.entryPrice;

  const capitalPerGrid = p.investment / p.gridCount;

  return {
    step, stepPct, profitPerFill, profitPerFillPct,
    fillsPerDay, dailyPnl, apr, liqPrice, liqBuffer,
    liqSafe, minStep, rangeWidth, capitalPerGrid
  };
}

// ─────────────────────────────────────────────────────────
// 2. BASIS ARB (Bybit Spot + Deribit Futures)
// ─────────────────────────────────────────────────────────

export interface BasisParams {
  spotPrice: number;      // цена спота (Bybit)
  futuresPrice: number;   // цена фьюча (Deribit)
  daysToExpiry: number;   // дней до экспирации
  lots: number;           // размер позиции (в единицах актива)
  spotFee?: number;       // комиссия спота
  futuresFee?: number;    // комиссия фьюча
}

export interface BasisResult {
  basis: number;          // basis в $
  basisPct: number;       // basis в %
  annualizedBasis: number; // годовых (%)
  grossPnl: number;       // брутто P&L
  fees: number;           // суммарные комиссии
  netPnl: number;         // нетто P&L
  capitalRequired: number; // капитал (спот + маржа)
  roi: number;            // ROI на капитал (%)
  roiAnnualized: number;  // ROI годовых (%)
  dailyDecay: number;     // дневное схождение ($)
}

export function calcBasis(p: BasisParams): BasisResult {
  const spotFee = p.spotFee ?? TAKER_FEE;
  const futFee = p.futuresFee ?? DERIBIT_FEE;

  const basis = p.futuresPrice - p.spotPrice;
  const basisPct = (basis / p.spotPrice) * 100;
  const annualizedBasis = basisPct * (365 / p.daysToExpiry);

  const grossPnl = basis * p.lots;
  const fees = (p.spotPrice * spotFee + p.futuresPrice * futFee) * p.lots * 2;
  const netPnl = grossPnl - fees;

  // Капитал = полная стоимость спота + маржа фьюча (5% от номинала)
  const capitalRequired = p.spotPrice * p.lots + p.futuresPrice * p.lots * 0.05;
  const roi = (netPnl / capitalRequired) * 100;
  const roiAnnualized = roi * (365 / p.daysToExpiry);
  const dailyDecay = netPnl / p.daysToExpiry;

  return {
    basis, basisPct, annualizedBasis, grossPnl,
    fees, netPnl, capitalRequired, roi, roiAnnualized, dailyDecay
  };
}

// ─────────────────────────────────────────────────────────
// 3. OPTIONS WRAPPER
// ─────────────────────────────────────────────────────────

export interface OptionsWrapperParams {
  // Bot параметры
  botLower: number;
  botUpper: number;
  botInvestment: number;
  entryPrice: number;
  daysToExpiry: number;
  contractSize: number; // лотов

  // Проданный CALL
  soldCallStrike: number;
  soldCallPremium: number;
  soldCallTheta: number; // в день
  soldCallDelta: number;
  soldCallVega: number;

  // Проданный PUT
  soldPutStrike: number;
  soldPutPremium: number;
  soldPutTheta: number;
  soldPutDelta: number;

  // Купленный PUT (защита)
  boughtPutStrike: number;
  boughtPutPremium: number;
  boughtPutTheta: number;
  boughtPutDelta: number;
}

export interface OptionsWrapperResult {
  netPremium: number;      // нетто премия при входе
  netThetaDaily: number;   // нетто тета в день
  aprWrapper: number;      // APR обёртки
  maxLossPutSpread: number; // макс убыток пут-спреда
  deltaNet: number;        // нетто дельта конструкции
  breakEvenDown: number;   // точка безубыточности снизу
  breakEvenUp: number;     // точка безубыточности сверху
  scenarios: {
    bestCase: number;      // всё в диапазоне → вся премия
    midCase: number;       // пробой на 50% выхода
    badDown: number;       // пробой вниз
    badUp: number;         // пробой вверх
  };
}

export function calcOptionsWrapper(p: OptionsWrapperParams): OptionsWrapperResult {
  const netPremium = (p.soldCallPremium + p.soldPutPremium - p.boughtPutPremium) * p.contractSize;
  const netThetaDaily = (p.soldCallTheta + p.soldPutTheta - p.boughtPutTheta) * p.contractSize;
  const aprWrapper = (netPremium * (365 / p.daysToExpiry)) / p.botInvestment * 100;

  const maxLossPutSpread = (p.boughtPutStrike - p.soldPutStrike) * p.contractSize - netPremium;
  const deltaNet = (p.soldCallDelta + p.soldPutDelta - p.boughtPutDelta) * p.contractSize;

  const breakEvenDown = p.soldPutStrike - netPremium / p.contractSize;
  const breakEvenUp = p.soldCallStrike + netPremium / p.contractSize;

  const scenarios = {
    bestCase: netPremium,
    midCase: netPremium * 0.5,
    badDown: -maxLossPutSpread,
    badUp: -(p.soldCallStrike - p.entryPrice) * p.contractSize * 0.5 + p.soldCallPremium * p.contractSize
  };

  return {
    netPremium, netThetaDaily, aprWrapper, maxLossPutSpread,
    deltaNet, breakEvenDown, breakEvenUp, scenarios
  };
}

// ─────────────────────────────────────────────────────────
// 4. IV ARB (Bybit Options vs Deribit Options)
// ─────────────────────────────────────────────────────────

export interface IVArbParams {
  ivSell: number;     // IV биржи где продаём (%)
  ivBuy: number;      // IV биржи где покупаем (%)
  vega: number;       // вега за 1 контракт (в $ на 1% IV)
  lots: number;
  feeSell: number;    // комиссия биржи продажи (%)
  feeBuy: number;     // комиссия биржи покупки (%)
  spotPrice: number;
  strikePrice: number;
  daysToExpiry: number;
}

export interface IVArbResult {
  ivSpread: number;         // разница IV (%)
  vegaPnl: number;          // P&L через вегу ($)
  fees: number;
  netPnl: number;
  breakEvenSpread: number;  // минимальный спред для безубыточности
  roi: number;
  annualizedRoi: number;
  isViable: boolean;        // спред > break-even
}

export function calcIVArb(p: IVArbParams): IVArbResult {
  const ivSpread = p.ivSell - p.ivBuy;
  const vegaPnl = ivSpread * p.vega * p.lots;
  const fees = (p.feeSell + p.feeBuy) * p.spotPrice * p.lots * 2;
  const netPnl = vegaPnl - fees;
  const breakEvenSpread = fees / (p.vega * p.lots);
  const capitalRequired = p.spotPrice * p.lots * 0.1; // ~10% маржа для опций
  const roi = (netPnl / capitalRequired) * 100;
  const annualizedRoi = roi * (365 / p.daysToExpiry);
  const isViable = ivSpread > breakEvenSpread;

  return { ivSpread, vegaPnl, fees, netPnl, breakEvenSpread, roi, annualizedRoi, isViable };
}

// ─────────────────────────────────────────────────────────
// 5. CALENDAR SPREAD (Deribit)
// ─────────────────────────────────────────────────────────

export interface CalSpreadParams {
  // Ближняя экспирация (продаём)
  nearIV: number;
  nearTheta: number;  // $ в день (положительный = нам платят)
  nearGamma: number;
  nearVega: number;
  nearDays: number;
  nearPremium: number;

  // Дальняя экспирация (покупаем)
  farIV: number;
  farTheta: number;   // $ в день (отрицательный = мы платим)
  farGamma: number;
  farVega: number;
  farDays: number;
  farPremium: number;

  lots: number;
  spotPrice: number;
  strike: number;
}

export interface CalSpreadResult {
  ivSpread: number;           // разница IV
  netDebit: number;           // нетто дебет при входе
  netThetaDaily: number;      // нетто тета в день
  netVega: number;            // нетто вега
  netGamma: number;           // нетто гамма
  breakEvenMove: number;      // % движения для достижения безубытка
  maxProfitAtStrike: number;  // макс прибыль при цене = страйк
  aprEstimate: number;        // APR оценка
  scenarios: {
    atStrike: number;
    move5pct: number;
    move10pct: number;
    volCrush: number;
  };
}

export function calcCalSpread(p: CalSpreadParams): CalSpreadResult {
  const ivSpread = p.nearIV - p.farIV;
  const netDebit = (p.farPremium - p.nearPremium) * p.lots;
  const netThetaDaily = (p.nearTheta - p.farTheta) * p.lots;
  const netVega = (p.farVega - p.nearVega) * p.lots;
  const netGamma = (p.farGamma - p.nearGamma) * p.lots;

  const breakEvenMove = netGamma !== 0
    ? Math.sqrt(2 * Math.abs(netThetaDaily) / Math.abs(netGamma)) / p.spotPrice * 100
    : 5; // default 5%

  const maxProfitAtStrike = netThetaDaily * p.nearDays - netDebit;
  const aprEstimate = maxProfitAtStrike > 0
    ? (maxProfitAtStrike / Math.max(netDebit, 1)) * (365 / p.nearDays) * 100
    : 0;

  const scenarios = {
    atStrike: netThetaDaily * p.nearDays - netDebit,
    move5pct: netThetaDaily * p.nearDays * 0.7 - netDebit,
    move10pct: netThetaDaily * p.nearDays * 0.3 - netDebit - Math.abs(netGamma) * Math.pow(p.spotPrice * 0.1, 2) / 2,
    volCrush: netVega * (-5) - netDebit + netThetaDaily * p.nearDays  // IV -5%
  };

  return {
    ivSpread, netDebit, netThetaDaily, netVega, netGamma,
    breakEvenMove, maxProfitAtStrike, aprEstimate, scenarios
  };
}

// ─────────────────────────────────────────────────────────
// 6. PORTFOLIO SUMMARY
// ─────────────────────────────────────────────────────────

export interface BotSummary {
  asset: string;
  timeframe: '1H' | '1D' | '1W';
  investment: number;
  apr: number;
  active: boolean;
}

export interface PortfolioResult {
  totalCapital: number;
  weightedAPR: number;
  dailyPnlEstimate: number;
  monthlyPnlEstimate: number;
  yearlyPnlEstimate: number;
  sharpeEstimate: number;
  maxDrawdownEstimate: number;
  activeBots: number;
}

export function calcPortfolio(bots: BotSummary[]): PortfolioResult {
  const active = bots.filter(b => b.active);
  const totalCapital = active.reduce((s, b) => s + b.investment, 0);
  const weightedAPR = totalCapital > 0
    ? active.reduce((s, b) => s + b.apr * b.investment, 0) / totalCapital
    : 0;

  const dailyPnlEstimate = totalCapital * weightedAPR / 100 / 365;
  const monthlyPnlEstimate = dailyPnlEstimate * 30;
  const yearlyPnlEstimate = totalCapital * weightedAPR / 100;

  // Упрощённые оценки
  const sharpeEstimate = weightedAPR / 15; // предположим 15% vol портфеля
  const maxDrawdownEstimate = 15; // исторически для такой стратегии ~10-20%

  return {
    totalCapital, weightedAPR, dailyPnlEstimate,
    monthlyPnlEstimate, yearlyPnlEstimate,
    sharpeEstimate, maxDrawdownEstimate, activeBots: active.length
  };
}

// ─────────────────────────────────────────────────────────
// УТИЛИТЫ
// ─────────────────────────────────────────────────────────

export function formatUSD(n: number, decimals = 0): string {
  const abs = Math.abs(n);
  const formatted = abs.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  return (n >= 0 ? '+$' : '-$') + formatted;
}

export function formatPct(n: number, decimals = 1): string {
  return (n >= 0 ? '+' : '') + n.toFixed(decimals) + '%';
}

export function atrFromCandles(high: number[], low: number[], close: number[]): number {
  if (high.length < 2) return 0;
  let atr = 0;
  for (let i = 1; i < high.length; i++) {
    const tr = Math.max(
      high[i] - low[i],
      Math.abs(high[i] - close[i - 1]),
      Math.abs(low[i] - close[i - 1])
    );
    atr += tr;
  }
  return atr / (high.length - 1);
}

// Рекомендованные диапазоны на основе ATR
export function recommendedRange(price: number, atr: number, multiplier = 1.5) {
  return {
    lower: price - multiplier * atr,
    upper: price + multiplier * atr
  };
}
