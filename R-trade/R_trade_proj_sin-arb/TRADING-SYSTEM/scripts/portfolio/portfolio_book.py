# -*- coding: utf-8 -*-
"""Вкладка «Портфель»: те же блоки, что были в «ETH книге» (плитки, вола, спот+Earn+истинная Δ,
куплено/продано, payoff, позиции с греками) — для всего портфеля и по группам активов.
Только чтение API. Вызывается из book_dashboard.py: portfolio_snapshot(sget, pub, earn_fn)."""
import math, time, datetime
from collections import deque
from tail_pairs import ff, _T, _N, _bs, cached, CRYPTO, inverse_positions, inv_base

# группа -> базовые активы; первый — «главный» (ось payoff и датчик волы)
GROUPS = {"ETH": ["ETH"], "MNT": ["MNT"], "SOL": ["SOL"], "BTC": ["BTC"], "QQQ": ["QQQ"],
          "GOLD": ["XAUT", "XAU", "XAG", "GDX"]}
GNAME = {"ALL": "Весь портфель", "ETH": "ETH", "MNT": "MNT", "SOL": "SOL", "BTC": "BTC",
         "QQQ": "QQQ", "GOLD": "Золото", "OTHER": "Прочее"}
OPT_BASES = ["ETH", "MNT", "SOL", "BTC", "QQQ", "XAUT"]      # у кого есть опционы на Bybit
STABLE = ("USDT", "USDC")
FLAT_USD = 150          # |Δ$| меньше — считаем нейтральным (подсветка)
PAY_RANGE = (-0.45, 0.25, 140)
VOLH = {}
RND = {"n": 0, "d": 4, "dusd": 1, "g": 5, "g1": 2, "t": 2, "v": 2}               # история ATM IV в сессии по активу (для ранга)

def _grp(base):
    for g, bs in GROUPS.items():
        if base in bs: return g
    return "OTHER"

def _perp_base(sym):
    s = sym.split("-")[0]
    for suf in ("USDT", "USDC", "PERP"):
        if s.endswith(suf) and len(s) > len(suf): return s[:-len(suf)]
    return inv_base(s) or s

def _lin_px(pub):
    def f():
        out = {}
        for t in pub("/v5/market/tickers", {"category": "linear"})["result"]["list"]:
            p = ff(t.get("markPrice")) or ff(t.get("lastPrice"))
            if p > 0: out[t["symbol"]] = p
        return out
    return cached("pb_lin", 5, f)

def _opt_tk(pub, base):
    def f():
        return {t["symbol"]: t for t in pub("/v5/market/tickers", {"category": "option", "baseCoin": base})["result"]["list"]}
    return cached("pb_opt_" + base, 10, f)

def _rv30(pub, base):
    def f():
        kl = pub("/v5/market/kline", {"category": "linear", "symbol": base + "USDT", "interval": "D", "limit": "31"})["result"]["list"]
        kl = sorted(kl, key=lambda c: int(c[0]))
        cl = [ff(c[4]) for c in kl if ff(c[4]) > 0]
        if len(cl) < 6: return 0.0
        r = [math.log(cl[i] / cl[i - 1]) for i in range(1, len(cl))]
        mu = sum(r) / len(r); var = sum((x - mu) ** 2 for x in r) / max(len(r) - 1, 1)
        span = (int(kl[-1][0]) - int(kl[0][0])) / 86400000 or 1
        bars_y = (len(cl) - 1) / span * 365          # TradFi без выходных — годовых баров меньше
        return math.sqrt(var * bars_y) * 100
    return cached("pb_rv_" + base, 600, f)

