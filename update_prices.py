#!/usr/bin/env python3
"""Price-only refresh (Python stdlib only). Run between hourly research editions.

1. Fetches BTC/ETH/SOL (+ HYPE and total market cap when available) price and 24h change:
   CoinGecko free API first; fallback Coinbase Exchange public stats, then Kraken public ticker (BTC/ETH/SOL only).
2. Writes market.json and re-renders from the newest editions/<date>.pool.json. Only the [[MARKET]] tokens in the
   pool (price sentences, market headline, prices-as-of time) change; story text is untouched.
3. If every source fails, nothing is written and the script exits 0.
"""
import json, os, sys, datetime, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "trenches-feed-prices/1.0", "Accept": "application/json"}

def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        return json.loads(r.read().decode())

def coingecko():
    d = get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,hyperliquid&vs_currencies=usd&include_24hr_change=true")
    ids = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "HYPE": "hyperliquid"}
    assets = {k: dict(usd=d[v]["usd"], change_24h_pct=round(d[v]["usd_24h_change"], 2)) for k, v in ids.items() if v in d and d[v].get("usd_24h_change") is not None}
    out = dict(source="coingecko", assets=assets)
    try:
        g = get("https://api.coingecko.com/api/v3/global")["data"]
        out["global"] = dict(total_mcap_usd=g["total_market_cap"]["usd"], total_mcap_change_24h_pct=round(g["market_cap_change_percentage_24h_usd"], 2))
    except Exception as e:
        print("coingecko global failed:", e)
    return out

def coinbase():
    assets = {}
    for k in ("BTC", "ETH", "SOL"):
        s = get(f"https://api.exchange.coinbase.com/products/{k}-USD/stats")
        last, op = float(s["last"]), float(s["open"])  # open = price 24h ago
        assets[k] = dict(usd=last, change_24h_pct=round((last / op - 1) * 100, 2))
    return dict(source="coinbase", assets=assets)

def kraken():
    import time
    pairs = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
    assets = {}
    for k, p in pairs.items():
        t = get(f"https://api.kraken.com/0/public/Ticker?pair={p}")["result"]
        last = float(next(iter(t.values()))["c"][0])
        o = get(f"https://api.kraken.com/0/public/OHLC?pair={p}&interval=60&since={int(time.time()) - 25 * 3600}")["result"]
        candles = next(v for kk, v in o.items() if kk != "last")
        target = time.time() - 24 * 3600
        ref = min(candles, key=lambda c: abs(c[0] - target))  # hourly candle closest to 24h ago
        assets[k] = dict(usd=last, change_24h_pct=round((last / float(ref[1]) - 1) * 100, 2))
    return dict(source="kraken", assets=assets)

def main():
    m = None
    for fn in (coingecko, coinbase, kraken):
        try:
            r = fn()
            if all(k in r["assets"] for k in ("BTC", "ETH", "SOL")):
                m = r; break
            print(f"{fn.__name__}: incomplete data")
        except Exception as e:
            print(f"{fn.__name__} failed: {e}")
    if not m:
        print("all price sources failed; leaving files unchanged"); return 0
    path = os.path.join(BASE, "market.json")
    old = json.load(open(path)) if os.path.exists(path) else {}
    if m.get("source") != "coingecko":  # keep last known HYPE / global figures rather than dropping sentences
        for k in ("HYPE",):
            if k in old.get("assets", {}): m["assets"][k] = old["assets"][k]
        if "global" in old: m["global"] = old["global"]
    m["prices_as_of"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    json.dump(m, open(path, "w"), indent=1)
    print("prices:", {k: v["usd"] for k, v in m["assets"].items()}, "via", m["source"])
    sys.argv = sys.argv[:1]
    import render_feed
    render_feed.main()
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never fail the scheduled job
        print("update_prices error:", e); sys.exit(0)
