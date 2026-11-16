"""Параметры зеркальной пары (вкладка «Ерши»): SOXL / SOXS — 3× бык и 3× медведь на один индекс (полупроводники SOX).
ТОЛЬКО ЧТЕНИЕ.

Пара НЕ сходится по цене: ноги ходят в противоположные стороны, отношение SOXL/SOXS ≈ ход индекса × 6.
Лонг обеих (или шорт обеих) в равных долларах ≈ нейтраль по индексу; лонг одной + шорт другой = двойная ставка.
Что считается:
  рынок   — цены, 24ч, фандинг, оборот, комиссии аккаунта; бета и корреляция SOXS к SOXL по часовым свечам (~8 дн.);
            «лонг обеих поровну» за 24ч / 7д / 30д = (доходность SOXL + доходность SOXS) / 2 — что даёт держание пары;
  позиция — ноги, $, средняя, нереализованный P&L; захеджированная часть = min($SOXL, $SOXS) при одинаковом знаке;
            чистая ставка в «долларах SOXL» = $SOXL − $SOXS (лонг SOXS = минус), в долларах индекса = × LEV;
            P&L на ±1% индекса; сколько докупить/продать до нейтрали; сценарии индекса −5…+5%;
  заявки  — крайние случаи: исполнились все покупки SOXL + продажи SOXS (макс. ставка на рост) и наоборот;
  итог    — реализовано за сегодня (grid_stats) + нереализовано.
Маршрут: /pair → pair_snapshot(sget, pub, open_orders). Кэш 8 с, свечи 10 мин.
"""
import time, math
import grid_stats, ruff_scan

A, B, LEV, INDEX = "SOXL", "SOXS", 3, "SOX (полупроводники)"
_C = {}

def ff(x):
    try: return float(x)
    except Exception: return 0.0

def _cached(k, ttl, fn):
    v = _C.get(k)
    if v and time.time() - v[0] < ttl: return v[1]
    r = fn(); _C[k] = (time.time(), r); return r

def _kl(pub, sym, interval, limit):
    k = pub("/v5/market/kline", {"category": "linear", "symbol": sym, "interval": interval, "limit": str(limit)})["result"]["list"]
    return [(int(x[0]), ff(x[4])) for x in reversed(k)]

def _hist(pub):
    ha, hb = dict(_kl(pub, A + "USDT", "60", 200)), dict(_kl(pub, B + "USDT", "60", 200))
    ts = sorted(set(ha) & set(hb)); ca = [ha[t] for t in ts]; cb = [hb[t] for t in ts]
    ra = [ca[i] / ca[i-1] - 1 for i in range(1, len(ts))]; rb = [cb[i] / cb[i-1] - 1 for i in range(1, len(ts))]
    out = {"n_h": len(ra)}
    if len(ra) > 10:
        ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
        va = sum((x - ma) ** 2 for x in ra); vb = sum((x - mb) ** 2 for x in rb); cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
        out["beta"] = round(cov / va, 3) if va > 0 else None
        out["corr"] = round(cov / math.sqrt(va * vb), 3) if va > 0 and vb > 0 else None
        out["vol_h"] = round(math.sqrt(va / len(ra)) * 100, 2)                 # ст. отклонение часовой доходности SOXL, %
    both = lambda n: round(((ca[-1] / ca[-1-n] - 1) + (cb[-1] / cb[-1-n] - 1)) / 2 * 100, 2) if len(ca) > n else None
    one = lambda c, n: round((c[-1] / c[-1-n] - 1) * 100, 2) if len(c) > n else None
    out["w24"] = {"a": one(ca, 24), "b": one(cb, 24), "both": both(24)}
    out["w7"] = {"a": one(ca, 168), "b": one(cb, 168), "both": both(168)}
    da, db = dict(_kl(pub, A + "USDT", "D", 31)), dict(_kl(pub, B + "USDT", "D", 31))
    td = sorted(set(da) & set(db))
    if len(td) > 1:
        a0, a1, b0, b1 = da[td[0]], da[td[-1]], db[td[0]], db[td[-1]]
        out["w30"] = {"a": round((a1 / a0 - 1) * 100, 1), "b": round((b1 / b0 - 1) * 100, 1),
                      "both": round(((a1 / a0 - 1) + (b1 / b0 - 1)) / 2 * 100, 1), "days": len(td) - 1}
    return out

