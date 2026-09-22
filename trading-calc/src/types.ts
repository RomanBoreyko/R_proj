export type Asset = 'XAUT' | 'ETH' | 'MNT' | 'XRP' | 'SOL';
export type Strategy = 'grid' | 'basis' | 'options' | 'ivarb' | 'calendar' | 'portfolio';
export type GridType = 'arithmetic' | 'geometric';
export type Timeframe = '1H' | '1D' | '1W';

export interface AssetConfig {
  price: number;
  atr1H: number;
  atr1D: number;
  atr1W: number;
  leverage: number;
  label: string;
  unit: string;
}

export const ASSETS: Record<Asset, AssetConfig> = {
  XAUT: { price: 3300, atr1H: 25,  atr1D: 90,  atr1W: 280, leverage: 1.0, label: 'XAUTUSDT', unit: 'XAU' },
  ETH:  { price: 2450, atr1H: 55,  atr1D: 160, atr1W: 520, leverage: 1.5, label: 'ETHUSDT',  unit: 'ETH' },
  MNT:  { price: 0.68, atr1H: 0.012, atr1D: 0.035, atr1W: 0.12, leverage: 1.5, label: 'MNTUSDT', unit: 'MNT' },
  XRP:  { price: 2.85, atr1H: 0.06, atr1D: 0.18, atr1W: 0.55, leverage: 1.5, label: 'XRPUSDT', unit: 'XRP' },
  SOL:  { price: 155,  atr1H: 3.2,  atr1D: 9.5, atr1W: 32,  leverage: 1.5, label: 'SOLUSDT',  unit: 'SOL' },
};

export const FEES = {
  BYBIT_TAKER: 0.00055,
  BYBIT_MAKER: 0.00002,
  DERIBIT_TAKER: 0.0003,
  COINBASE_TAKER: 0.006,
} as const;
