"""Сканер «ершей» MEXC ↔ Bybit (промпт «трёхногий каркас», п. B). ТОЛЬКО МОНИТОРИНГ.

«Ёрш» — мелкая крипто-монета, которая есть перпом и на Bybit, и на MEXC, мечется в диапазоне
и/или расходится по цене и фандингу между биржами.

Источники (публичные, без ключей MEXC; Bybit — read-only):
  MEXC  contract.mexc.com/api/v1/contract/ticker        — цены, bid/ask, оборот, фандинг (все контракты)
  MEXC  contract.mexc.com/api/v1/contract/detail        — комиссии мейкер/тейкер, zero-fee, размер контракта
  MEXC  contract.mexc.com/api/v1/contract/funding_rate/X — период фандинга (только кандидаты, фоном)
  Bybit /v5/market/tickers, /v5/market/instruments-info, /v5/market/kline 60m×48 (осцилляция, фоном)
  Bybit /v5/account/fee-rate                            — комиссии аккаунта
Вывод средств (сети, комиссии, статус) — только через приватные API; здесь не показывается.
Маршрут дашборда: /ruff → ruff_snapshot(sget, pub).
"""
import json, time, math, threading, urllib.request
from concurrent.futures import ThreadPoolExecutor

MX = "https://contract.mexc.com/api/v1/contract"
CFG = {"turn_min": 300_000, "turn_max": 80_000_000, "n_heavy": 80}
_S = {"det": None, "det_t": 0, "inst": None, "inst_t": 0, "fees": None, "fees_t": 0,
      "osc": {}, "cyc": {}, "heavy_t": 0, "busy": False, "err": None}
_L = threading.Lock()

def ff(x):
    try: return float(x)
    except Exception: return 0.0

def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def _mx_detail():
    if _S["det"] and time.time() - _S["det_t"] < 3600: return _S["det"]
    d = {x["symbol"]: {"maker": ff(x.get("makerFeeRate")), "taker": ff(x.get("takerFeeRate")), "zero": bool(x.get("isZeroFeeSymbol") or x.get("isZeroFeeRate")),
                       "state": x.get("state"), "api": x.get("apiAllowed")} for x in _get(MX + "/detail")["data"]}
    _S["det"], _S["det_t"] = d, time.time()
    return d

def _bb_inst(pub):
    if _S["inst"] and time.time() - _S["inst_t"] < 3600: return _S["inst"]
    out, cur = [], ""
    while True:
        p = {"category": "linear", "limit": "1000"}
        if cur: p["cursor"] = cur
        r = pub("/v5/market/instruments-info", p)["result"]; out += r["list"]; cur = r.get("nextPageCursor") or ""
        if not cur: break
    inst = {i["symbol"]: {"fi": ff(i.get("fundingInterval")) or 480, "type": i.get("symbolType") or ""} for i in out
            if i.get("status") == "Trading" and i.get("settleCoin") == "USDT" and i.get("contractType") == "LinearPerpetual"
            and (i.get("symbolType") or "") in ("", "innovation")}
    _S["inst"], _S["inst_t"] = inst, time.time()
    return inst

def _bb_fees(sget):
    if _S["fees"] is not None and time.time() - _S["fees_t"] < 3600: return _S["fees"]
    try: f = {x["symbol"]: (ff(x["makerFeeRate"]), ff(x["takerFeeRate"])) for x in sget("/v5/account/fee-rate", {"category": "linear"})["result"]["list"]}
    except Exception: f = {}
    _S["fees"], _S["fees_t"] = f, time.time()
    return f

def _osc_one(pub, sym):
    k = pub("/v5/market/kline", {"category": "linear", "symbol": sym, "interval": "60", "limit": "48"})["result"]["list"]
    c = [ff(x[4]) for x in reversed(k)]
    if len(c) < 10 or min(c) <= 0: return None
    r = [math.log(c[i]/c[i-1]) for i in range(1, len(c))]
    net = abs(sum(r)); path = sum(abs(x) for x in r)
    m = sum(c)/len(c); cross = sum(1 for i in range(1, len(c)) if (c[i-1]-m)*(c[i]-m) < 0)
    return {"chop": round(min(path/net, 99.0), 1) if net > 1e-9 else 99.0,             # путь / смещение: чем больше, тем сильнее «болтанка»
            "cross": cross,                                                # пересечений средней за 48 ч
            "rng48": round((max(c)-min(c))/m*100, 1), "vol1h": round(math.sqrt(sum(x*x for x in r)/len(r))*100, 2)}

def _cyc_one(mx_sym):
    d = _get(MX + "/funding_rate/" + mx_sym, timeout=10)["data"]
    return ff(d.get("collectCycle")) or 8