def _variant(a, b, k):
    """Ставка пары, если 1% хода SOXL соответствует k% хода SOXS в обратную сторону (k=1 — доллар к доллару, k=|бета| — по истории)."""
    net = a["usd"] - k * b["usd"]
    hedged = min(abs(a["usd"]), k * abs(b["usd"])) if a["usd"] * b["usd"] > 0 else 0.0
    net_up = a["after_buy_usd"] - k * b["after_sell_usd"]; net_dn = a["after_sell_usd"] - k * b["after_buy_usd"]
    return {"k": round(k, 3), "net": round(net, 2), "net_idx": round(net * LEV, 2), "per1": round(net * LEV / 100, 2), "hedged": round(hedged, 2),
            "fix_a": round(-net / a["px"], 2) if a["px"] else None, "fix_b": round(net / (k * b["px"]), 2) if b["px"] and k else None,
            "scen": [{"mv": m, "a": round(a["usd"] * LEV * m / 100, 2), "b": round(-k * b["usd"] * LEV * m / 100, 2),
                      "pnl": round(net * LEV * m / 100, 2)} for m in (-5, -3, -2, -1, 1, 2, 3, 5)],
            "per1_up": round(net_up * LEV / 100, 2), "per1_dn": round(net_dn * LEV / 100, 2)}

