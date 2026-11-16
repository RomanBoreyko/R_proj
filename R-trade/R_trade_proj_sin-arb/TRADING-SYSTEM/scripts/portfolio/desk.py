"""Вкладка «Деск»: текущее положение портфеля — ETH и спред SOXL/SOXS. ТОЛЬКО ЧТЕНИЕ.

Собирает на одном экране то, на что Роман смотрит при работе (по сессии 15–16.11.2026):
  счёт: эквити, IM/MM, запас до ликвидации (эквити − MM), займ USDT;
  маржа по монетам из /v5/asset/portfolio-margin (MM, худший сценарий цена×IV, строка сценариев);
  дельта по монетам и готовые хедж-ордера перпом (ETH; SOX с бетой SOXS к SOXL; прочие монеты);
  ETH: ноги (Δ, вега, тета, бид/аск), «закрыть → маржа Bybit ±$», стресс цена×IV (Блэк–Шоулз),
       хвост проданных путов по датам экспираций (когда купленные истекают раньше проданных);
  SOX: ноги, Δ в SOXL-эквиваленте с β, псевдо-стренгл пары перпов (Γ$, распад/сутки, безубыток, VR), фандинг;
  тета по дням (до 10:00 Киева = 08:00 UTC, при неизменных цене и IV);
  плечи: маржа, займ, чистая дельта, брутто перпов, обязательства по проданным путам.
Маршрут: /desk → desk_snapshot(sget, pub, port). Кэш 12 с.
"""
import time, math, datetime

_C = {}
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}

def ff(x):
    try: return float(x)
    except Exception: return 0.0

def _cached(k, ttl, fn):
    v = _C.get(k)
    if v and time.time() - v[0] < ttl: return v[1]
    r = fn(); _C[k] = (time.time(), r); return r