def _heavy(pub, bb_syms, mx_syms):
    with _L:
        if _S["busy"]: return
        _S["busy"] = True
    try:
        osc, cyc = {}, {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            for s, r in zip(bb_syms, ex.map(lambda s: _safe(_osc_one, pub, s), bb_syms)):
                if r: osc[s] = r
            for s, r in zip(mx_syms, ex.map(lambda s: _safe1(_cyc_one, s), mx_syms)):
                if r: cyc[s] = r
        _S["osc"], _S["cyc"], _S["heavy_t"], _S["err"] = osc, cyc, time.time(), None
    except Exception as e:
        _S["err"] = str(e)
    finally:
        _S["busy"] = False

def _safe(fn, pub, s):
    try: return fn(pub, s)
    except Exception: return None

def _safe1(fn, s):
    try: return fn(s)
    except Exception: return None

def ruff_snapshot(sget, pub):
    inst = _bb_inst(pub); fees = _bb_fees(sget); det = _mx_detail()
    mx = {x["symbol"]: x for x in _get(MX + "/ticker")["data"]}
    bb = {x["symbol"]: x for x in pub("/v5/market/tickers", {"category": "linear"})["result"]["list"] if x["symbol"] in inst}
    rows = []
    for sym, t in bb.items():
        base = sym[:-4] if sym.endswith("USDT") else None
        if not base: continue
        ms = base + "_USDT"; m = mx.get(ms)
        if not m: continue
        bb_bid, bb_ask, bb_last = ff(t.get("bid1Price")), ff(t.get("ask1Price")), ff(t.get("lastPrice"))
        mx_bid, mx_ask, mx_last = ff(m.get("bid1")), ff(m.get("ask1")), ff(m.get("lastPrice"))
        if min(bb_bid, bb_ask, mx_bid, mx_ask) <= 0: continue
        bmid, mmid = (bb_bid+bb_ask)/2, (mx_bid+mx_ask)/2
        if not (0.8 < mmid/bmid < 1.25): continue                       # разные множители (1000X и т.п.) — пропуск
        bturn, mturn = ff(t.get("turnover24h")), ff(m.get("amount24"))
        bmk, btk = fees.get(sym, (0.0002, 0.00055)); md = det.get(ms, {}); mtk = md.get("taker", 0.0004); mmk = md.get("maker", 0.0)
        # исполнимый эдж: купить дешевле по ask, продать дороже по bid, минус тейкер на обеих
        e1 = (mx_bid - bb_ask)/bmid*100; e2 = (bb_bid - mx_ask)/bmid*100
        edge, dirn = (e1, "купить Bybit / продать MEXC") if e1 >= e2 else (e2, "купить MEXC / продать Bybit")
        net = edge - (btk + mtk)*100
        fi_b = inst[sym]["fi"]/60 or 8; fi_m = _S["cyc"].get(ms) or 8
        fb = ff(t.get("fundingRate"))*100*8/fi_b; fm = ff(m.get("fundingRate"))*100*8/fi_m      # % за 8 ч
        hi, lo = ff(t.get("highPrice24h")), ff(t.get("lowPrice24h"))
        o = _S["osc"].get(sym) or {}
        rows.append({"coin": base, "bb_px": bb_last, "mx_px": mx_last, "diff": round((mmid/bmid-1)*100, 3),
                     "bb_sp": round((bb_ask-bb_bid)/bmid*100, 3), "mx_sp": round((mx_ask-mx_bid)/mmid*100, 3),
                     "edge": round(edge, 3), "net": round(net, 3), "dir": dirn,
                     "fb": round(fb, 4), "fm": round(fm, 4), "fdiff": round(fm - fb, 4),
                     # фандинг за 8 ч в выбранном направлении: лонг платит ставку, шорт получает
                     "f8": round((fm - fb) if e1 >= e2 else (fb - fm), 4), "net8": round(net + ((fm - fb) if e1 >= e2 else (fb - fm)), 3), "fi_b": fi_b, "fi_m": fi_m if ms in _S["cyc"] else None,
                     "bturn": round(bturn), "mturn": round(mturn), "rng": round((hi-lo)/lo*100, 1) if lo > 0 else None,
                     "chop": o.get("chop"), "cross": o.get("cross"), "rng48": o.get("rng48"), "vol1h": o.get("vol1h"),
                     "bfee": [bmk, btk], "mfee": [mmk, mtk], "mzero": md.get("zero", False), "junk": CFG["turn_min"] <= bturn <= CFG["turn_max"]})
    junk = [r for r in rows if r["junk"]]
    if time.time() - _S["heavy_t"] > 900 and not _S["busy"]:
        pick = sorted(junk, key=lambda r: -(abs(r["diff"]) + (r["rng"] or 0)/10 + abs(r["fdiff"])))[:CFG["n_heavy"]]
        threading.Thread(target=_heavy, args=(pub, [r["coin"]+"USDT" for r in pick], [r["coin"]+"_USDT" for r in pick]), daemon=True).start()
    best = lambda L, k: max((r for r in L if r.get(k) is not None), key=lambda r: r[k], default=None)
    top = {"net": best(junk, "net8"), "fdiff": max(junk, key=lambda r: abs(r["fdiff"]), default=None), "chop": best(junk, "chop"), "n": len(rows), "junk": len(junk)}
    return {"t": time.strftime("%H:%M:%S"), "rows": rows, "top": top, "cfg": CFG,
            "heavy_age": round(time.time() - _S["heavy_t"]) if _S["heavy_t"] else None, "busy": _S["busy"], "osc_n": len(_S["osc"]), "err": _S["err"]}
