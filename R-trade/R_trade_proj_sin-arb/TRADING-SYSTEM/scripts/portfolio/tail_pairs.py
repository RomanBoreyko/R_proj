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

def inverse_positions(sget):
    """Инверсные перпы/фьючи (ETHUSD, BTCUSD, ETHUSDZ26…) в «линейном» виде.
    P&L$ инверса = (Q/E)·(S−E) → это линейный перп размером Q/E монет со входом E.
    size — в монетах, unrealisedPnl — в $, usd — исходный размер в USD. Ошибка API → []."""
    out = []
    try:
        for p in sget("/v5/position/list", {"category": "inverse", "limit": "200"})["result"]["list"]:
            q, e = ff(p.get("size")), ff(p.get("avgPrice"))
            if q <= 0 or e <= 0: continue
            mk = ff(p.get("markPrice")) or e
            out.append({"symbol": p["symbol"], "side": p["side"], "size": str(q / e), "avgPrice": str(e),
                        "markPrice": str(mk), "unrealisedPnl": str(ff(p.get("unrealisedPnl")) * mk),
                        "inv": True, "usd": q})
    except Exception:
        return []
    return out

def inv_base(sym):
    m = re.match(r"^([A-Z0-9]+?)USD([FGHJKMNQUVXZ]\d\d)?$", sym or "")
    return m.group(1) if m else None

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

def _rv7(pub):
    def f():
        r = pub("/v5/market/kline", {"category":"linear","symbol":"ETHUSDT","interval":"D","limit":"9"})["result"]["list"]
        c = [ff(x[4]) for x in reversed(r)]
        lr = [math.log(c[i]/c[i-1]) for i in range(1, len(c))][-7:]
        return round(st.pstdev(lr)*math.sqrt(365)*100, 1)
    try: return cached("rv7", 600, f)
    except Exception: return None