def _build(sget, pub, open_orders):
    sa, sb = A + "USDT", B + "USDT"
    tk = {x["symbol"]: x for x in pub("/v5/market/tickers", {"category": "linear"})["result"]["list"] if x["symbol"] in (sa, sb)}
    pos = {p["symbol"]: p for p in sget("/v5/position/list", {"category": "linear", "settleCoin": "USDT", "limit": "200"})["result"]["list"]
           if p["symbol"] in (sa, sb) and ff(p["size"]) > 0}
    try: oo = [o for o in open_orders(sget) if o["sym"] in (sa, sb)]
    except Exception: oo = []
    try: fees = ruff_scan._bb_fees(sget)
    except Exception: fees = {}
    try: gr = {r["sym"]: r for r in grid_stats.grid_snapshot(sget, open_orders, "today")["rows"]}
    except Exception: gr = {}
    hist = _cached("hist", 600, lambda: _hist(pub))
    legs = {}
    for s in (sa, sb):
        t = tk.get(s, {}); p = pos.get(s); px = ff(t.get("markPrice")) or ff(t.get("lastPrice"))
        size = ff(p["size"]) * (1 if p["side"] == "Buy" else -1) if p else 0.0
        o = [x for x in oo if x["sym"] == s]; bq = sum(x["qty"] for x in o if x["side"] == "Buy"); sq = sum(x["qty"] for x in o if x["side"] == "Sell")
        g = gr.get(s, {}); fr = fees.get(s, (None, None))
        legs[s] = {"sym": s, "px": px, "bid": ff(t.get("bid1Price")), "ask": ff(t.get("ask1Price")), "chg24": round(ff(t.get("price24hPcnt")) * 100, 2),
                   "fund": round(ff(t.get("fundingRate")) * 100, 4), "turn": round(ff(t.get("turnover24h"))),
                   "maker": fr[0], "taker": fr[1],
                   "size": size, "usd": round(size * px, 2), "avg": ff(p["avgPrice"]) if p else None,
                   "upl": round(ff(p.get("unrealisedPnl")), 2) if p else 0.0, "real": g.get("real", 0.0), "n_ex": g.get("n_ex", 0),
                   "o_buy_q": round(bq, 2), "o_sell_q": round(sq, 2), "o_buy_usd": round(bq * px, 2), "o_sell_usd": round(sq * px, 2),
                   "after_buy_usd": round((size + bq) * px, 2), "after_sell_usd": round((size - sq) * px, 2)}
    a, b = legs[sa], legs[sb]
    net = a["usd"] - b["usd"]                                   # ставка в «долларах SOXL»: лонг SOXS = минус
    hedged = min(abs(a["usd"]), abs(b["usd"])) if a["usd"] * b["usd"] > 0 else 0.0
    per1 = net * LEV / 100                                      # P&L на +1% индекса
    # до нейтрали: изменить одну из ног
    fix_a = -net / a["px"] if a["px"] else None                 # купить(+)/продать(−) SOXL
    fix_b = net / b["px"] if b["px"] else None                  # купить(+)/продать(−) SOXS
    scen = [{"mv": m, "a": round(a["usd"] * LEV * m / 100, 2), "b": round(-b["usd"] * LEV * m / 100, 2),
             "pnl": round(net * LEV * m / 100, 2)} for m in (-5, -3, -2, -1, 1, 2, 3, 5)]
    net_up = a["after_buy_usd"] - b["after_sell_usd"]           # все покупки SOXL + все продажи SOXS → макс. ставка на рост индекса
    net_dn = a["after_sell_usd"] - b["after_buy_usd"]           # все продажи SOXL + все покупки SOXS → макс. ставка на падение
    wb = sget("/v5/account/wallet-balance", {"accountType": "UNIFIED"})["result"]["list"][0]
    eq, mm, im = ff(wb["totalEquity"]), ff(wb["totalMaintenanceMargin"]), ff(wb["totalInitialMargin"])
    kb = abs(hist.get("beta") or 1.0)

    # Спот и опционы SOXL / SOXS / SOXM
    soxl_spot = next((ff(c["walletBalance"]) for c in wb.get("coin", []) if c["coin"] == "SOXL"), 0.0)
    soxs_spot = next((ff(c["walletBalance"]) for c in wb.get("coin", []) if c["coin"] == "SOXS"), 0.0)
    soxm_spot = next((ff(c["walletBalance"]) for c in wb.get("coin", []) if c["coin"] == "SOXM"), 0.0)

    try:
        opts_raw = [o for o in sget("/v5/position/list", {"category": "option", "baseCoin": "SOXL", "limit": "200"})["result"]["list"] if ff(o.get("size")) > 0]
    except Exception: opts_raw = []
    opt_tks = _cached("opt_tk_soxl", 10, lambda: {t["symbol"]: t for t in pub("/v5/market/tickers", {"category": "option", "baseCoin": "SOXL"})["result"]["list"]})

    puts = []
    calls = []
    for o in opts_raw:
        sym = o["symbol"]
        pr = sym.split("-")
        if len(pr) < 4: continue
        K = ff(pr[2]); cp = pr[3]; exp = pr[1]
        sz = ff(o["size"]); sd = 1 if o["side"] == "Buy" else -1
        t_o = opt_tks.get(sym, {})
        mp = ff(o.get("markPrice")) or ff(t_o.get("markPrice"))
        d = ff(o.get("delta")) or ff(t_o.get("delta"))
        g = ff(o.get("gamma")) or ff(t_o.get("gamma"))
        th = ff(o.get("theta")) or ff(t_o.get("theta"))
        v = ff(o.get("vega")) or ff(t_o.get("vega"))
        iv = ff(t_o.get("markIv"))
        item = {
            "sym": sym.replace("-USDT", ""), "exp": exp, "K": K, "cp": cp, "side": o["side"],
            "sz": sz, "sd": sd, "entry": ff(o.get("avgPrice")), "mark": mp, "usd": round(sz * mp, 2),
            "d": round(d, 4), "dusd": round(d * a["px"], 2), "g": round(g, 5), "th": round(th, 3), "v": round(v, 3),
            "iv": round(iv * 100, 1) if iv < 5 else round(iv, 1), "upl": round(ff(o.get("unrealisedPnl")), 2)
        }
        if cp == "P": puts.append(item)
        else: calls.append(item)

    # Тета компонентов $/день
    th_soxl_perp = round(-a["usd"] * (a["fund"] / 100.0) * 3, 2)
    th_soxs_perp = round(-b["usd"] * (b["fund"] / 100.0) * 3, 2)
    th_puts = round(sum(p["th"] for p in puts), 2)
    th_calls = round(sum(c["th"] for c in calls), 2)
    th_tot = round(th_puts + th_calls + th_soxl_perp + th_soxs_perp, 2)

    # Дельта с учетом спота и опционов
    soxl_opt_d = sum(o["d"] for o in puts + calls)
    soxl_true_d = round(a["size"] + soxl_opt_d + soxl_spot, 4)
    soxl_true_dusd = round(soxl_true_d * a["px"], 2)
    soxs_true_d = round(b["size"] + soxs_spot, 4)
    soxs_true_dusd = round(soxs_true_d * b["px"], 2)

    # Чистая ставка с учетом всех инструментов
    net_all_dusd = round(soxl_true_dusd - soxs_true_dusd, 2)
    per1_all = round(net_all_dusd * LEV / 100.0, 2)

    # Конструкция 1: SOXL spot + SOXL put long + SOXL perp + SOXS perp
    c1_puts_q = sum(p["sz"] for p in puts)
    c1_puts_d = sum(p["d"] for p in puts)
    c1_puts_usd = sum(p["usd"] for p in puts)
    c1_puts_th = sum(p["th"] for p in puts)
    c1_puts_g = sum(p["g"] for p in puts)
    c1_puts_v = sum(p["v"] for p in puts)
    c1_net_dusd = round((soxl_spot * a["px"]) + (c1_puts_d * a["px"]) + a["usd"] - b["usd"], 2)
    c1_per1 = round(c1_net_dusd * LEV / 100.0, 2)
    c1_theta = round(c1_puts_th + th_soxl_perp + th_soxs_perp, 2)

    c1_scen = []
    for m in (-10, -5, -3, -2, -1, 0, 1, 2, 3, 5, 10):
        ds = a["px"] * (m * LEV / 100.0)
        p_pnl = sum((p["d"] * ds + 0.5 * p["g"] * (ds ** 2)) for p in puts)
        sp_pnl = soxl_spot * ds
        perp_l_pnl = a["size"] * ds
        perp_s_pnl = b["size"] * (b["px"] * (-m * LEV / 100.0))
        tot_pnl = round(sp_pnl + p_pnl + perp_l_pnl + perp_s_pnl, 2)
        c1_scen.append({"mv": m, "pnl": tot_pnl, "puts": round(p_pnl, 2), "perp_l": round(perp_l_pnl, 2), "perp_s": round(perp_s_pnl, 2)})

    # Конструкция 2: SOXL call long против SOXS perp
    c2_calls_q = sum(c["sz"] for c in calls)
    c2_calls_d = sum(c["d"] for c in calls)
    c2_calls_usd = sum(c["usd"] for c in calls)
    c2_calls_th = sum(c["th"] for c in calls)
    c2_calls_g = sum(c["g"] for c in calls)
    c2_calls_v = sum(c["v"] for c in calls)
    c2_net_dusd = round((c2_calls_d * a["px"]) - b["usd"], 2)
    c2_per1 = round(c2_net_dusd * LEV / 100.0, 2)
    c2_theta = round(c2_calls_th + th_soxs_perp, 2)

    c2_scen = []
    for m in (-10, -5, -3, -2, -1, 0, 1, 2, 3, 5, 10):
        ds = a["px"] * (m * LEV / 100.0)
        c_pnl = sum((c["d"] * ds + 0.5 * c["g"] * (ds ** 2)) for c in calls)
        c_pnl = max(-c2_calls_usd, c_pnl)
        perp_s_pnl = b["size"] * (b["px"] * (-m * LEV / 100.0))
        tot_pnl = round(c_pnl + perp_s_pnl, 2)
        c2_scen.append({"mv": m, "pnl": tot_pnl, "calls": round(c_pnl, 2), "perp_s": round(perp_s_pnl, 2)})

    return {"t": time.strftime("%H:%M:%S"), "a": a, "b": b, "lev": LEV, "index": INDEX,
            "var": {"eq": _variant(a, b, 1.0), "beta": _variant(a, b, kb)},
            "ratio": round(a["px"] / b["px"], 4) if b["px"] else None, "hist": hist,
            "gross": round(abs(a["usd"]) + abs(b["usd"]), 2), "hedged": round(hedged, 2), "net": round(net, 2),
            "net_idx": round(net * LEV, 2), "per1": round(per1, 2),
            "fix_a": round(fix_a, 2) if fix_a is not None else None, "fix_b": round(fix_b, 2) if fix_b is not None else None,
            "scen": scen, "net_up": round(net_up, 2), "net_dn": round(net_dn, 2),
            "per1_up": round(net_up * LEV / 100, 2), "per1_dn": round(net_dn * LEV / 100, 2),
            "real": round(a["real"] + b["real"], 2), "upl": round(a["upl"] + b["upl"], 2), "tot": round(a["real"] + b["real"] + a["upl"] + b["upl"], 2),
            "acct": {"eq": round(eq, 2), "room": round(eq - mm, 2), "buf": round(eq / mm, 2) if mm else None, "im_pct": round(im / eq * 100, 1) if eq else None,
                     "av": round(ff(wb.get("totalAvailableBalance")), 2)},
            "spot": {"soxl": soxl_spot, "soxl_usd": round(soxl_spot * a["px"], 2),
                     "soxs": soxs_spot, "soxs_usd": round(soxs_spot * b["px"], 2),
                     "soxm": soxm_spot},
            "theta": {"tot": th_tot, "puts": th_puts, "calls": th_calls, "soxl_perp": th_soxl_perp, "soxs_perp": th_soxs_perp},
            "true_delta": {"soxl_d": soxl_true_d, "soxl_dusd": soxl_true_dusd,
                           "soxs_d": soxs_true_d, "soxs_dusd": soxs_true_dusd,
                           "net_dusd": net_all_dusd, "per1_idx": per1_all},
            "puts": puts, "calls": calls,
            "c1": {"name": "SOXL spot + put long + SOXL perp + SOXS perp",
                   "net_dusd": c1_net_dusd, "per1": c1_per1, "theta": c1_theta,
                   "puts_q": round(c1_puts_q, 2), "puts_usd": round(c1_puts_usd, 2), "puts_d": round(c1_puts_d, 4),
                   "puts_g": round(c1_puts_g, 5), "puts_th": round(c1_puts_th, 2), "puts_v": round(c1_puts_v, 2),
                   "scen": c1_scen},
            "c2": {"name": "SOXL call long против SOXS perp",
                   "net_dusd": c2_net_dusd, "per1": c2_per1, "theta": c2_theta,
                   "calls_q": round(c2_calls_q, 2), "calls_usd": round(c2_calls_usd, 2), "calls_d": round(c2_calls_d, 4),
                   "calls_g": round(c2_calls_g, 5), "calls_th": round(c2_calls_th, 2), "calls_v": round(c2_calls_v, 2),
                   "scen": c2_scen}}

