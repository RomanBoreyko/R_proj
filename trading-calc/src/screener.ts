// ============================================================
// BYBIT SCREENER: Calendar spreads + Options vs BSM
// ============================================================

import { BybitClient } from './bybit.js';

const client = new BybitClient();

// ─────────────────────────────────────────────────────────
// HELPERS
// ─────────────────────────────────────────────────────────

function dte(deliveryTimeMs: number): number {
  return Math.ceil((deliveryTimeMs - Date.now()) / 86_400_000);
}

/** Парсинг даты Bybit: '25JUL25' → Date */
function parseBybitDate(s: string): Date {
  // Форматы: '25JAN25', '27JUN25', '26SEP25'
  const months: Record<string, number> = {
    JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5,
    JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11,
  };
  const day   = parseInt(s.slice(0, 2), 10);
  const mon   = months[s.slice(2, 5).toUpperCase()];
  const year  = 2000 + parseInt(s.slice(5, 7), 10);
  return new Date(Date.UTC(year, mon, day, 8, 0, 0)); // Bybit экспирирует в 08:00 UTC
}

/** Парсинг символа опциона: 'XAUT-25JUL25-3400-C' */
function parseOptionSymbol(symbol: string): { expiry: Date; strike: number; type: 'C' | 'P' } | null {
  const parts = symbol.split('-');
  if (parts.length < 4) return null;
  try {
    const expiry = parseBybitDate(parts[1]);
    const strike = parseFloat(parts[2]);
    const type   = parts[3].toUpperCase() as 'C' | 'P';
    if (isNaN(strike) || (type !== 'C' && type !== 'P')) return null;
    return { expiry, strike, type };
  } catch {
    return null;
  }
}

function fmtDate(d: Date): string {
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: '2-digit', timeZone: 'UTC' });
}

// ─────────────────────────────────────────────────────────
// CALENDAR SCREENER
// ─────────────────────────────────────────────────────────

export interface CalendarLeg {
  symbol: string;
  expiry: Date;
  dte: number;
  futuresPrice: number;
  spotPrice: number;
  basis: number;
  basisPct: number;
  annBasis: number;
  isContango: boolean;
}

export async function screenCalendars(baseCoin = 'XAUT'): Promise<CalendarLeg[]> {
  const spotSymbol = `${baseCoin}USDT`;

  const [spotList, futList] = await Promise.all([
    client.getTickers('spot',   { symbol: spotSymbol }),
    client.getTickers('linear', { baseCoin }),
  ]);

  const spotRaw  = spotList[0];
  if (!spotRaw) throw new Error(`Нет спот-тикера ${spotSymbol}`);
  const spotPrice = parseFloat(spotRaw.lastPrice ?? spotRaw.indexPrice);

  const results: CalendarLeg[] = [];

  for (const t of futList) {
    const sym: string = t.symbol ?? '';
    const deliveryTime = parseInt(t.deliveryTime ?? '0', 10);

    // Пропускаем бессрочные (deliveryTime = 0 или нет дефиса в символе после baseCoin)
    if (!deliveryTime || deliveryTime <= Date.now()) continue;

    const days = dte(deliveryTime);
    if (days <= 0) continue;

    const futuresPrice = parseFloat(t.markPrice ?? t.lastPrice ?? '0');
    if (!futuresPrice) continue;

    const basis    = futuresPrice - spotPrice;
    const basisPct = (basis / spotPrice) * 100;
    const annBasis = basisPct * (365 / days);

    results.push({
      symbol: sym,
      expiry: new Date(deliveryTime),
      dte: days,
      futuresPrice,
      spotPrice,
      basis,
      basisPct,
      annBasis,
      isContango: basis > 0,
    });
  }

  // Сортировка: сначала contango (дороже спота), потом backwardation; внутри по DTE
  return results.sort((a, b) => {
    if (a.isContango !== b.isContango) return a.isContango ? -1 : 1;
    return a.dte - b.dte;
  });
}

// ─────────────────────────────────────────────────────────
// OPTIONS SCREENER
// ─────────────────────────────────────────────────────────

export interface OptionEntry {
  symbol: string;
  strike: number;
  type: 'C' | 'P';
  expiry: Date;
  dte: number;
  bid: number;
  ask: number;
  mid: number;
  markPrice: number;
  markIV: number;
  delta: number;
  theta: number;
  vega: number;
  openInterest: number;
  priceDiff: number;     // mid - markPrice ($)
  priceDiffPct: number;  // %
  isCheap: boolean;      // mid < markPrice
}

export async function screenOptions(baseCoin = 'XAUT', minDiscountPct = 1): Promise<OptionEntry[]> {
  const list = await client.getTickers('option', { baseCoin });

  const results: OptionEntry[] = [];

  for (const t of list) {
    const parsed = parseOptionSymbol(t.symbol ?? '');
    if (!parsed) continue;

    const { expiry, strike, type } = parsed;
    const days = Math.ceil((expiry.getTime() - Date.now()) / 86_400_000);
    if (days <= 0) continue;

    const bid        = parseFloat(t.bid1Price ?? '0');
    const ask        = parseFloat(t.ask1Price ?? '0');
    const markPrice  = parseFloat(t.markPrice  ?? '0');
    const markIV     = parseFloat(t.markIV     ?? '0');

    // Пропускаем опционы без котировок или нулевой mark
    if (markPrice <= 0) continue;
    if (bid <= 0 && ask <= 0) continue;

    const mid = bid > 0 && ask > 0
      ? (bid + ask) / 2
      : (bid || ask);

    const priceDiff    = mid - markPrice;
    const priceDiffPct = (priceDiff / markPrice) * 100;
    const isCheap      = priceDiffPct <= -minDiscountPct;

    if (!isCheap) continue;

    results.push({
      symbol: t.symbol,
      strike,
      type,
      expiry,
      dte: days,
      bid,
      ask,
      mid,
      markPrice,
      markIV,
      delta:         parseFloat(t.delta         ?? '0'),
      theta:         parseFloat(t.theta         ?? '0'),
      vega:          parseFloat(t.vega          ?? '0'),
      openInterest:  parseFloat(t.openInterest  ?? '0'),
      priceDiff,
      priceDiffPct,
      isCheap,
    });
  }

  // Сортировка: самые дешёвые относительно BSM сначала
  return results.sort((a, b) => a.priceDiffPct - b.priceDiffPct);
}

export { fmtDate };
