# -*- coding: utf-8 -*-
"""Вкладка «Арбитраж» (/arb): мой арбитражный портфель + сканер арбитража Bybit.

  book(port)  — поверх готового /port: остаточный риск (разброс стресс-таблицы), связки
                (кэрри, базис золота, конверсия/реверсия, межперп), открытые ноги, фандинг $/день.
  scan(sget, pub) — паритет (конверсия/реверсия), боксы, cash-and-carry, календарь перп↔фьюч,
                кэрри спот+перп, инверс vs линейный. Мейкерские комиссии, цены — исполнимая сторона
                стакана (покупка по ask, продажа по bid). Ставка = 0 (без дисконтирования).
Только чтение API. sget/pub передаются из book_dashboard.
"""
import re, time, threading
from collections import deque, defaultdict
from tail_pairs import ff, _T, cached, inverse_positions, inv_base, CRYPTO

OPT_BASES = ["ETH", "BTC", "SOL", "MNT", "XAUT", "QQQ"]
BASKET = ["BTC", "ETH", "SOL", "MNT", "XAUT", "XRP", "DOGE"]
FEE_FALLBACK = {"option": 0.0002, "linear": 0.00036, "spot": 0.001}   # мейкер, если fee-rate недоступен
ARB_CFG = {"k_band": 0.30, "min_dte": 0.05, "top": 15, "box_top": 10, "carry_top": 12,
           "carry_min_turnover": 5e6, "scan_ttl": 30}
RISK_H = deque(maxlen=2160)         # история остаточного риска за сессию: [ts, $] (точка раз в 20 с ≈ 12 ч)
BOOK_H = deque(maxlen=2160)         # [ts, eq, im%, mm%, iv ETH, Δ$, Θ, вега, {база: цена}]
_SAMPLE_ERR = {"e": None}
BASKET_X = ("ETH",)                 # что не входит в «корзину» графика 1
_SL = threading.Lock()


def _dte_days(ms):
    return max((ff(ms) / 1000 - time.time()) / 86400, 0.0)


# ---------------------------------------------------------------- комиссии / фандинг
def _fee(sget, cat, key):
    """мейкерская ставка; key = baseCoin для опционов, symbol для linear/spot. → (rate, est)"""
    def f():
        q = {"category": cat}
        q["baseCoin" if cat == "option" else "symbol"] = key
        try:
            l = sget("/v5/account/fee-rate", q)["result"]["list"]
            if l: return (ff(l[0]["makerFeeRate"]), False)
        except Exception:
            pass
        return (FEE_FALLBACK[cat], True)
    return cached("arb_fee_%s_%s" % (cat, key), 3600, f)


def _fund_avg(pub, cat, sym, ih):
    """средняя ставка фандинга за ~7 дней (за период)."""
    def f():
        n = max(int(7 * 24 / (ih or 8)), 3)
        try:
            h = pub("/v5/market/funding/history", {"category": cat, "symbol": sym, "limit": str(min(n, 200))})["result"]["list"]
            v = [ff(x["fundingRate"]) for x in h]
            return sum(v) / len(v) if v else None
        except Exception:
            return None
    return cached("arb_fh_%s_%s" % (cat, sym), 900, f)


def _per_year(rate, ih):
    return rate * (24.0 / (ih or 8)) * 365 * 100           # % годовых


