"""Лаборатория ёрша для любого перпа (вкладка «Шиткоины»). ТОЛЬКО ЧТЕНИЕ, расчёт на прошлых минутных свечах.

Схемы (чистая позиция n(p) в монетах как функция цены, коридор lo…hi, размер L монет = notional / цена старта):
  core    — лонг-ядро + шорт по шкале: n = L у нижней границы → 0 у верхней (профиль покрытого колла: рост ограничен,
            падение полное); ядро — спот (без фандинга) или перп (фандинг на всю чистую позицию).
  neutral — нейтральная сетка (как «нейтральный фьючерсный грид» Bybit): n = +L у низа → −L у верха.
  trend   — по движению (трендовый): n = −L у низа → +L у верха (покупает рост, продаёт падение — платит за пилу,
            зарабатывает на тренде).
Сетка из N уровней: при касании уровня позиция доводится до n(уровня) лимиткой по цене уровня (мейкер аккаунта).
Путь внутри минуты восстановлен по свече (o → l → h → c или o → h → l → c), исполнение на касании — оптимистично.
Фандинг: по истории ставок символа (/v5/market/funding/history) на позицию в момент списания; лонг платит +ставку.
Маршрут: /altlab?sym=INITUSDT&mode=core&core=spot&lo=&hi=&n=20&usd=100&bars=720
"""
import time, math
import ruff_scan

_C = {}

def ff(x):
    try: return float(x)
    except Exception: return 0.0

def _cached(k, ttl, fn):
    v = _C.get(k)
    if v and time.time() - v[0] < ttl: return v[1]
    r = fn(); _C[k] = (time.time(), r); return r