def _N(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def _bs(S, K, T, s, cp):
    if T <= 0 or s <= 0: return max(0.0, (S - K) if cp == "C" else (K - S))
    d1 = (math.log(S / K) + 0.5 * s * s * T) / (s * math.sqrt(T)); d2 = d1 - s * math.sqrt(T)
    return S * _N(d1) - K * _N(d2) if cp == "C" else K * _N(-d2) - S * _N(-d1)

def _exp_ts(code):
    d = int(code[:-5]); m = MON[code[-5:-2]]; y = 2000 + int(code[-2:])
    return datetime.datetime(y, m, d, 8, 0, tzinfo=datetime.timezone.utc).timestamp()

def _beta(pub):
    """β SOXS к SOXL, σ индекса в день, variance ratio — по дневным свечам перпов Bybit (кэш 1 ч)."""
    def f():
        def kl(s):
            return {int(x[0]): ff(x[4]) for x in pub("/v5/market/kline", {"category": "linear", "symbol": s, "interval": "D", "limit": "120"})["result"]["list"]}
        a, b = kl("SOXLUSDT"), kl("SOXSUSDT"); t = sorted(set(a) & set(b))
        ra = [math.log(a[t[i]] / a[t[i - 1]]) for i in range(1, len(t)) if a[t[i - 1]] > 0 and a[t[i]] > 0]
        rb = [math.log(b[t[i]] / b[t[i - 1]]) for i in range(1, len(t)) if b[t[i - 1]] > 0 and b[t[i]] > 0]
        n = min(len(ra), len(rb)); ra, rb = ra[-n:], rb[-n:]
        if n < 20: return {"beta": -0.94, "sig": 0.031, "vr": {}, "n": n}
        va = sum(x * x for x in ra) / n; cov = sum(x * y for x, y in zip(ra, rb)) / n
        ix = [x / 3 for x in ra]; v1 = sum(x * x for x in ix) / n; vr = {}
        for k in (2, 5, 10):
            bl = [sum(ix[i:i + k]) for i in range(0, n - k + 1, k)]
            if bl and v1 > 0: vr[k] = round(sum(x * x for x in bl) / len(bl) / k / v1, 2)
        return {"beta": round(cov / va, 3) if va else -0.94, "sig": round(math.sqrt(v1), 4), "vr": vr, "n": n}
    try: return _cached("beta", 3600, f)
    except Exception: return {"beta": -0.94, "sig": 0.031, "vr": {}, "n": 0}

def _xbeta(pub):
    """β ETH к SOXL (и обратно), корреляция — дневные (120) и часовые (720) свечи перпов Bybit. Кэш 1 ч."""
    def f():
        out = {}
        for lab, iv, n in (("day", "D", "120"), ("hour", "60", "720")):
            def kl(s):
                return {int(x[0]): ff(x[4]) for x in pub("/v5/market/kline", {"category": "linear", "symbol": s, "interval": iv, "limit": n})["result"]["list"]}
            a, b = kl("ETHUSDT"), kl("SOXLUSDT"); t = sorted(set(a) & set(b))
            re, rl = [], []
            for i in range(1, len(t)):
                if a[t[i - 1]] > 0 and a[t[i]] > 0 and b[t[i - 1]] > 0 and b[t[i]] > 0:
                    re.append(math.log(a[t[i]] / a[t[i - 1]])); rl.append(math.log(b[t[i]] / b[t[i - 1]]))
            m = len(re)
            if m < 20: continue
            me, ml = sum(re) / m, sum(rl) / m
            ve = sum((x - me) ** 2 for x in re) / m; vl = sum((x - ml) ** 2 for x in rl) / m
            cv = sum((x - me) * (y - ml) for x, y in zip(re, rl)) / m
            out[lab] = {"eth_on_soxl": round(cv / vl, 3) if vl else None, "soxl_on_eth": round(cv / ve, 3) if ve else None,
                        "corr": round(cv / math.sqrt(ve * vl), 3) if ve and vl else None, "n": m,
                        "sd_eth": round(math.sqrt(ve) * 100, 2), "sd_soxl": round(math.sqrt(vl) * 100, 2)}
        return out
    try: return _cached("xbeta", 3600, f)
    except Exception: return {}

def _curve(legs, lin, S0, lo, hi, now, n=121):
    """Кривые P&L книги от цены базового (как в конструкторе Bybit): сегодня, через 30 дней, на последней экспирации.
    Опционы — Блэк–Шоулз по mark IV (после своей экспирации — внутренняя стоимость), lin(S) — перпы/спот. P&L относительно текущей стоимости."""
    if not S0: return None
    legs = [l for l in legs if l["ets"] - now > 24 * 3600]          # ноги, истекающие в ближайшие сутки, не рисуем (как будто уже истекли)
    last = max((l["ets"] for l in legs), default=now)
    def value(S, at):
        v = 0.0
        for l in legs:
            v += l["q"] * _bs(S, l["K"], max(0.0, (l["ets"] - at) / 31557600), max(0.05, l["iv"]), l["cp"])
        return v + lin(S)
    xs = [S0 * (lo + (hi - lo) * i / (n - 1)) for i in range(n)]
    b0 = value(S0, now)
    hz = [("today", now), ("d30", min(now + 30 * 86400, last) if last > now else now), ("exp", last)]
    out = {"x": [round(x, 4) for x in xs], "S0": S0, "exp_label": time.strftime("%d.%m.%y", time.gmtime(last)) if last > now else ""}
    for k, at in hz: out[k] = [round(value(x, at) - b0, 1) for x in xs]
    return out

def _sox_fills(sget, days=7):
    """Мои исполнения SOXL/SOXS за N дней — маркеры на графике пары: [t_сек, 'L'|'S', 'B'|'S', цена, кол-во, мейкер]."""
    out = []; st = str(int((time.time() - days * 86400) * 1000))
    for sym, k in (("SOXLUSDT", "L"), ("SOXSUSDT", "S")):
        cur = ""
        for _ in range(6):
            q = {"category": "linear", "symbol": sym, "startTime": st, "limit": "100"}
            if cur: q["cursor"] = cur
            try: r = sget("/v5/execution/list", q)["result"]
            except Exception: break
            for x in r.get("list", []):
                if x.get("execType", "Trade") != "Trade": continue
                out.append([int(x["execTime"]) // 1000, k, "B" if x["side"] == "Buy" else "S", ff(x["execPrice"]), ff(x["execQty"]), 1 if x.get("isMaker") else 0])
            cur = r.get("nextPageCursor") or ""
            if not cur: break
    return sorted(out)[-600:]

def _sox_orders(sget):
    """Открытые заявки SOXL/SOXS — уровни сетки на графике: [sym 'L'|'S', 'B'|'S', цена, кол-во]."""
    out = []
    for sym, k in (("SOXLUSDT", "L"), ("SOXSUSDT", "S")):
        try:
            for x in sget("/v5/order/realtime", {"category": "linear", "symbol": sym, "limit": "50"})["result"]["list"]:
                if ff(x.get("price")): out.append([k, "B" if x["side"] == "Buy" else "S", ff(x["price"]), ff(x["qty"]) - ff(x.get("cumExecQty"))])
        except Exception:
            pass
    return out

def _pm(sget):
    r = sget("/v5/asset/portfolio-margin", {})["result"]
    out = {"wallet": {k: ff(v) for k, v in (r.get("wallet") or {}).items()}, "coins": {}}
    for a in r.get("assetPnlRange") or []:
        P = []
        for p in a.get("perpPositionPnlRanges") or []: P.append((p["symbolName"], p["pnlRanges"]))
        for ex in a.get("optionExpiryDatePnlRanges") or []:
            for p in ex.get("optionPositionPnlRanges") or []: P.append((p["symbolName"], p["pnlRanges"]))
        tot = a.get("totalPnlRanges", {}).get("ALL", {}).get("pnlRanges") or []
        out["coins"][a["baseCoin"]] = {
            "mm": round(ff(a.get("asset", {}).get("assetMM")), 2), "im": round(ff(a.get("asset", {}).get("assetIM")), 2),
            "cont": round(ff(a.get("contingency", {}).get("contingencyComponents")), 2),
            "move": a.get("maxLossPriceMove"), "iv": a.get("maxLossIvShock"),
            "row": [[ff(p["priceScale"]), round(min(ff(x) for x in p["pnls"]), 1)] for p in tot if p.get("pnls")],
            "P": P}
    return out

def _worst(P, excl=None):
    if not P: return 0.0
    sc = len(P[0][1]); best = 0.0
    for i in range(sc):
        m = max(len(pr[i]["pnls"]) for _, pr in P)
        for j in range(m):
            v = 0.0
            for n, pr in P:
                if n == excl: continue
                pn = pr[i]["pnls"]; v += ff(pn[j] if len(pn) > 1 else pn[0])
            best = min(best, v)
    return best

def _alerts(im_pct, theta_day, thE, thS, fund, room, eq):
    a = []
    if im_pct >= 50: a.append({"lvl": "neg", "txt": "IM %.0f%% — выше 50%%: никаких новых ног, резать плечо" % im_pct})
    elif im_pct >= 40: a.append({"lvl": "warn", "txt": "IM %.0f%% — подходит к 50%%: новые ноги только закрывающие" % im_pct})
    if theta_day <= -10: a.append({"lvl": "neg" if theta_day <= -20 else "warn", "txt": "Тета портфеля $%.1f/сутки — больше $10 (ETH $%.1f, SOX $%.1f, фандинг $%.1f)" % (theta_day, thE, thS, fund)})
    if room < eq * 0.25: a.append({"lvl": "neg", "txt": "Запас до ликвидации $%.0f — меньше 25%% эквити" % room})
    return a


# ===================== Гамма-скальп SOXL/SOXS (16.11.2026) =====================
# Всё в терминах доходности SOXL R. SOXS ≈ P_S·(1+R)^β (β со знаком, ≈ −0.94):
#   Δ$  = dV/dR = Δ_L·S + Δ_S·P_S·β                                  (= «Δ SOX» в шапке)
#   Γ_R = d²V/dR² = Γ_опц·S² + β(β−1)·q_S·P_S (+ опционы SOXS: Γ_S·(β·P_S)² + Δ_S·β(β−1)·P_S)
#   гамма на ход R: ½·Γ_R·R²;  Δ$ меняется на Γ_R·R
#   тета пары перпов (модель): 3×-ETF теряет ровно столько, сколько даёт его выпуклость при реализованной воле:
#   θ_пары = −½·β(β−1)·q_S·P_S·σ²_день  (σ_день = RV 30 дн по часам / √365)
#   безубыточный ход за день R* = √(2·|θ_книги| / Γ_R)
def _rv(pub):
    """RV SOXL (год): час 7 д / 30 дн, день 30 / 120 дн; β SOXS/SOXL по часам за 3 дня; шум хеджа (σ остатка r_S − β·r_L по часам). Кэш 10 мин."""
    def f():
        def cl(sym, iv, n):
            return [ff(k[4]) for k in pub("/v5/market/kline", {"category": "linear", "symbol": sym, "interval": iv, "limit": str(n)})["result"]["list"]][::-1]
        def rets(c): return [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i - 1] > 0 and c[i] > 0]
        def sd(r):
            m = sum(r) / len(r); return math.sqrt(sum((x - m) ** 2 for x in r) / len(r))
        hL, hS, dL = cl("SOXLUSDT", "60", 720), cl("SOXSUSDT", "60", 720), cl("SOXLUSDT", "D", 121)
        out = {"h7": round(sd(rets(hL[-169:])) * math.sqrt(8760) * 100), "h30": round(sd(rets(hL)) * math.sqrt(8760) * 100),
               "d30": round(sd(rets(dL[-31:])) * math.sqrt(365) * 100), "d120": round(sd(rets(dL)) * math.sqrt(365) * 100)}
        n = min(len(hL), len(hS)); a, b = hL[-73:], hS[-73:]
        rl, rs = rets(a), rets(b); k = min(len(rl), len(rs)); rl, rs = rl[-k:], rs[-k:]
        ml, ms = sum(rl) / k, sum(rs) / k; vl = sum((x - ml) ** 2 for x in rl); cv = sum((x - ml) * (y - ms) for x, y in zip(rl, rs))
        out["beta_h3"] = round(cv / vl, 3) if vl else None
        bb = _beta(pub)["beta"]
        out["resid_h"] = sd([y - bb * x for x, y in zip(rl, rs)])      # σ остатка хеджа за час (доля)
        return out
    try: return _cached("rv", 600, f)
    except Exception: return {}

def _us_session(now):
    """Сессия NYSE Arca (SOXL/SOXS): открыта 9:30–16:00 ET, премаркет 4:00–9:30, постмаркет 16:00–20:00; праздники не учитываются."""
    u = datetime.datetime.fromtimestamp(now, datetime.timezone.utc); y = u.year
    def nth_sun(month, n):
        d = datetime.datetime(y, month, 1, tzinfo=datetime.timezone.utc)
        return d + datetime.timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))
    off = 4 if nth_sun(3, 2).replace(hour=7) <= u < nth_sun(11, 1).replace(hour=6) else 5
    et = u - datetime.timedelta(hours=off); wd = et.weekday(); m = et.hour * 60 + et.minute
    st = "выходные" if wd >= 5 else ("открыта" if 570 <= m < 960 else ("премаркет" if 240 <= m < 570 else ("постмаркет" if 960 <= m < 1200 else "закрыта")))
    nxt = et.replace(hour=9, minute=30, second=0, microsecond=0)
    if wd >= 5 or m >= 570: nxt += datetime.timedelta(days=1)
    while nxt.weekday() >= 5: nxt += datetime.timedelta(days=1)
    cls = et.replace(hour=16, minute=0, second=0, microsecond=0)
    return {"state": st, "et": et.strftime("%a %H:%M ET"), "to_open_h": round((nxt - et).total_seconds() / 3600, 1),
            "to_close_h": round((cls - et).total_seconds() / 3600, 1) if st == "открыта" else None}