# ---------------------------------------------------------------- сканер
def _scan(sget, pub):
    est = False
    lin = {t["symbol"]: t for t in pub("/v5/market/tickers", {"category": "linear"})["result"]["list"]}
    spot = {t["symbol"]: t for t in pub("/v5/market/tickers", {"category": "spot"})["result"]["list"]}
    try: inv = {t["symbol"]: t for t in pub("/v5/market/tickers", {"category": "inverse"})["result"]["list"]}
    except Exception: inv = {}
    bid = lambda t: ff(t.get("bid1Price")); ask = lambda t: ff(t.get("ask1Price"))
    ih = lambda t: ff(t.get("fundingIntervalHour")) or 8.0

    def lfee(sym):                                   # ставка по перпу базы (у фьючей та же), иначе ETHUSDT
        nonlocal est
        k = sym.split("-")[0]
        r, e = _fee(sget, "linear", k if k in lin else "ETHUSDT"); est |= e; return r

    def sfee(sym):
        nonlocal est
        r, e = _fee(sget, "spot", sym); est |= e; return r

    # ---------- паритет и боксы ----------
    par, box, nscan = [], [], 0
    for b in OPT_BASES:
        try: ol = pub("/v5/market/tickers", {"category": "option", "baseCoin": b})["result"]["list"]
        except Exception: continue
        mo, e = _fee(sget, "option", b); est |= e
        ch = defaultdict(lambda: defaultdict(dict))          # exp -> K -> {C,P}
        for t in ol:
            p = t["symbol"].split("-")
            if len(p) < 4: continue
            if len(p) > 4 and p[4] != "USDT": continue
            ch[p[1]][ff(p[2])][p[3]] = t
        perp = lin.get(b + "USDT")
        for exp, ks in ch.items():
            dte = _T(exp) * 365
            if dte < ARB_CFG["min_dte"]: continue
            fut = lin.get("%sUSDT-%s" % (b, exp))
            if fut and bid(fut) > 0 and ask(fut) > 0:
                hs, hk, Fb, Fa, drag = fut["symbol"], "фьюч", bid(fut), ask(fut), 0.0
            elif perp and bid(perp) > 0:
                hs, hk, Fb, Fa = perp["symbol"], "перп", bid(perp), ask(perp)
                fa = _fund_avg(pub, "linear", perp["symbol"], ih(perp))
                fr = fa if fa is not None else ff(perp.get("fundingRate"))
                drag = fr * (24.0 / ih(perp)) * dte          # доля цены, которую лонг перпа заплатит до экспирации
            else:
                continue
            mf = lfee(hs)
            F = (Fb + Fa) / 2
            good = []
            for K, cp in ks.items():
                C, P = cp.get("C"), cp.get("P")
                if not C or not P or abs(K / F - 1) > ARB_CFG["k_band"]: continue
                ca, cb, pa, pb = ask(C), bid(C), ask(P), bid(P)
                S = ff(C.get("underlyingPrice")) or F
                fees = 2 * mo * S + mf * F + mo * S          # 2 опц. ноги + хедж + поставка (≈ мейкер опциона)
                nscan += 1
                def row(kind, ed, fu, legs, sz):
                    ap = lambda v: round(v / S * 365 / max(dte, 0.5) * 100, 1)
                    par.append({"b": b, "exp": exp, "dte": round(dte, 2), "K": K, "kind": kind, "hedge": hk, "hs": hs,
                                "edge": round(ed, 4), "pct": round(ed / S * 100, 3), "apy": ap(ed),
                                "fund": round(fu, 4), "edge_f": round(ed + fu, 4), "apy_f": ap(ed + fu),
                                "fund_only": ed <= 0 < ed + fu, "legs": legs, "sz": sz})
                if cb > 0 and pa > 0:                         # конверсия: продать колл, купить пут, купить хедж
                    row("конверсия", cb - pa + K - Fa - fees, -drag * Fa,      # лонг перпа платит фандинг
                        "SELL C %g @%g · BUY P @%g · BUY %s @%g" % (K, cb, pa, hs, Fa),
                        min(ff(C.get("bid1Size")), ff(P.get("ask1Size"))))
                if ca > 0 and pb > 0:                         # реверсия: купить колл, продать пут, шорт хедж
                    row("реверсия", Fb - K - ca + pb - fees, drag * Fb,        # шорт перпа получает фандинг
                        "BUY C %g @%g · SELL P @%g · SELL %s @%g" % (K, ca, pb, hs, Fb),
                        min(ff(C.get("ask1Size")), ff(P.get("bid1Size"))))
                if ca > 0 and cb > 0 and pa > 0 and pb > 0:
                    good.append((K, ca, cb, pa, pb, S))
            good.sort()
            for i in range(len(good)):                       # боксы K1<K2
                K1, ca1, cb1, pa1, pb1, S = good[i]
                for j in range(i + 1, len(good)):
                    K2, ca2, cb2, pa2, pb2, _ = good[j]
                    w = K2 - K1; fees = 6 * mo * S           # 4 ноги + до 2 поставок
                    lb = w - (ca1 - cb2 + pa2 - pb1) - fees   # купить бокс: платим сейчас, получаем w
                    sb = (cb1 - ca2 + pb2 - pa1) - w - fees   # продать бокс: получаем сейчас, платим w
                    for ed, kd, cost in ((lb, "лонг-бокс", ca1 - cb2 + pa2 - pb1), (sb, "шорт-бокс", w)):
                        if ed > 0:
                            box.append({"b": b, "exp": exp, "dte": round(dte, 2), "K1": K1, "K2": K2, "kind": kd,
                                        "edge": round(ed, 4), "pct": round(ed / max(cost, 1e-9) * 100, 3),
                                        "apy": round(ed / max(cost, 1e-9) * 365 / max(dte, 0.5) * 100, 1)})
    par = sorted([r for r in par if r["edge"] > 0 or r["edge_f"] > 0],
                 key=lambda r: (r["fund_only"], -r["apy_f"]))[:ARB_CFG["top"]]   # сначала чистые
    box = sorted(box, key=lambda r: -r["apy"])[:ARB_CFG["box_top"]]

    # ---------- cash-and-carry и календарь ----------
    cc, cal = [], []
    for sym, t in lin.items():
        m = re.match(r"^([A-Z0-9]+)USDT-(\d+[A-Z]{3}\d{2})$", sym)
        if not m: continue
        b, exp = m.group(1), m.group(2)
        dte = _dte_days(t.get("deliveryTime"))
        if dte < 0.5 or bid(t) <= 0 or ask(t) <= 0: continue
        mf = lfee(sym); dfee = ff(t.get("deliveryFeeRate"))
        st = spot.get(b + "USDT")
        if st and ask(st) > 0:
            ms = sfee(b + "USDT")
            e1 = (bid(t) - ask(st)) / ask(st) - ms - mf - dfee      # купить спот, продать фьюч
            e2 = (bid(st) - ask(t)) / bid(st) - ms - mf - dfee      # продать спот, купить фьюч (нужен спот)
            for ed, kd in ((e1, "лонг спот / шорт фьюч"), (e2, "шорт спот / лонг фьюч")):
                cc.append({"b": b, "fut": sym, "dte": round(dte, 1), "kind": kd, "pct": round(ed * 100, 3),
                           "apy": round(ed * 365 / dte * 100, 1), "need_spot": kd.startswith("шорт")})
        p = lin.get(b + "USDT")
        if p and bid(p) > 0:
            fa = _fund_avg(pub, "linear", p["symbol"], ih(p)) if b in BASKET else None
            fr = fa if fa is not None else ff(p.get("fundingRate"))
            fund = fr * (24.0 / ih(p)) * dte                          # доля, которую получит шорт перпа до экспирации
            e1 = (bid(t) - ask(p)) / ask(p) - 2 * mf - dfee - fund   # лонг перп / шорт фьюч: платим фандинг
            e2 = (bid(p) - ask(t)) / bid(p) - 2 * mf - dfee + fund   # шорт перп / лонг фьюч: получаем фандинг
            for ed, kd in ((e1, "лонг перп / шорт фьюч"), (e2, "шорт перп / лонг фьюч")):
                cal.append({"b": b, "fut": sym, "dte": round(dte, 1), "kind": kd, "pct": round(ed * 100, 3),
                            "fund_pct": round(fund * 100, 3), "apy": round(ed * 365 / dte * 100, 1)})
    cc = sorted(cc, key=lambda r: -r["apy"])[:ARB_CFG["top"]]
    cal = sorted(cal, key=lambda r: -r["apy"])[:ARB_CFG["top"]]

    # ---------- кэрри спот + перп ----------
    carry = []
    for sym, t in lin.items():
        if not sym.endswith("USDT") or "-" in sym or sym not in spot: continue
        b = sym[:-4]
        if ff(t.get("turnover24h")) < ARB_CFG["carry_min_turnover"] and b not in BASKET: continue
        fr = ff(t.get("fundingRate")); h = ih(t)
        fa = _fund_avg(pub, "linear", sym, h) if b in BASKET else None
        st = spot[sym]
        if ask(st) <= 0 or bid(t) <= 0: continue
        fees = 2 * (sfee(sym) + lfee(sym)) * 100 if b in BASKET else 2 * (FEE_FALLBACK["spot"] + FEE_FALLBACK["linear"]) * 100
        ap = _per_year(fr, h)
        carry.append({"b": b, "sym": sym, "rate": round(fr * 100, 4), "ih": h, "apy": round(ap, 1),
                      "apy7": round(_per_year(fa, h), 1) if fa is not None else None,
                      "basis": round((bid(t) / ask(st) - 1) * 100, 3), "fees_rt": round(fees, 3),
                      "be_days": round(fees / (abs(ap) / 365), 1) if ap else None,
                      "dir": "лонг спот / шорт перп" if fr >= 0 else "шорт спот (заём) / лонг перп",
                      "basket": b in BASKET})
    carry = sorted(carry, key=lambda r: (-r["basket"], -abs(r["apy"])))
    carry = [r for r in carry if r["basket"]] + sorted([r for r in carry if not r["basket"]], key=lambda r: -abs(r["apy"]))[:ARB_CFG["carry_top"]]

    # ---------- инверс vs линейный ----------
    il = []
    for sym, t in inv.items():
        b = inv_base(sym)
        if not b or sym != b + "USD": continue
        l = lin.get(b + "USDT")
        if not l: continue
        a_i, a_l = _per_year(ff(t.get("fundingRate")), ih(t)), _per_year(ff(l.get("fundingRate")), ih(l))
        d = a_i - a_l
        il.append({"b": b, "inv": round(a_i, 1), "lin": round(a_l, 1), "diff": round(d, 1),
                   "dir": "шорт инверс / лонг линейный" if d > 0 else "лонг инверс / шорт линейный"})
    il = sorted(il, key=lambda r: -abs(r["diff"]))[:ARB_CFG["top"]]
    return {"parity": par, "box": box, "cc": cc, "cal": cal, "carry": carry, "inv": il,
            "nscan": nscan, "est": est, "ts": time.time()}