def tail_snapshot(sget, pub):
    C = TAIL_CFG
    tk = cached("eth_opt_tk", 8, lambda: {x["symbol"]: x for x in pub("/v5/market/tickers", {"category":"option","baseCoin":"ETH"})["result"]["list"]})
    S = ff(cached("eth_perp", 4, lambda: pub("/v5/market/tickers", {"category":"linear","symbol":"ETHUSDT"})["result"]["list"][0])["markPrice"])
    wb = sget("/v5/account/wallet-balance", {"accountType":"UNIFIED"})["result"]["list"][0]
    eq = ff(wb["totalEquity"]); mm = ff(wb["totalMaintenanceMargin"]); mmp = round(mm/eq*100, 1) if eq else 0
    im = ff(wb["totalInitialMargin"]); imp = round(im/eq*100, 1) if eq else 0; buf = round(eq/mm, 2) if mm else None; av = ff(wb.get("totalAvailableBalance"))
    eth_spot = sum(ff(c["walletBalance"]) for c in wb["coin"] if c["coin"] == "ETH")
    opts = [o for o in sget("/v5/position/list", {"category":"option","baseCoin":"ETH","limit":"200"})["result"]["list"] if ff(o["size"]) > 0]
    _pl = []
    for _sc in ("USDT", "USDC"):   # ETHUSDT + ETHPERP (USDC) + инверс ETHUSD
        _pl += [p for p in sget("/v5/position/list", {"category":"linear","settleCoin":_sc,"limit":"200"})["result"]["list"]
                if p["symbol"] in ("ETHUSDT", "ETHPERP") or p["symbol"].startswith(("ETH-", "ETHUSDT-"))]   # + датированные ETHUSDT-25DEC26
    _pl += [p for p in inverse_positions(sget) if inv_base(p["symbol"]) == "ETH"]
    _lin = sum((ff(p["size"]) if p["side"] == "Buy" else -ff(p["size"])) for p in _pl if p["symbol"].startswith(("ETHUSDT", "ETHPERP", "ETH-")))   # линейные фьючи/перпы
    perp = sum((ff(p["size"]) if p["side"] == "Buy" else -ff(p["size"])) for p in _pl)
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
                     "g": ff(o.get("gamma")), "age": round((time.time()*1000 - ff(o.get("openTime") or o.get("createdTime")))/86400000, 1) if (o.get("openTime") or o.get("createdTime")) else None,
                     "T": _T(pr[1]), "dte": round(_T(pr[1])*365, 1)})
    # --- дальние шорт-путы и их крылья, по экспирам
    far = [l for l in legs if l["cp"] == "P" and l["q"] < 0 and l["K"] <= S*C["far_pct"]]
    groups = []; tot_def = 0.0; tot_naked_notional = 0.0; theta_far = 0.0; vega_far = 0.0; prem_far = 0.0
    for e in sorted(set(l["exp"] for l in far), key=lambda x: _exp_dt(x)):
        sh = sorted([l for l in far if l["exp"] == e], key=lambda l: -l["K"])
        # покрытие — ЛЮБЫЕ купленные путы той же экспиры: и ниже (спред), и выше страйка (лонг 1700P закрывает шорт 1600P целиком)
        lo = sorted([l for l in legs if l["exp"] == e and l["cp"] == "P" and l["q"] > 0], key=lambda l: -l["K"])
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
    def spnl(mv, sh_, h=0.0):   # P&L книги при ходе mv и IV-шоке sh_; h — доп. шорт фьючерса ETH
        S2 = S*(1+mv); tot = (perp+eth_spot-h)*(S2-S)
        for l in legs:
            s2 = l["iv"] + (sh_ if (mv < 0 and l["cp"] == "P") else (sh_/2 if mv < 0 else sh_/3))
            tot += l["q"]*(_bs(S2, l["K"], l["T"], s2, l["cp"]) - l["mark"])
        return round(tot)
    stress = [{"mv": round(mv*100), "S": round(S*(1+mv)), "pnl": [spnl(mv, s) for s in shocks]} for mv in moves]
    # --- уровни ликвидации по цене ETH: где убыток ETH-книги (T+0) съест эквити над MM.
    #     Прочие активы стоят. На падении IV +20 п.п., на росте +10 (spnl: коллам треть шока).
    #     «при текущей MM» — MM не меняется; «консервативно» — MM вырастет на 10% (PM пересчитает сценарии).
    room = eq - mm
    def _liq(room_):
        out = {}
        for side, rng in (("down", [-k/100 for k in range(1, 91)]), ("up", [k/100 for k in range(1, 201)])):
            for mv in rng:
                if spnl(mv, 0.2 if mv < 0 else 0.1) <= -room_:
                    out[side] = {"S": round(S*(1+mv)), "pct": round(mv*100)}; break
        return out
    liq = {"room": round(room, 2), "now": _liq(room), "cons": _liq(eq - 1.1*mm)}
    # --- прикрытие голых путов шортом фьючерса против купленных коллов (одна монета → PM зачитывает)
    def _bsd(S_, K, T, s, cp):
        if T <= 0 or s <= 0: return (1.0 if S_ > K else 0.0) if cp == "C" else (-1.0 if S_ < K else 0.0)
        d1 = (math.log(S_/K) + 0.5*s*s*T)/(s*math.sqrt(T))
        return _N(d1) if cp == "C" else _N(d1) - 1
    naked_legs = []   # голая часть каждой группы, пропорционально шортам
    for g in groups:
        if g["naked"] > 0:
            sh = [l for l in far if l["exp"] == g["exp"]]; sq = sum(-l["q"] for l in sh)
            naked_legs += [dict(l, q=l["q"]*g["naked"]/sq) for l in sh]
    naked_q = sum(-l["q"] for l in naked_legs)
    d_naked = lambda S_, sh_=0.0: sum(-l["q"]*-_bsd(S_, l["K"], l["T"], l["iv"]+sh_, "P") for l in naked_legs)   # >0: лонг-дельта шортов
    d_book = lambda S_, sh_=0.0: perp + eth_spot + sum(l["q"]*_bsd(S_, l["K"], l["T"], l["iv"]+(sh_ if l["cp"] == "P" else sh_/2), l["cp"]) for l in legs)
    calls = [l for l in legs if l["cp"] == "C"]
    FUT = "ETHUSDT-25DEC26"; Tf = _T(FUT.split("-")[1])
    # сверху шорт фьючерса закрывают только коллы, живущие не меньше фьючерса (11NOV/13NOV не в счёт)
    call_q = sum(l["q"] for l in calls if l["T"] >= Tf - 1e-6)
    fut = {}
    for fs in (FUT,):
        try:
            x = cached("tk_"+fs, 30, lambda fs=fs: pub("/v5/market/tickers", {"category":"linear","symbol":fs})["result"]["list"][0])
            F = ff(x["markPrice"]); T_ = _T(fs.split("-")[1])
            fut = {"sym": fs, "F": round(F, 2), "basis": round((F/S-1)*100, 2), "basis_y": round((F/S-1)/T_*100, 1) if T_ > 0 else None, "dte": round(T_*365)}
        except Exception as e: fut = {"sym": fs, "err": str(e)}
    def variant(name, h, note):
        h = round(max(h, 0.0), 2)
        return {"name": name, "h": h, "usd": round(h*S), "up_naked": round(max(0.0, h-call_q), 2), "note": note,
                "pnl": [spnl(mv, 0.2 if mv < 0 else 0.0, h) for mv in (-0.5, -0.3, -0.2, -0.1, 0.1, 0.2)]}
    db0 = d_book(S)
    fh = {"naked_q": round(naked_q, 2), "d_naked": round(d_naked(S), 3), "d_book": round(db0, 3), "call_q": round(call_q, 2),
          "calls": [[l["exp"], l["K"], l["q"], l["T"] >= Tf - 1e-6] for l in calls], "perp": round(perp, 3), "fut": fut,
          "moves": [-50, -30, -20, -10, 10, 20],
          "base": [spnl(mv, 0.2 if mv < 0 else 0.0) for mv in (-0.5, -0.3, -0.2, -0.1, 0.1, 0.2)],
          "variants": [variant("Δ голых путов", d_naked(S), "гасит только дельту проданного края сейчас"),
                       variant("вся ETH-книга в ноль", db0, "дельта ETH ≈ 0: коллы, путы, инверс, спот"),
                       variant("1:1 к голым путам", naked_q, "статично закрывает обвал до 0 на экспирации; сверху голо всё, что больше коллов")],
          # лестница: сколько шорта держать на каждом уровне падения (дельта растёт из-за гаммы, IV растёт на падении)
          "ladder": [{"mv": m, "S": round(S*(1+m/100)), "iv_sh": sh_, "h_naked": round(d_naked(S*(1+m/100), sh_/100), 2),
                      "d_book": round(d_book(S*(1+m/100), sh_/100), 2)}   # дельта всей книги на уровне (без нового шорта), ETH
                     for m, sh_ in ((-10, 10), (-20, 20), (-30, 20), (-40, 30), (-50, 30))]}
    # --- вся ETH-книга: все ноги одной таблицей
    def _orow(l):
        q = l["q"]; aq = abs(q); short = q < 0
        be = (l["K"] - l["avg"]) if l["cp"] == "P" else (l["K"] + l["avg"])          # безубыток на экспирации
        sig = l["iv"]*math.sqrt(max(l["T"], 1e-9))                                     # 1σ до экспирации, доля цены
        dist = l["K"]/S - 1
        prem = l["avg"]*aq; now = l["mark"]*aq
        return {"sym": l["sym"], "kind": ("шорт " if short else "лонг ") + l["cp"], "q": round(q, 4), "K": l["K"], "dte": l["dte"], "age": l.get("age"),
                "dist": round(dist*100, 1), "dsig": round(dist/sig, 2) if sig > 0 else None,
                "avg": round(l["avg"], 2), "mark": round(l["mark"], 2), "bid": l["bid"], "ask": l["ask"], "iv": round(l["iv"]*100),
                "d1": round(l["d"]/q, 3) if q else 0, "dusd": round(l["d"]*S), "g1": round(l["g"]*S*S*0.01, 1),
                "t": round(l["t"], 2), "v": round(l["v"], 2), "pitm": round(abs(l["d"]/q)*100) if q else None,
                "be": round(be, 1), "be_dist": round((be/S-1)*100, 1), "prem": round(prem, 2), "now": round(now, 2),
                "capt": round((1 - l["mark"]/l["avg"])*100) if short and l["avg"] > 0 else None,
                "up": round(l["up"], 2), "up_pct": round(l["up"]/prem*100) if prem > 0 else None}
    book = [_orow(l) for l in sorted(legs, key=lambda l: (_exp_dt(l["exp"]), l["cp"], l["K"]))]
    for p in _pl:
        if ff(p["size"]) <= 0: continue
        q = ff(p["size"])*(1 if p["side"] == "Buy" else -1)
        nm = p["symbol"] + (" (инв.)" if not p["symbol"].startswith(("ETHUSDT", "ETHPERP", "ETH-")) else "")
        book.append({"sym": nm, "kind": "лонг F" if q > 0 else "шорт F", "q": round(q, 4), "avg": round(ff(p.get("avgPrice")), 2), "mark": round(ff(p.get("markPrice")), 2),
                     "d1": 1, "dusd": round(q*S), "g1": 0, "t": 0, "v": 0, "up": round(ff(p.get("unrealisedPnl")), 2),
                     "be": round(ff(p.get("avgPrice")), 1), "be_dist": round((ff(p.get("avgPrice"))/S-1)*100, 1) if ff(p.get("avgPrice")) else None})
    if eth_spot:
        # спот ETH в кошельке (куплен через ETHUSDC / ETHUSDT): средняя цена — по нетто-покупкам за 7 дней
        def _sp():
            fills = []
            for sym in ("ETHUSDC", "ETHUSDT"):
                try: fills += [(sym, x) for x in sget("/v5/execution/list", {"category":"spot","symbol":sym,"limit":"100"})["result"]["list"]]
                except Exception: pass
            return fills
        fills = cached("eth_spot_fills", 60, _sp)
        # средняя последних покупок, покрывающих текущий остаток (LIFO по времени)
        need, cq, cv = eth_spot, 0.0, 0.0
        for _, x in sorted([f for f in fills if f[1]["side"] == "Buy"], key=lambda f: -ff(f[1]["execTime"])):
            take = min(ff(x["execQty"]), need - cq)
            if take <= 0: break
            cq += take; cv += take*ff(x["execPrice"])
        avg = cv/cq if cq > 1e-9 else None; bq, sq_ = cq, 0.0
        via = "/".join(sorted(set(s_ for s_, _ in fills))) or "кошелёк"
        book.append({"sym": "ETH спот (" + via + ")", "kind": "спот", "q": round(eth_spot, 4), "avg": round(avg, 2) if avg else None, "mark": round(S, 2), "d1": 1,
                     "dusd": round(eth_spot*S), "g1": 0, "t": 0, "v": 0,
                     "up": round((S - avg)*min(eth_spot, bq - sq_), 2) if avg else None,
                     "be": round(avg, 1) if avg else None, "be_dist": round((avg/S-1)*100, 1) if avg else None})
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

    # ===================== ТРЁХНОГИЙ КАРКАС ETH (промпт 2026-11-10) =====================
    # Нога 1 — проданные дальние путы; нога 2 — шорт инверса ETHUSD до 1:1 (вместе = синтетический проданный колл);
    # нога 3 — купленные OTM-коллы ≤7д против роста; однодневки — проданные коллы ≤1.5д (окупают тету ноги 3).
    inv_pl = [p for p in _pl if p.get("inv")]
    inv_q = sum((ff(p["size"]) if p["side"] == "Buy" else -ff(p["size"])) for p in inv_pl)
    inv_short = max(0.0, -inv_q)
    leg1 = [l for l in far]
    leg3 = [l for l in legs if l["cp"] == "C" and l["q"] > 0 and l["dte"] <= 7]
    one = [l for l in legs if l["cp"] == "C" and l["q"] < 0 and l["dte"] <= 1.6]
    used = set(id(l) for l in leg1 + leg3 + one)
    other = [l for l in legs if id(l) not in used]
    def lrow(l):
        return {"sym": l["sym"], "q": round(l["q"], 4), "K": l["K"], "dte": l["dte"], "avg": round(l["avg"], 2), "mark": round(l["mark"], 2),
                "d": round(l["d"], 4), "dusd": round(l["d"]*S), "t": round(l["t"], 2), "v": round(l["v"], 2), "up": round(l["up"], 2)}
    def lsum(L):
        return {"d": round(sum(l["d"] for l in L), 4), "dusd": round(sum(l["d"] for l in L)*S), "t": round(sum(l["t"] for l in L), 2),
                "v": round(sum(l["v"] for l in L), 2), "up": round(sum(l["up"] for l in L), 2), "q": round(sum(l["q"] for l in L), 4)}
    inv_rows = [{"sym": p["symbol"], "q": round(ff(p["size"])*(1 if p["side"] == "Buy" else -1), 4), "avg": round(ff(p["avgPrice"]), 2),
                 "mark": round(ff(p["markPrice"]), 2), "d": round(ff(p["size"])*(1 if p["side"] == "Buy" else -1), 4),
                 "dusd": round(ff(p["size"])*(1 if p["side"] == "Buy" else -1)*S), "t": 0, "v": 0, "up": round(ff(p["unrealisedPnl"]), 2)} for p in inv_pl]
    lin_rows = [{"sym": p["symbol"], "q": round(ff(p["size"])*(1 if p["side"] == "Buy" else -1), 4), "avg": round(ff(p.get("avgPrice")), 2),
                 "mark": round(ff(p.get("markPrice")), 2), "d": round(ff(p["size"])*(1 if p["side"] == "Buy" else -1), 4),
                 "dusd": round(ff(p["size"])*(1 if p["side"] == "Buy" else -1)*S), "t": 0, "v": 0, "up": round(ff(p.get("unrealisedPnl")), 2)} for p in _pl if not p.get("inv") and ff(p["size"]) > 0]
    def fsum(R): return {"d": round(sum(r["d"] for r in R), 4), "dusd": round(sum(r["dusd"] for r in R)), "t": 0, "v": 0, "up": round(sum(r["up"] for r in R), 2), "q": round(sum(r["q"] for r in R), 4)}
    nq = fh["naked_q"]; dn = fh["d_naked"]
    synth_q = round(min(inv_short, nq), 4)
    # 2. резерв: сколько USDT держать, чтобы IM/Equity ≤ 50% (IM без пересчёта при стрессе)
    reserve = {"im_load": round(im/eq*100, 1) if eq else None, "need_now": round(max(0.0, 2*im - eq), 2),
               "stress": [{"mv": m, "S": round(S*(1+m/100)), "pnl": spnl(m/100, 0.2 if m < 0 else 0.1),
                           "need": round(max(0.0, 2*im - (eq + spnl(m/100, 0.2 if m < 0 else 0.1))), 2),
                           "room": round(eq + spnl(m/100, 0.2 if m < 0 else 0.1) - mm, 2)} for m in (-30, -20, -10, 10, 20, 30)]}
    # 5. хедж-калькулятор: шорт фьючей (линейные + инверс) против голых путов
    fut_d = _lin + inv_q            # дельта всех фьючей/перпов, ETH (шорт < 0)
    h_now = round(-fut_d, 4)        # текущий шорт (если < 0 — это нетто-лонг)
    IMR = 0.10                      # ориентир IM на 1 ETH инверса, доля цены (консервативно; в PM против путов фактически меньше)
    hedge = {"h_now": h_now, "inv_q": round(inv_q, 4), "lin_q": round(_lin, 4), "h_delta": dn, "h_full": nq,
             "pos_pct": round(h_now/nq*100) if nq else None,
             "inv_by_av": round(av/(S*IMR), 2) if S else None, "inv_to_im50": round(max(0.0, 0.5*eq - im)/(S*IMR), 2) if S else None, "imr": IMR}
    # кандидаты: трёхдневка (2–5 дн) и однодневка (≤1.6 дн), OTM-коллы +2…+10%
    exps = sorted(set(sym.split("-")[1] for sym in tk), key=lambda e: _exp_dt(e))
    def pick(lo, hi):
        c = [e for e in exps if lo <= _T(e)*365 <= hi]
        return c[0] if c else None
    e3 = pick(1.8, 5.5); e1 = pick(0.15, 1.6)
    def calls(e, lo=1.01, hi=1.12):
        out = []
        for sym, x in tk.items():
            p = sym.split("-")
            if p[1] != e or p[3] != "C": continue
            K = ff(p[2])
            if not (S*lo <= K <= S*hi): continue
            out.append({"sym": "-".join(p[:4]), "K": K, "dist": round((K/S-1)*100, 1), "bid": ff(x["bid1Price"]), "ask": ff(x["ask1Price"]),
                        "mark": round(ff(x["markPrice"]), 2), "iv": round(ff(x["markIv"])*100), "d": round(ff(x["delta"]), 3), "th": round(ff(x["theta"]), 3)})
        return sorted(out, key=lambda r: r["K"])
    c3 = calls(e3) if e3 else []; c1 = calls(e1) if e1 else []
    l3 = lsum(leg3); l3c = sum(l["q"] for l in leg3 if l["dte"] >= 1.5)   # прикрытие синтетики считают только трёхдневки (однодневки сгорят завтра)
    need_calls_now = round(max(0.0, synth_q - l3c), 2); need_calls_full = round(max(0.0, nq - l3c), 2)
    calc3 = [dict(r, cost_now=round(r["ask"]*need_calls_now, 2), cost_full=round(r["ask"]*need_calls_full, 2),
                  th_now=round(r["th"]*need_calls_now, 2), th_full=round(r["th"]*need_calls_full, 2)) for r in c3 if r["ask"] > 0][:8]
    # 7. однодневки: объём продажи, чтобы покрыть 10/25/50% теты трёхдневок
    th3 = abs(l3["t"]) if l3["t"] < 0 else 0.0
    cover_q = l3["q"]   # больше этого — проданные однодневки голые
    calc1 = []
    for r in c1:
        if r["bid"] <= 0 or r["th"] >= 0: continue
        row = dict(r, qs=[round(p*th3/abs(r["th"]), 2) if th3 else 0 for p in (0.10, 0.25, 0.50)])
        row["naked"] = [q > cover_q + 1e-9 for q in row["qs"]]
        calc1.append(row)
    calc1 = calc1[:8]
    # 8. скальп синтетического пута (шорт инверса + купленные коллы)
    syn_d = round(inv_q + l3["d"], 4)
    def _scalp():
        try:
            r = sget("/v5/position/closed-pnl", {"category":"linear","symbol":"ETHUSDT","limit":"100"})["result"]["list"]
            return {"pnl7": round(sum(ff(x["closedPnl"]) for x in r), 2), "n": len(r)}
        except Exception as e:
            return {"err": str(e)}
    sc = cached("eth_scalp_pnl", 60, _scalp)
    theta_spent = round(sum((l["avg"] - l["mark"])*l["q"] for l in leg3), 2)
    # 9. вол-сенсор для покупки коллов: IV трёхдневки ATM против RV
    iv3 = None
    if e3:
        arr = [(abs(ff(sym.split("-")[2]) - S), x) for sym, x in tk.items() if sym.split("-")[1] == e3]
        if arr:
            k0 = min(a[0] for a in arr); iv3 = round(st.mean([ff(x["markIv"]) for a0, x in arr if a0 == k0])*100, 1)
    rv7 = _rv7(pub)
    frame = {"leg1": [lrow(l) for l in leg1], "leg1s": lsum(leg1), "inv": inv_rows, "invs": fsum(inv_rows) if inv_rows else None,
             "lin": lin_rows, "lins": fsum(lin_rows) if lin_rows else None,
             "leg3": [lrow(l) for l in leg3], "leg3s": l3, "one": [lrow(l) for l in one], "ones": lsum(one),
             "other": [lrow(l) for l in other], "others": lsum(other),
             "inv_short": round(inv_short, 4), "inv_target": nq, "inv_fill": round(inv_short/nq*100) if nq else None, "synth_q": synth_q,
             "reserve": reserve, "hedge": hedge, "e3": e3, "e1": e1, "calc3": calc3, "need_calls_now": need_calls_now, "need_calls_full": need_calls_full,
             "calc1": calc1, "th3": round(th3, 2), "cover_q": round(cover_q, 4), "l3c": round(l3c, 4),
             "scalp": {"syn_d": syn_d, "perp_to_neutral": round(-syn_d, 4), "pnl7": sc.get("pnl7"), "n": sc.get("n"), "err": sc.get("err"), "theta_spent": theta_spent},
             "vol": {"iv3": iv3, "rv7": rv7, "ok": (iv3 is not None and rv7 is not None and iv3 < rv7)}}
    warns = []
    if mmp > C["mm_max"]: warns.append(f"MM {mmp}% > {C['mm_max']}% — новые продажи не открывать")
    if tot_naked_notional > 0: warns.append(f"Голые дальние путы: номинал ${tot_naked_notional:,.0f} — макс. убыток не ограничен (см. «крыло» в таблице)")
    if eq and tot_def > C["maxloss_budget"]*eq: warns.append(f"Макс. убыток спредов ${tot_def:.0f} > {int(C['maxloss_budget']*100)}% эквити")
    if hedge_theta < 0 and theta_far > 0 and -hedge_theta > 0.5*theta_far: warns.append(f"Тактические хеджи съедают {(-hedge_theta/theta_far*100):.0f}% теты дальних шортов")
    return {"t": time.strftime("%H:%M:%S"), "S": round(S, 2), "eq": round(eq, 2), "mm": mmp, "im": imp, "buf": buf, "av": round(av, 2), "perp": perp, "spot": round(eth_spot, 4),
            "groups": groups, "tot_def": round(tot_def, 1), "naked_notional": round(tot_naked_notional), "theta_far": round(theta_far, 2),
            "vega_far": round(vega_far, 2), "prem_far": round(prem_far, 2), "budget": round(C["maxloss_budget"]*eq, 0),
            "hedges": [[l["sym"], l["q"], l["avg"], round(l["mark"],2), l["dte"], round(-l["mark"]*l["q"]/max(l["dte"], 0.25),2)] for l in hedges], "hedge_theta": round(hedge_theta, 2),
            "stress": stress, "shocks": [int(s*100) for s in shocks], "cands": cands[:10], "fh": fh, "book": book, "liq": liq, "frame": frame,
            # греки как в конструкторе Bybit: в монетах ETH (Δ — штук ETH, Γ — Δ на $1, Θ — $/день, Vega — $ на 1 п.п. IV)
            "bg": {"d_opt": round(sum(l["d"] for l in legs), 4), "d_lin": round(_lin, 4), "d_inv": round(perp - _lin, 4), "d_spot": round(eth_spot, 4),
                   "g": round(sum(l["g"] for l in legs), 5), "t": round(sum(l["t"] for l in legs), 4), "v": round(sum(l["v"] for l in legs), 4)},
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
