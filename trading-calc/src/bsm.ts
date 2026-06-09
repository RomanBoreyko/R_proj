// Black-Scholes Model (r=0, no dividends) — перенесено из gold-volarb-calculator.html

export function ncdf(x: number): number {
  const t = 1 / (1 + 0.2316419 * Math.abs(x));
  const d = 0.3989422804 * Math.exp(-x * x / 2);
  const p = d * t * (0.31938153 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
  return x > 0 ? 1 - p : p;
}

export function npdf(x: number): number {
  return 0.3989422804014327 * Math.exp(-x * x / 2);
}

export interface BSMResult {
  price: number;
  delta: number;
  gamma: number;
  theta: number; // $ в день (r=0)
  vega: number;  // $ на 1% изменения IV
}

/** S=спот, K=страйк, T=лет до экспирации, sigma=IV (доля, напр. 0.25), type='C'|'P' */
export function bsm(S: number, K: number, T: number, sigma: number, type: 'C' | 'P'): BSMResult {
  if (T <= 0 || sigma <= 0) {
    const intr = type === 'C' ? Math.max(S - K, 0) : Math.max(K - S, 0);
    const delta = type === 'C' ? (S > K ? 1 : 0) : (S < K ? -1 : 0);
    return { price: intr, delta, gamma: 0, theta: 0, vega: 0 };
  }
  const v = sigma * Math.sqrt(T);
  const d1 = (Math.log(S / K) + 0.5 * sigma * sigma * T) / v;
  const d2 = d1 - v;
  const price = type === 'C'
    ? S * ncdf(d1) - K * ncdf(d2)
    : K * ncdf(-d2) - S * ncdf(-d1);
  const delta = type === 'C' ? ncdf(d1) : ncdf(d1) - 1;
  const gamma = npdf(d1) / (S * v);
  // theta в $ за день (r=0)
  const theta = (-S * npdf(d1) * sigma / (2 * Math.sqrt(T))) / 365;
  // vega в $ на 1% изменения IV
  const vega = S * npdf(d1) * Math.sqrt(T) / 100;
  return { price, delta, gamma, theta, vega };
}