def pair_snapshot(sget, pub, open_orders):
    return _cached("snap", 8, lambda: _build(sget, pub, open_orders))


# ===================== Графики пары: секундные бары из сделок Bybit + минутные/часовые свечи =====================
import threading
from collections import deque

KEEP_S = 7200                                   # секундная история, с (2 ч); копится с запуска сервера
_T = {A: {}, B: {}}                             # coin -> {сек: [t_ms последней сделки, цена, оборот $]}
_SEEN = {A: (set(), deque()), B: (set(), deque())}
_TERR, _started = {}, False

def _poll(pub, c):
    rows = pub("/v5/market/recent-trade", {"category": "linear", "symbol": c + "USDT", "limit": "1000"})["result"]["list"]
    seen, q = _SEEN[c]; d = _T[c]
    for x in rows:
        i = x["execId"]
        if i in seen: continue
        seen.add(i); q.append(i)
        if len(q) > 8000: seen.discard(q.popleft())
        t = int(x["time"]); sec = t // 1000; p = ff(x["price"]); v = p * ff(x["size"])
        if p <= 0: continue
        cur = d.get(sec)
        if cur is None: d[sec] = [t, p, v]
        else:
            cur[2] += v
            if t >= cur[0]: cur[0], cur[1] = t, p
    if len(d) > KEEP_S + 900:
        cut = time.time() - KEEP_S
        for k in [k for k in d if k < cut]: del d[k]

