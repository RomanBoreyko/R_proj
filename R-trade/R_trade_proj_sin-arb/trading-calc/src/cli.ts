// ============================================================
// CLI-раннер калькулятора синт-спреда (живые данные Bybit).
// Запуск:  npm run calc        (через vite-node)
// Конфиг по умолчанию ниже; переопределяется флагами:
//   npm run calc -- --legA XAUTUSDT --legB XAUUSDT --interval 60 --notional 1000
// ============================================================

import { analyzeSpread, type SpreadConfig } from './spread';
import type { Interval } from './bybit';

const DEFAULTS: SpreadConfig = {
  category: 'linear',
  legA: 'XAUTUSDT',
  legB: 'XAUUSDT',
  interval: '60',
  window: 200,
  kInner: 1.0,
  kOuter: 2.0,
  spreadPct: false,
  targetNotionalUsd: 1000,
  strangleHorizonDays: 7,
};

function parseArgs(argv: string[]): SpreadConfig {
  const cfg: SpreadConfig = { ...DEFAULTS };
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i]?.replace(/^--/, '');
    const val = argv[i + 1];
    if (!key || val === undefined) continue;
    switch (key) {
      case 'legA': cfg.legA = val; break;
      case 'legB': cfg.legB = val; break;
      case 'interval': cfg.interval = val as Interval; break;
      case 'window': cfg.window = parseInt(val, 10); break;
      case 'kInner': cfg.kInner = parseFloat(val); break;
      case 'kOuter': cfg.kOuter = parseFloat(val); break;
      case 'notional': cfg.targetNotionalUsd = parseFloat(val); break;
      case 'horizon': cfg.strangleHorizonDays = parseFloat(val); break;
      case 'pct': cfg.spreadPct = val === 'true'; break;
    }
  }
  return cfg;
}

const n = (x: number, p = 4) =>
  Number.isFinite(x) ? x.toLocaleString('en-US', { minimumFractionDigits: p, maximumFractionDigits: p }) : '—';
const line = (t = '') => console.log(t ? `──── ${t} `.padEnd(64, '─') : '─'.repeat(64));

async function main() {
  const cfg = parseArgs(process.argv.slice(2));
  const a = await analyzeSpread(cfg);
  const u = a.unit;

  line('ЦЕНЫ');
  console.log(`  ${a.legA.padEnd(16)} (нога A, лонг) : ${n(a.priceA, 2)}`);
  console.log(`  ${a.legB.padEnd(16)} (нога B, шорт) : ${n(a.priceB, 2)}`);
  console.log(`  Спред A−B (сейчас)        : ${n(a.stats.current)} ${u}`);

  line('СТАТИСТИКА СПРЕДА');
  console.log(`  Окно                      : ${cfg.window} баров (${cfg.interval})`);
  console.log(`  Среднее / Sigma           : ${n(a.stats.mean)} / ${n(a.stats.sigma)} ${u}`);
  console.log(`  Z-score                   : ${n(a.stats.z, 2)}`);
  console.log(`  Проданный центр (±${cfg.kInner}σ)   : ${n(a.stats.innerDn)}  …  ${n(a.stats.innerUp)} ${u}`);
  console.log(`  Крылья (±${cfg.kOuter}σ)           : ${n(a.stats.outerDn)}  …  ${n(a.stats.outerUp)} ${u}`);

  line('ВОЛА СПРЕДА');
  console.log(`  Сигма ретёрнов (окно)     : ${n(a.vol.sigmaReturns)} ${u}`);
  console.log(`  Аннуализированная         : ${n(a.vol.annualized)} ${u}`);
  console.log(`  Перцентиль волы           : ${n(a.vol.percentile * 100, 1)}%  (${a.vol.verdict})`);

  line('ФАНДИНГ / КЭРРИ (перп-нога B шорт)');
  console.log(`  Фандинг за период         : ${n(a.carry.perPeriodPct, 4)}%  (${a.carry.favorable ? 'в нашу пользу' : 'против/0'})`);
  console.log(`  ≈ годовых (×3×365)        : ${n(a.carry.annualizedPct, 2)}%`);

  if (a.sizing) {
    line('САЙЗИНГ (дельта-нейтрально)');
    console.log(`  Ноционал на ногу          : ${n(a.sizing.notionalPerLeg, 2)} USD`);
    console.log(`  Лонг ${a.legA.padEnd(14)}     : ${n(a.sizing.qtyLong)}`);
    console.log(`  Шорт ${a.legB.padEnd(14)}     : ${n(a.sizing.qtyShort)}`);
  }
  if (a.strangle) {
    line(`СТРЕНГЛ (${a.legA}, горизонт ${cfg.strangleHorizonDays}д)`);
    console.log(`  RV годовая ноги A         : ${n(a.strangle.rvAnnual * 100, 1)}%`);
    console.log(`  Ожид. ход (±1σ)           : ±${n(a.strangle.expectedMove, 2)} USD`);
    console.log(`  Проданные страйки (±1σ)   : ${n(a.strangle.soldDn, 2)}  …  ${n(a.strangle.soldUp, 2)}`);
    console.log(`  Крылья (±2σ)              : ${n(a.strangle.wingDn, 2)}  …  ${n(a.strangle.wingUp, 2)}`);
  }
  line();
  console.log('Готово. Уровни — ориентиры, не приказ к исполнению.');
}

main().catch((e) => { console.error('ОШИБКА:', e.message); process.exit(1); });
