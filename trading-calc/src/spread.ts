// ============================================================
// SYNTHETIC SPREAD — синтетический спред золота (спот-лонг / перп-шорт)
// Спред = ногаA − ногаB. Торгуем волу спреда стренглами.
// Чистые функции + оркестратор с живыми данными Bybit.
// См. стратегию: TRADING-SYSTEM/03-STRATEGIES/xau-synthetic-spread.md
// ============================================================

import { BybitClient, BARS_PER_YEAR, type Interval, type Candle } from './bybit';

// ─────────────────────────── статистика ───────────────────────────
export function mean(xs: number[]): number {
  return xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : NaN;
}

export function stdev(xs: number[]): number {
  const n = xs.length;
  if (n < 2) return NaN;
  const m = mean(xs);
  return Math.sqrt(xs.reduce((s, x) => s + (x - m) ** 2, 0) / (n - 1));
}

/** Доля значений series, которые <= value (0..1). */
export function percentileRank(series: number[], value: number): number {
  if (!series.length) return NaN;
  return series.filter((x) => x <= value).length / series.length;
}

/** Совмещает два ряда свечей по таймстемпу и считает спред (USD или %). */
export function alignSpread(a: Candle[], b: Candle[], usePct: boolean): number[] {
  const mapB = new Map(b.map((c) => [c.ts, c.close]));
  const out: number[] = [];
  for (const c of a) {
    const pb = mapB.get(c.ts);
    if (pb !== undefined) out.push(usePct ? ((c.close - pb) / pb) * 100 : c.close - pb);
  }
  return out;
}

// ─────────────────────────── модели результата ───────────────────────────
export interface SpreadStats {
  current: number;
  mean: number;
  sigma: number;
  z: number;
  innerUp: number; innerDn: number;   // проданный центр ±k_in·σ
  outerUp: number; outerDn: number;   // крылья ±k_out·σ
}

export interface SpreadVol {
  sigmaReturns: number;   // сигма ретёрнов спреда (окно)
  annualized: number;     // аннуализированная (если известен интервал)
  percentile: number;     // 0..1 — перцентиль текущей волы
  verdict: 'дёшево' | 'нейтрально' | 'дорого';
}

export interface Carry {
  fundingShortLeg: number;   // фандинг перп-ноги B (доля за период)
  perPeriodPct: number;      // % за период (в пользу шорта если > 0)
  annualizedPct: number;     // ≈ годовых (×3×365)
  favorable: boolean;
}

export interface Sizing {
  notionalPerLeg: number;
  qtyLong: number;
  qtyShort: number;
}

export interface StrangleLevels {
  rvAnnual: number;          // RV годовая ноги A
  expectedMove: number;      // ±1σ ожидаемый ход цены
  soldDn: number; soldUp: number;   // проданные страйки ±1σ
  wingDn: number; wingUp: number;   // крылья ±2σ
}

// ─────────────────────────── чистые расчёты ───────────────────────────
export function computeStats(spread: number[], window: number, kIn: number, kOut: number): SpreadStats {
  const win = spread.slice(-window);
  const m = mean(win);
  const sg = stdev(win);
  const cur = spread[spread.length - 1];
  return {
    current: cur,
    mean: m,
    sigma: sg,
    z: sg ? (cur - m) / sg : NaN,
    innerUp: m + kIn * sg, innerDn: m - kIn * sg,
    outerUp: m + kOut * sg, outerDn: m - kOut * sg,
  };
}

export function computeVol(spread: number[], window: number, interval?: Interval): SpreadVol {
  const rets: number[] = [];
  for (let i = 1; i < spread.length; i++) rets.push(spread[i] - spread[i - 1]);
  const volNow = stdev(rets.slice(-window));

  // ряд скользящей сигмы для перцентиля
  const volSeries: number[] = [];
  for (let i = window; i <= rets.length; i++) volSeries.push(stdev(rets.slice(i - window, i)));
  const pr = percentileRank(volSeries, volNow);

  const bpy = interval ? BARS_PER_YEAR[interval] : 0;
  return {
    sigmaReturns: volNow,
    annualized: bpy ? volNow * Math.sqrt(bpy) : NaN,
    percentile: pr,
    verdict: pr < 0.3 ? 'дёшево' : pr > 0.7 ? 'дорого' : 'нейтрально',
  };
}

