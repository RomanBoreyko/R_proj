import sys, json, urllib.request
sys.stdout.reconfigure(encoding="utf-8")

# MetaScalp SDK: локальный шлюз 127.0.0.1:17845-17855
# Read-only зонд: ping → connections → balance/positions. Никаких ордеров.

def get(port, path):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, headers={"Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        body = r.read().decode("utf-8", "replace")
        try: return json.loads(body)
        except: return body

# 1. Поиск живого порта
alive = None
for port in range(17845, 17856):
    try:
        r = get(port, "/ping")
        print(f"✅ порт {port} отвечает: {r}")
        alive = port
        break
    except Exception:
        pass

if not alive:
    print("❌ Порты 17845–17855 молчат.")
    print("   → MetaScalp запущен? Local API включён в настройках?")
    sys.exit(1)

# 2. Подключения к биржам
try:
    conns = get(alive, "/api/connections")
    print(f"\n── Подключения ──")
    print(json.dumps(conns, ensure_ascii=False, indent=2)[:2000])
    # ищем bybit
    ids = []
    if isinstance(conns, list):
        ids = [c.get("id") for c in conns if isinstance(c, dict)]
    elif isinstance(conns, dict):
        ids = [c.get("id") for c in conns.get("connections", conns.get("data", [])) if isinstance(c, dict)]
    print(f"\n   connection ids: {ids}")
except Exception as e:
    print(f"⚠ /api/connections: {e}")
    ids = []

# 3. Балансы и позиции по каждому подключению (read-only)
for cid in ids:
    if cid is None: continue
    for ep in ("balance", "positions"):
        try:
            r = get(alive, f"/api/connections/{cid}/{ep}")
            print(f"\n── {cid} / {ep} ──")
            print(json.dumps(r, ensure_ascii=False, indent=2)[:1500])
        except Exception as e:
            print(f"⚠ {cid}/{ep}: {e}")
