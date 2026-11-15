"""Секундный спред Bybit ↔ MEXC по одной-трём монетам (вкладка «Ерши»). ТОЛЬКО ЧТЕНИЕ, публичные API.

Фоновый поток раз в STEP с снимает bid/ask/last перпа на Bybit (XUSDT) и MEXC (X_USDT) по «отслеживаемым» монетам
(те, что недавно запрашивал дашборд; по умолчанию SOXL). История — в памяти сервера (~2 ч), после перезапуска пропадает.
Маршрут: /rufftick?coin=SOXL&bar=10 → бары по bar секунд:
  diff  — (mid Bybit / mid MEXC − 1)·100, как отношение BYBIT/MEXC на TradingView;
  e1    — исполнимо «купить Bybit по ask / продать MEXC по bid», %; e2 — «купить MEXC / продать Bybit», % (макс. за бар);
  fee   — тейкер обеих бирж, %, порог: эдж выше него = прибыль до проскальзывания.
"""
import json, time, threading, urllib.request
from collections import deque
import ruff_scan

STEP, KEEP, IDLE = 2.0, 3600, 1800       # шаг сэмпла, точек в истории (2 ч), сколько секунд держать монету без запросов
MX = "https://contract.mexc.com/api/v1/contract/ticker?symbol="
_H, _SEEN, _ERR = {}, {"SOXL": time.time()}, {}
_LP = {}      # coin -> {"B": {сек: (t_ms, цена последней сделки)}, "M": {...}} — «как на TradingView» (по сделкам, last)
_TID = {}     # coin -> {"B": max seq, "M": max id} — дедуп сделок
_L = threading.Lock()
_started = False

def ff(x):
    try: return float(x)
    except Exception: return 0.0

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=5))

def _one(pub, coin):
    b = pub("/v5/market/tickers", {"category": "linear", "symbol": coin + "USDT"})["result"]["list"][0]
    m = _get(MX + coin + "_USDT")["data"]
    return (time.time(), ff(b["bid1Price"]), ff(b["ask1Price"]), ff(b["lastPrice"]), ff(m.get("bid1")), ff(m.get("ask1")), ff(m.get("lastPrice")))

def _trades(pub, c):
    """Последние сделки обеих бирж → последняя цена по каждой секунде. Bybit 1000 сделок, MEXC 100 (~50 с у SOXL)."""
    lp = _LP.setdefault(c, {"B": {}, "M": {}, "VB": {}}); lp.setdefault("VB", {}); ti = _TID.setdefault(c, {"B": 0, "M": 0})
    b = pub("/v5/market/recent-trade", {"category": "linear", "symbol": c + "USDT", "limit": "1000"})["result"]["list"]
    m = _get("https://contract.mexc.com/api/v1/contract/deals/" + c + "_USDT?limit=100").get("data") or []
    for side, rows in (("B", [(int(x["time"]), int(x.get("seq") or 0), ff(x["price"]), ff(x.get("size"))) for x in b]),
                       ("M", [(int(x["t"]), int(x.get("i") or 0), ff(x["p"]), 0.0) for x in m])):
        mx = ti[side]
        for t, i, p, q in rows:
            if i <= ti[side] or p <= 0: continue
            mx = max(mx, i); sec = t // 1000; cur = lp[side].get(sec)
            if cur is None or t >= cur[0]: lp[side][sec] = (t, p)
            if side == "B": lp["VB"][sec] = lp["VB"].get(sec, 0.0) + p * q      # оборот Bybit, $
        ti[side] = mx
        if len(lp[side]) > KEEP * 2 + 600:
            cut = time.time() - KEEP * STEP
            for k in [k for k in lp[side] if k < cut]: del lp[side][k]
            for k in [k for k in lp["VB"] if k < cut]: del lp["VB"][k]