export function computeCarry(fundingShortLeg: number): Carry {
  const per = fundingShortLeg * 100;
  return {
    fundingShortLeg,
    perPeriodPct: per,
    annualizedPct: fundingShortLeg * 3 * 365 * 100,
    favorable: fundingShortLeg > 0,
  };
}

export function computeSizing(notionalPerLeg: number, priceLong: number, priceShort: number): Sizing {
  return {
    notionalPerLeg,
    qtyLong: notionalPerLeg / priceLong,
    qtyShort: notionalPerLeg / priceShort,
  };
}

/** Уровни стренгла по реализованной воле ноги A (на underlying, не на спреде). */
export function computeStrangle(
  candlesA: Candle[], price: number, window: number, horizonDays: number, interval?: Interval,
): StrangleLevels {
  const logRets: number[] = [];
  for (let i = 1; i < candlesA.length; i++) logRets.push(Math.log(candlesA[i].close / candlesA[i - 1].close));
  const bpy = interval ? BARS_PER_YEAR[interval] : 0;
  const rvAnnual = bpy ? stdev(logRets.slice(-window)) * Math.sqrt(bpy) : NaN;
  const em = price * rvAnnual * Math.sqrt(horizonDays / 365);
  return {
    rvAnnual,
    expectedMove: em,
    soldDn: price - em, soldUp: price + em,
    wingDn: price - 2 * em, wingUp: price + 2 * em,
  };
}

// ─────────────────────────── оркестратор (живые данные) ───────────────────────────
export interface SpreadConfig {
  category?: 'linear' | 'spot' | 'inverse';
  legA: string;            // лонг (напр. XAUTUSDT)
  legB: string;            // шорт (напр. XAUUSDT)
  interval: Interval;
  window: number;
  kInner: number;
  kOuter: number;
  spreadPct?: boolean;
  targetNotionalUsd?: number;
  strangleHorizonDays?: number;
}

export interface SpreadAnalysis {
  legA: string; legB: string;
  priceA: number; priceB: number;
  unit: 'USD' | '%';
  stats: SpreadStats;
  vol: SpreadVol;
  carry: Carry;
  sizing?: Sizing;
  strangle?: StrangleLevels;
}

export async function analyzeSpread(cfg: SpreadConfig): Promise<SpreadAnalysis> {
  const client = new BybitClient(cfg.category ?? 'linear');
  const usePct = !!cfg.spreadPct;

  // Тянем историю с запасом: перцентиль волы считается по ряду скользящих
  // сигм, которому нужно МНОГО окон. limit = window+5 дал бы ~5 точек → шум.
  const limit = Math.min(1000, Math.max(cfg.window * 3, 300));
  const [ta, tb, ca, cb] = await Promise.all([
    client.getTicker(cfg.legA),
    client.getTicker(cfg.legB),
    client.getCandles(cfg.legA, cfg.interval, limit),
    client.getCandles(cfg.legB, cfg.interval, limit),
  ]);

  const spread = alignSpread(ca, cb, usePct);
  if (spread.length < cfg.window) {
    throw new Error(`Недостаточно совмещённых баров: ${spread.length} < окно ${cfg.window}`);
  }

  const stats = computeStats(spread, cfg.window, cfg.kInner, cfg.kOuter);
  const vol = computeVol(spread, cfg.window, cfg.interval);
  const carry = computeCarry(tb.fundingRate);

  const result: SpreadAnalysis = {
    legA: cfg.legA, legB: cfg.legB,
    priceA: ta.lastPrice, priceB: tb.lastPrice,
    unit: usePct ? '%' : 'USD',
    stats, vol, carry,
  };

  if (cfg.targetNotionalUsd && cfg.targetNotionalUsd > 0) {
    result.sizing = computeSizing(cfg.targetNotionalUsd, ta.lastPrice, tb.lastPrice);
    result.strangle = computeStrangle(
      ca, ta.lastPrice, cfg.window, cfg.strangleHorizonDays ?? 7, cfg.interval,
    );
  }

  return result;
}