def _loop(pub):
    while True:
        t0 = time.time()
        for c in (A, B):
            try: _poll(pub, c); _TERR.pop(c, None)
            except Exception as e: _TERR[c] = str(e)[:120]
        time.sleep(max(0.3, 2.0 - (time.time() - t0)))

def start(pub):
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, args=(pub,), daemon=True).start()

def _sec_series(n):
    sa, sb = sorted(_T[A].items()), sorted(_T[B].items())
    if not sa or not sb: return []
    t0 = max(sa[0][0], sb[0][0]) // n * n; t1 = int(time.time()) // n * n
    ia = ib = 0; pa = pb = None; out = []
    while ia < len(sa) and sa[ia][0] < t0: pa = sa[ia][1][1]; ia += 1
    while ib < len(sb) and sb[ib][0] < t0: pb = sb[ib][1][1]; ib += 1
    for k in range(t0, t1 + 1, n):
        v = 0.0
        while ia < len(sa) and sa[ia][0] < k + n: pa = sa[ia][1][1]; v += sa[ia][1][2]; ia += 1
        while ib < len(sb) and sb[ib][0] < k + n: pb = sb[ib][1][1]; ib += 1
        if pa and pb: out.append([k, pa, pb, round(v)])
    return out

def _kl_series(pub, iv, limit):
    def g(s):
        k = pub("/v5/market/kline", {"category": "linear", "symbol": s, "interval": iv, "limit": str(limit)})["result"]["list"]
        return {int(x[0]) // 1000: (ff(x[4]), ff(x[6])) for x in k}
    ka, kb = g(A + "USDT"), g(B + "USDT")
    return [[t, ka[t][0], kb[t][0], round(ka[t][1])] for t in sorted(set(ka) & set(kb))]

BARS = {"10s": 10, "30s": 30, "1m": 60, "5m": 300, "1h": 3600}

def series(pub, bar="10s"):
    """Ряды для графика пары: [ts, цена SOXL, цена SOXS, оборот SOXL $]. 10с/30с — из сделок (копятся с запуска), 1м/5м/1ч — свечи Bybit."""
    bar = bar if bar in BARS else "10s"
    if bar.endswith("s"):
        rows = _sec_series(BARS[bar])
    else:
        iv, lim = {"1m": ("1", 1000), "5m": ("5", 720), "1h": ("60", 500)}[bar]
        rows = _cached("kl" + bar, 15, lambda: _kl_series(pub, iv, lim))
    return {"t": time.strftime("%H:%M:%S"), "bar": bar, "sec": BARS[bar], "a": A, "b": B, "rows": rows, "n": len(rows),
            "err": "; ".join(f"{k}: {v}" for k, v in _TERR.items()) or None}



# ===================== Лаборатория: волатильность пары и симулятор сетки (минутные свечи OHLC) =====================
def _ohlc(pub, sym, limit=720):
    k = pub("/v5/market/kline", {"category": "linear", "symbol": sym, "interval": "1", "limit": str(limit)})["result"]["list"]
    return {int(x[0]) // 1000: (ff(x[1]), ff(x[2]), ff(x[3]), ff(x[4])) for x in k}

def _volstats(c):
    if len(c) < 5 or min(c) <= 0: return None
    r = [math.log(c[i] / c[i-1]) for i in range(1, len(c))]
    net = abs(sum(r)); path = sum(abs(x) for x in r); m = sum(c) / len(c)
    cross = sum(1 for i in range(1, len(c)) if (c[i-1] - m) * (c[i] - m) < 0)
    return {"sd": round(math.sqrt(sum(x * x for x in r) / len(r)) * 100, 3),          # σ минутной доходности, %
            "rng": round((max(c) - min(c)) / m * 100, 2), "ret": round((c[-1] / c[0] - 1) * 100, 2),
            "path": round(path * 100, 1), "chop": round(min(path / net, 99.0), 1) if net > 1e-9 else 99.0, "cross": cross}

TAKER = 0.000275      # тейкер Bybit по SOXL/SOXS (ставка аккаунта на 11.11.2026) — для хедж-сделок в симуляции

class _Grid:
    """Симметричная сетка: уровни через step% от стартовой цены, лот lot$. Покупка при касании уровня ниже последнего
    исполненного, продажа — уровня выше. Исполнение, уменьшающее остаток, закрывает лот с прибылью lot*step."""
    def __init__(self, p0, step, lot):
        self.p0, self.ds, self.lot, self.step = p0, p0 * step / 100, lot, step
        self.f = 0; self.fills = 0; self.cycles = 0; self.maxf = 0
    def feed(self, p):
        while p <= self.p0 + (self.f - 1) * self.ds:
            if self.f > 0: self.cycles += 1
            self.f -= 1; self.fills += 1; self.maxf = max(self.maxf, abs(self.f))
        while p >= self.p0 + (self.f + 1) * self.ds:
            if self.f < 0: self.cycles += 1
            self.f += 1; self.fills += 1; self.maxf = max(self.maxf, abs(self.f))
    def inv_usd(self): return -self.f * self.lot                       # + лонг, - шорт
    def unreal(self, p):
        u = 0.0
        for j in range(1, abs(self.f) + 1):
            e = self.p0 - j * self.ds if self.f < 0 else self.p0 + j * self.ds
            if e > 0: u += self.lot * ((p / e - 1) if self.f < 0 else (1 - p / e))
        return u
    def out(self, p, hours):
        real = self.cycles * self.lot * self.step / 100; un = self.unreal(p)
        return {"fills": self.fills, "cycles": self.cycles, "real": round(real, 2), "per_h": round(real / hours, 2) if hours else None,
                "inv_lots": -self.f, "inv_usd": round(self.inv_usd(), 2), "unreal": round(un, 2), "net": round(real + un, 2),
                "max_lots": self.maxf, "max_usd": round(self.maxf * self.lot, 2)}

def lab(pub, step=0.05, lot=12.0, win=360):
    step = max(0.01, min(5.0, ff(step) or 0.05)); lot = max(1.0, min(5000.0, ff(lot) or 12.0)); win = max(30, min(720, int(ff(win) or 360)))
    oa, ob = _cached("ohlc", 20, lambda: (_ohlc(pub, A + "USDT"), _ohlc(pub, B + "USDT")))
    ts = sorted(set(oa) & set(ob))
    vol = {}
    for w in (60, 360, 720):
        t = ts[-w:]
        if len(t) < 5: continue
        ca, cb = [oa[x][3] for x in t], [ob[x][3] for x in t]
        vol[str(w)] = {"a": _volstats(ca), "b": _volstats(cb), "ratio": _volstats([x / y for x, y in zip(ca, cb)]),
                       "both": round(((ca[-1] / ca[0] - 1) + (cb[-1] / cb[0] - 1)) / 2 * 100, 2), "n": len(t)}
    c5a, c5b = [oa[x][3] for x in ts[::5]], [ob[x][3] for x in ts[::5]]          # бета на коротком горизонте: 5-мин доходности за 12 ч
    ra = [c5a[i] / c5a[i-1] - 1 for i in range(1, len(c5a))]; rb = [c5b[i] / c5b[i-1] - 1 for i in range(1, len(c5b))]
    beta5 = corr5 = None
    if len(ra) > 10:
        ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
        va = sum((x - ma) ** 2 for x in ra); vb = sum((x - mb) ** 2 for x in rb); cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
        if va > 0 and vb > 0: beta5, corr5 = round(cov / va, 3), round(cov / math.sqrt(va * vb), 3)
    t = ts[-win:]; sim = None
    if len(t) > 5:
        ga, gb = _Grid(oa[t[0]][0], step, lot), _Grid(ob[t[0]][0], step, lot)
        mx_idx = mx_a = mx_b = 0.0
        # вариант «сетка только на SOXL + хедж остатка ногой SOXS в ту же сторону»: раз в минуту, если отклонение >= band
        band = 5 * lot; hu = 0.0; hpnl = 0.0; hfee = 0.0; htr = 0; hres = 0.0; pbp = ob[t[0]][0]
        for x in t:
            for g, (o, h, l, c) in ((ga, oa[x]), (gb, ob[x])):
                for p in ((o, l, h, c) if c >= o else (o, h, l, c)): g.feed(p)
            ea, eb = ga.inv_usd(), gb.inv_usd()
            mx_idx = max(mx_idx, abs(ea - eb)); mx_a = max(mx_a, abs(ea)); mx_b = max(mx_b, abs(eb))
            pbc = ob[x][3]; hpnl += hu * (pbc - pbp); pbp = pbc
            d = ea - hu * pbc                                   # лонг-остаток SOXL хеджируется лонгом SOXS на те же доллары
            if abs(d) >= band:
                hu += d / pbc; hfee += abs(d) * TAKER; htr += 1
            hres = max(hres, abs(ea - hu * pbc))
        hours = len(t) / 60; pa, pb = oa[t[-1]][3], ob[t[-1]][3]
        A_, B_ = ga.out(pa, hours), gb.out(pb, hours)
        sim = {"a": A_, "b": B_, "hours": round(hours, 1),
               "both": {"real": round(A_["real"] + B_["real"], 2), "unreal": round(A_["unreal"] + B_["unreal"], 2), "net": round(A_["net"] + B_["net"], 2),
                        "idx_now": round(A_["inv_usd"] - B_["inv_usd"], 2), "idx_max": round(mx_idx, 2), "per1_max": round(mx_idx * LEV / 100, 2)},
               "one": {"hedge_usd_max": round(mx_a, 2), "per1_unhedged": round(mx_a * LEV / 100, 2),
                       "hedge_pnl": round(hpnl, 2), "fee": round(hfee, 2), "trades": htr, "band": band,
                       "net": round(A_["net"] + hpnl - hfee, 2), "resid_max": round(hres, 2), "per1_resid": round(hres * LEV / 100, 2)}}
    return {"t": time.strftime("%H:%M:%S"), "step": step, "lot": lot, "win": win, "vol": vol, "beta5": beta5, "corr5": corr5, "sim": sim,
            "px": {"a": oa[ts[-1]][3], "b": ob[ts[-1]][3]} if ts else None}
