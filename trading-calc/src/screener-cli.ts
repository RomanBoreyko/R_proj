// ============================================================
// screener-cli.ts — запуск: npm run screener
// ============================================================

import { screenCalendars, screenOptions, fmtDate } from './screener.js';

// ─── CLI args ──────────────────────────────────────────────
const args = process.argv.slice(2);
function getArg(name: string, def: string): string {
  const i = args.indexOf(`--${name}`);
  return i !== -1 && args[i + 1] ? args[i + 1] : def;
}
const baseCoin      = getArg('baseCoin',    'XAUT');
const minDiscount   = parseFloat(getArg('minDiscount', '1'));

// ─── Форматирование ────────────────────────────────────────
function pad(s: string | number, n: number, right = false): string {
  const str = String(s);
  return right ? str.padStart(n) : str.padEnd(n);
}
function sign(n: number, decimals = 2): string {
  return (n >= 0 ? '+' : '') + n.toFixed(decimals);
}
function fmtPrice(n: number): string {
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// ─── CALENDARS ─────────────────────────────────────────────
async function printCalendars(): Promise<void> {
  console.log(`\nЗагружаю календарные фьючерсы ${baseCoin}...`);
  const legs = await screenCalendars(baseCoin);

  const title = ` CALENDARS: ${baseCoin}USDT `;
  const line  = '═'.repeat(84);
  console.log('\n' + line);
  console.log(title.padStart(Math.floor((84 + title.length) / 2), '═').padEnd(84, '═'));
  console.log(line);

  if (legs.length === 0) {
    console.log('  Датированных фьючерсов не найдено.\n');
    return;
  }

  const hdr = [
    pad('Symbol', 24),
    pad('Expiry', 12),
    pad('DTE', 5, true),
    pad('Futures', 10, true),
    pad('Spot', 10, true),
    pad('Basis$', 10, true),
    pad('Basis%', 8, true),
    pad('Annual%', 9, true),
    '  Status',
  ].join('  ');
  console.log(hdr);
  console.log('─'.repeat(84));

  for (const leg of legs) {
    const status = leg.isContango ? '[CONTANGO]' : '[BACKWARDATION]';
    const row = [
      pad(leg.symbol, 24),
      pad(fmtDate(leg.expiry), 12),
      pad(leg.dte, 5, true),
      pad(fmtPrice(leg.futuresPrice), 10, true),
      pad(fmtPrice(leg.spotPrice), 10, true),
      pad(sign(leg.basis), 10, true),
      pad(sign(leg.basisPct) + '%', 8, true),
      pad(sign(leg.annBasis) + '%', 9, true),
      '  ' + status,
    ].join('  ');
    console.log(row);
  }
  console.log('═'.repeat(84) + '\n');
}

// ─── OPTIONS ───────────────────────────────────────────────
async function printOptions(): Promise<void> {
  console.log(`Загружаю опционы ${baseCoin} (discount ≥ ${minDiscount}%)...`);
  const opts = await screenOptions(baseCoin, minDiscount);

  const title = ` OPTIONS дешевле BSM (${baseCoin}, скидка ≥ ${minDiscount}%) `;
  const line  = '═'.repeat(92);
  console.log('\n' + line);
  console.log(title.padStart(Math.floor((92 + title.length) / 2), '═').padEnd(92, '═'));
  console.log(line);

  if (opts.length === 0) {
    console.log(`  Нет опционов с bid < markPrice на ${minDiscount}% и более.\n`);
    return;
  }

  const hdr = [
    pad('Symbol', 28),
    pad('Strike', 8, true),
    pad('Type', 5),
    pad('DTE', 4, true),
    pad('Bid', 8, true),
    pad('Ask', 8, true),
    pad('Mark(BSM)', 10, true),
    pad('IV%', 7, true),
    pad('Diff%', 7, true),
    pad('Delta', 7, true),
    pad('OI', 8, true),
  ].join('  ');
  console.log(hdr);
  console.log('─'.repeat(92));

  for (const o of opts) {
    const row = [
      pad(o.symbol, 28),
      pad(fmtPrice(o.strike), 8, true),
      pad(o.type, 5),
      pad(o.dte, 4, true),
      pad(fmtPrice(o.bid), 8, true),
      pad(fmtPrice(o.ask), 8, true),
      pad(fmtPrice(o.markPrice), 10, true),
      pad(o.markIV > 0 ? o.markIV.toFixed(1) + '%' : '—', 7, true),
      pad(sign(o.priceDiffPct, 1) + '%', 7, true),
      pad(o.delta !== 0 ? o.delta.toFixed(3) : '—', 7, true),
      pad(o.openInterest > 0 ? o.openInterest.toFixed(0) : '—', 8, true),
    ].join('  ');
    console.log(row);
  }
  console.log('═'.repeat(92) + '\n');
}

// ─── MAIN ──────────────────────────────────────────────────
(async () => {
  try {
    await printCalendars();
    await printOptions();
  } catch (e) {
    console.error('Ошибка:', e instanceof Error ? e.message : e);
    process.exit(1);
  }
})();
