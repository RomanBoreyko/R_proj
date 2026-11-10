"""Сканер TradFi на Bybit (акции, ETF, сырьё, форекс — линейные перпы USDT).

Задача (промпт «трёхногий каркас», п. A): найти самый ликвидный и самый волатильный инструмент
с учётом нулевой мейкерской комиссии.

Источники (только чтение):
  /v5/market/instruments-info (symbolType: stock / ETF / commodity / forex, fullName, marketRegion)
  /v5/market/tickers          (оборот 24ч, OI, bid/ask, изменение, фандинг)
  /v5/market/kline  D × 15    (ATR14 %/день; тяжёлое — фоновой нитью, кэш 30 мин)
  /v5/account/fee-rate        (мейкер/тейкер аккаунта по каждому символу)
Маршрут дашборда: /tradfi → tradfi_snapshot(sget, pub).
"""
import time, threading
from concurrent.futures import ThreadPoolExecutor

TYPES = {"stock": "акция", "ETF": "ETF", "commodity": "сырьё", "forex": "форекс"}
_S = {"inst": None, "inst_t": 0, "atr": {}, "atr_t": 0, "busy": False, "err": None, "fees": None, "fees_t": 0}
_L = threading.Lock()

def ff(x):
    try: return float(x)
    except Exception: return 0.0

def _instruments(pub):
    if _S["inst"] and time.time() - _S["inst_t"] < 3600: return _S["inst"]
    out, cur = [], ""
    while True:
        p = {"category": "linear", "limit": "1000"}
        if cur: p["cursor"] = cur
        r = pub("/v5/market/instruments-info", p)["result"]
        out += r["list"]; cur = r.get("nextPageCursor") or ""
        if not cur: break
    inst = {i["symbol"]: {"type": i.get("symbolType"), "name": i.get("fullName") or i.get("baseCoin"), "region": i.get("marketRegion") or "",
                          "status": i.get("status")} for i in out if i.get("symbolType") in TYPES and i.get("status") == "Trading"}
    _S["inst"], _S["inst_t"] = inst, time.time()
    return inst

def _fees(sget):
    if _S["fees"] is not None and time.time() - _S["fees_t"] < 3600: return _S["fees"]
    try:
        f = {x["symbol"]: (ff(x["makerFeeRate"]), ff(x["takerFeeRate"])) for x in sget("/v5/account/fee-rate", {"category": "linear"})["result"]["list"]}
    except Exception:
        f = {}
    _S["fees"], _S["fees_t"] = f, time.time()
    return f

def _atr_one(pub, sym):
    k = pub("/v5/market/kline", {"category": "linear", "symbol": sym, "interval": "D", "limit": "16"})["result"]["list"]
    k = [(ff(x[2]), ff(x[3]), ff(x[4])) for x in reversed(k)]     # high, low, close; старые → новые
    if len(k) < 3: return None
    tr = []
    for i in range(1, len(k)):
        h, l, c = k[i]; pc = k[i-1][2]
        if pc > 0: tr.append(max(h - l, abs(h - pc), abs(l - pc)) / pc * 100)
    tr = tr[-14:]
    return {"atr": round(sum(tr)/len(tr), 2) if tr else None, "n": len(tr)}

def _refresh_atr(pub, syms):
    with _L:
        if _S["busy"]: return
        _S["busy"] = True
    try:
        res = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            for sym, r in zip(syms, ex.map(lambda s: _safe(pub, s), syms)):
                if r: res[sym] = r
        _S["atr"], _S["atr_t"], _S["err"] = res, time.time(), None
    except Exception as e:
        _S["err"] = str(e)
    finally:
        _S["busy"] = False

def _safe(pub, s):
    try: return _atr_one(pub, s)
    except Exception: return None

def tradfi_snapshot(sget, pub):
    inst = _instruments(pub)
    fees = _fees(sget)
    tk = {x["symbol"]: x for x in pub("/v5/market/tickers", {"category": "linear"})["result"]["list"] if x["symbol"] in inst}
    if time.time() - _S["atr_t"] > 1800 and not _S["busy"]:
        threading.Thread(target=_refresh_atr, args=(pub, list(inst)), daemon=True).start()
    rows = []
    for sym, meta in inst.items():
        t = tk.get(sym)
        if not t: continue
        last = ff(t.get("lastPrice")); bid = ff(t.get("bid1Price")); ask = ff(t.get("ask1Price"))
        mid = (bid + ask)/2 if bid > 0 and ask > 0 else last
        spread = (ask - bid)/mid*100 if bid > 0 and ask > 0 and mid > 0 else None
        hi, lo = ff(t.get("highPrice24h")), ff(t.get("lowPrice24h"))
        a = _S["atr"].get(sym) or {}
        atr = a.get("atr")
        mk, tk_ = fees.get(sym, (None, None))
        rows.append({"sym": sym, "type": TYPES[meta["type"]], "name": meta["name"], "region": meta["region"], "px": last,
                     "chg": round(ff(t.get("price24hPcnt"))*100, 2), "turn": round(ff(t.get("turnover24h"))), "oi": round(ff(t.get("openInterestValue"))),
                     "spread": round(spread, 4) if spread is not None else None, "rng": round((hi - lo)/lo*100, 2) if lo > 0 else None,
                     "atr": atr, "atr_sp": round(atr/spread, 1) if atr and spread else None,
                     "maker": mk, "taker": tk_, "fund": round(ff(t.get("fundingRate"))*100, 4)})
    liq = [r for r in rows if r["turn"] >= 1_000_000]
    best = lambda L, k: max((r for r in L if r.get(k) is not None), key=lambda r: r[k], default=None)
    top = {"liquid": best(rows, "turn"), "vol": best(liq, "atr"), "grid": best(liq, "atr_sp"), "liq_n": len(liq)}
    return {"t": time.strftime("%H:%M:%S"), "n": len(rows), "rows": rows, "top": top,
            "atr_age": round(time.time() - _S["atr_t"]) if _S["atr_t"] else None, "atr_busy": _S["busy"], "atr_n": len(_S["atr"]), "err": _S["err"],
            "zero_maker": sum(1 for r in rows if r["maker"] == 0)}
