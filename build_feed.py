#!/usr/bin/env python3
"""Trenches feed - data gathering (re-runnable, no API keys needed).

Pulls free public APIs and writes:
  raw/<YYYY-MM-DD>/*.json      raw API responses
  data-<YYYY-MM-DD>.md         human-readable digest for the writer/host

What this script does NOT do (needs judgment via X + web search):
  - Story selection & narrative: run X `search_news` (max_age_hours=48) for queries like
    "crypto bitcoin market", "memecoin pump.fun solana", "crypto ETF SEC regulation hack exploit",
    "BNB Chain Base memecoin four.meme", "memecoin narrative meta AI agents tokenized stocks",
    then X `search_posts_all` on any trending ticker to learn WHY it is running.
  - Filtering shill/bot spam (copy-paste "vote for $TICKER" campaigns), paid DexScreener boosts,
    and rugs vs. real runners.
  - Writing the spoken script + JSON (feed-<date>.md / feed-<date>.json).
Usage: python3 build_feed.py
"""
import json, os, sys, time, datetime, urllib.request

UA = {"User-Agent": "trenches-feed/1.0", "Accept": "application/json"}
SOURCES = {
    "prices": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,binancecoin&vs_currencies=usd&include_24hr_change=true&include_market_cap=true",
    "global": "https://api.coingecko.com/api/v3/global",
    "cg_trending": "https://api.coingecko.com/api/v3/search/trending",
    "gt_solana": "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page=1",
    "gt_base": "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1",
    "gt_bsc": "https://api.geckoterminal.com/api/v2/networks/bsc/trending_pools?page=1",
    "ds_boosts_top": "https://api.dexscreener.com/token-boosts/top/v1",
    "ds_boosts_latest": "https://api.dexscreener.com/token-boosts/latest/v1",
    "ds_profiles": "https://api.dexscreener.com/token-profiles/latest/v1",
    "fng": "https://api.alternative.me/fng/",
    "llama_sol_dex": "https://api.llama.fi/overview/dexs/solana?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true",
}

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def money(x):
    try: x = float(x)
    except (TypeError, ValueError): return "n/a"
    for div, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div: return f"${x/div:.2f}{s}"
    return f"${x:.2f}"

def main():
    base = os.path.dirname(os.path.abspath(__file__))
    day = datetime.date.today().isoformat()
    outdir = os.path.join(base, "raw", day)
    os.makedirs(outdir, exist_ok=True)
    data, status = {}, {}
    for k, u in SOURCES.items():
        try:
            data[k] = get(u); status[k] = "ok"
            with open(os.path.join(outdir, f"{k}.json"), "w") as fh: json.dump(data[k], fh, indent=1)
        except Exception as e:
            status[k] = f"FAILED: {e}"
        time.sleep(1.5)  # stay polite with free tiers (CoinGecko ~30 req/min)

    L = [f"# Trenches data digest - {datetime.datetime.now().astimezone():%Y-%m-%d %H:%M %Z}", ""]
    L.append("## Source status"); L += [f"- {k}: {v}" for k, v in status.items()]; L.append("")
    L.append("## Majors / market")
    if "prices" in data:
        for c, v in data["prices"].items():
            L.append(f"- {c}: ${v['usd']:,} ({v['usd_24h_change']:+.2f}% 24h)")
    if "global" in data:
        g = data["global"]["data"]
        L.append(f"- Total mcap {money(g['total_market_cap']['usd'])} ({g['market_cap_change_percentage_24h_usd']:+.2f}% 24h), BTC dominance {g['market_cap_percentage']['btc']:.1f}%")
    if "fng" in data:
        f = data["fng"]["data"][0]; L.append(f"- Fear & Greed: {f['value']} ({f['value_classification']})")
    if "llama_sol_dex" in data:
        d = data["llama_sol_dex"]; L.append(f"- Solana DEX volume 24h: {money(d.get('total24h'))} ({d.get('change_1d')}% 1d)")
    L.append("")
    if "cg_trending" in data:
        L.append("## CoinGecko trending searches")
        for c in data["cg_trending"]["coins"]:
            i = c["item"]; ch = (i.get("data") or {}).get("price_change_percentage_24h", {}).get("usd")
            L.append(f"- {i['symbol']} ({i['name']}) 24h: {ch:+.1f}%" if ch is not None else f"- {i['symbol']} ({i['name']})")
        L.append("")
    for net in ("solana", "base", "bsc"):
        k = f"gt_{net}"
        if k not in data: continue
        L.append(f"## GeckoTerminal trending pools - {net}")
        for p in data[k]["data"][:15]:
            a = p["attributes"]; dex = p["relationships"]["dex"]["data"]["id"]
            L.append(f"- {a['name']} | FDV {money(a.get('fdv_usd'))} | vol24 {money(a['volume_usd']['h24'])} | 24h {a['price_change_percentage']['h24']}% | created {a['pool_created_at']} | {dex} | https://www.geckoterminal.com/{net}/pools/{a['address']}")
        L.append("")
    if "ds_boosts_top" in data:
        L.append("## DexScreener top boosted tokens (PAID promotion - not organic signal)")
        for x in data["ds_boosts_top"][:20]:
            L.append(f"- [{x.get('chainId')}] {x.get('tokenAddress')} boost={x.get('totalAmount')} {(x.get('description') or '')[:80].replace(chr(10), ' ')}")
        L.append("")
    out = os.path.join(base, f"data-{day}.md")
    with open(out, "w") as fh: fh.write("\n".join(L))
    print(f"wrote {out} and {outdir}/")
    for k, v in status.items(): print(k, v)
    return 0 if all(v == "ok" for v in status.values()) else 1

if __name__ == "__main__":
    sys.exit(main())
