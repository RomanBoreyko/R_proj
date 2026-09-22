// ============================================================
// BYBIT v5 API — клиент маркет-данных
// Работает и в браузере (Vite), и в Node 18+ (глобальный fetch).
// Публичные эндпоинты — без ключей.
// ============================================================

export type Category = 'linear' | 'spot' | 'inverse';
export type Interval =
  | '1' | '3' | '5' | '15' | '30' | '60'
  | '120' | '240' | '360' | '720' | 'D' | 'W' | 'M';

// Баров в году для аннуализации волы по интервалу klines.
export const BARS_PER_YEAR: Record<Interval, number> = {
  '1': 525600, '3': 175200, '5': 105120, '15': 35040, '30': 17520,
  '60': 8760, '120': 4380, '240': 2190, '360': 1460, '720': 730,
  D: 365, W: 52, M: 12,
};

export interface Ticker {
  symbol: string;
  lastPrice: number;
  markPrice: number;
  indexPrice: number;
  fundingRate: number;       // за период (доля, напр. 0.0001 = 0.01%)
  nextFundingTime: number;   // ms
}

export interface Candle {
  ts: number;    // ms открытия бара
  close: number;
}

const BASE_URL = 'https://api.bybit.com';

async function apiGet(path: string, params: Record<string, string>): Promise<any> {
  const url = `${BASE_URL}${path}?${new URLSearchParams(params).toString()}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Bybit HTTP ${res.status} на ${path}`);
  const json = await res.json();
  if (json.retCode !== 0) throw new Error(`Bybit retCode ${json.retCode}: ${json.retMsg}`);
  return json.result;
}

export class BybitClient {
  constructor(private category: Category = 'linear') {}

  async getTicker(symbol: string): Promise<Ticker> {
    const r = await apiGet('/v5/market/tickers', { category: this.category, symbol });
    const t = r.list?.[0];
    if (!t) throw new Error(`Нет тикера ${symbol}`);
    return {
      symbol: t.symbol,
      lastPrice: parseFloat(t.lastPrice),
      markPrice: parseFloat(t.markPrice ?? t.lastPrice),
      indexPrice: parseFloat(t.indexPrice ?? t.lastPrice),
      fundingRate: parseFloat(t.fundingRate ?? '0'),
      nextFundingTime: parseInt(t.nextFundingTime ?? '0', 10),
    };
  }

  /** Свечи по возрастанию времени (Bybit отдаёт по убыванию). */
  async getCandles(symbol: string, interval: Interval, limit = 200): Promise<Candle[]> {
    const r = await apiGet('/v5/market/kline', {
      category: this.category,
      symbol,
      interval,
      limit: String(Math.min(limit, 1000)),
    });
    const rows: string[][] = r.list ?? [];
    // [start, open, high, low, close, volume, turnover]
    return rows
      .map((x) => ({ ts: parseInt(x[0], 10), close: parseFloat(x[4]) }))
      .sort((a, b) => a.ts - b.ts);
  }
}