_SCAN = {"v": None, "busy": False, "err": None}

def _run(sget, pub):
    try:
        _SCAN["v"] = _scan(sget, pub); _SCAN["err"] = None
    except Exception as e:
        _SCAN["err"] = str(e)
    finally:
        _SCAN["busy"] = False

def scan(sget, pub):
    """Неблокирующий: отдаёт последний скан (или None), обновление — в фоновом потоке."""
    v = _SCAN["v"]
    if (not v or time.time() - v["ts"] >= ARB_CFG["scan_ttl"]) and not _SCAN["busy"]:
        with _SL:
            if not _SCAN["busy"]:
                _SCAN["busy"] = True
                threading.Thread(target=_run, args=(sget, pub), daemon=True).start()
    return v


# ---------------------------------------------------------------- моя книга
def _parse_opt(sym):
    p = sym.split("-")
    if len(p) >= 4 and p[3] in ("C", "P"):
        return p[0], p[1], ff(p[2]), p[3]
    return None


def _perp_info(sym):
    """(base, kind) для линейного/инверсного перпа или None."""
    inv = sym.endswith(" (инв.)")
    s = sym.replace(" (инв.)", "")
    if inv: return (inv_base(s) or s), "inv"
    s0 = s.split("-")[0]
    for suf in ("USDT", "USDC", "PERP"):
        if s0.endswith(suf) and len(s0) > len(suf): return s0[:-len(suf)], "lin"
    return None


