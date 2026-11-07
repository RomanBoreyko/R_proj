# -*- coding: utf-8 -*-
"""Две вкладки дашборда (костяк):
  /tail  — хвосты ETH: учёт и хедж продажи очень дальних путов + продажа/откуп волы по ним
  /pairs — факторные спреды (пары без стопов: размер от MAE, добор на 3σ, выход по времени) + корзина по факторам
Только чтение. Ключ передаётся из book_dashboard (sget/pub).
"""
import json, math, time, re, datetime, threading, urllib.request, statistics as st

MON = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
def ff(x):
    try: return float(x)
    except: return 0.0
def _exp_dt(code):
    m = re.match(r"(\d+)([A-Z]{3})(\d{2})", code or "")
    return datetime.datetime(2000+int(m.group(3)), MON[m.group(2)], int(m.group(1)), 8, 0, 0) if m else None
def _T(code):
    e = _exp_dt(code)
    return max((e-datetime.datetime.utcnow()).total_seconds()/(365*86400), 0.0) if e else 0.0
def _N(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def _bs(S, K, T, s, cp):
    if T <= 0 or s <= 0: return max(0.0, (S-K) if cp == "C" else (K-S))
    d1 = (math.log(S/K)+0.5*s*s*T)/(s*math.sqrt(T)); d2 = d1-s*math.sqrt(T)
    return S*_N(d1)-K*_N(d2) if cp == "C" else K*_N(-d2)-S*_N(-d1)

_C = {}
def cached(key, ttl, fn):
    v = _C.get(key)
    if v and time.time()-v[0] < ttl: return v[1]
    r = fn(); _C[key] = (time.time(), r); return r

# =====================================================================
#  ВКЛАДКА 1: ХВОСТЫ ETH
# =====================================================================
TAIL_CFG = {
    "far_pct": 0.85,        # «дальний» пут: страйк ≤ 85% цены ETH
    "maxloss_budget": 0.25, # потолок суммарного макс. убытка спредов, доля эквити
    "mm_max": 50.0,         # не открывать новые продажи при MM выше, %
    "dvol_sell": 80,        # перцентиль DVOL (1г) для продажи веги
    "dvol_cover": 30,       # перцентиль DVOL для откупа шортов
    "tp_capture": 60,       # фиксировать, когда собрано ≥ % премии
    "cand_dte": (20, 200),  # кандидаты в спреды: дни до экспиры
    "cand_delta": (0.015, 0.09),
    "cand_width": (100, 400),
}

def _dvol():
    def f():
        e = int(time.time()*1000); s = e-370*86400000
        u = f"https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=ETH&start_timestamp={s}&end_timestamp={e}&resolution=1D"
        d = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent":"Mozilla/5.0"}), timeout=20))["result"]["data"]
        c = [x[4] for x in d][-365:]; now = c[-1]; srt = sorted(c)
        q = lambda p: srt[min(len(srt)-1, int(p*(len(srt)-1)))]
        return {"now": round(now,1), "pct": round(sum(1 for v in c if v < now)/len(c)*100),
                "p10": round(q(.1),1), "p50": round(q(.5),1), "p80": round(q(.8),1), "p90": round(q(.9),1),
                "min": round(min(c),1), "max": round(max(c),1), "series": c[-120:]}
    try: return cached("dvol", 600, f)
    except Exception as e: return {"err": str(e)}

def _rv30(pub):
    def f():
        r = pub("/v5/market/kline", {"category":"linear","symbol":"ETHUSDT","interval":"D","limit":"32"})["result"]["list"]
        c = [ff(x[4]) for x in reversed(r)]
        lr = [math.log(c[i]/c[i-1]) for i in range(1, len(c))][-30:]
        return round(st.pstdev(lr)*math.sqrt(365)*100, 1)
    return cached("rv30", 600, f)

def tail_snapshot(sget, pub):
    C = TAIL_CFG
    tk = cached("eth_opt_tk", 8, lambda: {x["symbol"]: x for x in pub("/v5/market/tickers", {"category":"option","baseCoin":"ETH"})["result"]["list"]})
    S = ff(cached("eth_perp", 4, lambda: pub("/v5/market/tickers", {"category":"linear","symbol":"ETHUSDT"})["result"]["list"][0])["markPrice"])
    wb = sget("/v5/account/wallet-balance", {"accountType":"UNIFIED"})["result"]["list"][0]
    eq = ff(wb["totalEquity"]); mm = ff(wb["totalMaintenanceMargin"]); mmp = round(mm/eq*100, 1) if eq else 0
    eth_spot = sum(ff(c["walletBalance"]) for c in wb["coin"] if c["coin"] == "ETH")
    opts = [o for o in sget("/v5/position/list", {"category":"option","baseCoin":"ETH","limit":"200"})["result"]["list"] if ff(o["size"]) > 0]
    perp = sum((ff(p["size"]) if p["side"] == "Buy" else -ff(p["size"])) for p in sget("/v5/position/list", {"category":"linear","symbol":"ETHUSDT"})["result"]["list"])
    legs = []
    for o in opts:
        pr = o["symbol"].split("-")
        if len(pr) < 4: continue
        sym = o["symbol"] if o["symbol"].endswith("-USDT") else o["symbol"]+"-USDT"
        t = tk.get(sym, {})
        q = ff(o["size"])*(1 if o["side"] == "Buy" else -1)
        legs.append({"sym": "-".join(pr[:4]), "exp": pr[1], "K": ff(pr[2]), "cp": pr[3], "q": q, "avg": ff(o["avgPrice"]),
                     "mark": ff(o["markPrice"]), "iv": ff(t.get("markIv")) or 0.6, "bid": ff(t.get("bid1Price")), "ask": ff(t.get("ask1Price")),
                     "d": ff(o.get("delta")), "t": ff(o.get("theta")), "v": ff(o.get("vega")), "up": ff(o.get("unrealisedPnl")),
                     "T": _T(pr[1]), "dte": round(_T(pr[1])*365, 1)})
    # --- дальние шорт-путы и их крылья, по экспирам
    far = [l for l in legs if l["cp"] == "P" and l["q"] < 0 and l["K"] <= S*C["far_pct"]]
    groups = []; tot_def = 0.0; tot_naked_notional = 0.0; theta_far = 0.0; vega_far = 0.0; prem_far = 0.0
    for e in sorted(set(l["exp"] for l in far), key=lambda x: _exp_dt(x)):
        sh = sorted([l for l in far if l["exp"] == e], key=lambda l: -l["K"])
        lo = sorted([l for l in legs if l["exp"] == e and l["cp"] == "P" and l["q"] > 0 and l["K"] < max(x["K"] for x in sh)], key=lambda l: -l["K"])
        sq = sum(-l["q"] for l in sh); lq = sum(l["q"] for l in lo)
        credit = sum(-l["q"]*l["avg"] for l in sh) - sum(l["q"]*l["avg"] for l in lo)
        now_val = sum(-l["q"]*l["mark"] for l in sh) - sum(l["q"]*l["mark"] for l in lo)
        # макс. убыток на экспирации: по payoff на сетке цен 0..K_max
        Ks = sorted(set([0.0]+[l["K"] for l in sh+lo]))
        pay = lambda P: sum(l["q"]*max(0, l["K"]-P) for l in sh+lo)
        worst = min(pay(P) for P in Ks)
        naked = max(0.0, sq-lq)
        ml = (-worst - credit) if naked < 1e-9 else None
        if ml is not None: tot_def += max(ml, 0)
        else: tot_naked_notional += naked*max(l["K"] for l in sh)
        th = sum(l["t"] for l in sh+lo); vg = sum(l["v"] for l in sh+lo)
        theta_far += th; vega_far += vg; prem_far += credit
        # крыло для голой части: самый дешёвый пут ниже (ширина 100..400)
        wing = None
        if naked > 1e-9:
            Kt = max(l["K"] for l in sh); c = []
            for sym, x in tk.items():
                p = sym.split("-")
                if p[1] != e or p[3] != "P": continue
                K = ff(p[2]); a = ff(x["ask1Price"])
                if a <= 0 or not (50 <= Kt-K <= 600) or any(abs(K-l["K"]) < 1e-9 for l in sh): continue
                legs2 = sh+lo+[{"K": K, "q": naked}]
                Ks2 = sorted(set([0.0]+[l["K"] for l in legs2]))
                w2 = min(sum(l["q"]*max(0, l["K"]-P) for l in legs2) for P in Ks2)
                c.append({"K": K, "ask": a, "cost": round(a*naked, 2), "ml": round(-w2-(credit-a*naked), 1)})
            wing = sorted(c, key=lambda r: r["ml"]+r["cost"]*3)[:3] or "нет страйков ниже с живым ask"
        captured = round((1 - now_val/credit)*100) if credit > 0 else None
        groups.append({"exp": e, "dte": sh[0]["dte"], "shorts": [[l["K"], -l["q"], l["avg"], round(l["mark"],2), round(l["iv"]*100), l["bid"], l["ask"]] for l in sh],
                       "longs": [[l["K"], l["q"], l["avg"], round(l["mark"],2)] for l in lo],
                       "cover": round(min(lq/sq, 1)*100) if sq else 0, "naked": round(naked, 2), "credit": round(credit, 2), "now": round(now_val, 2),
                       "pnl": round(credit-now_val, 2), "captured": captured, "maxloss": round(ml, 1) if ml is not None else None,
                       "theta": round(th, 2), "vega": round(vg, 2), "wing": wing,
                       "tp": captured is not None and captured >= C["tp_capture"]})
    # --- тактические хеджи: лонг-путы ≤14д
    hedges = [l for l in legs if l["cp"] == "P" and l["q"] > 0 and l["dte"] <= 14]
    hedge_theta = -sum(max(l["t"]*0, l["mark"]*l["q"]/max(l["dte"], 0.25)) for l in hedges)   # стоимость/день = остаток премии / дней (BS-тета у экспиры завышена)
    # --- стресс всей ETH-книги (мгновенный шок)
    moves = [-0.5, -0.4, -0.3, -0.2, -0.1, -0.05, 0, 0.05, 0.1, 0.2]
    shocks = [0, 0.2, 0.4]
    stress = []
    for mv in moves:
        S2 = S*(1+mv); row = []
        for sh_ in shocks:
            tot = (perp+eth_spot)*(S2-S)
            for l in legs:
                s2 = l["iv"] + (sh_ if (mv < 0 and l["cp"] == "P") else (sh_/2 if mv < 0 else sh_/3))
                tot += l["q"]*(_bs(S2, l["K"], l["T"], s2, l["cp"]) - l["mark"])
            row.append(round(tot))
        stress.append({"mv": round(mv*100), "S": round(S2), "pnl": row})
    # --- кандидаты в пут-спреды
    cands = []
    byexp = {}
    for sym, x in tk.items():
        p = sym.split("-")
        if p[3] != "P": continue
        byexp.setdefault(p[1], []).append((ff(p[2]), x))
    for e, arr in byexp.items():
        dte = _T(e)*365
        if not (C["cand_dte"][0] <= dte <= C["cand_dte"][1]): continue
        for Ks, xs in arr:
            d = abs(ff(xs["delta"])); b = ff(xs["bid1Price"])
            if not (C["cand_delta"][0] <= d <= C["cand_delta"][1]) or b <= 0: continue
            for Kl, xl in arr:
                w = Ks-Kl; a = ff(xl["ask1Price"])
                if not (C["cand_width"][0] <= w <= C["cand_width"][1]) or a <= 0: continue
                cr = b-a
                if cr <= 0: continue
                rr = cr/(w-cr)*100
                cands.append({"exp": e, "dte": round(dte), "Ks": Ks, "Kl": Kl, "dist": round((Ks/S-1)*100, 1), "delta": round(d, 3),
                              "ivs": round(ff(xs["markIv"])*100), "ivl": round(ff(xl["markIv"])*100), "credit": round(cr, 2), "ml": round(w-cr, 1),
                              "rr": round(rr, 1), "rr_m": round(rr/(dte/30.4), 2), "pl": round(abs(ff(xl["delta"])), 3)})
    cands.sort(key=lambda r: -r["rr_m"])
    # --- вола
    dv = _dvol(); rv = _rv30(pub)
    atm = None
    ex = sorted(set(s.split("-")[1] for s in tk), key=lambda e: _exp_dt(e))
    for e in ex:
        if _T(e)*365 < 7: continue
        arr = [(abs(ff(s.split("-")[2])-S), x) for s, x in tk.items() if s.split("-")[1] == e]
        if arr:
            k0 = min(a[0] for a in arr); ivs = [ff(x["markIv"]) for a0, x in arr if a0 == k0]
            atm = {"exp": e, "iv": round(st.mean(ivs)*100, 1)}; break
    pct = dv.get("pct")
    if pct is None: vsig, vk = "DVOL недоступен", "wait"
    elif pct >= C["dvol_sell"]: vsig, vk = f"ПРОДАВАТЬ ВЕГУ — DVOL {dv['now']} в {pct}-м перцентиле года", "sell"
    elif pct <= C["dvol_cover"]: vsig, vk = f"ОТКУПАТЬ ШОРТЫ — вола дешёвая (DVOL {dv['now']}, {pct}-й перцентиль)", "cover"
    else: vsig, vk = f"ЖДАТЬ — DVOL {dv['now']} в {pct}-м перцентиле (продажа ≥{C['dvol_sell']}, откуп ≤{C['dvol_cover']})", "wait"
    warns = []
    if mmp > C["mm_max"]: warns.append(f"MM {mmp}% > {C['mm_max']}% — новые продажи не открывать")
    if tot_naked_notional > 0: warns.append(f"Голые дальние путы: номинал ${tot_naked_notional:,.0f} — макс. убыток не ограничен (см. «крыло» в таблице)")
    if eq and tot_def > C["maxloss_budget"]*eq: warns.append(f"Макс. убыток спредов ${tot_def:.0f} > {int(C['maxloss_budget']*100)}% эквити")
    if hedge_theta < 0 and theta_far > 0 and -hedge_theta > 0.5*theta_far: warns.append(f"Тактические хеджи съедают {(-hedge_theta/theta_far*100):.0f}% теты дальних шортов")
    return {"t": time.strftime("%H:%M:%S"), "S": round(S, 2), "eq": round(eq, 2), "mm": mmp, "perp": perp, "spot": round(eth_spot, 4),
            "groups": groups, "tot_def": round(tot_def, 1), "naked_notional": round(tot_naked_notional), "theta_far": round(theta_far, 2),
            "vega_far": round(vega_far, 2), "prem_far": round(prem_far, 2), "budget": round(C["maxloss_budget"]*eq, 0),
            "hedges": [[l["sym"], l["q"], l["avg"], round(l["mark"],2), l["dte"], round(-l["mark"]*l["q"]/max(l["dte"], 0.25),2)] for l in hedges], "hedge_theta": round(hedge_theta, 2),
            "stress": stress, "shocks": [int(s*100) for s in shocks], "cands": cands[:10],
            "dvol": dv, "rv30": rv, "atm": atm, "vsig": vsig, "vk": vk, "warns": warns, "cfg": C}

# =====================================================================
#  ВКЛАДКА 2: ФАКТОРНЫЕ СПРЕДЫ + КОРЗИНА
# =====================================================================
# (A, B, сессия, статус) — статус «рабочая» = прошла бэктест без стопа (жёсткая связь); «наблюдать» = без стопа нельзя
PAIRS = [("ETH","BTC","ALL","рабочая"), ("ETH","SOL","ALL","рабочая"), ("ETH","ETHFI","ALL","наблюдать"), ("ETH","LDO","ALL","наблюдать"),
         ("BMNR","ETH","US","наблюдать"), ("ETH","MSTR","US","наблюдать"),
         ("GDX","XAU","US","рабочая"), ("NVDA","QQQ","US","рабочая"), ("SAMSUNG","SKHYNIX","KR","рабочая"), ("EWY","SAMSUNG","WK","рабочая"),
         ("BZ","CL","WK","наблюдать"), ("XAG","XAU","WK","наблюдать"), ("SNDK","MU","US","наблюдать"), ("MU","SKHYNIX","WK","наблюдать"),
         ("DRAM","MU","US","наблюдать"), ("SKHY","SKHYNIX","WK","наблюдать"), ("QQQ","SPY","US","наблюдать")]
# мейкер-комиссия ноги (твои ставки по API): крипто-перпы 0.036%, TradFi 0%
CRYPTO = {"ETH","BTC","SOL","ETHFI","LDO","ARB","MNT"}
def _fee(sym): return 0.00036 if sym in CRYPTO else 0.0
PAIR_CFG = {"W": 260, "zin": 2.0, "zadd": 3.0, "tmax_d": 10, "budget": 30.0, "days": 80}
# фактор: {символ: вес в «долларах фактора»}; GDX ≈ 2× золото по бете
FACTORS = {
    "Золото":        ({"XAUUSDT":1, "XAUTUSDT":1, "GDXUSDT":2, "XAGUSDT":1}, 150),
    "Техи США":      ({"QQQUSDT":1, "SPYUSDT":1, "NVDAUSDT":1, "TSLAUSDT":1, "TQQQUSDT":1, "SOXLUSDT":1, "SOXSUSDT":1, "SQQQUSDT":1}, 150),
    "Память/Корея":  ({"MUUSDT":1, "SNDKUSDT":1, "SKHYNIXUSDT":1, "SKHYUSDT":1, "SAMSUNGUSDT":1, "DRAMUSDT":1, "EWYUSDT":1, "KORUUSDT":1, "SAMSUNGEMUSDT":1}, 150),
    "Нефть":         ({"CLUSDT":1, "BZUSDT":1}, 100),
}
_PL = threading.Lock(); _PS = {"busy": False}

def _sess(name, t):
    if name == "ALL": return True                         # крипта 24/7
    tm = time.gmtime(t); m = tm.tm_hour*60+tm.tm_min
    if tm.tm_wday >= 5: return False
    if name == "KR": return m < 390                       # KRX 00:00–06:30 UTC
    if name == "WK": return True
    dst = (1773558000 <= t < 1793516400) or (1805007600 <= t < 1825570800)  # 08.03.26–01.11.26, 14.03.27–07.11.27
    o, c = (13*60+30, 20*60) if dst else (14*60+30, 21*60)
    return o <= m < c

def _kl(pub, sym, days):
    out = {}; end = int(time.time()*1000); start = end-days*86400000
    while True:
        r = pub("/v5/market/kline", {"category":"linear","symbol":sym,"interval":"30","limit":"1000","end":str(end)})["result"]["list"]
        if not r: break
        for x in r: out[int(x[0])//1000] = ff(x[4])
        end = int(r[-1][0])-1
        if end < start: break
    return out

def _roll(ya, xb, W):
    """скользящая OLS ya~xb на окне W (O(n)): beta, mean, sd спреда, z на каждом баре."""
    n = len(ya); y0, x0 = ya[0], xb[0]
    y = [v-y0 for v in ya]; x = [v-x0 for v in xb]
    sx=[0.0]; sy=[0.0]; sxx=[0.0]; syy=[0.0]; sxy=[0.0]
    for a, b in zip(x, y):
        sx.append(sx[-1]+a); sy.append(sy[-1]+b); sxx.append(sxx[-1]+a*a); syy.append(syy[-1]+b*b); sxy.append(sxy[-1]+a*b)
    res = [None]*n
    for i in range(W, n):
        j = i-W
        mx = (sx[i]-sx[j])/W; my = (sy[i]-sy[j])/W
        vx = (sxx[i]-sxx[j])/W-mx*mx; vy = (syy[i]-syy[j])/W-my*my; cv = (sxy[i]-sxy[j])/W-mx*my
        if vx <= 0: continue
        be = cv/vx; vs = vy-cv*cv/vx
        if vs <= 0: continue
        sd = math.sqrt(vs); m = my-be*mx
        res[i] = (be, m, sd, (y[i]-be*x[i]-m)/sd)
    return res, y0, x0

def _bt(ts, ya, xb, R, C):
    pos = 0; tr = []; ent = []; mae = 0.0
    for i in range(len(ts)):
        if R[i] is None: continue
        be, m, sd, z = R[i]
        if pos:
            pnl = sum(pos*((ya[i]-ya[e])-lb*(xb[i]-xb[e])) for e, lb in ent)/2
            mae = min(mae, pnl)
            if (pos == 1 and z >= 0) or (pos == -1 and z <= 0) or (ts[i]-ts[ent[0][0]] > C["tmax_d"]*86400):
                tr.append((pnl*1e4, mae*1e4, (ts[i]-ts[ent[0][0]])/86400, len(ent))); pos = 0
            elif len(ent) == 1 and abs(z) >= C["zadd"] and (z > 0) == (pos == -1): ent.append((i, be))
        elif abs(z) >= C["zin"]:
            pos = -1 if z > 0 else 1; ent = [(i, be)]; mae = 0.0
    op = None
    if pos:
        i = len(ts)-1
        op = {"dir": "лонг A / шорт B" if pos == 1 else "шорт A / лонг B", "units": len(ent),
              "days": round((ts[i]-ts[ent[0][0]])/86400, 1), "pnl_bp": round(sum(pos*((ya[i]-ya[e])-lb*(xb[i]-xb[e])) for e, lb in ent)/2*1e4),
              "z_in": round(R[ent[0][0]][3], 2)}
    p = [t[0] for t in tr]
    return {"n": len(p), "win": round(sum(1 for v in p if v > 0)/len(p)*100) if p else None, "avg": round(st.mean(p)) if p else None,
            "worst": round(min(p)) if p else None, "mae": round(min(t[1] for t in tr)) if tr else None,
            "hold": round(st.median(t[2] for t in tr), 1) if tr else None, "open": op}

def _pairs_heavy(pub):
    C = PAIR_CFG
    syms = sorted(set([a for a, b, s, k in PAIRS]+[b for a, b, s, k in PAIRS]+["QQQ"]))
    D = {s: _kl(pub, s+"USDT", C["days"]) for s in syms}
    info = {}
    for s in syms:
        try:
            x = pub("/v5/market/instruments-info", {"category":"linear","symbol":s+"USDT"})["result"]["list"][0]["lotSizeFilter"]
            info[s] = (ff(x["qtyStep"]), ff(x["minOrderQty"]))
        except Exception: info[s] = (0.01, 0.01)
    out = []
    for a, b, ss, kind in PAIRS:
        ts = sorted(t for t in D[a] if t in D[b] and _sess(ss, t))
        if len(ts) < C["W"]+50: out.append({"a": a, "b": b, "err": f"мало данных ({len(ts)})"}); continue
        ya = [math.log(D[a][t]) for t in ts]; xb = [math.log(D[b][t]) for t in ts]
        R, y0, x0 = _roll(ya, xb, C["W"])
        bt = _bt(ts, ya, xb, R, C)
        last = R[-1]
        cost = round(2*(_fee(a)+abs(last[0])*_fee(b))*1e4, 1)   # круг мейкером обеих ног, бп ноги A
        bt["cost"] = cost; bt["net"] = round(bt["avg"]-cost) if bt["avg"] is not None else None
        zs = [[ts[i]*1000, round(R[i][3], 3)] for i in range(max(0, len(ts)-400), len(ts)) if R[i]]
        # полураспад на последнем окне
        be = last[0]; s = [ya[i]-be*xb[i] for i in range(len(ts)-C["W"], len(ts))]
        xx, yy = s[:-1], s[1:]; m1, m2 = st.mean(xx), st.mean(yy)
        ph = sum((u-m1)*(v-m2) for u, v in zip(xx, yy))/sum((u-m1)**2 for u in xx)
        hl = round(-math.log(2)/math.log(ph), 1) if 0 < ph < 1 else None
        out.append({"a": a, "b": b, "sess": ss, "kind": kind, "beta": round(last[0], 3), "m": last[1], "sd": last[2], "y0": y0, "x0": x0,
                    "z_close": round(last[3], 2), "sd_pm": round(last[2]*1000, 1), "hl_bars": hl, "bt": bt, "zs": zs,
                    "last_bar": ts[-1], "step": {a: info[a], b: info[b]}})
    # беты к QQQ (RTH, 30м доходности) для корзины
    bq = {}
    q = D["QQQ"]
    for s in syms:
        ts = sorted(t for t in D[s] if t in q and _sess("US", t) and (t-1800) in D[s] and (t-1800) in q and _sess("US", t-1800))
        if len(ts) < 100: continue
        rx = [math.log(q[t]/q[t-1800]) for t in ts]; ry = [math.log(D[s][t]/D[s][t-1800]) for t in ts]
        mx, my = st.mean(rx), st.mean(ry)
        bq[s+"USDT"] = round(sum((u-mx)*(v-my) for u, v in zip(rx, ry))/sum((u-mx)**2 for u in rx), 2)
    return {"pairs": out, "bq": bq, "t": time.time()}

def _refresh(pub):
    try:
        r = _pairs_heavy(pub); _C["pairs_heavy"] = (time.time(), r)
    except Exception as e: _C["pairs_err"] = (time.time(), str(e))
    finally: _PS["busy"] = False

def pairs_snapshot(sget, pub):
    C = PAIR_CFG
    h = _C.get("pairs_heavy")
    if (not h or time.time()-h[0] > 1800) and not _PS["busy"]:
        with _PL:
            if not _PS["busy"]:
                _PS["busy"] = True; threading.Thread(target=_refresh, args=(pub,), daemon=True).start()
    if not h:
        e = _C.get("pairs_err")
        return {"loading": True, "err": e[1] if e else None}
    H = h[1]
    tk = cached("lin_tk", 4, lambda: {x["symbol"]: x for x in pub("/v5/market/tickers", {"category":"linear"})["result"]["list"]})
    pos = {}
    for sc in ("USDT",):
        for p in sget("/v5/position/list", {"category":"linear","settleCoin":sc,"limit":"200"})["result"]["list"]:
            if ff(p["size"]) > 0: pos[p["symbol"]] = (ff(p["size"])*(1 if p["side"] == "Buy" else -1), ff(p["markPrice"]))
    now = time.time(); rows = []
    for P in H["pairs"]:
        if "err" in P: rows.append(P); continue
        a, b = P["a"]+"USDT", P["b"]+"USDT"
        ta, tb = tk.get(a, {}), tk.get(b, {})
        pa = (ff(ta.get("bid1Price"))+ff(ta.get("ask1Price")))/2; pb = (ff(tb.get("bid1Price"))+ff(tb.get("ask1Price")))/2
        zl = None
        if pa > 0 and pb > 0:
            zl = round(((math.log(pa)-P["y0"]) - P["beta"]*(math.log(pb)-P["x0"]) - P["m"])/P["sd"], 2)
        open_ = _sess(P["sess"], now)
        z = zl if zl is not None else P["z_close"]
        if P["kind"] != "рабочая": sig, sk = "без стопа не торговать", "wait"
        elif abs(z) >= C["zadd"]: sig, sk = "ДОБОР: "+("лонг A / шорт B" if z < 0 else "шорт A / лонг B"), "add"
        elif abs(z) >= C["zin"]: sig, sk = "ВХОД: "+("лонг A / шорт B" if z < 0 else "шорт A / лонг B"), "in"
        elif abs(z) <= 0.3: sig, sk = "у нуля — выход, если в позиции", "exit"
        else: sig, sk = "ждать", "wait"
        if not open_ and sk in ("in", "add"): sig += " (вне сессии — ждать открытия)"
        bt = P["bt"]
        mae = abs(bt["mae"]) if bt.get("mae") else 3*P["sd"]*1e4
        legA = C["budget"]/(2*mae/1e4) if mae else 0
        legB = legA*P["beta"]
        qa = qb = None
        if pa > 0 and pb > 0:
            sa, ma = P["step"][P["a"]]; sb, mb = P["step"][P["b"]]
            qa = round(max(ma, round(legA/pa/sa)*sa), 6); qb = round(max(mb, round(legB/pb/sb)*sb), 6)
        # мои позиции по ногам
        mya = pos.get(a, (0, 0)); myb = pos.get(b, (0, 0))
        mine = None
        if mya[0] and myb[0]:
            na, nb = mya[0]*pa, myb[0]*pb
            mine = {"a": mya[0], "b": myb[0], "ratio": round(-nb/na, 2) if na else None, "hedged": (na > 0) != (nb > 0)}
        rows.append({"a": P["a"], "b": P["b"], "sess": P["sess"], "open": open_, "kind": P["kind"], "beta": P["beta"], "sd_pm": P["sd_pm"],
                     "hl": P["hl_bars"], "z": z, "z_close": P["z_close"], "sig": sig, "sk": sk, "bt": bt,
                     "legA": round(legA), "legB": round(legB), "qa": qa, "qb": qb, "pa": round(pa, 4), "pb": round(pb, 4), "mine": mine, "zs": P["zs"]})
    # корзина
    fac = []; used = set()
    for name, (mp, lim) in FACTORS.items():
        net = 0.0; bqq = 0.0; parts = []
        for s, w in mp.items():
            if s in pos:
                q, m = pos[s]; n = q*m
                net += n*w; bqq += n*H["bq"].get(s, 0); parts.append(f"{s.replace('USDT','')} {n:+.0f}$"); used.add(s)
        fac.append({"f": name, "net": round(net), "lim": lim, "bqq": round(bqq), "parts": parts, "over": abs(net) > lim})
    other = [(s, q*m) for s, (q, m) in pos.items() if s not in used and s != "ETHUSDT"]
    if other: fac.append({"f": "Прочее", "net": round(sum(n for s, n in other)), "lim": None, "bqq": 0, "parts": [f"{s.replace('USDT','')} {n:+.0f}$" for s, n in other], "over": False})
    tot_bqq = round(sum(f["bqq"] for f in fac))
    return {"t": time.strftime("%H:%M:%S"), "rows": rows, "fac": fac, "tot_bqq": tot_bqq, "cfg": C,
            "age_min": round((time.time()-h[0])/60), "busy": _PS["busy"]}