def _klines(pub, sym, bars):
    out, end = {}, None
    while len(out) < bars:
        p = {"category": "linear", "symbol": sym, "interval": "1", "limit": str(min(1000, bars - len(out)))}
        if end: p["end"] = str(end)
        k = pub("/v5/market/kline", p)["result"]["list"]
        if not k: break
        for x in k: out[int(x[0]) // 1000] = (ff(x[1]), ff(x[2]), ff(x[3]), ff(x[4]), ff(x[6]))
        end = int(k[-1][0]) - 1
        if len(k) < 2: break
    ts = sorted(out)[-bars:]
    return [(t,) + out[t] for t in ts]

def _funding(pub, sym, since):
    out, end = [], None
    for _ in range(5):
        p = {"category": "linear", "symbol": sym, "limit": "200"}
        if end: p["endTime"] = str(end)
        h = pub("/v5/market/funding/history", p)["result"]["list"]
        if not h: break
        out += [(int(x["fundingRateTimestamp"]) // 1000, ff(x["fundingRate"])) for x in h]
        last = int(h[-1]["fundingRateTimestamp"])
        if last // 1000 < since or len(h) < 200: break
        end = last - 1
    return sorted(x for x in out if x[0] >= since)

def _target(mode, p, lo, hi, L):
    x = min(1.0, max(0.0, (p - lo) / (hi - lo))) if hi > lo else 0.5
    if mode == "neutral": return L * (1 - 2 * x)
    if mode == "trend": return L * (2 * x - 1)
    return L * (1 - x)                                   # core: чистая позиция = ядро − шорт

def _chop(c):
    r = [math.log(c[i] / c[i-1]) for i in range(1, len(c)) if c[i-1] > 0 and c[i] > 0]
    net = abs(sum(r)); path = sum(abs(x) for x in r)
    return round(min(path / net, 99.0), 1) if net > 1e-9 else 99.0, round(path * 100, 1)

def lab(sget, pub, sym="INITUSDT", mode="core", core="spot", lo=None, hi=None, n=20, usd=100.0, bars=720):
    sym = (sym or "INITUSDT").upper().strip()
    if not sym.endswith("USDT"): sym += "USDT"
    mode = mode if mode in ("core", "neutral", "trend") else "core"; core = core if core in ("spot", "perp") else "spot"
    n = max(2, min(200, int(ff(n) or 20))); usd = max(5.0, min(100000.0, ff(usd) or 100.0)); bars = max(60, min(2880, int(ff(bars) or 720)))
    K = _cached("k:" + sym + ":" + str(bars), 30, lambda: _klines(pub, sym, bars))
    if len(K) < 30: return {"err": "мало свечей по " + sym}
    t0 = K[0][0]
    F = _cached("f:" + sym, 120, lambda: _funding(pub, sym, t0 - 3600))
    F = [x for x in F if x[0] >= t0]
    tk = pub("/v5/market/tickers", {"category": "linear", "symbol": sym})["result"]["list"][0]
    try: inst = pub("/v5/market/instruments-info", {"category": "linear", "symbol": sym})["result"]["list"][0]
    except Exception: inst = {}
    try: fees = ruff_scan._bb_fees(sget).get(sym)
    except Exception: fees = None
    mk, tkf = (fees or (0.0002, 0.00055))
    closes = [k[4] for k in K]; plo, phi = min(k[3] for k in K), max(k[2] for k in K)
    lo = ff(lo) or plo; hi = ff(hi) or phi
    if hi <= lo: hi = lo * 1.05
    p0 = K[0][1]; L = usd / p0; step = (hi - lo) / n
    lev = lambda p: int(math.floor((min(max(p, lo), hi) - lo) / step + 1e-9))
    # старт: позиция n(p0) покупается/продаётся по p0 (мейкер)
    pos = _target(mode, lo + lev(p0) * step, lo, hi, L); cash = -pos * p0; fee = abs(pos) * p0 * mk; trades = 1 if pos else 0; turn = abs(pos) * p0
    core_q = L if mode == "core" else 0.0               # ядро в монетах (для схемы core)
    fund_short = fund_core = fund_net = 0.0; fi = 0
    li = lev(p0); eq_curve = []; maxabs = abs(pos) * p0; peak = 0.0; mdd = 0.0
    for t, o, h, l, c, vol in K:
        while fi < len(F) and F[fi][0] <= t:
            rate = F[fi][1]; px = o
            fund_net += -pos * px * rate                                   # если вся позиция — перп
            if mode == "core":
                short_q = core_q - pos                                     # шорт-нога (≥0)
                fund_short += short_q * px * rate                          # шорт получает +ставку
                fund_core += -core_q * px * rate
            fi += 1
        for p in ((o, l, h, c) if c >= o else (o, h, l, c)):
            nl = lev(p)
            while nl != li:
                li += 1 if nl > li else -1
                lp = lo + li * step; tgt = _target(mode, lp, lo, hi, L); dq = tgt - pos
                if abs(dq) > 1e-12:
                    cash -= dq * lp; fee += abs(dq) * lp * mk; turn += abs(dq) * lp; trades += 1; pos = tgt
        val = cash + pos * c - fee
        maxabs = max(maxabs, abs(pos) * c); peak = max(peak, val); mdd = min(mdd, val - peak)
        eq_curve.append([t, round(val, 4), round(pos * c, 2)])
    pend = closes[-1]; hours = len(K) / 60
    if mode == "core": fund = fund_short + (fund_core if core == "perp" else 0.0)
    else: fund = fund_net
    grid_pnl = cash + pos * pend - fee                                     # без фандинга
    hold0 = _target(mode, lo + lev(p0) * step, lo, hi, L) * (pend - p0)    # если бы стартовую позицию просто держали
    total = grid_pnl + fund
    fr_now = ff(tk.get("fundingRate")); fih = ff(tk.get("fundingIntervalHour")) or (ff(inst.get("fundingInterval")) / 60 or 8)
    short_now = (core_q - pos) if mode == "core" else max(0.0, -pos)
    ch, path = _chop(closes)
    step_pct = step / ((lo + hi) / 2) * 100
    return {"t": time.strftime("%H:%M:%S"), "sym": sym, "mode": mode, "core": core, "bars": len(K), "hours": round(hours, 1),
            "lo": lo, "hi": hi, "n": n, "usd": usd, "L": round(L, 4), "step": round(step, 8), "step_pct": round(step_pct, 3),
            "fee_rt_pct": round(2 * mk * 100, 4), "maker": mk, "taker": tkf,
            "px": {"start": p0, "end": pend, "lo": plo, "hi": phi, "chg": round((pend / p0 - 1) * 100, 2), "rng": round((phi - plo) / plo * 100, 1)},
            "chop": ch, "path": path,
            "res": {"total": round(total, 2), "grid": round(grid_pnl, 2), "hold": round(hold0, 2), "edge": round(grid_pnl - hold0, 2),
                    "fund": round(fund, 2), "fund_short": round(fund_short, 2), "fund_core": round(fund_core, 2), "fee": round(fee, 2),
                    "trades": trades, "turn": round(turn, 2), "per_h": round(total / hours, 3) if hours else None,
                    "pos_end": round(pos, 4), "pos_end_usd": round(pos * pend, 2), "max_usd": round(maxabs, 2), "mdd": round(mdd, 2),
                    "pct": round(total / usd * 100, 2)},
            "fund_now": {"rate": round(fr_now * 100, 4), "every_h": fih, "next": int(ff(tk.get("nextFundingTime")) / 1000),
                         "day_pct": round(fr_now * 100 * 24 / fih, 3),
                         "short_day_usd": round(short_now * pend * fr_now * 24 / fih, 2),
                         "sum": round(sum(x[1] for x in F) * 100, 3), "n": len(F),
                         "sum24": round(sum(x[1] for x in F if x[0] >= K[-1][0] - 86400) * 100, 3)},
            "curve": eq_curve[::max(1, len(eq_curve) // 400)]}