def _resid(port):
    """остаточный риск = лучший − худший исход стресс-таблицы β-блока (и 0)."""
    A = port["groups"]["ALL"]
    cells = [0.0]
    for r in ((A.get("beta") or {}).get("stress") or {}).get("rows", []):
        cells += [ff(x) for x in r.get("pnl", [])]
    return round(max(cells) - min(cells), 1), round(min(cells), 1), round(max(cells), 1)


def _is_crypto(b):
    return b in CRYPTO or b in ("MNT", "XTZ", "H")


def sample(port):
    """Точка истории из готового снимка /port (без запросов к API). Вызывается фоновым сэмплером раз в 20 с."""
    now = time.time()
    if BOOK_H and now - BOOK_H[-1][0] < 19: return
    A = port["groups"]["ALL"]; tot = A.get("tot", {}); ac = port.get("acct") or {}
    px = {}
    for g in port["groups"].values():
        for r in g.get("rows", []):
            if ff(r.get("px")) > 0 and _is_crypto(r["base"]): px[r["base"]] = ff(r["px"])
    vol = (port["groups"].get("ETH") or {}).get("vol") or {}
    resid = _resid(port)[0]
    RISK_H.append([now, resid])
    BOOK_H.append([round(now), ac.get("eq"), ac.get("im_pct"), ac.get("mm_pct"), vol.get("iv"),
                   tot.get("dusd"), tot.get("t"), tot.get("v"), px])