def _sox_pnl(sget):
    """Учёт по перпам SOXL/SOXS за 7 дней и за сегодня (с 00:00 UTC): closed PnL Bybit, комиссии и фандинг из исполнений. Кэш 60 с."""
    def f():
        st = int((time.time() - 7 * 86400) * 1000)
        d0 = int(datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        o = {"closed_7d": 0.0, "closed_today": 0.0, "n_close_7d": 0, "fee_7d": 0.0, "fee_today": 0.0, "fund_7d": 0.0, "n_trade_7d": 0, "n_trade_today": 0}
        for sym in ("SOXLUSDT", "SOXSUSDT"):
            for path, kind in (("/v5/position/closed-pnl", "c"), ("/v5/execution/list", "e")):
                cur = ""
                for _ in range(6):
                    q = {"category": "linear", "symbol": sym, "startTime": str(st), "limit": "100"}
                    if cur: q["cursor"] = cur
                    try: r = sget(path, q)["result"]
                    except Exception: break
                    for x in r.get("list", []):
                        if kind == "c":
                            v = ff(x.get("closedPnl")); t = int(x.get("updatedTime") or x.get("createdTime") or 0)
                            o["closed_7d"] += v; o["n_close_7d"] += 1
                            if t >= d0: o["closed_today"] += v
                        else:
                            fee = ff(x.get("execFee")); t = int(x.get("execTime") or 0); et = x.get("execType", "Trade")
                            if et == "Funding": o["fund_7d"] -= fee
                            elif et == "Trade":
                                o["fee_7d"] -= fee; o["n_trade_7d"] += 1
                                if t >= d0: o["fee_today"] -= fee; o["n_trade_today"] += 1
                    cur = r.get("nextPageCursor") or ""
                    if not cur: break
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in o.items()}
    try: return _cached("soxpnl", 60, f)
    except Exception as e: return {"err": str(e)}