def _vol(pub, base):
    def f():
        try:
            tk = _opt_tk(pub, base); exps = {}
            for s, t in tk.items():
                pr = s.split("-")
                if len(pr) < 4: continue
                exps.setdefault(pr[1], []).append(t)
            cand = sorted((_T(e) * 365, e) for e in exps if _T(e) * 365 >= 7) or sorted((_T(e) * 365, e) for e in exps)
            dte, E = cand[0]
            und = next((ff(t.get("underlyingPrice")) for t in exps[E] if ff(t.get("underlyingPrice")) > 0), 0)
            Ks = sorted(set(ff(t["symbol"].split("-")[2]) for t in exps[E]))
            Ka = min(Ks, key=lambda k: abs(k - und))
            ivs = [ff(t.get("markIv")) for t in exps[E] if ff(t["symbol"].split("-")[2]) == Ka and ff(t.get("markIv")) > 0]
            iv = sum(ivs) / len(ivs) * 100 if ivs else 0.0
            rv = _rv30(pub, base)
        except Exception as e:
            return {"base": base, "iv": 0, "rv": 0, "prem": 0, "band": "нет данных: " + str(e)[:60], "rank": None, "lo": 0, "hi": 0, "exp": "", "dte": 0}
        h = VOLH.setdefault(base, deque(maxlen=800))
        if iv > 0: h.append(iv)
        lo, hi = (min(h), max(h)) if h else (iv, iv)
        rank = round((iv - lo) / (hi - lo) * 100) if hi > lo else None
        ratio = iv / rv if rv > 0 else 0
        if not ratio:        band = "нет RV"
        elif ratio < 0.9:    band = "ДЁШЕВО — IV ниже реализованной (окно покупать волу)"
        elif ratio < 1.1:    band = "норма — ждать"
        elif ratio < 1.3:    band = "повышенная — присматриваться к продаже"
        else:                band = "ДОРОГО — IV сильно выше реализованной (окно продавать)"
        return {"base": base, "iv": round(iv, 1), "rv": round(rv, 1), "prem": round(iv - rv, 1), "ratio": round(ratio, 2),
                "band": band, "rank": rank, "lo": round(lo, 1), "hi": round(hi, 1), "exp": E, "dte": round(dte, 1), "K": Ka}
    return cached("pb_vol_" + base, 60, f)

BETA_REF = "ETH"
STRESS_MOVES = [-20, -10, -5, 5, 10, 20]
GAM_SHOCKS = [x / 100 for x in range(-15, 16)]     # профиль Δ$/Γ$ по цене: ±15% шагом 1%


def _bsdg(S, K, T, s, cp):
    """Black-Scholes дельта и гамма (без ставки), на 1 опцион."""
    if T <= 0 or s <= 0 or S <= 0 or K <= 0:
        return ((1.0 if S > K else 0.0) if cp == "C" else (-1.0 if S < K else 0.0)), 0.0
    sq = s * math.sqrt(T); d1 = (math.log(S / K) + 0.5 * s * s * T) / sq
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return (_N(d1) if cp == "C" else _N(d1) - 1.0), pdf / (S * sq)


def _gamma_view(pub, A, inst):
    """Блок «Гамма и скальп»: по каждому активу с опционами — греки, безубыточный ход,
    профиль Δ$/Γ$ по цене (BS при текущей markIv, перпы+спот линейно) и гамма по экспирациям."""
    out = []
    for b in sorted(A, key=lambda b: -abs(A[b]["g1"])):
        oi = [i for i in inst if i["base"] == b and i["k"] == "opt"]
        if not oi: continue
        r = A[b]; S = r["px"] or oi[0]["S"]
        lin = sum(i["d"] for i in inst if i["base"] == b and i["k"] == "perp") + r["spot"] + r["earn"]
        prof = []
        for m in GAM_SHOCKS:
            d = lin; g = 0.0
            for i in oi:
                dd, gg = _bsdg(i["S"] * (1 + m), i["K"], i["T"], i["iv"], i["cp"])
                d += i["side"] * i["sz"] * dd; g += i["side"] * i["sz"] * gg
            S1 = S * (1 + m)
            prof.append([round(m * 100), round(S1, 6), round(d * S1, 1), round(g * S1 * S1 * 0.01, 2)])
        ex = {}
        for i in oi:
            e = ex.setdefault(i["exp"], {"exp": i["exp"], "dte": round(i["T"] * 365, 1), "g1": 0.0, "g1l": 0.0, "g1s": 0.0, "t": 0.0, "v": 0.0, "n": 0})
            e["g1"] += i["g1"]; e["g1l" if i["side"] > 0 else "g1s"] += i["g1"]; e["t"] += i["t"]; e["v"] += i["v"]; e["n"] += 1
        exps = sorted(ex.values(), key=lambda e: e["dte"])
        for e in exps:
            for k in ("g1", "g1l", "g1s", "t", "v"): e[k] = round(e[k], 2)
        th, g1 = r["ot"], r["g1"]
        be = math.sqrt(2 * abs(th) / (100 * abs(g1))) * 100 if g1 and th else None     # % дневного хода, при котором ½ΓS²x² = |Θ|
        vol = _vol(pub, b) if b in OPT_BASES else {}
        out.append({"base": b, "px": S, "g": r["og"], "g1": g1, "t": th, "v": r["ov"], "true": r["true"], "dusd": r["dusd"],
                    "be": round(be, 2) if be else None, "iv": vol.get("iv"), "rv": vol.get("rv"),
                    "perp": b + "USDT", "prof": prof, "exps": exps})
    return out

