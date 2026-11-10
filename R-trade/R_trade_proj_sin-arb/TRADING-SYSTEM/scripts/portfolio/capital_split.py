"""Учёт деления капитала Bybit / MEXC (промпт «трёхногий каркас», п. C). ТОЛЬКО ЧТЕНИЕ.

Bybit — из общего снимка /port (эквити, IM/MM, доля ETH-каркаса по Δ$ и uPnL).
MEXC — по read-only ключу из C:\\Users\\DoOs\\.bybit\\mexc_credentials.json:
    {"api_key": "...", "api_secret": "..."}
  ключ создаётся на MEXC с правом ТОЛЬКО «чтение», без вывода и торговли; файл вне волта и вне git.
  Ключи не печатаются, не логируются и не уходят в ответ /capital.
Эндпоинты MEXC (GET, подпись HMAC-SHA256):
  спот     api.mexc.com/api/v3/account                       (X-MEXC-APIKEY, signature в query)
  фьючерсы contract.mexc.com/api/v1/private/account/assets  (ApiKey, Request-Time, Signature = HMAC(key+time+query))
           contract.mexc.com/api/v1/private/position/open_positions
Маршрут дашборда: /capital → capital_snapshot(port).
"""
import json, os, time, hmac, hashlib, urllib.request, urllib.parse

CREDS = r"C:\Users\DoOs\.bybit\mexc_credentials.json"
_C = {}

def ff(x):
    try: return float(x)
    except Exception: return 0.0

def _cached(k, ttl, fn):
    v = _C.get(k)
    if v and time.time() - v[0] < ttl: return v[1]
    r = fn(); _C[k] = (time.time(), r); return r

def _creds():
    if not os.path.isfile(CREDS): return None
    try:
        d = json.load(open(CREDS, encoding="utf-8"))
        k, s = d.get("api_key") or d.get("key"), d.get("api_secret") or d.get("secret")
        return (k, s) if k and s else None
    except Exception:
        return None

def _get(url, headers, timeout=15):
    req = urllib.request.Request(url, headers=dict(headers, **{"User-Agent": "Mozilla/5.0"}))
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def _spot(k, s):
    q = urllib.parse.urlencode({"timestamp": int(time.time()*1000), "recvWindow": 20000})
    sig = hmac.new(s.encode(), q.encode(), hashlib.sha256).hexdigest()
    acc = _get("https://api.mexc.com/api/v3/account?" + q + "&signature=" + sig, {"X-MEXC-APIKEY": k})
    bal = [(b["asset"], ff(b["free"]) + ff(b["locked"])) for b in acc.get("balances", []) if ff(b["free"]) + ff(b["locked"]) > 0]
    px = _cached("mx_spot_px", 30, lambda: {x["symbol"]: ff(x["price"]) for x in _get("https://api.mexc.com/api/v3/ticker/price", {})})
    rows = []
    for a, q_ in bal:
        p = 1.0 if a in ("USDT", "USDC", "USD") else px.get(a + "USDT")
        rows.append({"asset": a, "qty": round(q_, 8), "usd": round(q_*p, 2) if p else None})
    return rows

def _fut_get(k, s, path, params=None):
    params = params or {}
    q = urllib.parse.urlencode(sorted(params.items()))
    t = str(int(time.time()*1000))
    sig = hmac.new(s.encode(), (k + t + q).encode(), hashlib.sha256).hexdigest()
    url = "https://contract.mexc.com" + path + ("?" + q if q else "")
    return _get(url, {"ApiKey": k, "Request-Time": t, "Signature": sig, "Content-Type": "application/json"})

def _futures(k, s):
    a = _fut_get(k, s, "/api/v1/private/account/assets")
    assets = [{"asset": x.get("currency"), "equity": round(ff(x.get("equity")), 2), "avail": round(ff(x.get("availableBalance")), 2),
               "upl": round(ff(x.get("unrealized")), 2), "im": round(ff(x.get("positionMargin")) + ff(x.get("frozenBalance")), 2)}
              for x in (a.get("data") or []) if ff(x.get("equity")) != 0]
    p = _fut_get(k, s, "/api/v1/private/position/open_positions")
    pos = [{"sym": x.get("symbol"), "side": "long" if x.get("positionType") == 1 else "short", "vol": ff(x.get("holdVol")),
            "avg": ff(x.get("holdAvgPrice")), "liq": ff(x.get("liquidatePrice")), "im": round(ff(x.get("im")), 2), "lev": x.get("leverage")}
           for x in (p.get("data") or [])]
    return assets, pos

def capital_snapshot(port):
    a = port.get("acct", {}); G = port.get("groups", {})
    eth = (G.get("ETH") or {}).get("tot", {}); allt = (G.get("ALL") or {}).get("tot", {})
    bybit = {"eq": a.get("eq"), "im": a.get("im"), "mm": a.get("mm"), "av": a.get("av"), "im_pct": a.get("im_pct"), "buf": a.get("buf"),
             "eth_dusd": eth.get("dusd"), "eth_up": eth.get("up"), "all_dusd": allt.get("dusd"), "all_up": allt.get("up"),
             "stable": port.get("stable")}
    cr = _creds()
    mexc = {"key": bool(cr), "path": CREDS}
    if cr:
        try:
            spot = _cached("mx_spot", 20, lambda: _spot(*cr))
            fa, fp = _cached("mx_fut", 20, lambda: _futures(*cr))
            s_usd = round(sum(r["usd"] or 0 for r in spot), 2); f_usd = round(sum(x["equity"] for x in fa if x["asset"] in ("USDT", "USDC")), 2)
            mexc.update({"spot": spot, "fut_assets": fa, "fut_pos": fp, "spot_usd": s_usd, "fut_usd": f_usd, "eq": round(s_usd + f_usd, 2)})
        except Exception as e:
            mexc["err"] = str(e)[:200]
    tot = (bybit["eq"] or 0) + (mexc.get("eq") or 0)
    return {"t": time.strftime("%H:%M:%S"), "bybit": bybit, "mexc": mexc, "total": round(tot, 2)}