def _gamma(sget, pub, now, sox_legs, sox_perps, pxL, pxS, Bsign, soxEq, fund_sox, rowsL):
    rv = _rv(pub); b = Bsign; c2 = b * (b - 1)
    qS = sum(p["q"] for p in sox_perps if p["sym"] == "SOXSUSDT") + ff(rowsL.get("SOXS_spot"))
    g_optL = sum(l["g"] for l in sox_legs if l["base"] == "SOXL") * pxL * pxL
    g_optS = sum(l["g"] * (b * pxS) ** 2 + l["d"] * c2 * pxS for l in sox_legs if l["base"] == "SOXS")
    g_pair = c2 * qS * pxS
    gR = g_optL + g_optS + g_pair
    sig_d = (rv.get("h30") or 0) / 100 / math.sqrt(365)
    th_opt = sum(l["th"] for l in sox_legs); th_pair = -0.5 * g_pair * sig_d ** 2; th_book = th_opt + th_pair + fund_sox
    be = math.sqrt(2 * abs(th_book) / gR) if gR > 0 and th_book < 0 else None
    exp_day = 0.5 * gR * sig_d ** 2 + th_book
    # цепочка опционов SOXL у денег по экспирациям (≤ 35 дн)
    try: tk = _cached("otk:SOXL", 10, lambda: pub("/v5/market/tickers", {"category": "option", "baseCoin": "SOXL"})["result"]["list"])
    except Exception: tk = []
    by = {}
    for x in tk:
        p = x["symbol"].split("-"); by.setdefault(p[1], {}).setdefault(ff(p[2]), {})[p[3]] = x
    chain = []
    for e in sorted(by, key=_exp_ts):
        days = (_exp_ts(e) - now) / 86400
        if days <= 0 or days > 35: continue
        K = min((k for k, v in by[e].items() if "C" in v and "P" in v), key=lambda k: abs(k - pxL), default=None)
        if K is None: continue
        cc, pp = by[e][K]["C"], by[e][K]["P"]
        mk = ff(cc["markPrice"]) + ff(pp["markPrice"]); spr = (ff(cc["ask1Price"]) - ff(cc["bid1Price"])) + (ff(pp["ask1Price"]) - ff(pp["bid1Price"]))
        g = ff(cc["gamma"]) + ff(pp["gamma"]); th = ff(cc["theta"]) + ff(pp["theta"])
        chain.append({"exp": e, "days": round(days, 1), "K": K, "iv": round((ff(cc["markIv"]) + ff(pp["markIv"])) / 2 * 100, 1), "straddle": round(mk, 2),
                      "spr_pct": round(spr / mk * 100, 1) if mk else None, "g1": round(g * pxL * pxL * 0.0001, 4), "th": round(th, 3),
                      "g_per_th": round(g * pxL * pxL / abs(th), 1) if th else None, "delta": round(ff(cc["delta"]) + ff(pp["delta"]), 3)})
    iv_ref = next((c["iv"] for c in chain if c["days"] >= 7), chain[0]["iv"] if chain else None)
    pnl = _sox_pnl(sget)
    resid_day = (rv.get("resid_h") or 0) * math.sqrt(24) * abs(qS * pxS) if rv else None
    return {"gR": round(gR, 1), "g_optL": round(g_optL, 1), "g_optS": round(g_optS, 1), "g_pair": round(g_pair, 1),
            "g5": round(0.5 * gR * 0.0025, 2), "g10": round(0.5 * gR * 0.01, 2),
            "th_opt": round(th_opt, 2), "th_pair": round(th_pair, 2), "th_fund": round(fund_sox, 2), "th_book": round(th_book, 2),
            "be_pct": round(be * 100, 2) if be else None, "rv_day_pct": round(sig_d * 100, 2), "exp_day": round(exp_day, 2),
            "exp_gamma_day": round(0.5 * gR * sig_d ** 2, 2), "rv": {k: v for k, v in rv.items() if k in ("h7", "h30", "d30", "d120", "beta_h3")},
            "iv_ref": iv_ref, "resid_day": round(resid_day, 2) if resid_day is not None else None, "us": _us_session(now),
            "chain": chain, "pnl": pnl, "D0": round(soxEq, 1), "S": pxL, "qS": round(qS, 4)}