def _betas(pub, bases):
    """β и корреляция 4ч лог-доходностей базы к ETH за 30д. {base: (beta, corr)}; ETH = (1, 1)."""
    def kl(b):
        r = pub("/v5/market/kline", {"category": "linear", "symbol": b + "USDT", "interval": "240", "limit": "180"})["result"]["list"]
        return {int(x[0]): ff(x[4]) for x in r if ff(x[4]) > 0}
    def f():
        out = {BETA_REF: (1.0, 1.0)}
        try: E = kl(BETA_REF)
        except Exception: return out
        for b in bases:
            if b == BETA_REF: continue
            try: X = kl(b)
            except Exception: out[b] = (None, None); continue
            ts = sorted(t for t in X if t in E)
            rx, ry = [], []
            for i in range(1, len(ts)):
                if ts[i] - ts[i - 1] != 14400000: continue          # только соседние 4ч бары (у TradFi есть дыры)
                rx.append(math.log(E[ts[i]] / E[ts[i - 1]])); ry.append(math.log(X[ts[i]] / X[ts[i - 1]]))
            if len(rx) < 30: out[b] = (None, None); continue
            mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
            vx = sum((a - mx) ** 2 for a in rx); vy = sum((a - my) ** 2 for a in ry)
            cv = sum((a - mx) * (c - my) for a, c in zip(rx, ry))
            out[b] = (round(cv / vx, 3), round(cv / math.sqrt(vx * vy), 3)) if vx > 0 and vy > 0 else (None, None)
        return out
    return cached("pb_beta_" + ",".join(sorted(bases)), 1800, f)

def _shock(inst, held, mv):
    """P&L T+0 относительно текущего, если каждая база сдвинута на свой % mv(base) (доли). IV без изменений."""
    tot = 0.0
    for i in inst:
        m = mv(i["base"])
        if not m: continue
        if i["k"] == "perp":
            tot += i["side"] * i["sz"] * i["S"] * m
        else:
            tot += i["side"] * i["sz"] * (_bs(i["S"] * (1 + m), i["K"], i["T"], i["iv"], i["cp"]) - _bs(i["S"], i["K"], i["T"], i["iv"], i["cp"]))
    for b, q, S in held:
        tot += q * S * mv(b)
    return tot