def _loop(pub):
    while True:
        t0 = time.time()
        with _L:
            coins = [c for c, t in _SEEN.items() if t0 - t < IDLE or c == "SOXL"][:4]
        for c in coins:
            try:
                p = _one(pub, c)
                if min(p[1:3]) > 0 and min(p[4:6]) > 0:
                    _H.setdefault(c, deque(maxlen=KEEP)).append(p); _ERR.pop(c, None)
                _trades(pub, c)
            except Exception as e:
                _ERR[c] = str(e)[:120]
        time.sleep(max(0.2, STEP - (time.time() - t0)))

def start(pub):
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, args=(pub,), daemon=True).start()

def _fees(sget, coin):
    try: bt = ruff_scan._bb_fees(sget).get(coin + "USDT", (0.0002, 0.00055))[1]
    except Exception: bt = 0.00055
    try: mt = ruff_scan._mx_detail().get(coin + "_USDT", {}).get("taker", 0.0004)
    except Exception: mt = 0.0004
    return bt * 100, mt * 100

def series(sget, coin="SOXL", bar=10):
    coin = (coin or "SOXL").upper().replace("USDT", "").replace("_", "").strip() or "SOXL"
    bar = max(2, min(300, int(bar or 10)))
    with _L: _SEEN[coin] = time.time()
    H = list(_H.get(coin, []))
    fb, fm = _fees(sget, coin)
    bars = {}
    for t, bb, ba, bl, mb, ma, ml in H:
        bm, mm = (bb + ba) / 2, (mb + ma) / 2
        k = int(t // bar) * bar
        d = round((bm / mm - 1) * 100, 4); e1 = (mb - ba) / bm * 100; e2 = (bb - ma) / bm * 100
        r = bars.get(k)
        if r is None: bars[k] = [k, d, d, d, d, e1, e2, bm, mm]          # ts, open, high, low, close, e1max, e2max, mid B, mid M
        else: r[2] = max(r[2], d); r[3] = min(r[3], d); r[4] = d; r[5] = max(r[5], e1); r[6] = max(r[6], e2); r[7] = bm; r[8] = mm
    out = [[b[0], b[1], b[2], b[3], b[4], round(b[5], 4), round(b[6], 4), b[7], b[8]] for b in sorted(bars.values())]
    fee = round(fb + fm, 4)
    # «как на TradingView»: отношение последних цен сделок BYBIT/MEXC на закрытии каждого бара (с переносом вперёд)
    lp = _LP.get(coin, {"B": {}, "M": {}}); tv = []
    sb, sm = sorted(lp["B"].items()), sorted(lp["M"].items())
    if sb and sm:
        t0 = max(sb[0][0], sm[0][0]) // bar * bar; t1 = int(time.time()) // bar * bar
        ib = im = 0; pb = pm = None
        for k in range(t0, t1 + 1, bar):
            while ib < len(sb) and sb[ib][0] < k + bar: pb = sb[ib][1][1]; ib += 1
            while im < len(sm) and sm[im][0] < k + bar: pm = sm[im][1][1]; im += 1
            if pb and pm: tv.append([k, pb, pm, round((pb / pm - 1) * 100, 4), round(sum(lp.get("VB", {}).get(x, 0.0) for x in range(k, k + bar)))])
    cl = [b[4] for b in out]
    st = {}
    if len(cl) >= 5:
        mu = sum(cl) / len(cl); sd = (sum((x - mu) ** 2 for x in cl) / len(cl)) ** 0.5
        win = sum(1 for b in out if max(b[5], b[6]) > fee)
        st = {"mean": round(mu, 4), "sd": round(sd, 4), "z": round((cl[-1] - mu) / sd, 2) if sd > 0 else None,
              "lo": round(min(b[3] for b in out), 4), "hi": round(max(b[2] for b in out), 4),
              "win": win, "win_pct": round(win / len(out) * 100, 1), "best": round(max(max(b[5], b[6]) for b in out), 4)}
    return {"t": time.strftime("%H:%M:%S"), "coin": coin, "bar": bar, "step": STEP, "n": len(H), "bars": out, "tv": tv,
            "fee_b": round(fb, 4), "fee_m": round(fm, 4), "fee": fee, "stats": st, "err": _ERR.get(coin),
            "tracked": sorted(_H.keys())}