def basket_weights(rows):
    """Веса корзины графика «эквити/ETH/корзина»: лонговые (Δ$ > 0) крипто-базы кроме ETH, по Δ$."""
    bw = {r["base"]: ff(r.get("dusd")) for r in rows if _is_crypto(r["base"]) and r["base"] not in BASKET_X and ff(r.get("dusd")) > 0}
    s = sum(bw.values())
    if s > 0: return {k: round(v / s, 4) for k, v in bw.items()}, False
    return {"BTC": 0.3333, "SOL": 0.3333, "MNT": 0.3334}, True


def hist(port):
    """Лёгкий ответ /hist для вкладки «Книга»: история сэмплера + веса корзины (без скана и связок)."""
    if not BOOK_H: sample(port)
    bw, beq = basket_weights(port["groups"]["ALL"].get("rows", []))
    return {"bhist": list(BOOK_H), "basket_w": bw, "basket_eq": beq, "sample_err": _SAMPLE_ERR["e"]}


def book(port, pub):
    A = port["groups"]["ALL"]
    pos, rows, tot = A.get("pos", []), A.get("rows", []), A.get("tot", {})
    # ---------- остаточный риск (история пишется сэмплером, здесь только чтение) ----------
    resid, cmin, cmax = _resid(port)
    if not RISK_H: sample(port)
    # ---------- веса корзины графика 1: лонговые крипто-базы кроме ETH по Δ$ ----------
    bw, beq = basket_weights(rows)
    # ---------- ноги ----------
    legs = []
    for p in pos:
        sd = 1 if p["side"] == "B" else -1
        o = _parse_opt(p["sym"])
        if o:
            legs.append({"sym": p["sym"], "k": "opt", "b": o[0], "exp": o[1], "K": o[2], "cp": o[3], "sd": sd,
                         "q": ff(p["sz"]), "rem": ff(p["sz"]), "dusd": ff(p["dusd"]), "t": ff(p["t"]), "up": ff(p["up"])})
        else:
            pi = _perp_info(p["sym"])
            if not pi: continue
            legs.append({"sym": p["sym"], "k": pi[1], "b": pi[0], "sd": sd, "q": ff(p["sz"]), "rem": ff(p["sz"]),
                         "dusd": ff(p["dusd"]), "t": 0.0, "up": ff(p["up"])})
    for r in rows:
        q = ff(r.get("spot")) + ff(r.get("earn"))
        if q > 0:
            legs.append({"sym": r["base"] + " спот" + ("+Earn" if ff(r.get("earn")) else ""), "k": "spot", "b": r["base"],
                         "sd": 1, "q": q, "rem": q, "dusd": q * ff(r.get("px")), "t": 0.0, "up": 0.0})
    EPS = 1e-9
    links = []

    def take(lst, q):
        """списать q с ног lst по порядку; вернуть [(sym, взято)]"""
        out = []
        for l in lst:
            if q <= EPS: break
            x = min(l["rem"], q)
            if x > EPS: l["rem"] -= x; q -= x; out.append((l["sym"], x))
        return out

    def avail(lst): return sum(l["rem"] for l in lst)
    lin_of = lambda b, sd: [l for l in legs if l["b"] == b and l["k"] in ("lin", "inv") and l["sd"] == sd and l["rem"] > EPS]
    spot_of = lambda b: [l for l in legs if l["b"] == b and l["k"] == "spot" and l["rem"] > EPS]

    def link(kind, groups, note=""):
        q = min(avail(g) for g in groups)
        if q <= EPS: return
        parts = []
        for g in groups: parts += take(g, q)
        syms = [s for s, _ in parts]
        up = sum(l["up"] for l in legs if l["sym"] in syms and l["k"] != "spot")
        links.append({"kind": kind, "q": round(q, 6), "legs": ", ".join("%s ×%g" % (s, round(x, 6)) for s, x in parts),
                      "up": round(up, 2), "note": note})

    bases = sorted(set(l["b"] for l in legs))
    # 1. кэрри: спот(+Earn) против шорта перпа того же актива
    for b in bases:
        if spot_of(b) and lin_of(b, -1): link("кэрри (спот / шорт перпа)", [spot_of(b), lin_of(b, -1)], "доход = фандинг")
    # 2. базис золота XAUT ↔ XAU
    if (spot_of("XAUT") or lin_of("XAUT", 1)) and lin_of("XAU", -1):
        link("базис золота (лонг XAUT / шорт XAU)", [spot_of("XAUT") + lin_of("XAUT", 1), lin_of("XAU", -1)], "схождение спреда")
    if lin_of("XAUT", -1) and lin_of("XAU", 1):
        link("базис золота (шорт XAUT / лонг XAU)", [lin_of("XAUT", -1), lin_of("XAU", 1)], "расхождение спреда")
    # 3. конверсия / реверсия
    for b in bases:
        keys = sorted(set((l["exp"], l["K"]) for l in legs if l["k"] == "opt" and l["b"] == b))
        for exp, K in keys:
            o = lambda cp, sd: [l for l in legs if l["k"] == "opt" and l["b"] == b and l["exp"] == exp and l["K"] == K
                                and l["cp"] == cp and l["sd"] == sd and l["rem"] > EPS]
            if o("P", 1) and o("C", -1) and (spot_of(b) or lin_of(b, 1)):
                link("конверсия %s %s %g" % (b, exp, K), [o("P", 1), o("C", -1), spot_of(b) + lin_of(b, 1)], "выплата = K − F, фикс.")
            if o("C", 1) and o("P", -1) and lin_of(b, -1):
                link("реверсия %s %s %g" % (b, exp, K), [o("C", 1), o("P", -1), lin_of(b, -1)], "выплата = F − K, фикс.")
    # 4. межперп: лонг и шорт перпов одного актива (разные символы)
    for b in bases:
        if lin_of(b, 1) and lin_of(b, -1):
            link("межперп %s (фандинг-спред)" % b, [lin_of(b, 1), lin_of(b, -1)], "доход = разница фандингов")
    # ---------- открытые ноги ----------
    opn = []
    for l in legs:
        if l["rem"] <= EPS * max(1, l["q"]): continue
        fr = l["rem"] / l["q"] if l["q"] else 1
        opn.append({"sym": l["sym"], "side": "B" if l["sd"] > 0 else "S", "rem": round(l["rem"], 6), "q": round(l["q"], 6),
                    "part": fr < 0.999, "dusd": round(l["dusd"] * fr, 1), "t": round(l["t"] * fr, 2),
                    "up": round(l["up"], 2), "close_ok": l["up"] > 0 and l["k"] != "spot"})
    opn.sort(key=lambda r: -abs(r["dusd"]))
    # ---------- фандинг книги $/день ----------
    def ftk():
        out = {}
        for cat in ("linear", "inverse"):
            try:
                for t in pub("/v5/market/tickers", {"category": cat})["result"]["list"]:
                    out[t["symbol"]] = (ff(t.get("fundingRate")), ff(t.get("fundingIntervalHour")) or 8.0)
            except Exception:
                pass
        return out
    fk = cached("arb_ftk", 30, ftk)
    fund, frows = 0.0, []
    for l in legs:
        if l["k"] not in ("lin", "inv"): continue
        s = l["sym"].replace(" (инв.)", "")
        r, h = fk.get(s, (0.0, 8.0))
        d = -l["sd"] * abs(l["dusd"]) * r * 24.0 / h        # лонг платит при r>0
        fund += d
        frows.append({"sym": l["sym"], "rate": round(r * 100, 4), "apy": round(_per_year(r, h), 1), "usd_day": round(d, 2)})
    frows.sort(key=lambda r: r["usd_day"])
    tiles = {"resid": resid, "dusd": tot.get("dusd"), "eq": tot.get("eq"), "g1": tot.get("g1"), "t": tot.get("t"),
             "v": tot.get("v"), "fund_day": round(fund, 2), "links": len(links), "open": len(opn),
             "min": cmin, "max": cmax}
    return {"tiles": tiles, "links": links, "open": opn, "fund": frows,
            "hist": [[round(a), b] for a, b in RISK_H], "bhist": list(BOOK_H), "basket_w": bw, "basket_eq": beq,
            "sample_err": _SAMPLE_ERR["e"], "acct": port.get("acct")}


def arb_snapshot(sget, pub, port_fn, want_scan=True):
    out = {"t": time.strftime("%H:%M:%S")}
    out.update(book(port_fn(), pub))
    if want_scan:
        s = scan(sget, pub)
        out["scan"] = s; out["scan_age"] = round(time.time() - s["ts"]) if s else None
        out["scan_busy"] = _SCAN["busy"]; out["scan_err"] = _SCAN["err"]
    return out