def _build(sget, pub, port):
    now = time.time()
    acct = port.get("acct", {}); rows = {r["base"]: r for r in port.get("groups", {}).get("ALL", {}).get("rows", [])}
    pm = _pm(sget); W = pm["wallet"]
    eq = W.get("equity") or ff(acct.get("eq")); mm = W.get("accountMM") or ff(acct.get("mm")); im = W.get("accountIM") or ff(acct.get("im"))
    # --- позиции
    opts = [p for p in sget("/v5/position/list", {"category": "option", "limit": "200"})["result"]["list"] if ff(p["size"])]
    lin = []
    for sc in ("USDT", "USDC"):
        try: lin += [p for p in sget("/v5/position/list", {"category": "linear", "settleCoin": sc, "limit": "200"})["result"]["list"] if ff(p["size"])]
        except Exception: pass
    try: inv = [p for p in sget("/v5/position/list", {"category": "inverse", "limit": "200"})["result"]["list"] if ff(p["size"])]
    except Exception: inv = []
    T = {}
    for b in sorted({p["symbol"].split("-")[0] for p in opts}):
        try:
            for x in _cached("otk:" + b, 10, lambda b=b: pub("/v5/market/tickers", {"category": "option", "baseCoin": b})["result"]["list"]): T[x["symbol"]] = x
        except Exception: pass
    LT = {}
    for s in {p["symbol"] for p in lin} | {"ETHUSDT", "SOXLUSDT", "SOXSUSDT"}:
        try: LT[s] = _cached("ltk:" + s, 10, lambda s=s: pub("/v5/market/tickers", {"category": "linear", "symbol": s})["result"]["list"][0])
        except Exception: pass
    legs = []
    for p in opts:
        s = p["symbol"]; parts = s.split("-"); b, e, k, cp = parts[0], parts[1], parts[2], parts[3]; t = T.get(s, {})
        q = ff(p["size"]) * (1 if p["side"] == "Buy" else -1)
        legs.append({"base": b, "sym": s.replace("-USDT", ""), "q": q, "K": ff(k), "cp": cp, "exp": e, "ets": _exp_ts(e),
                     "S": ff(t.get("underlyingPrice")) or ff(rows.get(b, {}).get("px")), "iv": ff(t.get("markIv")),
                     "mark": round(ff(p["markPrice"]), 4), "avg": round(ff(p["avgPrice"]), 4), "bid": ff(t.get("bid1Price")), "ask": ff(t.get("ask1Price")),
                     "d": round(ff(p.get("delta")), 4), "g": round(ff(p.get("gamma")), 5), "v": round(ff(p.get("vega")), 3), "th": round(ff(p.get("theta")), 3),
                     "upl": round(ff(p.get("unrealisedPnl")), 2), "val": round(q * ff(p["markPrice"]), 2)})
    perps = []
    for p in lin + inv:
        q = ff(p["size"]) * (1 if p["side"] == "Buy" else -1); s = p["symbol"]; lt = LT.get(s, {})
        fr = ff(lt.get("fundingRate")); fi = ff(lt.get("fundingIntervalHour")) or 8; mk = ff(p["markPrice"])
        is_inv = s.endswith("USD")
        notional = q if is_inv else q * mk
        perps.append({"sym": s, "q": q, "avg": round(ff(p["avgPrice"]), 2), "mark": mk, "usd": round(notional, 2),
                      "upl": round(ff(p.get("unrealisedPnl")) * (mk if is_inv else 1), 2), "real": round(ff(p.get("cumRealisedPnl")) * (mk if is_inv else 1), 2),
                      "fund": round(fr * 100, 4), "fund_day": round(-notional * fr * 24 / fi, 2), "inv": is_inv})
    # --- маржа по монетам
    margin = []
    for c, v in sorted(pm["coins"].items(), key=lambda kv: -kv[1]["mm"]):
        if v["mm"] < 0.5: continue
        margin.append({"coin": c, "mm": v["mm"], "im": v["im"], "cont": v["cont"], "move": v["move"], "iv": v["iv"], "row": v["row"],
                       "worst": round(_worst(v["P"]), 1)})
    worst_sum = round(sum(m["worst"] for m in margin), 1)
    # --- дельты и хеджи
    B = _beta(pub); beta = abs(B["beta"]) or 0.94
    eth = rows.get("ETH", {}); pxE = ff(eth.get("px")) or ff(LT.get("ETHUSDT", {}).get("markPrice"))
    dE = ff(eth.get("true")); pxL = ff(LT.get("SOXLUSDT", {}).get("markPrice")) or ff(rows.get("SOXL", {}).get("px"))
    pxS = ff(LT.get("SOXSUSDT", {}).get("markPrice")) or ff(rows.get("SOXS", {}).get("px"))
    dL = ff(rows.get("SOXL", {}).get("true")); qS = ff(rows.get("SOXS", {}).get("true"))
    soxEq = dL * pxL - qS * pxS * beta
    todo = []
    if abs(dE) >= 0.1 and pxE:
        todo.append({"lvl": "warn" if abs(dE) < 0.3 else "neg", "txt": "ETHUSDT %s %.2f — дельта ETH %+.2f ($%+.0f)" % ("Buy" if dE < 0 else "Sell", abs(dE), dE, dE * pxE)})
    if abs(soxEq) >= 40 and pxL:
        todo.append({"lvl": "warn" if abs(soxEq) < 100 else "neg", "txt": "SOXLUSDT %s %.2f — дельта SOX $%+.0f в SOXL-экв. (с SOXS × β %.2f)" % ("Buy" if soxEq < 0 else "Sell", abs(soxEq) / pxL, soxEq, beta)})
    for b, r in rows.items():
        if b in ("ETH", "SOXL", "SOXS"): continue
        if abs(ff(r.get("dusd"))) >= 50 and ff(r.get("px")):
            todo.append({"lvl": "", "txt": "%sUSDT %s %.4g — дельта %s $%+.0f" % (b, "Buy" if ff(r["dusd"]) < 0 else "Sell", abs(ff(r["true"])), b, ff(r["dusd"]))})
    # экспирации ближайших 36 ч
    soon = {}
    for l in legs:
        if 0 < l["ets"] - now < 36 * 3600: soon.setdefault((l["base"], l["exp"]), []).append(l)
    for (b, e), L in soon.items():
        dd = sum(l["d"] for l in L); hrs = (L[0]["ets"] - now) / 3600
        sym = {"ETH": "ETHUSDT"}.get(b, b + "USDT")
        todo.append({"lvl": "warn", "txt": "%s %s экспирирует через %.0f ч (10:00 Киев): %s; их дельта %+.2f уйдёт → после экспирации %s %s ~%.2f" % (
            b, e, hrs, ", ".join("%s ×%g" % (l["sym"].split("-", 2)[-1], l["q"]) for l in L), dd, sym, "Sell" if dd < 0 else "Buy", abs(dd))})
    room = eq - mm; im_pct = im / eq * 100 if eq else 0
    pass   # предупреждения по IM — в alerts (ниже)
    # --- ETH: ноги, «закрыть → маржа», стресс, хвост
    pe = pm["coins"].get("ETH", {}); baseW = _worst(pe.get("P") or [])
    eth_legs = [l for l in legs if l["base"] == "ETH"]
    for l in eth_legs:
        nm = l["sym"] + "-USDT"
        l["dmm"] = round(_worst(pe.get("P") or [], nm) - baseW, 1) if pe else None   # + освободит, − добавит
        l["exit"] = round(l["q"] * (l["bid"] if l["q"] > 0 else l["ask"]), 2) if (l["bid"] if l["q"] > 0 else l["ask"]) else None
        l["tail"] = l["q"] < 0 and l["cp"] == "P"
    eth_perps = [p for p in perps if p["sym"].startswith("ETH")]
    spotE = ff(eth.get("spot"))
    def eth_book(S, dv):
        v = 0.0
        for l in eth_legs:
            Tt = (l["ets"] - now) / 31557600
            if Tt <= 0: continue
            v += l["q"] * _bs(S, l["K"], Tt, max(0.05, l["iv"] + dv), l["cp"])
        for p in eth_perps: v += p["q"] * (S / pxE - 1) if p["inv"] else p["q"] * (S - pxE)
        return v + spotE * (S - pxE)
    stress = []
    if pxE and eth_legs:
        b0 = eth_book(pxE, 0.0)
        for mv in (-0.7, -0.5, -0.4, -0.3, -0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2):
            stress.append({"mv": mv, "S": round(pxE * (1 + mv)), "v": [round(eth_book(pxE * (1 + mv), dv) - b0) for dv in (-0.15, 0.0, 0.15, 0.30)]})
    tail = []
    puts = [l for l in eth_legs if l["cp"] == "P"]
    for e in sorted({l["ets"] for l in puts}):
        alive = [l for l in puts if l["ets"] > e]
        lo = sum(l["q"] for l in alive if l["q"] > 0); sh = -sum(l["q"] for l in alive if l["q"] < 0)
        minK = min((l["K"] for l in alive if l["q"] < 0), default=None)
        pay = lambda S: round(sum(l["q"] * max(0.0, l["K"] - S) for l in alive))
        tail.append({"after": time.strftime("%d.%m.%y", time.gmtime(e)), "long": round(lo, 2), "short": round(sh, 2),
                     "naked": round(max(0.0, sh - lo), 2), "minK": minK, "pay50": pay(pxE * 0.5), "pay70": pay(pxE * 0.3)})
    short_notional = round(sum(-l["q"] * l["K"] for l in puts if l["q"] < 0))
    # шорт инверса ETHUSD под проданные путы. В $ инверс-шорт N контрактов ($1) даёт N·(1 − S/S0): при ETH → 0 ровно $N.
    # Голые путы на нуле теряют Σq·K (живые ноги; купленные, истёкшие раньше, уже не помогают) → N = max по окнам Σq·K
    # закрывает хвост на экспирации при любой цене (при S0 ≥ K шорт на всём пути вниз даёт не меньше убытка путов).
    win = []
    for e in [now] + sorted({l["ets"] for l in puts}):
        alive = [l for l in puts if l["ets"] > e]
        if alive: win.append((round(-sum(l["q"] * l["K"] for l in alive)), e))
    need, need_ts = max(win) if win else (0, now); need = max(0, need); need_now = max(0, win[0][0]) if win else 0
    have = round(max(0.0, -sum(p["q"] for p in eth_perps if p["inv"])))
    cover = {"need": need, "need_now": need_now, "from": time.strftime("%d.%m.%y", time.gmtime(need_ts)) if need_ts > now else "сейчас",
             "have": have, "add": max(0, need - have), "inv_long": round(max(0.0, sum(p["q"] for p in eth_perps if p["inv"]))), "eth": round(need / pxE, 2) if pxE else None, "up10": round(-0.1 * need)}
    # --- SOX
    sox_legs = [l for l in legs if l["base"] in ("SOXL", "SOXS")]
    sox_perps = [p for p in perps if p["sym"] in ("SOXLUSDT", "SOXSUSDT")]
    Ld = sum(p["usd"] for p in sox_perps if p["sym"] == "SOXLUSDT"); Sd = sum(p["usd"] for p in sox_perps if p["sym"] == "SOXSUSDT")
    Npair = max(0.0, Ld) + max(0.0, Sd); sig = B["sig"]
    pseudo = {"N": round(Npair, 2), "gam": {str(x): round(4.5 * Npair * (x / 100) ** 2, 2) for x in (5, 10, 20)},
              "drag": round(-4.5 * Npair * sig * sig, 2), "be": {str(h): round(sig * math.sqrt(h) * 100, 1) for h in (1, 5, 10)},
              "vr": B["vr"], "sig": round(sig * 100, 2), "beta": B["beta"]}
    # β пары (лонг SOXL + лонг SOXS) к SOXL: доходность пары на 1 доходности SOXL; $ на 1% хода SOXL
    pair_beta = round((Ld + Sd * B["beta"]) / (Ld + Sd), 3) if (Ld + Sd) else None
    pair_usd1 = round((Ld + Sd * B["beta"]) / 100, 2)
    qL = sum(p["q"] for p in sox_perps if p["sym"] == "SOXLUSDT"); qSp = sum(p["q"] for p in sox_perps if p["sym"] == "SOXSUSDT")
    sox_lin = lambda S: (qL * (S - pxL) + (qSp * pxS * ((S / pxL) ** B["beta"] - 1) if pxL and pxS else 0.0))
    sox_curve = _curve([l for l in sox_legs if l["base"] == "SOXL"], sox_lin, pxL, 0.4, 1.6, now) if pxL else None
    sox = {"legs": sox_legs, "perps": sox_perps, "eq_usd": round(soxEq, 1), "pxL": pxL, "pxS": pxS, "beta": beta,
           "opt": {k: round(sum(l[k] for l in sox_legs), 3) for k in ("d", "g", "v", "th", "upl", "val")},
           "fund_day": round(sum(p["fund_day"] for p in sox_perps), 2), "pseudo": pseudo,
           "mm": {c: pm["coins"].get(c, {}).get("mm") for c in ("SOXL", "SOXS")},
           "hedge": round(-soxEq / pxL, 2) if pxL else None,
           "pair_beta": pair_beta, "pair_usd1": pair_usd1, "L": round(Ld, 2), "S": round(Sd, 2), "curve": sox_curve,
           "fills": _cached("soxfills", 30, lambda: _sox_fills(sget)), "orders": _sox_orders(sget),
           # из чего сложена дельта SOX: SOXL (перп + опционы + спот) − SOXS (перп + опционы + спот) × |β|, в SOXL-экв.
           "dparts": {"L_perp": round(qL, 4), "L_opt": round(sum(l["d"] for l in sox_legs if l["base"] == "SOXL"), 4),
                      "L_spot": round(ff(rows.get("SOXL", {}).get("spot")), 4), "L_tot": round(dL, 4), "L_usd": round(dL * pxL, 1),
                      "S_perp": round(qSp, 4), "S_opt": round(sum(l["d"] for l in sox_legs if l["base"] == "SOXS"), 4),
                      "S_spot": round(ff(rows.get("SOXS", {}).get("spot")), 4), "S_tot": round(qS, 4), "S_usd": round(-qS * pxS * beta, 1),
                      "shares": round(soxEq / pxL, 3) if pxL else None}}
    try: sox["gamma"] = _gamma(sget, pub, now, sox_legs, sox_perps, pxL, pxS, B["beta"], soxEq, sox["fund_day"], {"SOXS_spot": ff(rows.get("SOXS", {}).get("spot"))})
    except Exception as e: sox["gamma"] = {"err": str(e)}
    eth_lin = lambda S: sum((p["q"] * (S / pxE - 1) if p["inv"] else p["q"] * (S - pxE)) for p in eth_perps) + spotE * (S - pxE)
    eth_curve = _curve(eth_legs, eth_lin, pxE, 0.3, 1.6, now) if pxE else None
    xb = _xbeta(pub)
    # --- тета по дням (до 08:00 UTC)
    st = datetime.datetime.now(datetime.timezone.utc); b0d = st.replace(hour=8, minute=0, second=0, microsecond=0)
    if b0d.timestamp() <= now: b0d += datetime.timedelta(days=1)
    pts = [now] + [(b0d + datetime.timedelta(days=i)).timestamp() for i in range(14)]
    grp = lambda b: "ETH" if b == "ETH" else ("SOX" if b in ("SOXL", "SOXS") else "прочее")
    def val(at, g):
        return sum(l["q"] * _bs(l["S"], l["K"], max(0.0, (l["ets"] - at) / 31557600), l["iv"], l["cp"]) for l in legs if grp(l["base"]) == g and l["S"] and l["iv"])
    theta = []
    for i in range(1, len(pts)):
        rr = {g: round(val(pts[i], g) - val(pts[i - 1], g), 2) for g in ("ETH", "SOX", "прочее")}
        rr["d"] = time.strftime("%d.%m %a", time.localtime(pts[i])); rr["tot"] = round(sum(rr[g] for g in ("ETH", "SOX", "прочее")), 2)
        theta.append(rr)
    theta_tot = {str(dd): round(sum(val(now + dd * 86400, g) - val(now, g) for g in ("ETH", "SOX", "прочее")), 1) for dd in (30, 60, 90)}
    fund_all = round(sum(p["fund_day"] for p in perps), 2)
    # --- плечи
    stable = port.get("stable") or {}
    net = []
    for b, r in rows.items():
        if abs(ff(r.get("dusd"))) >= 3: net.append({"b": b, "true": round(ff(r.get("true")), 4), "usd": round(ff(r.get("dusd")), 1), "x": round(abs(ff(r.get("dusd"))) / eq, 2) if eq else None})
    lev = {"im_pct": round(im_pct, 1), "mm_pct": round(mm / eq * 100, 1) if eq else None, "room": round(room, 1), "room_x": round(eq / mm, 2) if mm else None,
           "usdt": round(ff(stable.get("USDT")), 2), "usdc": round(ff(stable.get("USDC")), 2),
           "gross": round(sum(abs(p["usd"]) for p in perps), 1), "gross_x": round(sum(abs(p["usd"]) for p in perps) / eq, 2) if eq else None,
           "short_puts": short_notional, "short_puts_x": round(short_notional / eq, 1) if eq else None,
           "naked_now": round(max(0.0, -sum(l["q"] for l in puts if l["q"] < 0) - sum(l["q"] for l in puts if l["q"] > 0)), 2),
           "sox_eq": round(soxEq, 1), "net": sorted(net, key=lambda x: -abs(x["usd"]))}
    gap = next((t for t in tail if t["pay50"] < 0), None)
    if gap: todo.append({"lvl": "warn", "txt": "Хвост ETH: после экспирации %s купленные путы уже истекли, проданных больше на %.1f (страйк %s). Выплата на экспирации: ETH −50%% → $%+d, −70%% → $%+d при эквити $%d. Закрыть/перекатить проданные до этой даты" % (gap["after"], gap["naked"], int(gap["minK"]) if gap["minK"] else "—", gap["pay50"], gap["pay70"], eq)})
    # --- маржа: сочетания SOXL+SOXS и SOXL+SOXS+ETH (сценарий по ходу SOXL; SOXS = β·ход, ETH = β ETH/SOXL·ход)
    def _interp(row, m):
        if not row: return 0.0
        if m <= row[0][0]: return row[0][1]
        if m >= row[-1][0]: return row[-1][1]
        for (a, va), (b, vb) in zip(row, row[1:]):
            if a <= m <= b: return va + (vb - va) * (m - a) / (b - a) if b > a else va
        return 0.0
    MB = {m["coin"]: m for m in margin}
    bE = ((xb.get("day") or {}).get("eth_on_soxl")) or 0.0
    if "SOXL" in MB:
        rL = MB["SOXL"]["row"]; rS = MB.get("SOXS", {}).get("row") or []; rE = MB.get("ETH", {}).get("row") or []
        for name, coins, fE in (("SOXL+SOXS", ("SOXL", "SOXS"), 0.0), ("SOXL+SOXS+ETH", ("SOXL", "SOXS", "ETH"), 1.0)):
            row = [[m, round(v + _interp(rS, B["beta"] * m) + fE * _interp(rE, bE * m), 1)] for m, v in rL]
            wv = min(row, key=lambda r: r[1]) if row else [0, 0]
            margin.append({"coin": name, "combo": True, "mm": round(sum(MB[c]["mm"] for c in coins if c in MB), 2),
                           "im": round(sum(MB[c]["im"] for c in coins if c in MB), 2), "cont": round(sum(MB[c]["cont"] for c in coins if c in MB), 2),
                           "worst": wv[1], "move": wv[0], "iv": None, "row": row,
                           "sum_worst": round(sum(MB[c]["worst"] for c in coins if c in MB), 1)})
    # --- тета портфеля в сутки (греки Bybit по всем опционам + фандинг перпов) и опасности
    theta_day = round(sum(l["th"] for l in legs) + fund_all, 2)
    alerts = _alerts(im_pct, theta_day, sum(l["th"] for l in legs if l["base"] == "ETH"), sum(l["th"] for l in legs if l["base"] in ("SOXL", "SOXS")), fund_all, room, eq)
    # --- дельта по позициям и конструкциям: ETH + SOXL + SOXS (в $; SOXS ещё и в SOXL-экв. через β)
    dpos = []
    def addp(grp, name, units, px, eqf=1.0):
        dpos.append({"grp": grp, "name": name, "d": round(units, 4), "usd": round(units * px, 1), "eq": round(units * px * eqf, 1)})
    for l in sorted(eth_legs, key=lambda l: l["ets"]): addp("ETH " + l["exp"], l["sym"], l["d"], pxE)
    for p in eth_perps: addp("ETH перп/спот", p["sym"], (p["q"] / pxE) if p["inv"] else p["q"], pxE)
    if abs(spotE) > 1e-6: addp("ETH перп/спот", "спот ETH", spotE, pxE)
    for l in sorted([l for l in sox_legs if l["base"] == "SOXL"], key=lambda l: l["ets"]): addp("SOXL " + l["exp"], l["sym"], l["d"], pxL)
    if qL: addp("SOXL перп/спот", "SOXLUSDT", qL, pxL)
    if ff(rows.get("SOXL", {}).get("spot")): addp("SOXL перп/спот", "спот SOXL", ff(rows["SOXL"]["spot"]), pxL)
    for l in [l for l in sox_legs if l["base"] == "SOXS"]: addp("SOXS " + l["exp"], l["sym"], l["d"], pxS, -beta)
    if qSp: addp("SOXS перп/спот", "SOXSUSDT", qSp, pxS, -beta)
    if ff(rows.get("SOXS", {}).get("spot")): addp("SOXS перп/спот", "спот SOXS", ff(rows["SOXS"]["spot"]), pxS, -beta)
    groups = []
    for g in dict.fromkeys(p["grp"] for p in dpos):
        G = [p for p in dpos if p["grp"] == g]
        groups.append({"grp": g, "d": round(sum(p["d"] for p in G), 4), "usd": round(sum(p["usd"] for p in G), 1), "eq": round(sum(p["eq"] for p in G), 1)})
    dsum = {"eth_usd": round(sum(p["usd"] for p in dpos if p["grp"].startswith("ETH")), 1),
            "sox_eq": round(sum(p["eq"] for p in dpos if p["grp"].startswith("SOX")), 1)}
    dsum["total"] = round(dsum["eth_usd"] + dsum["sox_eq"], 1)
    return {"t": time.strftime("%H:%M:%S"), "theta_day": theta_day, "alerts": alerts, "dpos": dpos, "dgroups": groups, "dsum": dsum, "acct": {"eq": round(eq, 2), "im": round(im, 2), "mm": round(mm, 2), "im_pct": round(im_pct, 1), "mm_pct": lev["mm_pct"],
                                                     "room": round(room, 1), "avail": round(W.get("availableBalance", ff(acct.get("av"))), 2)},
            "todo": todo, "margin": margin, "worst_sum": worst_sum,
            "eth": {"px": pxE, "delta": round(dE, 4), "dusd": round(dE * pxE, 1), "spot": spotE, "legs": eth_legs, "perps": eth_perps,
                    "theta": round(sum(l["th"] for l in eth_legs), 2), "vega": round(sum(l["v"] for l in eth_legs), 2),
                    "mm": pe.get("mm"), "move": pe.get("move"), "iv": pe.get("iv"), "stress": stress, "tail": tail, "short_notional": short_notional, "cover": cover,
                    "curve": eth_curve},
            "sox": sox, "xbeta": xb, "theta": theta, "theta_tot": theta_tot, "fund_day": fund_all, "lev": lev}