def portfolio_snapshot(sget, pub, earn_fn=None):
    wb = sget("/v5/account/wallet-balance", {"accountType": "UNIFIED"})["result"]["list"][0]
    eq = ff(wb["totalEquity"]); im = ff(wb["totalInitialMargin"]); mm = ff(wb["totalMaintenanceMargin"]); av = ff(wb["totalAvailableBalance"])
    coins = {c["coin"]: ff(c.get("walletBalance")) for c in wb.get("coin", [])}
    opts, cur = [], ""
    for _ in range(5):
        q = {"category": "option", "limit": "200"}
        if cur: q["cursor"] = cur
        r = sget("/v5/position/list", q)["result"]; opts += r["list"]; cur = r.get("nextPageCursor", "")
        if not cur: break
    opts = [o for o in opts if ff(o["size"]) > 0]
    perps = []
    for sc in STABLE:
        perps += [p for p in sget("/v5/position/list", {"category": "linear", "settleCoin": sc, "limit": "200"})["result"]["list"] if ff(p["size"]) > 0]
    perps += inverse_positions(sget)      # ETHUSD и пр.: size уже в монетах (Q/E), uPnL в $
    try: earn = (earn_fn() if earn_fn else {}).get("pos", {}) or {}
    except Exception: earn = {}
    LP = _lin_px(pub)
    px = lambda b: LP.get(b + "USDT") or LP.get(b + "PERP") or 0.0

    # ---------- инструменты ----------
    inst = []        # единый список: опционы и перпы
    for o in opts:
        pr = o["symbol"].split("-")
        if len(pr) < 4: continue
        b = pr[0]; sd = 1 if o["side"] == "Buy" else -1; sz = ff(o["size"])
        t = _opt_tk(pub, b).get(o["symbol"], {})
        S = ff(t.get("underlyingPrice")) or px(b)
        iv = ff(t.get("markIv"))
        d, g = ff(o.get("delta")), ff(o.get("gamma"))
        inst.append({"k": "opt", "sym": o["symbol"].replace("-USDT", ""), "base": b, "grp": _grp(b), "side": sd, "sz": sz,
                     "K": ff(pr[2]), "cp": pr[3], "exp": pr[1], "T": _T(pr[1]), "iv": iv, "S": S,
                     "entry": ff(o.get("avgPrice")), "d": d, "g": g, "t": ff(o.get("theta")), "v": ff(o.get("vega")),
                     "dusd": d * S, "g1": g * S * S * 0.01, "up": ff(o.get("unrealisedPnl"))})
    for p in perps:
        b = _perp_base(p["symbol"]); sd = 1 if p["side"] == "Buy" else -1; sz = ff(p["size"])
        S = ff(p.get("markPrice")) or px(b)
        inst.append({"k": "perp", "sym": p["symbol"] + (" (инв.)" if p.get("inv") else ""), "base": b, "grp": _grp(b), "side": sd, "sz": sz, "S": S,
                     "entry": ff(p.get("avgPrice")), "d": sd * sz, "g": 0.0, "t": 0.0, "v": 0.0,
                     "dusd": sd * sz * S, "g1": 0.0, "up": ff(p.get("unrealisedPnl"))})

    # ---------- по активу ----------
    bases = set(i["base"] for i in inst)
    bases |= {c for c, v in coins.items() if v and c not in STABLE and px(c) > 0}
    bases |= {c for c, v in earn.items() if v and c not in STABLE and px(c) > 0}
    for g, bl in GROUPS.items():            # показываем все активы группы, даже нулевые
        bases |= set(bl)
    A = {}
    for b in bases:
        S = px(b); spot = coins.get(b, 0.0); ea = ff(earn.get(b, 0.0))
        oi = [i for i in inst if i["base"] == b and i["k"] == "opt"]
        pi = [i for i in inst if i["base"] == b and i["k"] == "perp"]
        od = sum(i["d"] for i in oi); pd = sum(i["d"] for i in pi)
        tr = od + pd + spot + ea
        A[b] = {"base": b, "grp": _grp(b), "px": S, "spot": round(spot, 6), "earn": round(ea, 6),
                "od": round(od, 4), "og": round(sum(i["g"] for i in oi), 5), "ot": round(sum(i["t"] for i in oi), 2),
                "ov": round(sum(i["v"] for i in oi), 2), "pd": round(pd, 4), "true": round(tr, 4),
                "dusd": round(tr * S, 1), "g1": round(sum(i["g1"] for i in oi), 2),
                "hedge": round(-tr, 4), "n": len(oi) + len(pi),
                "up": round(sum(i["up"] for i in oi + pi), 2), "active": bool(oi or pi or spot or ea)}

    def group(gk):
        if gk == "ALL": sel = list(inst); bl = sorted([b for b in A if A[b]["active"]], key=lambda b: -abs(A[b]["dusd"]))
        else:
            sel = [i for i in inst if i["grp"] == gk]
            bl = GROUPS.get(gk) or sorted([b for b in A if A[b]["grp"] == "OTHER" and A[b]["active"]], key=lambda b: -abs(A[b]["dusd"]))
        bl = [b for b in bl if b in A]
        main = None if gk == "ALL" else (bl[0] if bl else None)
        Sm = A[main]["px"] if main else 0.0
        rows = [A[b] for b in bl]
        ops = [i for i in sel if i["k"] == "opt"]
        B = {"n": 0, "d": 0.0, "dusd": 0.0, "g": 0.0, "g1": 0.0, "t": 0.0, "v": 0.0}; Sx = dict(B)
        for i in ops:
            k = B if i["side"] > 0 else Sx
            k["n"] += 1
            for f in ("d", "dusd", "g", "g1", "t", "v"): k[f] += i[f]
        rat = lambda a, b: round(abs(a) / abs(b) * 100, 1) if b else 0
        tot = {"dusd": round(sum(r["dusd"] for r in rows), 1), "g1": round(sum(r["g1"] for r in rows), 2),
               "t": round(sum(r["ot"] for r in rows), 2), "v": round(sum(r["ov"] for r in rows), 2),
               "up": round(sum(i["up"] for i in sel), 2),
               "od_perp": round(sum(i["d"] for i in sel if main and i["base"] == main), 4),
               "true": A[main]["true"] if main else None, "g": A[main]["og"] if main else None}
        if gk == "GOLD":
            xp = px("XAU") or Sm
            tot["hedge_xau"] = round(-tot["dusd"] / xp, 4) if xp else None
        # payoff: все инструменты группы на один и тот же %; спот/Earn — от текущей цены
        lo, hi, N = PAY_RANGE; pay = []; now = []
        held = [(r["spot"] + r["earn"], r["px"]) for r in rows if (r["spot"] or r["earn"]) and r["px"]]
        for j in range(N + 1):
            m = lo + (hi - lo) * j / N; e = 0.0; n0 = 0.0
            for i in sel:
                S1 = i["S"] * (1 + m)
                if i["k"] == "perp":
                    v = i["side"] * i["sz"] * (S1 - i["entry"]); e += v; n0 += v
                else:
                    intr = max(S1 - i["K"], 0) if i["cp"] == "C" else max(i["K"] - S1, 0)
                    e += i["side"] * i["sz"] * (intr - i["entry"])
                    n0 += i["side"] * i["sz"] * (_bs(S1, i["K"], i["T"], i["iv"], i["cp"]) - i["entry"])
            for q, S in held:
                e += q * S * m; n0 += q * S * m
            x = round(Sm * (1 + m), 4) if main else round(m * 100, 1)
            pay.append([x, round(e, 1)]); now.append([x, round(n0, 1)])
        def bes(P):
            out = []
            for k in range(1, len(P)):
                y0, y1 = P[k - 1][1], P[k][1]
                if y0 * y1 < 0 or (y1 == 0 and k + 1 < len(P) and y0 * P[k + 1][1] < 0):
                    out.append(round(P[k - 1][0] + (P[k][0] - P[k - 1][0]) * (-y0) / (y1 - y0), 4 if Sm < 10 else 0))
            return sorted(set(out))
        def sk(i): return (list(GNAME).index(i["grp"]) if i["grp"] in GNAME else 99, i["base"], i["k"] != "opt", i["T"] if i["k"] == "opt" else 0, i.get("K", 0))
        pos = [{"grp": GNAME.get(i["grp"], i["grp"]), "sym": i["sym"], "side": "B" if i["side"] > 0 else "S", "sz": round(i["sz"], 6),
                "d": round(i["d"], 4 if i["k"] == "opt" else 6), "dusd": round(i["dusd"], 1), "g": round(i["g"], 5),
                "t": round(i["t"], 2), "v": round(i["v"], 2), "up": round(i["up"], 2),
                "iv": round(i["iv"] * 100, 1) if i["k"] == "opt" else None} for i in sorted(sel, key=sk)]
        vol = None
        if main:
            vb = main if main in OPT_BASES else None
            if vb and (ops or gk in GROUPS): vol = _vol(pub, vb)
        return {"key": gk, "name": GNAME.get(gk, gk), "main": main, "px": round(Sm, 6), "rows": rows, "tot": tot,
                "B": {k: round(v, RND[k]) for k, v in B.items()}, "S": {k: round(v, RND[k]) for k, v in Sx.items()},
                "cov": {"g": rat(Sx["g1"], B["g1"]), "t": rat(Sx["t"], B["t"]), "v": rat(Sx["v"], B["v"])},
                "pay": pay, "now": now, "be": bes(pay), "be_now": bes(now), "pct": main is None,
                "pos": pos, "vol": vol, "n": len(sel)}

    G = {}
    for gk in ["ALL"] + list(GROUPS) + ["OTHER"]:
        G[gk] = group(gk)
    if not G["OTHER"]["n"] and not any(A[b]["active"] for b in A if A[b]["grp"] == "OTHER"):
        G.pop("OTHER")
    # сводка по группам и вола по всем опционным активам — для вида «Весь портфель»
    summ = [{"key": k, "name": g["name"], "dusd": g["tot"]["dusd"], "g1": g["tot"]["g1"], "t": g["tot"]["t"],
             "v": g["tot"]["v"], "up": g["tot"]["up"], "n": g["n"]} for k, g in G.items() if k != "ALL"]
    vols = [_vol(pub, b) for b in OPT_BASES]
    G["ALL"]["summ"] = summ; G["ALL"]["vols"] = vols
    G["ALL"]["gam"] = _gamma_view(pub, A, inst)
    # β к ETH: ETH-эквивалент и стресс «ETH против корзины»
    act = [b for b in A if A[b]["active"] and A[b]["px"] > 0]
    BT = _betas(pub, act)
    brows = []
    for b in act:
        be, co = BT.get(b, (None, None))
        brows.append({"base": b, "dusd": A[b]["dusd"], "beta": be, "corr": co, "crypto": b in CRYPTO,
                      "eq": round(A[b]["dusd"] * be, 1) if be is not None else None})
    brows.sort(key=lambda r: -abs(r["eq"] or 0))
    held = [(b, A[b]["spot"] + A[b]["earn"], A[b]["px"]) for b in act if A[b]["spot"] or A[b]["earn"]]
    bask = lambda b: b != BETA_REF and b in CRYPTO
    scen = [("только ETH (корзина стоит)",                lambda m: (lambda b: m if b == BETA_REF else 0)),
            ("только корзина (ETH стоит)",                lambda m: (lambda b: m if bask(b) else 0)),
            ("ETH против корзины (ETH +m, корзина −m)",   lambda m: (lambda b: m if b == BETA_REF else (-m if bask(b) else 0))),
            ("согласованно по β (ETH m, актив β·m)",      lambda m: (lambda b: m * (BT.get(b, (None,))[0] or 0)))]
    G["ALL"]["beta"] = {"rows": brows,
        "eq_total": round(sum(r["eq"] for r in brows if r["eq"] is not None), 1),
        "eq_crypto": round(sum(r["eq"] for r in brows if r["eq"] is not None and r["crypto"]), 1),
        "basket": sorted(b for b in act if bask(b)),
        "stress": {"moves": STRESS_MOVES,
                   "rows": [{"name": nm, "pnl": [round(_shock(inst, held, fn(m / 100))) for m in STRESS_MOVES]} for nm, fn in scen]}}
    G["ALL"]["tot"]["eq"] = G["ALL"]["beta"]["eq_total"]
    stab = {c: round(coins.get(c, 0.0), 2) for c in STABLE}
    return {"t": time.strftime("%H:%M:%S"), "acct": {"eq": round(eq, 2), "im": round(im, 2), "mm": round(mm, 2), "av": round(av, 2),
            "im_pct": round(im / eq * 100, 1) if eq else 0, "mm_pct": round(mm / eq * 100, 1) if eq else 0,
            "buf": round(eq / mm, 2) if mm else 0}, "stable": stab, "groups": G, "order": list(G), "flat": FLAT_USD}
