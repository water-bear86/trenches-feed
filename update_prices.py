#!/usr/bin/env python3
"""OPTIONAL LOCAL HELPER (Python stdlib only). Not used by render_feed.py, publish.sh or any workflow.

The published feed leaves every price/market variable UNFILLED ({{BTC_PRICE}}, {{BTC_CHANGE_SPOKEN}}, ...);
the station fills them at playback from its own data (e.g. Pyth feeds via Helius RPC).
This helper fetches public prices (CoinGecko, falling back to Coinbase, then Kraken) and prints a JSON object of
example substitution values in the exact formats documented in latest.json -> "variables", so the station's
substitution code can be tested.   Usage: python3 update_prices.py [out.json]
If all sources fail it prints a message and exits 0. It never modifies published files.
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

ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
def words(n):
    n = int(n)
    if n < 20: return ONES[n]
    if n < 100: return TENS[n // 10] + ("" if n % 10 == 0 else "-" + ONES[n % 10])
    if n < 1000: return ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " and " + words(n % 100))
    if n < 1_000_000: return words(n // 1000) + " thousand" + ("" if n % 1000 == 0 else (" and " if n % 1000 < 100 else " ") + words(n % 1000))
    return words(n // 1_000_000) + " million" + ("" if n % 1_000_000 == 0 else " " + words(n % 1_000_000))
def spoken_price(sym, p):
    return words(round(p / 100) * 100) if sym == "BTC" else words(round(p / 5) * 5) if sym == "ETH" else words(round(p))
def spoken_change(pct):
    a = abs(pct)
    if a < 0.5: return "little changed"
    h = round(a * 2) / 2
    num = words(int(h)) + (" and a half" if h % 1 else "")
    return f"{'up' if pct > 0 else 'down'} about {num} per cent"
def spoken_trillions(x):
    t = f"{x / 1e12:.2f}".rstrip("0").rstrip("."); i, _, dec = t.partition(".")
    return words(int(i)) + ((" point " + " ".join(ONES[int(c)] for c in dec)) if dec else "") + " trillion"
def spoken_time(dt):
    h = dt.hour % 12 or 12; m = dt.minute
    return f"{words(h)} {'o' + chr(39) + 'clock' if m == 0 else ('oh ' + ONES[m]) if m < 10 else words(m)} Pacific time"

def main():
    from zoneinfo import ZoneInfo
    m = None
    for fn in (coingecko, coinbase, kraken):
        try:
            r = fn()
            if all(k in r["assets"] for k in ("BTC", "ETH", "SOL")):
                m = r; break
        except Exception as e:
            print(f"{fn.__name__} failed: {e}", file=sys.stderr)
    if not m:
        print("all price sources failed; nothing to do", file=sys.stderr); return 0
    v = {}
    for sym, a in m["assets"].items():
        v[f"{sym}_PRICE"] = spoken_price(sym, a["usd"]); v[f"{sym}_CHANGE_SPOKEN"] = spoken_change(a["change_24h_pct"])
        v[f"{sym}_CHANGE_PCT"] = f"{a['change_24h_pct']:+.1f}"
    v["BTC_PRICE_DIGITS"] = f"${round(m['assets']['BTC']['usd'] / 100) * 100:,.0f}"
    ch = [m["assets"][k]["change_24h_pct"] for k in ("BTC", "ETH", "SOL")]
    if all(c <= -0.5 for c in ch): d = ("fall", "fell")
    elif all(c >= 0.5 for c in ch): d = ("rise", "rose")
    elif all(abs(c) < 0.5 for c in ch): d = ("hold steady", "held steady")
    else: d = ("are mixed", "were mixed")
    v["MARKET_DIRECTION"], v["MARKET_DIRECTION_PAST"] = d
    if m.get("global"):
        v["TOTAL_MARKET_CAP"] = spoken_trillions(m["global"]["total_mcap_usd"])
        v["TOTAL_MARKET_CAP_CHANGE_SPOKEN"] = spoken_change(m["global"]["total_mcap_change_24h_pct"])
    v["PRICES_AS_OF_SPOKEN"] = spoken_time(datetime.datetime.now(ZoneInfo("America/Los_Angeles")))
    out = json.dumps(dict(source=m["source"], values=v), indent=1)
    if len(sys.argv) > 1: open(sys.argv[1], "w").write(out)
    print(out)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("update_prices error:", e, file=sys.stderr); sys.exit(0)