def desk_snapshot(sget, pub, port):
    v = _C.get("desk")
    if v and time.time() - v[0] < 12: return v[1]
    r = _build(sget, pub, port); _C["desk"] = (time.time(), r)
    return r

def desk_live(sget, pub):
    """Плитки шапки раз в 2 с: эквити/IM/MM (wallet-balance), цены, дельты ETH и SOX по живым позициям (дельта опционов — греки Bybit),
    тета в сутки, предупреждения. Спот, β и фандинг берутся из последнего полного снимка (/desk, 15 с). Кэш 1.5 с."""
    v = _C.get("live")
    if v and time.time() - v[0] < 1.5: return v[1]
    full = (_C.get("desk") or (0, None))[1]
    if not full: return {"wait": 1}
    from concurrent.futures import ThreadPoolExecutor
    def pos(q):
        try: return [p for p in sget("/v5/position/list", dict(q, limit="200"))["result"]["list"] if ff(p["size"])]
        except Exception: return []
    def tk(b):
        try: return ff(pub("/v5/market/tickers", {"category": "linear", "symbol": b + "USDT"})["result"]["list"][0]["markPrice"])
        except Exception: return 0.0
    with ThreadPoolExecutor(8) as ex:   # 8 запросов параллельно (~0.5 с вместо ~3 с подряд)
        fw = ex.submit(lambda: sget("/v5/account/wallet-balance", {"accountType": "UNIFIED"})["result"]["list"][0])
        fo = ex.submit(pos, {"category": "option"})
        fl = [ex.submit(pos, {"category": "linear", "settleCoin": sc}) for sc in ("USDT", "USDC")]
        fi = ex.submit(pos, {"category": "inverse"})
        fp = {b: ex.submit(tk, b) for b in ("ETH", "SOXL", "SOXS")}
        wb = fw.result(); opts = fo.result(); lin = fl[0].result() + fl[1].result(); inv = fi.result(); px = {b: f.result() for b, f in fp.items()}
    eq = ff(wb.get("totalEquity")); im = ff(wb.get("totalInitialMargin")); mm = ff(wb.get("totalMaintenanceMargin"))
    od = {}; th = {}
    for p in opts:
        b = p["symbol"].split("-")[0]; od[b] = od.get(b, 0.0) + ff(p.get("delta")); th[b] = th.get(b, 0.0) + ff(p.get("theta"))
    pq = {}
    for p in lin + inv:
        s = p["symbol"]; q = ff(p["size"]) * (1 if p["side"] == "Buy" else -1)
        b = s[:-4] if s.endswith(("USDT", "USDC")) else (s[:-3] if s.endswith("USD") else s.split("-")[0])
        if s.endswith("USD"): q = q / (ff(p["markPrice"]) or 1)      # инверс: контракты в $ → монеты
        pq[b] = pq.get(b, 0.0) + q
    e, x = full["eth"], full["sox"]; dp = x.get("dparts") or {}; beta = x.get("beta") or 0.94
    pxE, pxL, pxS = px["ETH"] or e["px"], px["SOXL"] or x["pxL"], px["SOXS"] or x["pxS"]
    dE = od.get("ETH", 0.0) + pq.get("ETH", 0.0) + ff(e.get("spot"))
    dL = od.get("SOXL", 0.0) + pq.get("SOXL", 0.0) + ff(dp.get("L_spot")); dS = od.get("SOXS", 0.0) + pq.get("SOXS", 0.0) + ff(dp.get("S_spot"))
    Lusd, Susd = dL * pxL, -dS * pxS * beta; soxEq = Lusd + Susd
    fund = ff(full.get("fund_day")); thE = th.get("ETH", 0.0); thS = th.get("SOXL", 0.0) + th.get("SOXS", 0.0)
    theta_day = round(sum(th.values()) + fund, 2); room = eq - mm; im_pct = im / eq * 100 if eq else 0
    r = {"t": time.strftime("%H:%M:%S"),
         "acct": {"eq": round(eq, 2), "im": round(im, 2), "mm": round(mm, 2), "im_pct": round(im_pct, 1), "mm_pct": round(mm / eq * 100, 1) if eq else None,
                  "room": round(room, 1), "avail": round(ff(wb.get("totalAvailableBalance")), 2)},
         "eth": {"px": pxE, "delta": round(dE, 4), "dusd": round(dE * pxE, 1), "theta": round(thE, 2)},
         "sox": {"pxL": pxL, "pxS": pxS, "eq_usd": round(soxEq, 1), "hedge": round(-soxEq / pxL, 2) if pxL else None,
                 "opt_th": round(thS, 3), "L_usd": round(Lusd, 1), "S_usd": round(Susd, 1), "shares": round(soxEq / pxL, 3) if pxL else None},
         "dsum": {"eth_usd": round(dE * pxE, 1), "sox_eq": round(soxEq, 1), "total": round(dE * pxE + soxEq, 1)},
         "theta_day": theta_day, "alerts": _alerts(im_pct, theta_day, thE, thS, fund, room, eq)}
    _C["live"] = (time.time(), r)
    return r
